"""Stop cleanup must stay bound to the response generation that scheduled it."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.cancellation import USER_STOP_CANCEL_MSG
from mindroom.message_target import MessageTarget
from mindroom.stop import StopManager


@pytest.mark.asyncio
async def test_delayed_clear_preserves_replacement_tracker_and_stop() -> None:
    """An old generation's delayed clear cannot disable Stop for its successor."""
    manager = StopManager()
    client = AsyncMock(spec=nio.AsyncClient)
    target = MessageTarget.resolve("!room:localhost", "$thread", "$response")
    old_task = asyncio.create_task(asyncio.sleep(0))
    await old_task
    release_new = asyncio.Event()

    async def new_response() -> None:
        await release_new.wait()

    new_task = asyncio.create_task(new_response())
    try:
        manager.set_current("$response", target, old_task)
        manager.clear_message("$response", client, remove_button=False, delay=0)
        # Let cleanup reach its sleep(0), then replace the tracker before it
        # resumes. This exercises the delayed deletion without a timer race.
        await asyncio.sleep(0)
        manager.set_current("$response", target, new_task)
        await asyncio.gather(*tuple(manager.cleanup_tasks))

        assert manager.can_handle_stop_reaction("$response")
        assert manager.tracked_messages["$response"].task is new_task
        assert manager.request_stop_if("$response", lambda: True)
        with pytest.raises(asyncio.CancelledError, match=USER_STOP_CANCEL_MSG):
            await new_task
    finally:
        release_new.set()
        await asyncio.gather(new_task, *tuple(manager.cleanup_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_clear_scheduled_before_replacement_never_redacts_new_stop_button() -> None:
    """Cleanup must capture the old reaction before its coroutine first runs."""
    manager = StopManager()
    client = AsyncMock(spec=nio.AsyncClient)
    target = MessageTarget.resolve("!room:localhost", "$thread", "$response")
    old_task = asyncio.create_task(asyncio.sleep(0))
    await old_task
    release_new = asyncio.Event()
    redacted: list[str] = []

    async def redact(*, room_id: str, event_id: str, reason: str) -> None:
        assert room_id == "!room:localhost"
        assert reason == "Response completed"
        redacted.append(event_id)

    async def new_response() -> None:
        await release_new.wait()

    client.room_redact.side_effect = redact
    new_task = asyncio.create_task(new_response())
    try:
        manager.set_current("$response", target, old_task, reaction_event_id="$old-stop")
        manager.clear_message("$response", client, delay=0)
        # No yield: the scheduled cleanup has not read its tracker yet.
        manager.set_current("$response", target, new_task, reaction_event_id="$new-stop")
        await asyncio.gather(*tuple(manager.cleanup_tasks))

        assert "$new-stop" not in redacted
        assert manager.tracked_messages["$response"].reaction_event_id == "$new-stop"
        assert manager.can_handle_stop_reaction("$response")
    finally:
        release_new.set()
        await asyncio.gather(new_task, *tuple(manager.cleanup_tasks), return_exceptions=True)
