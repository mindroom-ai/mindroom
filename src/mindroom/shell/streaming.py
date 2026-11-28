"""Raw byte-streamed command execution for ``workers run``."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import TYPE_CHECKING, Literal

from mindroom.shell.process import ManagedProcess, classify_launch_failure

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# The most bytes of a streamed command's output handed on at once.
_STREAM_CHUNK_BYTES = 32 * 1024
# Once a streamed command's process group was killed, only what its pipes still held is read. More bytes than pipes
# hold, or bytes still waited for after this long, come from a process that left the group, and are not read.
_KILLED_GROUP_PIPE_BYTES = 8 * 1024 * 1024
_KILLED_GROUP_READ_SECONDS = 1.0
# How long one chunk of a streamed command's stdin waits to leave before its sender is told to send it again.
_STDIN_WRITE_SECONDS = 20.0
_POST_EXIT_READER_GRACE_SECONDS = 0.5


class CommandStdin:
    """The stdin of a command `stream_command` runs, written in chunks numbered from 0, one at a time."""

    def __init__(self) -> None:
        self._writer: asyncio.StreamWriter | None = None
        self._next = 0
        self._writing = False

    def attach(self, writer: asyncio.StreamWriter | None) -> None:
        """Attach the spawned subprocess's stdin writer so incoming chunks reach the command."""
        self._writer = writer

    def detach(self) -> None:
        """Detach the subprocess's stdin writer so subsequent writes report the stream closed."""
        self._writer = None

    async def write(self, seq: int, data: bytes, *, eof: bool) -> bool:
        """Write chunk ``seq`` once, however often it arrives, and with ``eof`` close stdin after it.

        Return whether stdin takes a next chunk, once this one has left for the command: a command that reads
        slowly slows its writer down. One chunk at most waits to leave, and for a bounded time: TimeoutError says
        this one has not left yet, or that the one before it still waits, and that it is to be sent again.
        """
        writer = self._writer
        if writer is None:
            return False
        if seq > self._next:
            msg = f"stdin chunk {seq} arrived before chunk {self._next}"
            raise ValueError(msg)
        if self._writing:
            raise TimeoutError
        self._writing = True
        try:
            if seq == self._next:
                self._next += 1
                writer.write(data)
                if eof:
                    self._writer = None
                    writer.write_eof()
                    return False
            async with asyncio.timeout(_STDIN_WRITE_SECONDS):
                await writer.drain()
        except TimeoutError:
            raise
        except OSError:
            # The command closed its stdin or ended.
            self._writer = None
        finally:
            self._writing = False
        return self._writer is not None


async def _start_streamed_command(
    argv: list[str],
    env: dict[str, str],
    cwd: str | None,
    on_start: Callable[[], Awaitable[None]],
    on_output: Callable[[Literal["out", "err"], bytes], Awaitable[None]],
) -> ManagedProcess | int:
    """Start a streamed command; one whose program is missing or cannot be executed gets its exit code back."""
    try:
        return await ManagedProcess.spawn(argv, env, cwd, stdin=asyncio.subprocess.PIPE)
    except OSError as exc:
        if (failure := classify_launch_failure(argv, env, cwd, exc)) is None:
            raise
        await on_start()
        await on_output("err", f"{failure[1]}\n".encode())
        return failure[0]


