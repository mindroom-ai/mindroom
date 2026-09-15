"""Lazy Xvnc/Openbox lifecycle with a private worker-local RFB socket."""

import asyncio
import os
import shutil
from contextlib import suppress
from pathlib import Path

from mindroom.background_tasks import run_coroutine_until_complete


class WorkerDisplay:
    """Own the display children without changing the runner environment."""

    display = ":99"

    def __init__(self, root: Path, *, readiness_timeout: float = 15.0) -> None:
        self._lock = asyncio.Lock()
        self._root = root
        self.socket_path = root / "rfb.sock"
        self._timeout = readiness_timeout
        self._children: list[asyncio.subprocess.Process] = []

    def healthy(self) -> bool:
        """Return whether both display children remain alive."""
        return len(self._children) == 2 and all(child.returncode is None for child in self._children)

    async def start(self) -> None:
        """Start Xvnc without TCP listeners, then start the window manager."""
        async with self._lock:
            await self._start_locked()

    async def _start_locked(self) -> None:
        if self.healthy():
            return
        await run_coroutine_until_complete(self._close_children())
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._root.chmod(0o700)
        env = {**os.environ, "DISPLAY": self.display}
        executable = shutil.which("Xvnc") or shutil.which("Xtigervnc")
        if executable is None:
            msg = "Worker computer requires TigerVNC Xvnc in the worker image."
            raise RuntimeError(msg)
        try:
            server = await asyncio.create_subprocess_exec(
                executable,
                self.display,
                "-geometry",
                "1280x800",
                "-depth",
                "24",
                "-rfbport",
                "-1",
                "-rfbunixpath",
                str(self.socket_path),
                "-rfbunixmode",
                "0600",
                "-SecurityTypes",
                "None",
                "-AlwaysShared",
                "-nolisten",
                "tcp",
                "-ac",
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._children.append(server)
            async with asyncio.timeout(self._timeout):
                await self._wait_ready(server, env)
            manager = await asyncio.create_subprocess_exec(
                "openbox",
                "--sm-disable",
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._children.append(manager)
            await asyncio.sleep(0.1)
            if not self.healthy():
                msg = "Worker computer window manager exited during startup."
                raise RuntimeError(msg)  # noqa: TRY301 - startup rollback owns both children
        except BaseException:
            await run_coroutine_until_complete(self._close_children())
            raise

    async def _wait_ready(self, server: asyncio.subprocess.Process, env: dict[str, str]) -> None:
        while True:
            if server.returncode is not None:
                msg = "Worker computer display exited during startup."
                raise RuntimeError(msg)
            if self.socket_path.exists():
                probe = await asyncio.create_subprocess_exec(
                    "xdpyinfo",
                    "-display",
                    self.display,
                    env=env,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    if await probe.wait() == 0:
                        return
                finally:
                    if probe.returncode is None:
                        probe.kill()
                        await probe.wait()
            await asyncio.sleep(0.05)

    async def close(self) -> None:
        """Terminate and reap children, including partially started displays."""
        async with self._lock:
            await run_coroutine_until_complete(self._close_children())

    async def _close_children(self) -> None:
        errors: list[Exception] = []
        for child in reversed(self._children.copy()):
            try:
                if child.returncode is None:
                    with suppress(ProcessLookupError):
                        child.terminate()
                    try:
                        await asyncio.wait_for(child.wait(), timeout=3)
                    except TimeoutError:
                        with suppress(ProcessLookupError):
                            child.kill()
                        await child.wait()
            except Exception as exc:
                errors.append(exc)
            else:
                self._children.remove(child)
        if not self._children:
            self.socket_path.unlink(missing_ok=True)
        if errors:
            msg = "Failed to reap display children"
            raise ExceptionGroup(msg, errors)
