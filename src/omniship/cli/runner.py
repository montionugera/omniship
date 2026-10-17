import asyncio
import os
from dataclasses import replace
from importlib.metadata import version as distribution_version
from pathlib import Path

from rich.console import Console

from omniship.cli.ui import TerminalUI
from omniship.config.loader import find_config_file, load_and_validate
from omniship.core.artifact import ArtifactSet
from omniship.core.artifact_bundle import (
    export_artifacts,
    import_artifact_bundles,
    import_artifacts,
)
from omniship.core.context import ExecutionContext
from omniship.core.execution import load_execution_revision
from omniship.core.executor import StageExecutor
from omniship.core.graph import StageGraph
from omniship.core.input import RuntimeInputError, load_runtime_inputs
from omniship.core.stage import Stage
from omniship.plugins.discovery import load_plugins


def run_pipeline(
    stages_to_run: list[Stage],
    config_path: str | None = None,
    console: Console | None = None,
    artifact_import_path: str | None = None,
    artifact_export_path: str | None = None,
) -> int:
    console = console or Console()
    console.print(f"[bold]OmniShip v{distribution_version('omniship')}[/bold]")

    # 1. Load plugins
    registry = load_plugins()

    # 2. Locate config
    path = Path(config_path) if config_path else find_config_file()
    if not path or not path.is_file():
        console.print(
            "[bold red]Error:[/bold red] No omniship.yaml configuration file found."
        )
        return 1

    # 3. Load & validate configuration
    try:
        config, graphs = load_and_validate(path, registry)
    except Exception as exc:
        console.print(f"[bold red]Configuration Error:[/bold red] {exc}")
        return 1

    # 4. Set up context and executor
    workspace_root = path.parent.resolve()
    try:
        runtime_inputs = load_runtime_inputs(os.environ)
    except RuntimeInputError as exc:
        console.print(f"[bold red]Input Error:[/bold red] {exc}")
        return 1
    ui = TerminalUI(console=console, logging=config.logging)
    context = ExecutionContext(
        workspace_root=workspace_root,
        stage=stages_to_run[0],
        inputs=runtime_inputs,
        log_sink=ui.emit,
        revision=load_execution_revision(os.environ),
    )
    if artifact_import_path:
        try:
            source = Path(artifact_import_path)
            if not source.is_absolute():
                source = workspace_root / source
            context.artifacts = import_artifacts(
                source,
                expected_revision=context.revision,
            )
        except Exception as exc:
            console.print(f"[bold red]Artifact Import Error:[/bold red] {exc}")
            return 1

    executor = StageExecutor(registry=registry, listeners=[ui])

    async def _execute() -> bool:
        graphs_to_execute = [graphs[st] for st in stages_to_run if st in graphs]
        for g in graphs_to_execute:
            context.stage = g.stage
            success = await executor.execute_stage(g, context)
            if g.stage == Stage.BUILD and context.artifacts:
                ui.print_artifacts(context.artifacts.to_list())
            if not success:
                return False
        return True

    success = asyncio.run(_execute())

    if success:
        if artifact_export_path:
            try:
                destination = Path(artifact_export_path)
                if not destination.is_absolute():
                    destination = workspace_root / destination
                export_artifacts(context.artifacts, destination)
            except Exception as exc:
                console.print(f"[bold red]Artifact Export Error:[/bold red] {exc}")
                return 1
        if Stage.SHIP in stages_to_run:
            console.print("\n[bold green]Shipped successfully![/bold green]")
        else:
            console.print(
                f"\n[bold green]{stages_to_run[-1].value.capitalize()} completed successfully![/bold green]"
            )
        return 0
    else:
        return 1


def run_node(
    stage: Stage,
    node_id: str,
    config_path: str | None = None,
    console: Console | None = None,
    artifact_import_root: str | None = None,
    artifact_export_path: str | None = None,
    working_directory: str | None = None,
) -> int:
    console = console or Console()
    registry = load_plugins()
    path = Path(config_path) if config_path else find_config_file()
    if not path or not path.is_file():
        console.print(
            "[bold red]Error:[/bold red] No omniship.yaml configuration file found."
        )
        return 1

    try:
        config, graphs = load_and_validate(path, registry)
    except Exception as exc:
        console.print(f"[bold red]Configuration Error:[/bold red] {exc}")
        return 1

    graph = graphs[stage]
    if node_id not in graph.nodes:
        console.print(
            f"[bold red]Error:[/bold red] Node '{node_id}' does not exist in stage '{stage.value}'."
        )
        return 1

    repository_root = path.parent.resolve()
    workspace_root = repository_root
    if working_directory is not None:
        workspace_root = (repository_root / working_directory).resolve()
        if (
            not workspace_root.is_relative_to(repository_root)
            or not workspace_root.is_dir()
        ):
            console.print(
                "[bold red]Error:[/bold red] Working directory must be an "
                "existing directory inside the workspace."
            )
            return 1
    try:
        runtime_inputs = load_runtime_inputs(os.environ)
    except RuntimeInputError as exc:
        console.print(f"[bold red]Input Error:[/bold red] {exc}")
        return 1
    ui = TerminalUI(console=console, logging=config.logging)
    context = ExecutionContext(
        workspace_root=workspace_root,
        stage=stage,
        pipeline_root=repository_root,
        inputs=runtime_inputs,
        log_sink=ui.emit,
        revision=load_execution_revision(os.environ),
    )
    if artifact_import_root:
        try:
            source = Path(artifact_import_root)
            if not source.is_absolute():
                source = repository_root / source
            context.artifacts = import_artifact_bundles(
                source,
                expected_revision=(
                    os.environ.get("OMNISHIP_EXPECTED_ARTIFACT_REVISION")
                    or context.revision
                ),
            )
        except Exception as exc:
            console.print(f"[bold red]Artifact Import Error:[/bold red] {exc}")
            return 1

    imported_paths = {artifact.path for artifact in context.artifacts}
    selected = replace(graph.nodes[node_id], dependencies=frozenset())
    selected_graph = StageGraph(stage=stage)
    selected_graph.add_node(selected)
    executor = StageExecutor(registry=registry, listeners=[ui])
    success = asyncio.run(executor.execute_stage(selected_graph, context))
    if not success:
        return 1

    if artifact_export_path:
        try:
            destination = Path(artifact_export_path)
            if not destination.is_absolute():
                destination = repository_root / destination
            # Imported artifacts already travel in their producers' bundles.
            produced = ArtifactSet(
                [
                    artifact
                    for artifact in context.artifacts
                    if artifact.path not in imported_paths
                ]
            )
            export_artifacts(produced, destination)
        except Exception as exc:
            console.print(f"[bold red]Artifact Export Error:[/bold red] {exc}")
            return 1
    return 0