class _PipeReaders:
    """The tasks that hand each read of a streamed command's stdout and stderr to ``on_output``, unchanged."""

    def __init__(
        self,
        managed: ManagedProcess,
        on_output: Callable[[Literal["out", "err"], bytes], Awaitable[None]],
    ) -> None:
        self._on_output = on_output
        self._moved = 0
        self._idle: set[str] = set()
        # When the command's process group was killed, and how many bytes its pipes may still give.
        self.killed_at: float | None = None
        self._left = _KILLED_GROUP_PIPE_BYTES
        self.tasks = {
            asyncio.create_task(self._read(managed.stdout, "out")),
            asyncio.create_task(self._read(managed.stderr, "err")),
        }

    async def _read(self, stream: asyncio.StreamReader | None, kind: Literal["out", "err"]) -> None:
        assert stream is not None
        loop, waited = asyncio.get_running_loop(), 0.0
        while True:
            self._idle.add(kind)
            asked = loop.time()
            chunk = await stream.read(_STREAM_CHUNK_BYTES)
            self._idle.discard(kind)
            if self.killed_at is not None:
                # Time spent handing bytes on is the reader's and does not count: only the wait for new ones does.
                waited += loop.time() - max(asked, self.killed_at)
                self._left -= len(chunk)
                if waited > _KILLED_GROUP_READ_SECONDS or self._left < 0:
                    return
            if not chunk:
                return
            await self._on_output(kind, chunk)
            self._moved += len(chunk)

    async def drain(self, until: float | None) -> None:
        """Wait, at the pace of ``on_output``, until both pipes ended, stayed silent, or are read no further."""
        pending = self.tasks
        while pending:
            seen = self._moved
            async with asyncio.timeout_at(until):
                _, pending = await asyncio.wait(pending, timeout=_POST_EXIT_READER_GRACE_SECONDS)
            # Pipes that stay silent once their command ended are held open by a process it left behind.
            if self._moved == seen and len(self._idle) == len(pending):
                break

    async def cancel(self) -> None:
        """Cancel any pending reader tasks and wait for them to finish."""
        pending = [task for task in self.tasks if not task.done()]
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def stream_command(
    argv: list[str],
    env: dict[str, str],
    cwd: str | None,
    *,
    timeout: float | None,  # noqa: ASYNC109
    stdin: CommandStdin,
    on_start: Callable[[], Awaitable[None]],
    on_output: Callable[[Literal["out", "err"], bytes], Awaitable[None]],
    on_exit: Callable[[int, bool], Awaitable[None]],
    on_stopped: Callable[[], None],
) -> None:
    """Run one command on pipes of its own, handing ``on_output`` each read of its stdout and stderr unchanged.

    ``on_start`` is awaited once the command runs and ``stdin`` reaches it. A slow ``on_output`` slows the command
    down instead of letting its output pile up. ``on_exit`` gets the exit code and whether ``timeout`` stopped the
    command, once its leader and both pipes ended; a pipe only a silent process it left behind keeps open is not
    waited for. ``timeout`` runs until then, and a stop, at ``timeout`` or by cancellation, kills the whole process
    group, also once its leader ended. What the group wrote before ``timeout`` stopped it reaches ``on_output``
    before ``on_exit``, however slowly it is taken, and what a process outside the group writes after that is not
    waited for; ``on_stopped`` is called when a command that had not ended was cancelled and its group is gone,
    after the last ``on_output``. A program that is missing or cannot be executed ends as a shell ends it: the
    reason on stderr, exit code 127 or 126.
    """
    managed = await _start_streamed_command(argv, env, cwd, on_start, on_output)
    if isinstance(managed, int):
        await on_exit(managed, False)
        return
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    readers = _PipeReaders(managed, on_output)
    ended = timed_out = False
    try:
        stdin.attach(managed.stdin)
        await on_start()
        try:
            await managed.wait_exit(timeout_seconds=None if deadline is None else deadline - loop.time())
            await readers.drain(deadline)
        except TimeoutError:
            timed_out = True
            await managed.terminate_tree(whole_group=True)
            # The killed group closed its pipes, so what it had written is all read, and then they end.
            readers.killed_at = loop.time()
            await readers.drain(None)
        # The command ended here: a cancel that comes while its exit waits to be taken stops nothing it left behind.
        ended = True
        # Nothing follows the exit: a reader still at a pipe that a process left behind holds reads no more.
        await readers.cancel()
        await on_exit(-signal.SIGKILL if managed.returncode is None else managed.returncode, timed_out)
    finally:
        stdin.detach()
        stopped = not ended and await managed.terminate_tree(whole_group=True)
        await readers.cancel()
        if stopped:
            on_stopped()
        if managed.stdin is not None:
            managed.stdin.close()
