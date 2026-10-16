"""The embedded FalkorDB server behind the ``falkordb_lite`` backend.

MindRoom runs falkordblite's bundled ``redis-server`` with the FalkorDB module as its own child process, because
falkordblite's clients leave the server running when they close, so it outlived the process and a restart started a
second server on the same database file.
The server opens no TCP port and listens on a Unix socket in a private ``0700`` temporary directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable

logger = get_logger(__name__)

_PIDFILE = "server.pid"
_READY_TIMEOUT_SECONDS = 15.0
_STOP_TIMEOUT_SECONDS = 10.0
_POLL_SECONDS = 0.05


@dataclass
class EmbeddedServer:
    """A running embedded server and the socket it listens on."""

    process: asyncio.subprocess.Process
    socket_path: str

    @property
    def pid(self) -> int:
        """Return the server's process ID."""
        return self.process.pid

    async def stop(self) -> None:
        """Stop the server, waiting for its final write, and remove its socket directory."""
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), _STOP_TIMEOUT_SECONDS)
            except TimeoutError:
                # Redis refuses to exit when its final save fails; the append-only file already holds every write.
                logger.warning("Embedded knowledge graph database did not stop; killing it", pid=self.pid)
                self.process.kill()
                await self.process.wait()
        shutil.rmtree(Path(self.socket_path).parent, ignore_errors=True)


async def start_embedded_server(db_dir: Path) -> EmbeddedServer:
    """Start the server for the database in ``db_dir`` once a server a crashed runtime left there has stopped."""
    # why-lazy: falkordblite ships with the optional knowledge_graph extra.
    from redislite import __falkordb_module__, __redis_executable__  # noqa: PLC0415

    db_dir.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_stop_orphaned_server, db_dir, __redis_executable__)
    socket_dir = Path(tempfile.mkdtemp(prefix="mindroom-kg-"))
    socket_path = socket_dir / "falkordb.sock"
    process = await asyncio.create_subprocess_exec(
        __redis_executable__,
        *("--port", "0"),
        *("--unixsocket", str(socket_path)),
        *("--unixsocketperm", "700"),
        *("--dir", str(db_dir)),
        *("--save", ""),
        *("--appendonly", "yes"),
        *("--appendfsync", "everysec"),
        *("--pidfile", str(db_dir / _PIDFILE)),
        *("--logfile", str(db_dir / "server.log")),
        *("--loadmodule", __falkordb_module__),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    server = EmbeddedServer(process=process, socket_path=str(socket_path))
    try:
        await _wait_until_ready(server, db_dir)
    except BaseException:
        await server.stop()
        raise
    return server


async def _wait_until_ready(server: EmbeddedServer, db_dir: Path) -> None:
    from redis.asyncio import Redis  # noqa: PLC0415

    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if server.process.returncode is not None:
            msg = f"The embedded knowledge graph database exited at startup; see {db_dir / 'server.log'}"
            raise RuntimeError(msg)
        if Path(server.socket_path).exists():
            client = Redis(unix_socket_path=server.socket_path)
            try:
                if await cast("Awaitable[bool]", client.ping()):
                    return
            except OSError:
                pass
            finally:
                await client.aclose()
        await asyncio.sleep(_POLL_SECONDS)
    msg = f"The embedded knowledge graph database did not start within {_READY_TIMEOUT_SECONDS:.0f} seconds"
    raise RuntimeError(msg)


def _stop_orphaned_server(db_dir: Path, executable: str) -> None:
    """Stop a server that a crashed runtime left running on this database, so two never write one file."""
    import psutil  # noqa: PLC0415

    try:
        pid = int((db_dir / _PIDFILE).read_text(encoding="utf-8").strip())
        process = psutil.Process(pid)
        # A recycled PID belongs to another program or another database; only this database's server is stopped.
        # The binary path is not compared, because an upgrade moves it while the old server keeps running.
        is_this_server = process.name() == Path(executable).name and Path(process.cwd()).resolve() == db_dir.resolve()
    except (FileNotFoundError, ValueError, psutil.Error):
        return
    if not is_this_server:
        return
    logger.warning("Stopping an embedded knowledge graph database left by a previous run", pid=pid)
    process.terminate()
    try:
        process.wait(_STOP_TIMEOUT_SECONDS)
    except psutil.TimeoutExpired:
        process.kill()
        process.wait(_STOP_TIMEOUT_SECONDS)
