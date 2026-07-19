"""Behavioral contract shared by every durable Matrix event-cache backend."""

from __future__ import annotations

import asyncio
import zlib
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.constants import (
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
)
from mindroom.matrix.cache import (
    ConversationEventCache,
    cache_maintenance,
    event_cache_events,
    postgres_event_cache_events,
    postgres_event_cache_threads,
    postgres_streaming_compaction,
    sqlite_event_cache_events,
    sqlite_event_cache_threads,
    sqlite_streaming_compaction,
)
from mindroom.matrix.cache.postgres_event_cache import PostgresEventCache
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from tests.event_cache_test_support import replace_thread_unconditionally

if TYPE_CHECKING:
    from collections.abc import Callable


def test_terminal_stream_trigger_uses_maintenance_status_policy() -> None:
    """Compaction triggering and selection share one terminal-status policy."""
    assert event_cache_events.TERMINAL_STREAM_STATUSES is cache_maintenance.TERMINAL_STREAM_STATUSES


def _message_event(
    event_id: str,
    timestamp: int,
    *,
    body: str | None = None,
    sender: str = "@user:localhost",
    thread_id: str | None = None,
    edit_of: str | None = None,
    stream_status: str | None = None,
) -> dict[str, Any]:
    content: dict[str, Any] = {
        "body": event_id if body is None else body,
        "msgtype": "m.text",
    }
    if thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    if edit_of is not None:
        content["m.new_content"] = {"body": content["body"], "msgtype": "m.text"}
        if stream_status is not None:
            content["m.new_content"][STREAM_STATUS_KEY] = stream_status
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
        assert isinstance(event_cache.certification_generation, str)
        assert isinstance(event_cache.runtime_diagnostics()["cache_backend"], str)
        assert isinstance(event_cache.pending_durable_write_room_ids(), tuple)

        event_cache.disable("contract_test")

        assert event_cache.durable_writes_available is False
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
    async def test_lookup_writes_only_probe_compaction_for_terminal_streaming_edits(
        self,
        event_cache: ConversationEventCache,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Only terminal message edits with string senders probe compaction."""
        compaction_calls: list[str] = []

        async def record_compaction(*_args: object, **_kwargs: object) -> int:
            compaction_calls.append("called")
            return 0

        module = sqlite_event_cache_events if isinstance(event_cache, SqliteEventCache) else postgres_event_cache_events
        monkeypatch.setattr(module, "compact_superseded_streaming_edits", record_compaction)
        room_id = "!room:localhost"
        original_id = "$original:localhost"

        await event_cache.store_event(original_id, room_id, _message_event(original_id, 1))
        await event_cache.store_event(
            "$pending:localhost",
            room_id,
            _message_event(
                "$pending:localhost",
                2,
                edit_of=original_id,
                stream_status=STREAM_STATUS_PENDING,
            ),
        )
        assert compaction_calls == []

        wrong_type = _message_event(
            "$approval-terminal:localhost",
            3,
            edit_of=original_id,
            stream_status=STREAM_STATUS_COMPLETED,
        )
        wrong_type["type"] = "io.mindroom.tool_approval"
        await event_cache.store_event("$approval-terminal:localhost", room_id, wrong_type)
        missing_sender = _message_event(
            "$missing-sender-terminal:localhost",
            4,
            edit_of=original_id,
            stream_status=STREAM_STATUS_COMPLETED,
        )
        del missing_sender["sender"]
        await event_cache.store_event("$missing-sender-terminal:localhost", room_id, missing_sender)
        assert compaction_calls == []

        await event_cache.store_event(
            "$terminal:localhost",
            room_id,
            _message_event(
                "$terminal:localhost",
                3,
                edit_of=original_id,
                stream_status=STREAM_STATUS_COMPLETED,
            ),
        )
        assert compaction_calls == ["called"]

    @pytest.mark.asyncio
    async def test_thread_writes_only_probe_compaction_for_terminal_streaming_edits(
        self,
        event_cache: ConversationEventCache,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ordinary and nonterminal thread writes avoid the compaction query."""
        compaction_calls: list[str] = []

        async def record_compaction(*_args: object, **_kwargs: object) -> int:
            compaction_calls.append("called")
            return 0

        module = (
            sqlite_event_cache_threads if isinstance(event_cache, SqliteEventCache) else postgres_event_cache_threads
        )
        monkeypatch.setattr(module, "compact_superseded_streaming_edits", record_compaction)
        room_id = "!room:localhost"
        thread_id = "$root:localhost"
        root = _message_event(thread_id, 1)
        pending = _message_event(
            "$pending:localhost",
            2,
            edit_of=thread_id,
            stream_status=STREAM_STATUS_PENDING,
        )
        terminal = _message_event(
            "$terminal:localhost",
            3,
            edit_of=thread_id,
            stream_status=STREAM_STATUS_COMPLETED,
        )

        await replace_thread_unconditionally(event_cache, room_id, thread_id, [root, pending])
        assert compaction_calls == []
        assert await event_cache.append_event(room_id, thread_id, terminal) is True
        assert compaction_calls == ["called"]

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
    async def test_streaming_compaction_preserves_redaction_fallback_chain(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Compacted nonterminal edits remain exact cold history when newer edits are redacted."""
        room_id = "!room:localhost"
        thread_id = "$original:localhost"
        sender = "@agent:localhost"
        root = _message_event(thread_id, 1, sender=sender)
        pending = _message_event(
            "$pending:localhost",
            2,
            sender=sender,
            edit_of=thread_id,
            stream_status=STREAM_STATUS_PENDING,
        )
        first_terminal = _message_event(
            "$first-terminal:localhost",
            3,
            sender=sender,
            edit_of=thread_id,
            stream_status=STREAM_STATUS_COMPLETED,
        )
        streaming = _message_event(
            "$streaming:localhost",
            4,
            sender=sender,
            edit_of=thread_id,
            stream_status=STREAM_STATUS_STREAMING,
        )
        final_terminal = _message_event(
            "$final-terminal:localhost",
            5,
            sender=sender,
            edit_of=thread_id,
            stream_status=STREAM_STATUS_COMPLETED,
        )
        await replace_thread_unconditionally(
            event_cache,
            room_id,
            thread_id,
            [final_terminal, streaming, first_terminal, pending, root],
        )

        cached_thread = await event_cache.get_thread_events(room_id, thread_id)
        assert cached_thread is not None
        assert [event["event_id"] for event in cached_thread] == [
            thread_id,
            "$pending:localhost",
            "$first-terminal:localhost",
            "$streaming:localhost",
            "$final-terminal:localhost",
        ]
        assert await event_cache.get_event(room_id, "$pending:localhost") == pending
        assert await event_cache.get_event(room_id, "$streaming:localhost") == streaming
        assert await event_cache.get_latest_edit(room_id, thread_id, sender=sender) == final_terminal

        assert await event_cache.redact_event(room_id, "$final-terminal:localhost") is True
        assert await event_cache.get_latest_edit(room_id, thread_id, sender=sender) == streaming
        assert await event_cache.redact_event(room_id, "$streaming:localhost") is True
        assert await event_cache.get_latest_edit(room_id, thread_id, sender=sender) == first_terminal
        assert await event_cache.redact_event(room_id, "$first-terminal:localhost") is True
        assert await event_cache.get_latest_edit(room_id, thread_id, sender=sender) == pending
        assert await event_cache.redact_event(room_id, "$pending:localhost") is True
        assert await event_cache.get_latest_edit(room_id, thread_id, sender=sender) is None

    @pytest.mark.asyncio
    async def test_original_redaction_tombstones_compacted_edits(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Original redaction removes cold edits and blocks every late replay from resurrecting them."""
        room_id = "!room:localhost"
        original_id = "$original:localhost"
        sender = "@agent:localhost"
        original = _message_event(original_id, 1, sender=sender)
        pending = _message_event(
            "$pending:localhost",
            2,
            sender=sender,
            edit_of=original_id,
            stream_status=STREAM_STATUS_PENDING,
        )
        terminal = _message_event(
            "$terminal:localhost",
            3,
            sender=sender,
            edit_of=original_id,
            stream_status=STREAM_STATUS_COMPLETED,
        )
        await replace_thread_unconditionally(
            event_cache,
            room_id,
            original_id,
            [terminal, pending, original],
        )

        assert await event_cache.redact_event(room_id, original_id) is True
        assert await event_cache.get_event(room_id, pending["event_id"]) is None
        assert await event_cache.get_event(room_id, terminal["event_id"]) is None

        await event_cache.store_events_batch(
            [
                (original_id, room_id, original),
                (str(pending["event_id"]), room_id, pending),
                (str(terminal["event_id"]), room_id, terminal),
            ],
        )

        assert await event_cache.get_event(room_id, original_id) is None
        assert await event_cache.get_event(room_id, str(pending["event_id"])) is None
        assert await event_cache.get_event(room_id, str(terminal["event_id"])) is None
        assert await event_cache.get_latest_edit(room_id, original_id, sender=sender) is None


@pytest.mark.asyncio
async def test_streaming_compaction_survives_backend_restart(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Both backends retain compressed edit history and normalized thread ordering across restart."""
    room_id = "!room:localhost"
    thread_id = "$original:localhost"
    sender = "@agent:localhost"
    root = _message_event(thread_id, 1, sender=sender)
    pending = _message_event(
        "$pending:localhost",
        2,
        sender=sender,
        edit_of=thread_id,
        stream_status=STREAM_STATUS_PENDING,
    )
    terminal = _message_event(
        "$terminal:localhost",
        3,
        sender=sender,
        edit_of=thread_id,
        stream_status=STREAM_STATUS_COMPLETED,
    )
    first_cache = event_cache_factory()
    await first_cache.initialize()
    try:
        await replace_thread_unconditionally(
            first_cache,
            room_id,
            thread_id,
            [terminal, pending, root],
        )
    finally:
        await first_cache.close()

    restarted_cache = event_cache_factory()
    await restarted_cache.initialize()
    try:
        cached_thread = await restarted_cache.get_thread_events(room_id, thread_id)
        diagnostics = restarted_cache.runtime_diagnostics()

        assert cached_thread is not None
        assert [event["event_id"] for event in cached_thread] == [
            thread_id,
            "$pending:localhost",
            "$terminal:localhost",
        ]
        assert await restarted_cache.get_event(room_id, "$pending:localhost") == pending
        assert await restarted_cache.get_latest_edit(room_id, thread_id, sender=sender) == terminal
        assert diagnostics["cache_compacted_streaming_edit_archive_rows"] == 1
        assert diagnostics["cache_compacted_streaming_edit_archive_bytes"] > 0
        assert "database_url" not in str(diagnostics).lower()
        assert str(pending["content"]["body"]) not in str(diagnostics)
    finally:
        await restarted_cache.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corrupt_payload",
    [b"corrupt", zlib.compress(b"[]")],
    ids=["invalid-compressed-bytes", "valid-non-object-json"],
)
async def test_corrupt_compacted_payload_disables_advisory_cache(
    event_cache: ConversationEventCache,
    corrupt_payload: bytes,
) -> None:
    """Invalid cold payloads produce a cache miss and log-safe disabled state instead of escaping."""
    room_id = "!room:localhost"
    original_id = "$original:localhost"
    sender = "@agent:localhost"
    pending = _message_event(
        "$pending:localhost",
        2,
        sender=sender,
        edit_of=original_id,
        stream_status=STREAM_STATUS_PENDING,
    )
    terminal = _message_event(
        "$terminal:localhost",
        3,
        sender=sender,
        edit_of=original_id,
        stream_status=STREAM_STATUS_COMPLETED,
    )
    await event_cache.store_events_batch(
        [
            (str(pending["event_id"]), room_id, pending),
            (str(terminal["event_id"]), room_id, terminal),
        ],
    )

    if isinstance(event_cache, SqliteEventCache):
        db = event_cache._runtime.require_db()
        await db.execute(
            "UPDATE compacted_streaming_edits SET event_json_zlib = ? WHERE event_id = ?",
            (corrupt_payload, pending["event_id"]),
        )
        await db.commit()
    else:
        assert isinstance(event_cache, PostgresEventCache)
        db = event_cache._runtime.require_db()
        await db.execute(
            """
            UPDATE mindroom_event_cache_compacted_streaming_edits
            SET event_json_zlib = %s
            WHERE namespace = %s AND event_id = %s
            """,
            (corrupt_payload, event_cache.namespace, pending["event_id"]),
        )
        await db.commit()

    assert await event_cache.get_event(room_id, str(pending["event_id"])) is None
    assert event_cache.durable_writes_available is False
    diagnostics = event_cache.runtime_diagnostics()
    assert diagnostics["cache_backend"] in {"sqlite", "postgres"}
    assert "corrupt_compacted_event_payload" in str(diagnostics)
    assert str(pending["content"]["body"]) not in str(diagnostics)


@pytest.mark.asyncio
async def test_streaming_compaction_processes_multiple_bounded_batches(
    event_cache_factory: Callable[[], ConversationEventCache],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both backends archive large candidate sets through bounded transactional batches."""
    monkeypatch.setattr(sqlite_streaming_compaction, "_COMPACTION_BATCH_SIZE", 2)
    monkeypatch.setattr(postgres_streaming_compaction, "_COMPACTION_BATCH_SIZE", 2)
    room_id = "!room:localhost"
    original_id = "$original:localhost"
    sender = "@agent:localhost"
    pending_edits = [
        _message_event(
            f"$pending-{index}:localhost",
            index + 2,
            sender=sender,
            edit_of=original_id,
            stream_status=STREAM_STATUS_PENDING,
        )
        for index in range(5)
    ]
    terminal = _message_event(
        "$terminal:localhost",
        20,
        sender=sender,
        edit_of=original_id,
        stream_status=STREAM_STATUS_COMPLETED,
    )
    cache = event_cache_factory()
    await cache.initialize()
    try:
        await cache.store_events_batch(
            [(str(event["event_id"]), room_id, event) for event in pending_edits],
        )
        await cache.store_event(str(terminal["event_id"]), room_id, terminal)
        for pending in pending_edits:
            assert await cache.get_event(room_id, str(pending["event_id"])) == pending
    finally:
        await cache.close()

    restarted_cache = event_cache_factory()
    await restarted_cache.initialize()
    try:
        assert restarted_cache.runtime_diagnostics()["cache_compacted_streaming_edit_archive_rows"] == 5
        assert await restarted_cache.get_latest_edit(room_id, original_id, sender=sender) == terminal
    finally:
        await restarted_cache.close()


@pytest.mark.asyncio
async def test_streaming_compaction_cancellation_rolls_back_all_batches(
    event_cache_factory: Callable[[], ConversationEventCache],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation after one archived batch cannot expose a partially moved edit set."""
    monkeypatch.setattr(sqlite_streaming_compaction, "_COMPACTION_BATCH_SIZE", 2)
    monkeypatch.setattr(postgres_streaming_compaction, "_COMPACTION_BATCH_SIZE", 2)
    sqlite_archive_batch = sqlite_streaming_compaction._archive_candidate_batch
    postgres_archive_batch = postgres_streaming_compaction._archive_candidate_batch
    cancel_reason = "compaction cancelled after first batch"

    async def cancel_after_sqlite_batch(*args: object, **kwargs: object) -> None:
        await sqlite_archive_batch(*args, **kwargs)
        raise asyncio.CancelledError(cancel_reason)

    async def cancel_after_postgres_batch(*args: object, **kwargs: object) -> None:
        await postgres_archive_batch(*args, **kwargs)
        raise asyncio.CancelledError(cancel_reason)

    room_id = "!room:localhost"
    original_id = "$original:localhost"
    sender = "@agent:localhost"
    pending_edits = [
        _message_event(
            f"$pending-{index}:localhost",
            index + 2,
            sender=sender,
            edit_of=original_id,
            stream_status=STREAM_STATUS_PENDING,
        )
        for index in range(4)
    ]
    terminal = _message_event(
        "$terminal:localhost",
        20,
        sender=sender,
        edit_of=original_id,
        stream_status=STREAM_STATUS_COMPLETED,
    )
    cache = event_cache_factory()
    await cache.initialize()
    try:
        await cache.store_events_batch(
            [(str(event["event_id"]), room_id, event) for event in pending_edits],
        )
        monkeypatch.setattr(sqlite_streaming_compaction, "_archive_candidate_batch", cancel_after_sqlite_batch)
        monkeypatch.setattr(postgres_streaming_compaction, "_archive_candidate_batch", cancel_after_postgres_batch)
        with pytest.raises(asyncio.CancelledError, match="compaction cancelled"):
            await cache.store_event(str(terminal["event_id"]), room_id, terminal)
        assert await cache.get_event(room_id, str(terminal["event_id"])) is None
        for pending in pending_edits:
            assert await cache.get_event(room_id, str(pending["event_id"])) == pending
    finally:
        await cache.close()

    restarted_cache = event_cache_factory()
    await restarted_cache.initialize()
    try:
        assert restarted_cache.runtime_diagnostics()["cache_compacted_streaming_edit_archive_rows"] == 0
    finally:
        await restarted_cache.close()


@pytest.mark.asyncio
async def test_replaying_compacted_thread_member_preserves_snapshot_state(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Replaying a cold edit preserves its snapshot membership and learned thread mapping."""
    room_id = "!room:localhost"
    thread_id = "$root:localhost"
    sender = "@agent:localhost"
    pending = _message_event(
        "$pending:localhost",
        2,
        sender=sender,
        edit_of=thread_id,
        stream_status=STREAM_STATUS_PENDING,
    )
    terminal = _message_event(
        "$terminal:localhost",
        3,
        sender=sender,
        edit_of=thread_id,
        stream_status=STREAM_STATUS_COMPLETED,
    )
    cache = event_cache_factory()
    await cache.initialize()
    try:
        await replace_thread_unconditionally(cache, room_id, thread_id, [pending, terminal])
        assert await cache.get_thread_events(room_id, thread_id) == [pending, terminal]
        assert await cache.get_thread_id_for_event(room_id, str(pending["event_id"])) == thread_id

        await cache.store_event(str(pending["event_id"]), room_id, pending)
        assert await cache.get_thread_events(room_id, thread_id) == [pending, terminal]
        assert await cache.get_thread_id_for_event(room_id, str(pending["event_id"])) == thread_id

        await cache.store_events_batch([(str(pending["event_id"]), room_id, pending)])
    finally:
        await cache.close()

    restarted_cache = event_cache_factory()
    await restarted_cache.initialize()
    try:
        assert await restarted_cache.get_thread_events(room_id, thread_id) == [pending, terminal]
        assert await restarted_cache.get_thread_id_for_event(room_id, str(pending["event_id"])) == thread_id
        assert restarted_cache.runtime_diagnostics()["cache_compacted_streaming_edit_archive_rows"] == 1
    finally:
        await restarted_cache.close()


@pytest.mark.asyncio
async def test_repeated_compaction_keeps_equal_timestamp_write_order(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Cold fallback ordering remains stable after active SQLite rows are deleted and reused."""
    room_id = "!room:localhost"
    original_id = "$original:localhost"
    sender = "@agent:localhost"
    first_pending = _message_event(
        "$first-pending:localhost",
        2,
        sender=sender,
        edit_of=original_id,
        stream_status=STREAM_STATUS_PENDING,
    )
    first_terminal = _message_event(
        "$first-terminal:localhost",
        3,
        sender=sender,
        edit_of=original_id,
        stream_status=STREAM_STATUS_COMPLETED,
    )
    second_pending = _message_event(
        "$second-pending:localhost",
        2,
        sender=sender,
        edit_of=original_id,
        stream_status=STREAM_STATUS_PENDING,
    )
    final_terminal = _message_event(
        "$final-terminal:localhost",
        4,
        sender=sender,
        edit_of=original_id,
        stream_status=STREAM_STATUS_COMPLETED,
    )
    cache = event_cache_factory()
    await cache.initialize()
    try:
        await cache.store_events_batch(
            [
                (str(first_pending["event_id"]), room_id, first_pending),
                (str(first_terminal["event_id"]), room_id, first_terminal),
            ],
        )
        await cache.store_events_batch(
            [
                (str(second_pending["event_id"]), room_id, second_pending),
                (str(final_terminal["event_id"]), room_id, final_terminal),
            ],
        )
        assert await cache.redact_event(room_id, str(final_terminal["event_id"])) is True
        assert await cache.get_latest_edit(room_id, original_id, sender=sender) == first_terminal
        assert await cache.redact_event(room_id, str(first_terminal["event_id"])) is True
        assert await cache.get_latest_edit(room_id, original_id, sender=sender) == second_pending
        assert await cache.redact_event(room_id, str(second_pending["event_id"])) is True
        assert await cache.get_latest_edit(room_id, original_id, sender=sender) == first_pending
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_streaming_compaction_guards_sender_and_equal_timestamp(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Compaction requires a strictly newer terminal edit in the same sender partition."""
    room_id = "!room:localhost"
    original_id = "$original:localhost"
    first_sender = "@agent:localhost"
    other_sender = "@other:localhost"
    pending = _message_event(
        "$pending:localhost",
        2,
        sender=first_sender,
        edit_of=original_id,
        stream_status=STREAM_STATUS_PENDING,
    )
    equal_terminal = _message_event(
        "$equal-terminal:localhost",
        2,
        sender=first_sender,
        edit_of=original_id,
        stream_status=STREAM_STATUS_COMPLETED,
    )
    other_pending = _message_event(
        "$other-pending:localhost",
        1,
        sender=other_sender,
        edit_of=original_id,
        stream_status=STREAM_STATUS_PENDING,
    )
    cache = event_cache_factory()
    await cache.initialize()
    try:
        await cache.store_events_batch(
            [
                (str(pending["event_id"]), room_id, pending),
                (str(equal_terminal["event_id"]), room_id, equal_terminal),
                (str(other_pending["event_id"]), room_id, other_pending),
            ],
        )
    finally:
        await cache.close()

    equal_timestamp_restart = event_cache_factory()
    await equal_timestamp_restart.initialize()
    try:
        assert equal_timestamp_restart.runtime_diagnostics()["cache_compacted_streaming_edit_archive_rows"] == 0
        later_terminal = _message_event(
            "$later-terminal:localhost",
            3,
            sender=first_sender,
            edit_of=original_id,
            stream_status=STREAM_STATUS_COMPLETED,
        )
        await equal_timestamp_restart.store_event(
            str(later_terminal["event_id"]),
            room_id,
            later_terminal,
        )
    finally:
        await equal_timestamp_restart.close()

    partition_restart = event_cache_factory()
    await partition_restart.initialize()
    try:
        diagnostics = partition_restart.runtime_diagnostics()
        assert diagnostics["cache_compacted_streaming_edit_archive_rows"] == 1
        assert await partition_restart.get_event(room_id, str(pending["event_id"])) == pending
        assert (
            await partition_restart.get_latest_edit(
                room_id,
                original_id,
                sender=other_sender,
            )
            == other_pending
        )
    finally:
        await partition_restart.close()


@pytest.mark.asyncio
async def test_snapshot_replacement_removes_compacted_members(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Authoritative replacement removes both active and cold members of the old snapshot."""
    room_id = "!room:localhost"
    thread_id = "$original:localhost"
    sender = "@agent:localhost"
    root = _message_event(thread_id, 1, sender=sender)
    pending = _message_event(
        "$pending:localhost",
        2,
        sender=sender,
        edit_of=thread_id,
        stream_status=STREAM_STATUS_PENDING,
    )
    terminal = _message_event(
        "$terminal:localhost",
        3,
        sender=sender,
        edit_of=thread_id,
        stream_status=STREAM_STATUS_COMPLETED,
    )
    cache = event_cache_factory()
    await cache.initialize()
    try:
        await replace_thread_unconditionally(cache, room_id, thread_id, [terminal, pending, root])
        await replace_thread_unconditionally(cache, room_id, thread_id, [root])
    finally:
        await cache.close()

    restarted_cache = event_cache_factory()
    await restarted_cache.initialize()
    try:
        assert await restarted_cache.get_thread_events(room_id, thread_id) == [root]
        assert await restarted_cache.get_event(room_id, str(pending["event_id"])) is None
        assert await restarted_cache.get_event(room_id, str(terminal["event_id"])) is None
        assert restarted_cache.runtime_diagnostics()["cache_compacted_streaming_edit_archive_rows"] == 0
    finally:
        await restarted_cache.close()


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
    assert await event_cache.get_thread_id_for_event(room_id, thread_id) == thread_id

    if deletion == "redaction":
        assert await event_cache.redact_event(room_id, str(child["event_id"])) is True
    elif deletion == "replacement":
        await replace_thread_unconditionally(event_cache, room_id, thread_id, [])
    else:
        await event_cache.invalidate_thread(room_id, thread_id)

    assert await event_cache.get_thread_id_for_event(room_id, thread_id) is None


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


@pytest.mark.asyncio
async def test_restart_preserves_learned_root_mapping_proven_by_cold_child(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """A root self-mapping remains valid when its only surviving proof is a compacted child."""
    room_id = "!room:localhost"
    thread_id = "$unfetched-root:localhost"
    sender = "@agent:localhost"
    pending = _message_event(
        "$pending:localhost",
        2,
        sender=sender,
        edit_of=thread_id,
        stream_status=STREAM_STATUS_PENDING,
    )
    terminal = _message_event(
        "$terminal:localhost",
        3,
        sender=sender,
        edit_of=thread_id,
        stream_status=STREAM_STATUS_COMPLETED,
    )
    cache = event_cache_factory()
    await cache.initialize()
    try:
        await replace_thread_unconditionally(cache, room_id, thread_id, [terminal, pending])
        assert await cache.redact_event(room_id, str(terminal["event_id"])) is True
    finally:
        await cache.close()

    restarted_cache = event_cache_factory()
    await restarted_cache.initialize()
    try:
        assert await restarted_cache.get_thread_events(room_id, thread_id) == [pending]
        assert await restarted_cache.get_thread_id_for_event(room_id, thread_id) == thread_id
        assert restarted_cache.runtime_diagnostics()["cache_orphan_thread_indexes_after"] == 0
    finally:
        await restarted_cache.close()


@pytest.mark.asyncio
async def test_append_accepts_thread_with_only_compacted_members(
    event_cache: ConversationEventCache,
) -> None:
    """A cold-only valid snapshot remains appendable after its terminal edit is redacted."""
    room_id = "!room:localhost"
    thread_id = "$unfetched-root:localhost"
    sender = "@agent:localhost"
    pending = _message_event(
        "$pending:localhost",
        2,
        sender=sender,
        edit_of=thread_id,
        stream_status=STREAM_STATUS_PENDING,
    )
    terminal = _message_event(
        "$terminal:localhost",
        3,
        sender=sender,
        edit_of=thread_id,
        stream_status=STREAM_STATUS_COMPLETED,
    )
    appended = _message_event(
        "$appended:localhost",
        4,
        sender=sender,
        thread_id=thread_id,
    )
    await replace_thread_unconditionally(event_cache, room_id, thread_id, [terminal, pending])
    assert await event_cache.redact_event(room_id, str(terminal["event_id"])) is True

    assert await event_cache.append_event(room_id, thread_id, appended) is True
    assert await event_cache.get_thread_events(room_id, thread_id) == [pending, appended]
