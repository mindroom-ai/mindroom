"""Behavioral contract shared by every durable Matrix event-cache backend."""

from __future__ import annotations

from typing import Any

import pytest

from mindroom.matrix.cache import ConversationEventCache, ThreadAppendOutcome, thread_cache_rejection_reason
from tests.event_cache_test_support import replace_thread_unconditionally


def _sidecar_message_event(event_id: str, timestamp: int, *, mxc_url: str) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "sender": "@user:localhost",
        "origin_server_ts": timestamp,
        "type": "m.room.message",
        "content": {
            "msgtype": "m.file",
            "body": "preview",
            "url": mxc_url,
            "io.mindroom.long_text": {
                "version": 2,
                "encoding": "matrix_event_content_json",
            },
        },
    }


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
            await event_cache.get_mxc_texts(
                "!room:localhost",
                {("$missing", "mxc://server/missing")},
                expected_membership_epoch=0,
            )
            == {}
        )
        assert (
            await event_cache.get_recent_room_events(
                "!room:localhost",
                event_type="m.room.message",
                since_ts_ms=0,
            )
            == []
        )
        assert (
            await event_cache.apply_thread_mutation_append(
                "!room:localhost",
                "$thread:localhost",
                _message_event("$reply:localhost", 2),
                append_failed_reason="live_append_failed",
            )
            is ThreadAppendOutcome.WRITES_UNAVAILABLE
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
    async def test_batched_plaintext_read_preserves_cache_security_boundaries(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Batch reads keep point-read principal, room, owner, redaction, and departure rules."""
        room_id = "!room:localhost"
        mxc_url = "mxc://server/owned"
        owner = _sidecar_message_event("$owner:localhost", 1, mxc_url=mxc_url)
        await event_cache.store_event("$owner:localhost", room_id, owner)
        membership_epoch = await event_cache.room_membership_epoch(room_id)
        assert membership_epoch is not None
        assert await event_cache.store_mxc_text(
            room_id,
            "$owner:localhost",
            mxc_url,
            "owned plaintext",
            expected_membership_epoch=membership_epoch,
        )
        references = {
            ("$owner:localhost", mxc_url),
            ("$missing:localhost", mxc_url),
            ("$owner:localhost", "mxc://server/wrong"),
        }

        assert await event_cache.get_mxc_texts(
            room_id,
            references,
            expected_membership_epoch=membership_epoch,
        ) == {("$owner:localhost", mxc_url): "owned plaintext"}
        assert (
            await event_cache.get_mxc_texts(
                "!wrong:localhost",
                references,
                expected_membership_epoch=membership_epoch,
            )
            == {}
        )
        assert (
            await event_cache.for_principal("@other:localhost").get_mxc_texts(
                room_id,
                references,
                expected_membership_epoch=membership_epoch,
            )
            == {}
        )
        assert (
            await event_cache.get_mxc_texts(
                room_id,
                references,
                expected_membership_epoch=membership_epoch + 1,
            )
            == {}
        )

        assert await event_cache.redact_event(room_id, "$owner:localhost") is True
        assert (
            await event_cache.get_mxc_texts(
                room_id,
                references,
                expected_membership_epoch=membership_epoch,
            )
            == {}
        )
        event_cache.mark_room_departed(room_id)
        assert (
            await event_cache.get_mxc_texts(
                room_id,
                references,
                expected_membership_epoch=membership_epoch,
            )
            == {}
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
    async def test_thread_snapshot_append_ordering_and_gap_rules(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Thread snapshots share append ordering, index maintenance, and the two gap rules."""
        room_id = "!room:localhost"
        thread_id = "$thread:localhost"
        root = _message_event(thread_id, 1)
        reply = _message_event("$reply:localhost", 2, thread_id=thread_id)
        await replace_thread_unconditionally(event_cache, room_id, thread_id, [reply, root])

        appended = await event_cache.apply_thread_mutation_append(
            room_id,
            thread_id,
            _message_event("$appended:localhost", 3, thread_id=thread_id),
            append_failed_reason="live_append_failed",
        )
        assert appended is ThreadAppendOutcome.APPENDED
        assert await event_cache.get_thread_cache_gap(room_id, thread_id) is None

        cached_events = await event_cache.get_thread_events(room_id, thread_id)
        assert cached_events is not None
        assert [event["event_id"] for event in cached_events] == [
            "$thread:localhost",
            "$reply:localhost",
            "$appended:localhost",
        ]
        assert await event_cache.get_thread_id_for_event(room_id, "$appended:localhost") == thread_id

        # Rule 1: a gap marker makes the snapshot unusable, and a later append never clears it.
        await event_cache.mark_thread_gap(room_id, thread_id, reason="live_thread_mutation")
        re_appended = await event_cache.apply_thread_mutation_append(
            room_id,
            thread_id,
            _message_event("$appended:localhost", 3, thread_id=thread_id),
            append_failed_reason="live_append_failed",
        )
        assert re_appended is ThreadAppendOutcome.APPENDED
        assert await event_cache.get_thread_cache_gap(room_id, thread_id) is not None

        # Rule 2: a replacement whose fetch predates the marker leaves it in place.
        stale_stored = await event_cache.replace_thread(
            room_id,
            thread_id,
            [root, reply],
            expected_membership_epoch=await event_cache.room_membership_epoch(room_id),
            fetch_started_at=0.0,
        )
        assert stale_stored
        assert await event_cache.get_thread_cache_gap(room_id, thread_id) is not None

        # ...and a replacement whose fetch covers the marker clears it.
        await replace_thread_unconditionally(event_cache, room_id, thread_id, [root, reply])
        assert await event_cache.get_thread_cache_gap(room_id, thread_id) is None

    @pytest.mark.asyncio
    async def test_room_scoped_gap_reaches_every_thread_holding_a_snapshot(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """A room-scoped gap must mark every thread with a snapshot, and nothing outside that room.

        This is the wildcard-thread marker. It is a fan-out rather than a room-level flag, so the
        thing that can silently go wrong is scope: a thread that holds a snapshot but escapes the
        fan-out reads as complete across a gap with no marker at all. Narrowing the statement past
        (principal, room) is exactly that bug, so pin both directions.
        """
        room_id = "!gapped:localhost"
        other_room_id = "!untouched:localhost"
        thread_ids = ["$one:localhost", "$two:localhost", "$three:localhost"]
        for thread_id in thread_ids:
            await replace_thread_unconditionally(
                event_cache,
                room_id,
                thread_id,
                [_message_event(thread_id, 1)],
            )
        other_thread_id = "$other:localhost"
        await replace_thread_unconditionally(
            event_cache,
            other_room_id,
            other_thread_id,
            [_message_event(other_thread_id, 1)],
        )
        other_principal = event_cache.for_principal("@other:localhost")
        await replace_thread_unconditionally(
            other_principal,
            room_id,
            thread_ids[0],
            [_message_event(thread_ids[0], 1)],
        )
        for thread_id in thread_ids:
            assert await event_cache.get_thread_cache_gap(room_id, thread_id) is None

        await event_cache.mark_room_threads_gap(room_id, reason="limited_sync_timeline")

        for thread_id in thread_ids:
            gap = await event_cache.get_thread_cache_gap(room_id, thread_id)
            assert gap is not None, f"{thread_id} holds a snapshot but escaped the room-scoped fan-out"
            assert gap.gap_reason == "limited_sync_timeline"
            assert await event_cache.get_thread_events(room_id, thread_id) is not None

        assert await event_cache.get_thread_cache_gap(other_room_id, other_thread_id) is None
        assert await other_principal.get_thread_cache_gap(room_id, thread_ids[0]) is None

        # A full refetch of one thread clears only that thread's share of the room-scoped gap.
        await replace_thread_unconditionally(
            event_cache,
            room_id,
            thread_ids[0],
            [_message_event(thread_ids[0], 1)],
        )
        assert await event_cache.get_thread_cache_gap(room_id, thread_ids[0]) is None
        assert await event_cache.get_thread_cache_gap(room_id, thread_ids[1]) is not None

    @pytest.mark.asyncio
    async def test_room_scoped_gap_survives_a_fetch_that_was_already_running(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """A thread cached for the first time after a room gap must not land unmarked.

        The fan-out is an UPDATE over the room's existing ``thread_state`` rows, so it cannot reach a
        thread that has none yet - which is exactly the thread whose first fetch is still in flight.
        If the replacement that lands afterwards inserts a clean row, the room gap is silently lost
        for that one thread and its pre-gap snapshot is served as complete. This is the fan-out's
        blind spot, and the room-level copy of the marker is what covers it.
        """
        room_id = "!in-flight:localhost"
        thread_id = "$late:localhost"
        fetch_started_at = 1000.0

        # No snapshot yet, so no thread_state row for the fan-out to touch.
        assert await event_cache.get_thread_cache_gap(room_id, thread_id) is None
        await event_cache.mark_room_threads_gap(room_id, reason="limited_sync_timeline")

        # The fetch that began before the gap now lands.
        stored = await event_cache.replace_thread(
            room_id,
            thread_id,
            [_message_event(thread_id, 1)],
            expected_membership_epoch=await event_cache.room_membership_epoch(room_id),
            fetch_started_at=fetch_started_at,
        )
        assert stored

        gap = await event_cache.get_thread_cache_gap(room_id, thread_id)
        assert gap is not None, "a fetch older than the room gap installed a snapshot with no marker"
        assert gap.gap_reason == "limited_sync_timeline"

        # And a fetch that started after the room gap still clears it, or nothing would ever refill.
        await replace_thread_unconditionally(event_cache, room_id, thread_id, [_message_event(thread_id, 1)])
        assert await event_cache.get_thread_cache_gap(room_id, thread_id) is None

    @pytest.mark.asyncio
    async def test_snapshot_presence_is_reported_separately_from_the_gap_marker(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """A never-cached thread has no gap marker either, so presence has to be asked separately.

        ``thread_ids_needing_refill`` decides what startup prewarm fetches. Answering it from the
        marker alone reports every cold thread as a cache hit and turns prewarm into a no-op, which
        no read-path test catches because the reads themselves still refill on demand.
        """
        room_id = "!presence:localhost"
        cached_thread_id = "$cached:localhost"
        cold_thread_id = "$cold:localhost"

        assert await event_cache.has_thread_snapshot(room_id, cold_thread_id) is False
        assert await event_cache.get_thread_cache_gap(room_id, cold_thread_id) is None

        await replace_thread_unconditionally(
            event_cache,
            room_id,
            cached_thread_id,
            [_message_event(cached_thread_id, 1)],
        )
        assert await event_cache.has_thread_snapshot(room_id, cached_thread_id) is True

        # A gap marker does not remove the rows, so presence stays true while the thread is unusable.
        await event_cache.mark_thread_gap(room_id, cached_thread_id, reason="live_thread_mutation")
        assert await event_cache.has_thread_snapshot(room_id, cached_thread_id) is True

        # Purging the rows does flip it, and marking a gap on a thread that has none never invents one.
        await event_cache.invalidate_thread(room_id, cached_thread_id)
        assert await event_cache.has_thread_snapshot(room_id, cached_thread_id) is False
        await event_cache.mark_thread_gap(room_id, cold_thread_id, reason="live_thread_mutation")
        assert await event_cache.has_thread_snapshot(room_id, cold_thread_id) is False

    @pytest.mark.asyncio
    async def test_an_unreadable_gap_marker_refuses_the_snapshot(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Not knowing whether a gap exists must reject the snapshot, not serve it.

        "No gap recorded" and "could not find out" are opposite answers, and a backend that returns
        the same value for both makes an unreadable marker read as a clean thread. The trust algebra
        failed closed here - a missing state row rejected with ``no_cache_state`` - so the gap rework
        has to as well, or a marker written during SQLite write contention is invisible to the very
        next read and the stale snapshot is served as complete.
        """
        room_id = "!unreadable:localhost"
        thread_id = "$thread:localhost"
        await replace_thread_unconditionally(
            event_cache,
            room_id,
            thread_id,
            [_message_event(thread_id, 1)],
        )
        assert await event_cache.get_thread_cache_gap(room_id, thread_id) is None

        event_cache.disable("contract_test_unreadable_gap")

        gap = await event_cache.get_thread_cache_gap(room_id, thread_id)
        assert gap is not None, "an unreadable gap marker read as a clean thread"
        assert thread_cache_rejection_reason(gap) == "cache_gap_read_unavailable"

    @pytest.mark.asyncio
    async def test_older_fetch_cannot_bury_a_newer_snapshot(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """A replacement from an older fetch must not delete a newer fetch's thread events.

        Installing a snapshot deletes the events it omits, so replacement has to be ordered by
        ``fetch_started_at`` rather than by arrival. Two fetches overlap, the newer one lands
        first, and the older one arrives late carrying a thread that no longer has the new reply.
        If the late arrival wins, the cache silently loses an event *and* records no gap marker,
        so the next read serves the truncated thread as complete. Pin both halves.
        """
        room_id = "!race:localhost"
        thread_id = "$thread:localhost"
        root = _message_event(thread_id, 1)
        new_reply = _message_event("$new:localhost", 3, thread_id=thread_id)
        old_fetch_started_at = 1000.0
        new_fetch_started_at = 2000.0

        # The newer fetch lands first and installs root + new reply.
        await replace_thread_unconditionally(
            event_cache,
            room_id,
            thread_id,
            [root, new_reply],
            fetch_started_at=new_fetch_started_at,
        )

        # The older fetch arrives late, having never seen the new reply.
        stored = await event_cache.replace_thread(
            room_id,
            thread_id,
            [root],
            expected_membership_epoch=await event_cache.room_membership_epoch(room_id),
            fetch_started_at=old_fetch_started_at,
        )

        # The loser reports success: a strictly fresher snapshot is installed, so there is nothing
        # for the caller to retry and no reason to arm repair backoff.
        assert stored
        cached = await event_cache.get_thread_events(room_id, thread_id)
        assert cached is not None
        assert [event["event_id"] for event in cached] == [thread_id, "$new:localhost"]
        assert await event_cache.get_thread_cache_gap(room_id, thread_id) is None

        # A fetch that started after the installed one still replaces it.
        await replace_thread_unconditionally(
            event_cache,
            room_id,
            thread_id,
            [root],
            fetch_started_at=new_fetch_started_at + 1,
        )
        cached = await event_cache.get_thread_events(room_id, thread_id)
        assert cached is not None
        assert [event["event_id"] for event in cached] == [thread_id]

    @pytest.mark.asyncio
    async def test_in_flight_fetch_cannot_delete_a_live_event_appended_after_it_started(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """A live append makes an already-running fetch too old to replace the snapshot.

        This is the append-shaped version of burying a snapshot. A scan starts, a live event lands
        in the thread while it is running, and the scan finishes carrying a thread that predates
        that event. Installing it would delete the event *and* record no gap, so the next read
        would serve the thread as complete with the event missing. Nothing retains the delta and
        no barrier serializes the two any more, so the ordering watermark is what prevents it.
        """
        room_id = "!race:localhost"
        thread_id = "$thread:localhost"
        root = _message_event(thread_id, 1)
        await replace_thread_unconditionally(
            event_cache,
            room_id,
            thread_id,
            [root],
            fetch_started_at=1000.0,
        )

        # A scan starts here, at 2000.0, seeing only the root...
        fetch_started_at = 2000.0

        # ...and a live event lands in the thread while it is still running.
        live = _message_event("$live:localhost", 3, thread_id=thread_id)
        assert (
            await event_cache.apply_thread_mutation_append(
                room_id,
                thread_id,
                live,
                append_failed_reason="live_append_failed",
            )
            is ThreadAppendOutcome.APPENDED
        )

        stored = await event_cache.replace_thread(
            room_id,
            thread_id,
            [root],
            expected_membership_epoch=await event_cache.room_membership_epoch(room_id),
            fetch_started_at=fetch_started_at,
        )

        assert stored
        cached = await event_cache.get_thread_events(room_id, thread_id)
        assert cached is not None
        assert [event["event_id"] for event in cached] == [thread_id, "$live:localhost"]

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
