"""Async subprocess output capture with task-scoped live logging."""

import asyncio
import codecs
import os
import signal
import subprocess
from dataclasses import dataclass

from omniship.core.context import ExecutionContext
from omniship.core.logging import LogLevel, LogStream

_READ_SIZE = 64 * 1024
# Background processes may keep a pipe open after the main process exits.
_DRAIN_GRACE_SECONDS = 5.0


def new_process_group() -> dict[str, object]:
    """Subprocess options that let a timeout stop the whole process tree."""

    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _kill_tree(process: asyncio.subprocess.Process) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            if os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.kill()
    except ProcessLookupError:
        pass


@dataclass(frozen=True)
class ProcessOutput:
    """Captured subprocess streams and termination state."""

    return_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


async def stream_process(
    process: asyncio.subprocess.Process,
    context: ExecutionContext,
    *,
    timeout: float | None = None,
) -> ProcessOutput:
    """Wait for a process while streaming stdout and stderr into ``context``.

    Output is retained for the operation result and normalized to LF line
    endings so behavior is consistent across CI operating systems. Output is
    read in chunks, so lines of any length are accepted. A timeout kills the
    process and, when it leads its own process group, every descendant.
    """

    stdout: list[str] = []
    stderr: list[str] = []

    async def read_stream(
        reader: asyncio.StreamReader | None,
        destination: list[str],
        stream: LogStream,
    ) -> None:
        if reader is None:
            return
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending: list[str] = []

        def emit(line: str) -> None:
            text = line.replace("\r\n", "\n")
            destination.append(text)
            context.emit_log(LogLevel.INFO, text.rstrip("\r\n"), stream)

        try:
            while chunk := await reader.read(_READ_SIZE):
                first, *lines = decoder.decode(chunk).split("\n")
                pending.append(first)
                if lines:
                    emit("".join(pending) + "\n")
                    *complete, rest = lines
                    for line in complete:
                        emit(line + "\n")
                    pending = [rest]
            pending.append(decoder.decode(b"", final=True))
        finally:
            if remainder := "".join(pending):
                emit(remainder)

    readers = (
        asyncio.create_task(read_stream(process.stdout, stdout, LogStream.STDOUT)),
        asyncio.create_task(read_stream(process.stderr, stderr, LogStream.STDERR)),
    )
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        _kill_tree(process)
        await process.wait()
    except asyncio.CancelledError:
        # The child runs in its own session, so terminal interrupts miss it.
        _kill_tree(process)
        for reader_task in readers:
            reader_task.cancel()
        raise
    _, unfinished = await asyncio.wait(readers, timeout=_DRAIN_GRACE_SECONDS)
    for reader_task in unfinished:
        reader_task.cancel()
    for result in await asyncio.gather(*readers, return_exceptions=True):
        if isinstance(result, Exception):
            raise result
    return ProcessOutput(
        return_code=process.returncode or 0,
        stdout="".join(stdout),
        stderr="".join(stderr),
        timed_out=timed_out,
    )
