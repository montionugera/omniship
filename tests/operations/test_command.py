import asyncio
import os
import sys
from pathlib import Path

import pytest

from omniship.core.context import ExecutionContext
from omniship.core.logging import LogRecord, LogStream
from omniship.core.node import NodeInputs
from omniship.core.stage import Stage
from omniship.operations.command import CommandOperation


@pytest.mark.asyncio
async def test_command_streams_output_before_process_finishes(tmp_path: Path) -> None:
    records: list[LogRecord] = []
    context = ExecutionContext(
        workspace_root=tmp_path,
        stage=Stage.CHECK,
        task_id="stream",
        log_sink=records.append,
    )
    execution = asyncio.create_task(
        CommandOperation().execute(
            context,
            NodeInputs(
                params={
                    "run": [
                        sys.executable,
                        "-c",
                        (
                            "import time; print('first', flush=True); "
                            "time.sleep(0.25); print('second', flush=True)"
                        ),
                    ]
                }
            ),
        )
    )

    await asyncio.sleep(0.1)

    assert [(record.stream, record.message) for record in records] == [
        (LogStream.STDOUT, "first")
    ]
    result = await execution
    assert result.is_success
    assert result.stdout == "first\nsecond\n"
    assert [record.message for record in records] == ["first", "second"]


@pytest.mark.asyncio
async def test_command_normalizes_crlf_output(tmp_path: Path) -> None:
    records: list[LogRecord] = []
    context = ExecutionContext(
        workspace_root=tmp_path,
        stage=Stage.CHECK,
        task_id="crlf",
        log_sink=records.append,
    )

    result = await CommandOperation().execute(
        context,
        NodeInputs(
            params={
                "run": [
                    sys.executable,
                    "-c",
                    (
                        "import sys; "
                        "sys.stdout.buffer.write(b'first\\r\\nsecond\\r\\n'); "
                        "sys.stderr.buffer.write(b'warning\\r\\n')"
                    ),
                ]
            }
        ),
    )

    assert result.is_success
    assert result.stdout == "first\nsecond\n"
    assert result.stderr == "warning\n"
    assert [
        record.message for record in records if record.stream is LogStream.STDOUT
    ] == ["first", "second"]
    assert [
        record.message for record in records if record.stream is LogStream.STDERR
    ] == ["warning"]


@pytest.mark.asyncio
async def test_command_accepts_output_lines_longer_than_stream_limit(
    tmp_path: Path,
) -> None:
    context = ExecutionContext(workspace_root=tmp_path, stage=Stage.CHECK)
    script = "print('x' * 200_000); print('after')"

    result = await asyncio.wait_for(
        CommandOperation().execute(
            context,
            NodeInputs(params={"run": [sys.executable, "-c", script]}),
        ),
        timeout=10,
    )

    assert result.is_success, result.error_message
    assert result.stdout == "x" * 200_000 + "\nafter\n"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell command")
async def test_command_timeout_stops_child_processes_holding_output(
    tmp_path: Path,
) -> None:
    context = ExecutionContext(workspace_root=tmp_path, stage=Stage.CHECK)

    result = await asyncio.wait_for(
        CommandOperation().execute(
            context,
            NodeInputs(params={"run": "sleep 30 & wait", "timeout": 0.5}),
        ),
        timeout=10,
    )

    assert not result.is_success
    assert result.error_message == "Command timed out after 0.5s"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell command")
async def test_command_cancellation_stops_child_processes(tmp_path: Path) -> None:
    context = ExecutionContext(workspace_root=tmp_path, stage=Stage.CHECK)
    pid_file = tmp_path / "pid"
    execution = asyncio.create_task(
        CommandOperation().execute(
            context,
            NodeInputs(params={"run": f"sleep 30 & echo $! > {pid_file}; wait"}),
        )
    )
    for _ in range(100):
        if pid_file.is_file() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.05)
    child = int(pid_file.read_text())

    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    for _ in range(100):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("child process survived cancellation")
