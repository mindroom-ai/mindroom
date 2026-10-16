"""The embedded FalkorDB server starts, keeps data across restarts, stops, and never outlives a crashed runtime."""

from __future__ import annotations

from pathlib import Path

import psutil
import pytest
from falkordb.asyncio import FalkorDB

from mindroom.knowledge_graph.embedded_server import start_embedded_server


async def _query(socket_path: str, query: str) -> list[list[object]]:
    db = FalkorDB(unix_socket_path=socket_path)
    try:
        return (await db.select_graph("probe").query(query)).result_set
    finally:
        await db.connection.aclose()


@pytest.mark.asyncio
async def test_server_answers_and_stops_cleanly(tmp_path: Path) -> None:
    """A started server answers graph queries; stopping it ends the process and removes its socket."""
    server = await start_embedded_server(tmp_path)
    pid = server.pid
    try:
        assert await _query(server.socket_path, "RETURN 1") == [[1]]
        assert Path(server.socket_path).parent.stat().st_mode & 0o777 == 0o700
    finally:
        await server.stop()

    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
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
async def test_server_left_by_a_crashed_runtime_is_stopped_before_the_next_start(tmp_path: Path) -> None:
    """Two servers must never write one database file, so a start first stops a server a crash left behind."""
    orphan = await start_embedded_server(tmp_path)
    # A crash skips stop(); the child keeps running.
    try:
        successor = await start_embedded_server(tmp_path)
        try:
            assert not psutil.pid_exists(orphan.pid) or psutil.Process(orphan.pid).status() == psutil.STATUS_ZOMBIE
            assert await _query(successor.socket_path, "RETURN 1") == [[1]]
        finally:
            await successor.stop()
    finally:
        await orphan.stop()
