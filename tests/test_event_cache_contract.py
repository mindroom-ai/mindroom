"""Behavioral contract shared by every durable Matrix event-cache backend."""

from __future__ import annotations

from typing import Any

import pytest

from mindroom.matrix.cache import ConversationEventCache
from tests.event_cache_test_support import replace_thread_unconditionally


def _message_event(
    event_id: str,
    timestamp: int,
    *,
    body: str | None = None,
    sender: str = "@user:localhost",
    thread_id: str | None = None,
    edit_of: str | None = None,
) -> dict[str, Any]:
    content: dict[str, Any] = {
        "body": event_id if body is None else body,
        "msgtype": "m.text",
    }
    if thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    if edit_of is not None:
        content["m.new_content"] = {"body": content["body"], "msgtype": "m.text"}
        content["m.relates_to"] = {"rel_type": "m.replace", "event_id": edit_of}
    return {
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": timestamp,
        "type": "m.room.message",
        "content": content,
    }


class TestConversationEventCacheContract:
    """Run the public cache contract against each configured durable backend."""

    @pytest.mark.asyncio
    async def test_public_protocol_and_disabled_fail_open(self, event_cache: ConversationEventCache) -> None:
        """Implementations expose one protocol and disabled caches return advisory misses."""
        assert isinstance(event_cache, ConversationEventCache)
        assert event_cache.is_initialized is True
        assert event_cache.durable_writes_available is True
        assert isinstance(event_cache.cache_generation, str)
        assert isinstance(event_cache.runtime_diagnostics()["cache_backend"], str)
        assert isinstance(event_cache.pending_durable_write_room_ids(), tuple)

        event_cache.disable("contract_test")

        assert event_cache.durable_writes_available is False
        assert event_cache.cache_generation is None
        assert await event_cache.get_event("!room:localhost", "$missing") is None
        assert (
            await event_cache.get_recent_room_events(
                "!room:localhost",
                event_type="m.room.message",
                since_ts_ms=0,
            )
            == []
        )
        assert (
            await event_cache.append_event(
                "!room:localhost",
                "$thread:localhost",
                _message_event("$reply:localhost", 2),
            )
            is False
        )
        assert await event_cache.redact_event("!room:localhost", "$missing") is False

    @pytest.mark.asyncio
    async def test_lookup_normalization_ordering_and_edit_selection(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Lookup rows normalize payloads and apply the same ordering and edit rules."""
        runtime_marker = {"resolution_ms": 12}
        original = _message_event("$original:localhost", 1, body="original")
        original["com.mindroom.dispatch_pipeline_timing"] = runtime_marker
        other_sender_edit = _message_event(
            "$other-edit:localhost",
            2,
            body="other edit",
            sender="@other:localhost",
            edit_of="$original:localhost",
        )
        latest_edit = _message_event(
            "$latest-edit:localhost",
            3,
            body="latest edit",
            edit_of="$original:localhost",
        )
        await event_cache.store_events_batch(
            [
                ("$original:localhost", "!room:localhost", original),
                ("$other-edit:localhost", "!room:localhost", other_sender_edit),
                ("$latest-edit:localhost", "!room:localhost", latest_edit),
            ],
        )

        cached_original = await event_cache.get_event("!room:localhost", "$original:localhost")
        recent = await event_cache.get_recent_room_events(
            "!room:localhost",
            event_type="m.room.message",
            since_ts_ms=1,
            limit=2,
        )

        assert cached_original is not None
        assert "com.mindroom.dispatch_pipeline_timing" not in cached_original
        assert [event["event_id"] for event in recent] == ["$latest-edit:localhost", "$other-edit:localhost"]
        assert await event_cache.get_latest_edit("!room:localhost", "$original:localhost") == latest_edit
        assert (
            await event_cache.get_latest_edit(
                "!room:localhost",
                "$original:localhost",
                sender="@other:localhost",
            )
            == other_sender_edit
        )

    @pytest.mark.asyncio
    async def test_invalid_event_timestamp_is_rejected_consistently(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Every backend rejects booleans and missing values as Matrix timestamps."""
        for event_id, invalid_timestamp in (("$boolean:localhost", True), ("$missing:localhost", None)):
            event = _message_event(event_id, 1)
            if invalid_timestamp is None:
                del event["origin_server_ts"]
            else:
                event["origin_server_ts"] = invalid_timestamp

            with pytest.raises(ValueError, match="missing origin_server_ts"):
                await event_cache.store_event(event_id, "!room:localhost", event)

            assert await event_cache.get_event("!room:localhost", event_id) is None

    @pytest.mark.asyncio
    async def test_thread_snapshot_append_state_and_race_guard(self, event_cache: ConversationEventCache) -> None:
        """Thread snapshots share ordering, index, incremental-update, and replacement-guard semantics."""
        room_id = "!room:localhost"
        thread_id = "$thread:localhost"
        root = _message_event(thread_id, 1)
        reply = _message_event("$reply:localhost", 2, thread_id=thread_id)
        await replace_thread_unconditionally(event_cache, room_id, thread_id, [reply, root], validated_at=10.0)

        appended = await event_cache.append_event(
            room_id,
            thread_id,
            _message_event("$appended:localhost", 3, thread_id=thread_id),
        )
        await event_cache.mark_thread_stale(room_id, thread_id, reason="live_thread_mutation")
        revalidated = await event_cache.revalidate_thread_after_incremental_update(room_id, thread_id)
        guarded_replacement = await event_cache.replace_thread_if_not_newer(
            room_id,
            thread_id,
            [root],
            expected_membership_epoch=await event_cache.room_membership_epoch(room_id),
            fetch_started_at=0.0,
        )
        cached_events = await event_cache.get_thread_events(room_id, thread_id)

        assert appended is True
        assert revalidated is True
        assert guarded_replacement is False
        assert cached_events is not None
        assert [event["event_id"] for event in cached_events] == [
            "$thread:localhost",
            "$reply:localhost",
            "$appended:localhost",
        ]
        assert await event_cache.get_thread_id_for_event(room_id, "$appended:localhost") == thread_id

    @pytest.mark.asyncio
    async def test_redaction_tombstones_original_edits_and_late_replays(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Redactions remove derived rows and prevent late original or edit resurrection."""
        room_id = "!room:localhost"
        original_id = "$original:localhost"
        edit_id = "$edit:localhost"
        original = _message_event(original_id, 1)
        edit = _message_event(edit_id, 2, edit_of=original_id)
        await event_cache.store_events_batch(
            [
                (original_id, room_id, original),
                (edit_id, room_id, edit),
            ],
        )

        assert await event_cache.redact_event(room_id, original_id) is True
        assert await event_cache.get_event(room_id, original_id) is None
        assert await event_cache.get_event(room_id, edit_id) is None
        assert await event_cache.get_latest_edit(room_id, original_id) is None

        await event_cache.store_events_batch(
            [
                (original_id, room_id, original),
                (edit_id, room_id, edit),
            ],
        )

        assert await event_cache.get_event(room_id, original_id) is None
        assert await event_cache.get_event(room_id, edit_id) is None
        assert await event_cache.redact_event(room_id, original_id) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("deletion", ["redaction", "replacement", "invalidation"])
async def test_last_child_deletion_removes_unproven_thread_root_mapping_immediately(
    event_cache: ConversationEventCache,
    deletion: str,
) -> None:
    """Runtime deletions leave no learned root mapping that startup would reject."""
    room_id = "!room:localhost"
    thread_id = "$unfetched-root:localhost"
    child = _message_event(
        "$child:localhost",
        2,
        thread_id=thread_id,
    )
    if deletion == "redaction":
        await event_cache.store_event(str(child["event_id"]), room_id, child)
    else:
        await replace_thread_unconditionally(event_cache, room_id, thread_id, [child])
    root = _message_event(thread_id, 1)
    await event_cache.store_event(thread_id, room_id, root)
    assert await event_cache.get_thread_id_for_event(room_id, thread_id) == thread_id

    if deletion == "redaction":
        assert await event_cache.redact_event(room_id, str(child["event_id"])) is True
    elif deletion == "replacement":
        await replace_thread_unconditionally(event_cache, room_id, thread_id, [])
    else:
        await event_cache.invalidate_thread(room_id, thread_id)

    assert await event_cache.get_thread_id_for_event(room_id, thread_id) is None
    assert await event_cache.get_event(room_id, thread_id) == root


@pytest.mark.asyncio
async def test_runtime_deletion_removes_dependent_root_proof(
    event_cache: ConversationEventCache,
) -> None:
    """Runtime cleanup removes a root mapping whose dependent edit supplied its only proof."""
    room_id = "!room:localhost"
    thread_id = "$unfetched-root:localhost"
    original_id = "$uncached-original:localhost"
    edit = _message_event("$edit:localhost", 2, edit_of=original_id)
    new_content = edit["content"]["m.new_content"]
    assert isinstance(new_content, dict)
    new_content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    await event_cache.store_event(str(edit["event_id"]), room_id, edit)
    assert await event_cache.get_thread_id_for_event(room_id, thread_id) == thread_id

    assert await event_cache.redact_event(room_id, original_id) is True
    assert await event_cache.get_thread_id_for_event(room_id, thread_id) is None
