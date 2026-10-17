import asyncio
import time
from pathlib import Path

import pytest

from omniship.core.context import ExecutionContext
from omniship.core.executor import StageExecutor
from omniship.core.graph import StageGraph
from omniship.core.node import Node, NodeInputs
from omniship.core.result import NodeResult, NodeStatus
from omniship.core.stage import Stage
from omniship.operations import register_core_plugin
from omniship.plugins.registry import PluginRegistry


class SleepOperation:
    name = "test/sleep"
    stages = frozenset({Stage.CHECK, Stage.BUILD, Stage.SHIP})
    cacheable = False

    async def execute(self, context: ExecutionContext, inputs: NodeInputs) -> NodeResult:
        duration = float(inputs.params.get("duration", 0.1))
        await asyncio.sleep(duration)
        return NodeResult(status=NodeStatus.SUCCESS, duration=duration)


class FailingOperation:
    name = "test/failing"
    stages = frozenset({Stage.CHECK, Stage.BUILD, Stage.SHIP})
    cacheable = False

    async def execute(self, context: ExecutionContext, inputs: NodeInputs) -> NodeResult:
        return NodeResult(status=NodeStatus.FAILED, error_message="Deliberate failure")


@pytest.fixture
def registry():
    reg = PluginRegistry()
    register_core_plugin(reg)
    reg.register_operation(SleepOperation())
    reg.register_operation(FailingOperation())
    return reg


@pytest.mark.asyncio
async def test_parallel_fan_out_execution(tmp_path: Path, registry: PluginRegistry):
    """Test that fan-out nodes execute in parallel."""
    graph = StageGraph(stage=Stage.CHECK)
    graph.add_node(Node(id="compile", stage=Stage.CHECK, operation_name="core/noop"))
    graph.add_node(
        Node(
            id="unit",
            stage=Stage.CHECK,
            operation_name="test/sleep",
            inputs={"duration": 0.15},
            dependencies=frozenset({"compile"}),
        )
    )
    graph.add_node(
        Node(
            id="integration",
            stage=Stage.CHECK,
            operation_name="test/sleep",
            inputs={"duration": 0.15},
            dependencies=frozenset({"compile"}),
        )
    )
    graph.add_node(
        Node(
            id="summary",
            stage=Stage.CHECK,
            operation_name="core/noop",
            dependencies=frozenset({"unit", "integration"}),
        )
    )

    context = ExecutionContext(workspace_root=tmp_path, stage=Stage.CHECK)
    executor = StageExecutor(registry=registry)

    start = time.monotonic()
    success = await executor.execute_stage(graph, context)
    elapsed = time.monotonic() - start

    assert success is True
    # If unit and integration ran sequentially, elapsed would be >= 0.30s.
    # In parallel, it completes in around 0.15s - 0.25s.
    assert elapsed < 0.28
    assert context.results["summary"].is_success


@pytest.mark.asyncio
async def test_failure_propagation_and_skipping(tmp_path: Path, registry: PluginRegistry):
    """
    If:
      compile
      ├── failing
      └── unit
    and failing -> downstream:
    failing should fail, downstream should be skipped, unit should still succeed.
    """
    graph = StageGraph(stage=Stage.CHECK)
    graph.add_node(Node(id="compile", stage=Stage.CHECK, operation_name="core/noop"))
    graph.add_node(
        Node(
            id="failing",
            stage=Stage.CHECK,
            operation_name="test/failing",
            dependencies=frozenset({"compile"}),
        )
    )
    graph.add_node(
        Node(
            id="unit",
            stage=Stage.CHECK,
            operation_name="core/noop",
            dependencies=frozenset({"compile"}),
        )
    )
    graph.add_node(
        Node(
            id="downstream",
            stage=Stage.CHECK,
            operation_name="core/noop",
            dependencies=frozenset({"failing"}),
        )
    )

    context = ExecutionContext(workspace_root=tmp_path, stage=Stage.CHECK)
    executor = StageExecutor(registry=registry)

    success = await executor.execute_stage(graph, context)

    assert success is False
    assert context.results["compile"].is_success
    assert context.results["unit"].is_success
    assert context.results["failing"].is_failed
    assert context.results["downstream"].is_skipped
    assert "Dependency 'failing' failed" in context.results["downstream"].error_message


