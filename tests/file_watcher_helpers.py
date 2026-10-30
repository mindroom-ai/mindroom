"""Let tests wait on the config file watchers' scans instead of sleeping for a guessed time."""

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from mindroom import file_watcher

_SCAN_WAIT_SECONDS = 5


@dataclass
class WatcherScans:
    """The scans the watchers have completed, as seen from the test's event loop.

    A watcher reports an edit only against a scan taken before it, so a test waits for its file's first scan before editing it.
    """

    count: int = 0
    scanned_paths: set[Path] = field(default_factory=set)
    _new_scan: asyncio.Event = field(default_factory=asyncio.Event)

    def _record(self, paths: frozenset[Path]) -> None:
        self.count += 1
        self.scanned_paths |= paths
        self._new_scan.set()
        self._new_scan = asyncio.Event()

    async def _wait_until(self, condition: Callable[[], bool]) -> None:
        async with asyncio.timeout(_SCAN_WAIT_SECONDS):
            while not condition():
                await self._new_scan.wait()

    async def wait_for_scan_of(self, *paths: Path) -> None:
        """Wait until every path has been in a completed scan."""
        wanted = {path.resolve() for path in paths}
        await self._wait_until(lambda: wanted <= self.scanned_paths)

    async def wait_for_next_scan(self) -> None:
        """Wait for a scan that completes after this call.

        A watcher acts on a scan before it next checks its stop event, so stopping it after this wait observes what that scan triggered.
        """
        count = self.count
        await self._wait_until(lambda: self.count > count)


def record_watcher_scans(monkeypatch: pytest.MonkeyPatch) -> WatcherScans:
    """Record every config file scan; call it from the test's event loop."""
    loop = asyncio.get_running_loop()
    scans = WatcherScans()
    scan_mtimes = file_watcher.paths_mtime_snapshot

    def recording_scan(paths: Iterable[Path | str]) -> dict[Path, int]:
        mtimes = scan_mtimes(paths)
        # Watchers scan in worker threads, so the scan reaches the loop through call_soon_threadsafe.
        loop.call_soon_threadsafe(scans._record, frozenset(path.resolve() for path in mtimes))
        return mtimes

    monkeypatch.setattr(file_watcher, "paths_mtime_snapshot", recording_scan)
    return scans
