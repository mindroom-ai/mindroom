"""The capacity warm-up must not race the responder's initial room history."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import TYPE_CHECKING, Any, cast

import pytest

from scripts.testing.fuzz_live_matrix import (
    LiveFuzzRunner,
    LiveMatrixClient,
    ManagedTuwunelStack,
    sustained_stream_capacity_scenario,
)

if TYPE_CHECKING:
    from pathlib import Path


class _WarmClient:
    room_id = "!workload:example"

    def __init__(self) -> None:
        self.seen_events: dict[str, dict[str, Any]] = {}
        self.sent = asyncio.Event()

    async def sync_incremental(self, *, timeout_ms: int, allow_limited: bool) -> None:
        del timeout_ms, allow_limited

    async def send_event(self, event_type: str, txn_id: str, content: dict[str, Any]) -> str:
        del event_type, txn_id, content
        self.sent.set()
        return "$warm"


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", ["no_database", "no_room", "history", "departed", "different_room"])
async def test_capacity_warmup_waits_for_committed_room_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial: str,
) -> None:
    """Room creation/health alone cannot make a first-sync message actionable."""
    stack = ManagedTuwunelStack(profile="sustained-stream-capacity")
    stack.storage_path = tmp_path
    stack.agent_id = "@general:example"
    stack.room_id = "!workload:example"
    client = _WarmClient()
    database = tmp_path / "encryption_keys" / "general" / "@general:example_device.db"

    def write_baseline(*, ready: bool) -> None:
        database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS NioDurableRoom(room_id TEXT PRIMARY KEY, metadata TEXT)")
            if initial == "no_room" and not ready:
                return
            connection.execute(
                "INSERT OR REPLACE INTO NioDurableRoom VALUES (?, ?)",
                (
                    "!other:example" if initial == "different_room" and not ready else stack.room_id,
                    json.dumps(
                        {
                            "own_user_id": stack.agent_id,
                            "membership": "leave" if initial == "departed" and not ready else "join",
                            "baseline": ready or initial != "history",
                        },
                    ),
                ),
            )

    if initial != "no_database":
        write_baseline(ready=False)

    async def completed_warm_reply(**_kwargs: object) -> None:
        return

    runner = LiveFuzzRunner(
        stack,
        (cast("LiveMatrixClient", client),),
        sustained_stream_capacity_scenario(root_count=1),
        reply_timeout=2,
        settle_seconds=0,
    )
    monkeypatch.setattr(runner, "_wait_for_managed_stream_terminals", completed_warm_reply)
    monkeypatch.setattr(stack, "require_runtime_alive", lambda: None)
    task = asyncio.create_task(runner._prepare_managed_stream_baseline(run_id="unit"))
    try:
        await asyncio.sleep(0.03)
        assert not client.sent.is_set(), "warm-up was sent before the room baseline committed"
        write_baseline(ready=True)
        await asyncio.wait_for(task, timeout=1)
        assert client.sent.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        stack.close()