@pytest.mark.asyncio
async def test_stage_completion_barrier(tmp_path: Path, registry: PluginRegistry):
    """Check stage failure must stop pipeline before Build begins."""
    check_graph = StageGraph(stage=Stage.CHECK)
    check_graph.add_node(Node(id="check_fail", stage=Stage.CHECK, operation_name="test/failing"))

    build_graph = StageGraph(stage=Stage.BUILD)
    build_graph.add_node(Node(id="build_step", stage=Stage.BUILD, operation_name="core/noop"))

    context = ExecutionContext(workspace_root=tmp_path, stage=Stage.CHECK)
    executor = StageExecutor(registry=registry)

    pipeline_success = await executor.execute_pipeline([check_graph, build_graph], context)

    assert pipeline_success is False
    assert "check_fail" in context.results
    assert "build_step" not in context.results


@pytest.mark.asyncio
async def test_condition_skipping(tmp_path: Path, registry: PluginRegistry):
    """Nodes with unsatisfied condition should be skipped."""
    graph = StageGraph(stage=Stage.CHECK)
    graph.add_node(
        Node(
            id="conditional_node",
            stage=Stage.CHECK,
            operation_name="core/noop",
            condition={"env": {"NON_EXISTENT_VAR_123": "yes"}},
        )
    )

    context = ExecutionContext(workspace_root=tmp_path, stage=Stage.CHECK)
    executor = StageExecutor(registry=registry)

    success = await executor.execute_stage(graph, context)
    assert success is True
    assert context.results["conditional_node"].is_skipped


@pytest.mark.asyncio
async def test_failure_skips_are_reported_transitively(
    tmp_path: Path, registry: PluginRegistry
):
    """failing -> middle -> leaf: leaf is skipped because of a failure."""
    graph = StageGraph(stage=Stage.CHECK)
    graph.add_node(Node(id="failing", stage=Stage.CHECK, operation_name="test/failing"))
    graph.add_node(
        Node(
            id="middle",
            stage=Stage.CHECK,
            operation_name="core/noop",
            dependencies=frozenset({"failing"}),
        )
    )
    graph.add_node(
        Node(
            id="leaf",
            stage=Stage.CHECK,
            operation_name="core/noop",
            dependencies=frozenset({"middle"}),
        )
    )
    context = ExecutionContext(workspace_root=tmp_path, stage=Stage.CHECK)

    await StageExecutor(registry=registry).execute_stage(graph, context)

    assert context.results["leaf"].error_message == "Dependency 'middle' failed"


@pytest.mark.asyncio
async def test_condition_skips_are_not_reported_as_failures(
    tmp_path: Path, registry: PluginRegistry
):
    """conditional -> middle -> leaf: no node failed, so no failure is reported."""
    graph = StageGraph(stage=Stage.CHECK)
    graph.add_node(
        Node(
            id="conditional",
            stage=Stage.CHECK,
            operation_name="core/noop",
            condition={"env": {"NON_EXISTENT_VAR_123": "yes"}},
        )
    )
    graph.add_node(
        Node(
            id="middle",
            stage=Stage.CHECK,
            operation_name="core/noop",
            dependencies=frozenset({"conditional"}),
        )
    )
    graph.add_node(
        Node(
            id="leaf",
            stage=Stage.CHECK,
            operation_name="core/noop",
            dependencies=frozenset({"middle"}),
        )
    )
    context = ExecutionContext(workspace_root=tmp_path, stage=Stage.CHECK)

    await StageExecutor(registry=registry).execute_stage(graph, context)

    assert context.results["leaf"].error_message == "Dependency 'middle' was skipped"
