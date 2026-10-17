"""Concurrent execution of stage DAGs with dependency-aware skipping."""

import asyncio
import os
import subprocess
import traceback
from dataclasses import replace
from typing import Any, Protocol

from omniship.core.context import ExecutionContext
from omniship.core.graph import StageGraph
from omniship.core.input import RuntimeInputError, resolve_runtime_inputs
from omniship.core.node import Node, NodeInputs
from omniship.core.result import NodeResult, NodeStatus
from omniship.core.stage import Stage
from omniship.plugins.registry import PluginRegistry


class ExecutorListener(Protocol):
    """Observer notified synchronously as execution changes state."""

    def on_stage_start(self, stage: Stage, graph: StageGraph) -> None:
        """Handle the start of a stage graph."""
        ...

    def on_node_start(self, stage: Stage, node: Node) -> None:
        """Handle a node immediately before it is scheduled."""
        ...

    def on_node_finish(self, stage: Stage, node: Node, result: NodeResult) -> None:
        """Handle a node after its result has reached a terminal state."""
        ...

    def on_stage_finish(self, stage: Stage, success: bool) -> None:
        """Handle stage completion after all runnable nodes settle."""
        ...


def evaluate_condition(condition: Any, context: ExecutionContext) -> bool:
    """Evaluate the compact condition forms accepted by configuration nodes.

    Strings support booleans, the special ``tag`` predicate, and environment
    variable presence. Mappings support exact environment matches and branch
    selection, using Git metadata as a local fallback.
    """

    if condition is None:
        return True
    if isinstance(condition, str):
        cond = condition.strip()
        if cond.lower() in ("true", "1", "yes"):
            return True
        if cond.lower() in ("false", "0", "no"):
            return False
        if cond == "tag":
            return bool(
                os.environ.get("GITHUB_TAG")
                or os.environ.get("TAG")
                or os.environ.get("GITHUB_REF_TYPE") == "tag"
            )
        return bool(os.environ.get(cond) or context.env.get(cond))

    if isinstance(condition, dict):
        if "env" in condition and isinstance(condition["env"], dict):
            for k, v in condition["env"].items():
                curr = os.environ.get(k) or context.env.get(k)
                if curr != str(v):
                    return False

        if "branch" in condition:
            expected_branch = str(condition["branch"])
            current_branch = os.environ.get("GIT_BRANCH") or os.environ.get(
                "GITHUB_REF_NAME"
            )
            if not current_branch:
                try:
                    res = subprocess.run(
                        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                        cwd=context.workspace_root,
                        capture_output=True,
                        text=True,
                        timeout=2,
                    )
                    if res.returncode == 0:
                        current_branch = res.stdout.strip()
                except Exception:
                    pass
            if current_branch and current_branch != expected_branch:
                return False
        return True

    return True


