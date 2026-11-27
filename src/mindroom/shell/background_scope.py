"""Turn-scoped shell auto-backgrounding and remote cleanup tracking."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.shell.registry import ProcessRegistry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

_logger = logging.getLogger(__name__)


@dataclass
class _RemoteScopeEndpoint:
    cleanup: Callable[[], None]
    in_flight: int = 0
    needs_kill: bool = False
    release_lease: Callable[[], None] | None = None


def _run_remote_cleanup(cleanup: Callable[[], None]) -> None:
    try:
        cleanup()
    except Exception:
        _logger.warning("Remote shell background scope cleanup failed", exc_info=True)


@dataclass
class ShellBackgroundScope:
    """Turn-scoped owner for auto-backgrounded local and remote worker shell processes."""

    scope_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    registry: ProcessRegistry = field(default_factory=ProcessRegistry)
    owns_registry: bool = True
    had_backgrounded: bool = False
    closed: bool = False
    _remote_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _remote_endpoints: dict[str, _RemoteScopeEndpoint] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Keep an owned registry closed when constructed already closed."""
        if self.closed and self.owns_registry:
            self.registry.closed = True

    @property
    def namespace(self) -> str:
        """Return the registry namespace for processes owned by this turn scope."""
        return f"shell-scope:{self.scope_id}"

    def track_remote_start(self, endpoint_key: str, cleanup: Callable[[], None]) -> None:
        """Record an in-flight remote shell request for *endpoint_key*."""
        with self._remote_lock:
            entry = self._remote_endpoints.get(endpoint_key)
            if entry is None:
                self._remote_endpoints[endpoint_key] = _RemoteScopeEndpoint(cleanup=cleanup, in_flight=1)
            else:
                entry.cleanup = cleanup
                entry.in_flight += 1

    def track_remote_end(
        self,
        endpoint_key: str,
        *,
        backgrounded: bool,
        release_lease: Callable[[], None] | None = None,
    ) -> bool:
        """Record completion of a remote shell request; adopt *release_lease* or fire late cleanup if closed."""
        late_cleanup: Callable[[], None] | None = None
        adopted = False
        with self._remote_lock:
            entry = self._remote_endpoints.get(endpoint_key)
            if entry is None:
                return False
            entry.in_flight = max(0, entry.in_flight - 1)
            if backgrounded:
                self.had_backgrounded = True
                if self.closed:
                    late_cleanup = entry.cleanup
                else:
                    entry.needs_kill = True
                    if release_lease is not None and entry.release_lease is None:
                        entry.release_lease = release_lease
                        adopted = True
            if self.closed and entry.in_flight == 0:
                self._remote_endpoints.pop(endpoint_key, None)
        if late_cleanup is not None:
            _run_remote_cleanup(late_cleanup)
        return adopted

    async def close(self) -> None:
        """Stop unfinished local and remote processes in this scope, flush log trailers, and release leases."""
        with self._remote_lock:
            self.closed = True
            remote_cleanups = [
                (entry.cleanup, entry.release_lease)
                for entry in self._remote_endpoints.values()
                if entry.needs_kill or entry.in_flight > 0 or entry.release_lease is not None
            ]
            for key in [k for k, e in self._remote_endpoints.items() if e.in_flight == 0]:
                self._remote_endpoints.pop(key, None)
            for entry in self._remote_endpoints.values():
                entry.needs_kill = False
                entry.release_lease = None
        try:
            if self.owns_registry:
                await self.registry.close()
            else:
                await self.registry.close_namespace(self.namespace)
            if remote_cleanups:
                await asyncio.gather(
                    *(asyncio.to_thread(_run_remote_cleanup, cleanup) for cleanup, _ in remote_cleanups),
                    return_exceptions=True,
                )
        finally:
            for _, release_lease in remote_cleanups:
                if release_lease is not None:
                    release_lease()


_SHELL_BACKGROUND_SCOPE: ContextVar[ShellBackgroundScope | None] = ContextVar(
    "shell_background_scope",
    default=None,
)


def current_shell_background_scope() -> ShellBackgroundScope | None:
    """Return the active turn-scoped shell background owner, if any."""
    return _SHELL_BACKGROUND_SCOPE.get()


@contextlib.contextmanager
def bind_shell_background_scope(scope: ShellBackgroundScope | None) -> Iterator[ShellBackgroundScope | None]:
    """Bind *scope* (or ``None`` to clear) for the duration of the context block."""
    token = _SHELL_BACKGROUND_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SHELL_BACKGROUND_SCOPE.reset(token)


@contextlib.asynccontextmanager
async def shell_background_scope(*, reuse_bound: bool = False) -> AsyncIterator[ShellBackgroundScope]:
    """Create, bind, and close a fresh turn-scoped shell background owner, or reuse the active one."""
    if reuse_bound and (existing := current_shell_background_scope()) is not None:
        yield existing
        return
    scope = ShellBackgroundScope()
    try:
        with bind_shell_background_scope(scope):
            yield scope
    finally:
        await run_coroutine_until_complete(scope.close())
