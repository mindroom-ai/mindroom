"""The embedded FalkorDB server starts, keeps data across restarts, stops, and never outlives a crashed runtime."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch

import psutil
import pytest
import redislite
from falkordb.asyncio import FalkorDB
from redis.asyncio import Redis
from redis.exceptions import BusyLoadingError

from mindroom.knowledge_graph.embedded_server import start_embedded_server


async def _query(socket_path: str, query: str) -> list[list[object]]:
    db = FalkorDB(unix_socket_path=socket_path)
    try:
        return (await db.select_graph("probe").query(query)).result_set
    finally:
        await db.connection.aclose()


def _stopped(pid: int) -> bool:
    return not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


@pytest.mark.asyncio
async def test_server_answers_and_stops_cleanly(tmp_path: Path) -> None:
    """A started server answers graph queries; stopping it ends the process and removes its socket."""
    server = await start_embedded_server(tmp_path)
    pid = server.process.pid
    try:
        assert await _query(server.socket_path, "RETURN 1") == [[1]]
        assert Path(server.socket_path).parent.stat().st_mode & 0o777 == 0o700
    finally:
        await server.stop()

    assert _stopped(pid)
    assert not Path(server.socket_path).exists()


@pytest.mark.asyncio
async def test_graph_data_survives_a_restart(tmp_path: Path) -> None:
    """Data written before a stop is there after the next start."""
    server = await start_embedded_server(tmp_path)
    await _query(server.socket_path, "CREATE (:Fact {text: 'Alice leads Atlas'})")
    await server.stop()

    restarted = await start_embedded_server(tmp_path)
    try:
        assert await _query(restarted.socket_path, "MATCH (f:Fact) RETURN f.text") == [["Alice leads Atlas"]]
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_server_left_by_a_crash_is_stopped_before_the_next_start(tmp_path: Path) -> None:
    """Two servers must never write one database file, even when an upgrade moved the binary the old one runs from."""
    old_install = tmp_path / "old-install"
    old_install.mkdir()
    old_binary = old_install / "redis-server"
    shutil.copy2(redislite.__redis_executable__, old_binary)
    db_dir = tmp_path / "db"
    with patch.object(redislite, "__redis_executable__", str(old_binary)):
        orphan = await start_embedded_server(db_dir)
    try:
        successor = await start_embedded_server(db_dir)
        try:
            assert _stopped(orphan.process.pid)
            assert await _query(successor.socket_path, "RETURN 1") == [[1]]
        finally:
            await successor.stop()
    finally:
        await orphan.stop()


@pytest.mark.asyncio
async def test_a_server_still_loading_its_data_is_waited_for(tmp_path: Path) -> None:
    """A large database takes a while to load after a restart; the start waits instead of killing it."""
    loading = BusyLoadingError("Redis is loading the dataset in memory")
    with patch.object(Redis, "ping", AsyncMock(side_effect=[loading, loading, True])):
        server = await start_embedded_server(tmp_path)
    try:
        assert server.process.returncode is None
    finally:
        await server.stop()
