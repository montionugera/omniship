import json
import os
import sys
from pathlib import Path

import pytest

from omniship.core.context import ExecutionContext
from omniship.core.node import NodeInputs
from omniship.core.stage import Stage
from omniship.plugins.python import Python
from omniship.plugins.python.operations import (
    PytestOperation,
    RuffOperation,
    WheelOperation,
)
from omniship.runtime import TaskContext

FAKE_UV = f"""#!{sys.executable}
import json, os, pathlib, sys
with open(os.environ["FAKE_UV_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == "build":
    out = pathlib.Path(sys.argv[sys.argv.index("--out-dir") + 1])
    (out / "demo-1.0-py3-none-any.whl").write_bytes(b"wheel")
"""


@pytest.fixture
def uv_calls(tmp_path: Path, monkeypatch) -> Path:
    """Python tools must run in the project environment through uv.

    OmniShip itself may run from an isolated ``uvx`` environment that has
    none of the project's tools installed.
    """

    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    uv = bin_directory / "uv"
    uv.write_text(FAKE_UV, encoding="utf-8")
    uv.chmod(0o755)
    log = tmp_path / "uv.log"
    monkeypatch.setenv("PATH", f"{bin_directory}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_UV_LOG", str(log))
    return log


def _calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake executable")
async def test_python_operations_run_tools_in_project_environment(
    tmp_path: Path,
    uv_calls: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    context = ExecutionContext(workspace_root=workspace, stage=Stage.CHECK)

    ruff = await RuffOperation().execute(context, NodeInputs(params={}))
    tests = await PytestOperation().execute(context, NodeInputs(params={}))
    wheel = await WheelOperation().execute(context, NodeInputs(params={}))

    assert ruff.is_success, ruff.error_message
    assert tests.is_success, tests.error_message
    assert wheel.is_success, wheel.error_message
    assert [artifact.path.name for artifact in wheel.artifacts] == [
        "demo-1.0-py3-none-any.whl"
    ]
    calls = _calls(uv_calls)
    assert calls[0] == ["run", "ruff", "check", "."]
    assert calls[1] == ["run", "python", "-m", "pytest"]
    assert calls[2][:3] == ["build", "--wheel", "--out-dir"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake executable")
def test_python_facade_runs_tools_in_project_environment(
    tmp_path: Path,
    uv_calls: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    python = Python(TaskContext(workspace, dict(os.environ)))

    python.ruff()
    python.pytest()
    python.pyright()
    wheel = python.build_wheel()

    assert wheel.name == "demo-1.0-py3-none-any.whl"
    calls = _calls(uv_calls)
    assert calls[:3] == [
        ["run", "ruff", "check", "."],
        ["run", "python", "-m", "pytest"],
        ["run", "pyright"],
    ]
    assert calls[3][:3] == ["build", "--wheel", "--out-dir"]


def test_python_publish_requires_distributions(tmp_path: Path) -> None:
    from omniship.runtime import TaskFailure

    python = Python(TaskContext(tmp_path, {}))

    # Without files uv would publish whatever is in dist/, which may be stale.
    with pytest.raises(TaskFailure, match="No Python distributions"):
        python.publish(dry_run=True)