class StageExecutor:
    """Execute nodes concurrently when their dependencies are satisfied.

    Failures skip only their downstream dependency chain; unrelated branches
    continue. Pipeline execution adds a strict barrier between stage graphs and
    stops before the next stage if the current stage fails.
    """

    def __init__(
        self,
        registry: PluginRegistry,
        listeners: list[ExecutorListener] | None = None,
    ) -> None:
        """Create an executor backed by a plugin operation registry."""

        self.registry = registry
        self.listeners = listeners or []

    def add_listener(self, listener: ExecutorListener) -> None:
        """Register an execution observer."""

        self.listeners.append(listener)

    async def execute_node(
        self,
        node: Node,
        context: ExecutionContext,
    ) -> NodeResult:
        """Resolve and execute one node, converting exceptions into failures."""

        if not self.registry.has_operation(node.operation_name):
            return NodeResult(
                status=NodeStatus.FAILED,
                error_message=f"Operation '{node.operation_name}' is not registered in OmniShip",
            )

        operation = self.registry.get_operation(node.operation_name)
        if node.stage not in operation.stages:
            return NodeResult(
                status=NodeStatus.FAILED,
                error_message=f"Operation '{node.operation_name}' cannot be run in stage '{node.stage}'",
            )

        try:
            resolved_params = resolve_runtime_inputs(dict(node.inputs), context.inputs)
        except RuntimeInputError as exc:
            return NodeResult(
                status=NodeStatus.FAILED,
                error_message=str(exc),
            )
        inputs = NodeInputs(
            params=resolved_params,
            artifacts=context.artifacts.to_list(),
        )
        task_context = replace(context, task_id=node.id)

        try:
            return await operation.execute(task_context, inputs)
        except Exception as exc:
            return NodeResult(
                status=NodeStatus.FAILED,
                error_message=f"Unhandled exception during operation execution: {exc}",
                stderr=traceback.format_exc(),
            )

    async def execute_stage(
        self,
        graph: StageGraph,
        context: ExecutionContext,
    ) -> bool:
        """Run one validated DAG until every node succeeds, fails, or skips."""

        graph.validate()

        for listener in self.listeners:
            listener.on_stage_start(graph.stage, graph)

        if not graph.nodes:
            for listener in self.listeners:
                listener.on_stage_finish(graph.stage, True)
            return True

        completed_nodes: set[str] = set()
        failed_nodes: set[str] = set()
        skipped_nodes: set[str] = set()
        # Skipped because an upstream node failed, as opposed to a condition.
        failure_skipped: set[str] = set()
        running_tasks: dict[str, asyncio.Task[NodeResult]] = {}
        task_to_node: dict[asyncio.Task[NodeResult], Node] = {}

        while len(completed_nodes) + len(failed_nodes) + len(skipped_nodes) < len(
            graph.nodes
        ):
            # Find nodes that are ready to run or skip
            progress_made = False

            for node_id, node in graph.nodes.items():
                if (
                    node_id in completed_nodes
                    or node_id in failed_nodes
                    or node_id in skipped_nodes
                    or node_id in running_tasks
                ):
                    continue

                # Check if all dependencies have completed, failed, or been skipped
                deps = node.dependencies
                deps_done = [
                    d
                    for d in deps
                    if d in completed_nodes or d in failed_nodes or d in skipped_nodes
                ]

                if len(deps_done) < len(deps):
                    # Still waiting on some dependencies
                    continue

                # All dependencies resolved.
                # Check if any dependency failed or was skipped due to failure
                failed_dep = next(
                    (d for d in deps if d in failed_nodes or d in failure_skipped),
                    None,
                )

                if failed_dep:
                    res = NodeResult(
                        status=NodeStatus.SKIPPED,
                        duration=0.0,
                        error_message=f"Dependency '{failed_dep}' failed",
                    )
                    context.record_result(node_id, res)
                    skipped_nodes.add(node_id)
                    failure_skipped.add(node_id)
                    progress_made = True
                    for listener in self.listeners:
                        listener.on_node_finish(graph.stage, node, res)
                    continue

                # Check if any dependency was skipped due to condition
                skipped_dep = next((d for d in deps if d in skipped_nodes), None)
                if skipped_dep:
                    res = NodeResult(
                        status=NodeStatus.SKIPPED,
                        duration=0.0,
                        error_message=f"Dependency '{skipped_dep}' was skipped",
                    )
                    context.record_result(node_id, res)
                    skipped_nodes.add(node_id)
                    progress_made = True
                    for listener in self.listeners:
                        listener.on_node_finish(graph.stage, node, res)
                    continue

                # Check node condition
                if not evaluate_condition(node.condition, context):
                    res = NodeResult(
                        status=NodeStatus.SKIPPED,
                        duration=0.0,
                        error_message="Condition not met",
                    )
                    context.record_result(node_id, res)
                    skipped_nodes.add(node_id)
                    progress_made = True
                    for listener in self.listeners:
                        listener.on_node_finish(graph.stage, node, res)
                    continue

                # Node is ready to run!
                for listener in self.listeners:
                    listener.on_node_start(graph.stage, node)

                task = asyncio.create_task(self.execute_node(node, context))
                running_tasks[node_id] = task
                task_to_node[task] = node
                progress_made = True

            if running_tasks:
                done, _ = await asyncio.wait(
                    running_tasks.values(),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    node = task_to_node.pop(task)
                    running_tasks.pop(node.id)
                    result = task.result()
                    context.record_result(node.id, result)

                    if result.is_success:
                        completed_nodes.add(node.id)
                    else:
                        failed_nodes.add(node.id)

                    for listener in self.listeners:
                        listener.on_node_finish(graph.stage, node, result)
            elif not progress_made:
                # No tasks running and no progress made -> break to prevent infinite loop
                break

        stage_success = len(failed_nodes) == 0
        for listener in self.listeners:
            listener.on_stage_finish(graph.stage, stage_success)

        return stage_success

    async def execute_pipeline(
        self,
        graphs: list[StageGraph],
        context: ExecutionContext,
    ) -> bool:
        """Execute stage graphs sequentially, stopping at the first failure."""

        for graph in graphs:
            context.stage = graph.stage
            success = await self.execute_stage(graph, context)
            if not success:
                return False
        return True
