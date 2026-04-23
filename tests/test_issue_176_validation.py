"""Validation test for ISSUE-176 sibling-thread read parallelism."""

from __future__ import annotations

import asyncio
import statistics
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.matrix.cache.event_cache import _EventCache
from mindroom.matrix.cache.event_cache_threads import load_thread_events as load_thread_events_impl
from mindroom.matrix.cache.write_coordinator import _EventCacheWriteCoordinator
from mindroom.matrix.conversation_cache import MatrixConversationCache
from tests.conftest import bind_runtime_paths, make_matrix_client_mock, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

ROOM_ID = "!issue-176:localhost"
THREAD_A_ID = "$thread-a:localhost"
THREAD_B_ID = "$thread-b:localhost"
THREAD_A_REPLY_ID = "$thread-a-reply:localhost"
THREAD_B_REPLY_ID = "$thread-b-reply:localhost"
LOCK_HOLD_SECONDS = 0.2
MEASUREMENT_RUNS = 3


def _message_source(
    *,
    room_id: str,
    event_id: str,
    body: str,
    sender: str,
    origin_server_ts: int,
    thread_root_event_id: str | None = None,
) -> dict[str, object]:
    content: dict[str, object] = {
        "body": body,
        "msgtype": "m.text",
    }
    if thread_root_event_id is not None:
        content["m.relates_to"] = {
            "rel_type": "m.thread",
            "event_id": thread_root_event_id,
        }
    return {
        "content": content,
        "event_id": event_id,
        "origin_server_ts": origin_server_ts,
        "room_id": room_id,
        "sender": sender,
        "type": "m.room.message",
    }


def _validation_runtime(tmp_path: Path, event_cache: _EventCache) -> BotRuntimeState:
    runtime_paths = test_runtime_paths(tmp_path / "runtime")
    config = bind_runtime_paths(
        Config(agents={"code": AgentConfig(display_name="Code", rooms=[ROOM_ID])}),
        runtime_paths,
    )
    return BotRuntimeState(
        client=make_matrix_client_mock(user_id="@mindroom_general:localhost"),
        config=config,
        runtime_paths=runtime_paths_for(config),
        enable_streaming=True,
        orchestrator=None,
        event_cache=event_cache,
        event_cache_write_coordinator=_EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=object(),
        ),
        runtime_started_at=time.time() - 60.0,
    )


async def _seed_thread_cache(event_cache: _EventCache) -> None:
    await event_cache.replace_thread(
        ROOM_ID,
        THREAD_A_ID,
        [
            _message_source(
                room_id=ROOM_ID,
                event_id=THREAD_A_ID,
                body="Thread A root",
                sender="@user:localhost",
                origin_server_ts=1_000,
            ),
            _message_source(
                room_id=ROOM_ID,
                event_id=THREAD_A_REPLY_ID,
                body="Thread A reply",
                sender="@user:localhost",
                origin_server_ts=2_000,
                thread_root_event_id=THREAD_A_ID,
            ),
        ],
        validated_at=time.time(),
    )
    await event_cache.replace_thread(
        ROOM_ID,
        THREAD_B_ID,
        [
            _message_source(
                room_id=ROOM_ID,
                event_id=THREAD_B_ID,
                body="Thread B root",
                sender="@user:localhost",
                origin_server_ts=3_000,
            ),
            _message_source(
                room_id=ROOM_ID,
                event_id=THREAD_B_REPLY_ID,
                body="Thread B reply",
                sender="@user:localhost",
                origin_server_ts=4_000,
                thread_root_event_id=THREAD_B_ID,
            ),
        ],
        validated_at=time.time(),
    )


async def _measure_sibling_thread_read_wait_ms(
    access: MatrixConversationCache,
) -> tuple[float, list[str]]:
    thread_a_inside_locked_read = asyncio.Event()
    slowed_thread_a_read = False

    async def slow_load_thread_events(
        db: object,
        *,
        room_id: str,
        thread_id: str,
    ) -> list[dict[str, object]] | None:
        nonlocal slowed_thread_a_read
        if room_id == ROOM_ID and thread_id == THREAD_A_ID and not slowed_thread_a_read:
            slowed_thread_a_read = True
            thread_a_inside_locked_read.set()
            await asyncio.sleep(LOCK_HOLD_SECONDS)
        return await load_thread_events_impl(
            db,
            room_id=room_id,
            thread_id=thread_id,
        )

    with patch(
        "mindroom.matrix.cache.event_cache_threads.load_thread_events",
        new=slow_load_thread_events,
    ):
        slow_thread_a_task = asyncio.create_task(access.get_thread_history(ROOM_ID, THREAD_A_ID))
        await asyncio.wait_for(thread_a_inside_locked_read.wait(), timeout=1.0)

        started_at = time.perf_counter()
        thread_b_history = await asyncio.wait_for(access.get_thread_history(ROOM_ID, THREAD_B_ID), timeout=1.0)
        wait_ms = round((time.perf_counter() - started_at) * 1000, 1)

        await asyncio.wait_for(slow_thread_a_task, timeout=1.0)

    return wait_ms, [message.event_id for message in thread_b_history]


@pytest.mark.asyncio
async def test_issue_176_real_event_cache_measures_same_room_sibling_thread_wait(tmp_path: Path) -> None:
    """Measure sibling thread read latency while a real same-room cache read holds the SQLite lock."""
    event_cache = _EventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    runtime = _validation_runtime(tmp_path, event_cache)
    access = MatrixConversationCache(logger=MagicMock(), runtime=runtime)

    try:
        await _seed_thread_cache(event_cache)

        thread_a_state = await event_cache.get_thread_cache_state(ROOM_ID, THREAD_A_ID)
        thread_b_state = await event_cache.get_thread_cache_state(ROOM_ID, THREAD_B_ID)
        assert thread_a_state is not None
        assert thread_b_state is not None

        wait_samples_ms: list[float] = []
        for _ in range(MEASUREMENT_RUNS):
            wait_ms, thread_b_event_ids = await _measure_sibling_thread_read_wait_ms(access)
            wait_samples_ms.append(wait_ms)
            assert thread_b_event_ids == [THREAD_B_ID, THREAD_B_REPLY_ID]

        median_wait_ms = round(statistics.median(wait_samples_ms), 1)
        print(f"issue_176_thread_b_wait_samples_ms={wait_samples_ms}")
        print(f"issue_176_thread_b_wait_ms={median_wait_ms}")

        assert median_wait_ms >= LOCK_HOLD_SECONDS * 1000 * 0.75
    finally:
        await runtime.event_cache_write_coordinator.close()
        await event_cache.close()
