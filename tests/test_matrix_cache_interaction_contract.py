"""Owning-seam contract tests for Matrix event-cache interaction families."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.logging_config import get_logger
from mindroom.matrix.cache import (
    ConversationEventCache,
    EventCacheWriteCoordinator,
    thread_cache_rejection_reason,
)
from mindroom.matrix.cache.thread_write_cache_ops import ThreadMutationCacheOps
from mindroom.matrix.cache.thread_writes import ThreadSyncWritePolicy
from mindroom.matrix.client_thread_history import fetch_dispatch_thread_snapshot, fetch_thread_history
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_bookkeeping import ThreadMutationResolver
from mindroom.matrix.thread_diagnostics import (
    THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC,
    THREAD_HISTORY_DEGRADED_DIAGNOSTIC,
    THREAD_HISTORY_ERROR_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_STALE_CACHE,
)
from tests.event_cache_test_support import (
    raw_nio_event,
    raw_nio_redaction,
    replace_thread_unconditionally,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import nio

    from mindroom.bot_runtime_view import BotRuntimeView


_ROOM_ID = "!cache-contract:localhost"
_THREAD_ID = "$thread-root"
_THREAD_CHILD_ID = "$thread-child"
_OTHER_THREAD_ID = "$other-thread-root"
_SENDER = "@user:localhost"


@dataclass(slots=True)
class _EventSection:
    events: list[object] = field(default_factory=list)


@dataclass(slots=True)
class _Timeline:
    events: list[nio.Event] = field(default_factory=list)
    limited: bool = False


@dataclass(slots=True)
class _RoomSync:
    timeline: _Timeline = field(default_factory=_Timeline)
    state: _EventSection = field(default_factory=_EventSection)
    ephemeral: _EventSection = field(default_factory=_EventSection)
    account_data: _EventSection = field(default_factory=_EventSection)


@dataclass(slots=True)
class _SyncRooms:
    join: dict[str, _RoomSync] = field(default_factory=dict)
    invite: dict[str, _RoomSync] = field(default_factory=dict)
    leave: dict[str, _RoomSync] = field(default_factory=dict)


@dataclass(slots=True)
class _DeviceLists:
    changed: list[str] = field(default_factory=list)
    left: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _SyncEnvelope:
    rooms: _SyncRooms = field(default_factory=_SyncRooms)
    presence: _EventSection = field(default_factory=_EventSection)
    account_data: _EventSection = field(default_factory=_EventSection)
    to_device: _EventSection = field(default_factory=_EventSection)
    device_lists: _DeviceLists = field(default_factory=_DeviceLists)


@dataclass(slots=True)
class _CacheRuntime:
    event_cache: ConversationEventCache
    event_cache_write_coordinator: EventCacheWriteCoordinator


@dataclass(slots=True)
class _SyncHarness:
    policy: ThreadSyncWritePolicy

    async def apply(self, response: _SyncEnvelope) -> None:
        """Apply one sync envelope and require its admitted timeline writes to persist."""
        result = await self.policy.cache_sync_timeline_for_certification(
            cast("nio.SyncResponse", response),
        )
        assert result.complete is True
        assert result.errors == ()


def _event_source(
    event_id: str,
    event_type: str,
    content: dict[str, Any],
    *,
    timestamp: int,
    state_key: str | None = None,
) -> dict[str, Any]:
    source: dict[str, Any] = {
        "content": content,
        "event_id": event_id,
        "origin_server_ts": timestamp,
        "room_id": _ROOM_ID,
        "sender": _SENDER,
        "type": event_type,
    }
    if state_key is not None:
        source["state_key"] = state_key
    return source


def _message_source(
    event_id: str,
    msgtype: str,
    *,
    timestamp: int,
    body: str | None = None,
    relation: dict[str, Any] | None = None,
    extra_content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    content: dict[str, Any] = {
        "body": event_id if body is None else body,
        "msgtype": msgtype,
    }
    if relation is not None:
        content["m.relates_to"] = relation
    if extra_content is not None:
        content.update(extra_content)
    return _event_source(
        event_id,
        "m.room.message",
        content,
        timestamp=timestamp,
    )


def _thread_relation(thread_id: str = _THREAD_ID) -> dict[str, str]:
    return {"event_id": thread_id, "rel_type": "m.thread"}


def _reply_relation(event_id: str) -> dict[str, dict[str, str]]:
    return {"m.in_reply_to": {"event_id": event_id}}


def _edit_source(
    event_id: str,
    original_event_id: str,
    *,
    body: str,
    timestamp: int,
    thread_id: str | None = None,
) -> dict[str, Any]:
    new_content: dict[str, Any] = {"body": body, "msgtype": "m.text"}
    if thread_id is not None:
        new_content["m.relates_to"] = _thread_relation(thread_id)
    return _message_source(
        event_id,
        "m.text",
        body=f"* {body}",
        timestamp=timestamp,
        relation={"event_id": original_event_id, "rel_type": "m.replace"},
        extra_content={"m.new_content": new_content},
    )


def _sync_response(events: Sequence[nio.Event], *, room_id: str = _ROOM_ID) -> _SyncEnvelope:
    return _SyncEnvelope(
        rooms=_SyncRooms(
            join={room_id: _RoomSync(timeline=_Timeline(events=list(events)))},
        ),
    )


def _room_level_timeline_sources() -> list[dict[str, Any]]:
    """Return the complete joined-timeline room-level interaction matrix."""
    return [
        _message_source("$text", "m.text", timestamp=10, body="text"),
        _message_source("$notice", "m.notice", timestamp=11, body="notice"),
        _message_source("$emote", "m.emote", timestamp=12, body="waves"),
        _message_source(
            "$location",
            "m.location",
            timestamp=13,
            body="location",
            extra_content={"geo_uri": "geo:51.5,-0.1"},
        ),
        _message_source(
            "$file",
            "m.file",
            timestamp=14,
            body="tiny.txt",
            extra_content={"info": {"mimetype": "text/plain", "size": 4}, "url": "mxc://localhost/file"},
        ),
        _message_source(
            "$image",
            "m.image",
            timestamp=15,
            body="tiny.png",
            extra_content={
                "info": {"h": 1, "mimetype": "image/png", "size": 68, "w": 1},
                "url": "mxc://localhost/image",
            },
        ),
        _message_source(
            "$audio",
            "m.audio",
            timestamp=16,
            body="silence.wav",
            extra_content={
                "info": {"duration": 20, "mimetype": "audio/wav", "size": 48},
                "org.matrix.msc1767.audio": {"duration": 20, "waveform": [0]},
                "org.matrix.msc3245.voice": {},
                "url": "mxc://localhost/audio",
            },
        ),
        _message_source(
            "$video",
            "m.video",
            timestamp=17,
            body="black.webm",
            extra_content={
                "info": {"duration": 40, "h": 2, "mimetype": "video/webm", "size": 100, "w": 2},
                "url": "mxc://localhost/video",
            },
        ),
        _event_source(
            "$sticker",
            "m.sticker",
            {"body": "sticker", "info": {"h": 1, "w": 1}, "url": "mxc://localhost/sticker"},
            timestamp=18,
        ),
        _event_source(
            "$poll-start",
            "m.poll.start",
            {
                "m.poll.start": {
                    "answers": [{"id": "a", "m.text": "A"}],
                    "kind": "m.disclosed",
                    "max_selections": 1,
                    "question": {"m.text": "Pick"},
                },
            },
            timestamp=19,
        ),
        _event_source(
            "$poll-response",
            "m.poll.response",
            {
                "m.poll.response": {"answers": ["a"]},
                "m.relates_to": {"event_id": "$poll-start", "rel_type": "m.reference"},
            },
            timestamp=20,
        ),
        _event_source(
            "$poll-end",
            "m.poll.end",
            {
                "m.poll.end": {"m.text": "Closed"},
                "m.relates_to": {"event_id": "$poll-start", "rel_type": "m.reference"},
            },
            timestamp=21,
        ),
        _event_source(
            "$beacon-info",
            "m.beacon_info",
            {"asset": {"type": "m.self"}, "description": "audit", "live": True, "timeout": 60000},
            timestamp=22,
            state_key="@user:localhost",
        ),
        _event_source(
            "$beacon",
            "m.beacon",
            {
                "m.relates_to": {"event_id": "$beacon-info", "rel_type": "m.reference"},
                "org.matrix.msc3488.location": {"description": "audit", "uri": "geo:51.5,-0.1"},
                "org.matrix.msc3488.ts": 22,
            },
            timestamp=23,
        ),
        _event_source(
            "$create",
            "m.room.create",
            {"creator": _SENDER, "room_version": "10"},
            timestamp=24,
            state_key="",
        ),
        _event_source(
            "$member",
            "m.room.member",
            {"membership": "join"},
            timestamp=25,
            state_key=_SENDER,
        ),
        _event_source("$name", "m.room.name", {"name": "Audit"}, timestamp=26, state_key=""),
        _event_source("$topic", "m.room.topic", {"topic": "Audit"}, timestamp=27, state_key=""),
        _event_source(
            "$avatar",
            "m.room.avatar",
            {"url": "mxc://localhost/avatar"},
            timestamp=28,
            state_key="",
        ),
        _event_source(
            "$power",
            "m.room.power_levels",
            {"events_default": 0, "state_default": 50, "users": {_SENDER: 100}},
            timestamp=29,
            state_key="",
        ),
        _event_source("$join", "m.room.join_rules", {"join_rule": "invite"}, timestamp=30, state_key=""),
        _event_source(
            "$history",
            "m.room.history_visibility",
            {"history_visibility": "shared"},
            timestamp=31,
            state_key="",
        ),
        _event_source(
            "$guest",
            "m.room.guest_access",
            {"guest_access": "forbidden"},
            timestamp=32,
            state_key="",
        ),
        _event_source(
            "$alias",
            "m.room.canonical_alias",
            {"alias": "#audit:localhost", "alt_aliases": []},
            timestamp=33,
            state_key="",
        ),
        _event_source(
            "$encryption",
            "m.room.encryption",
            {"algorithm": "m.megolm.v1.aes-sha2"},
            timestamp=34,
            state_key="",
        ),
        _event_source(
            "$pin",
            "m.room.pinned_events",
            {"pinned": [_THREAD_ID]},
            timestamp=35,
            state_key="",
        ),
        _event_source(
            "$space-child",
            "m.space.child",
            {"order": "a", "suggested": False, "via": ["localhost"]},
            timestamp=36,
            state_key="!child:localhost",
        ),
        _event_source(
            "$generic-state",
            "com.mindroom.cache.audit.state",
            {"value": "state"},
            timestamp=37,
            state_key="contract",
        ),
        _event_source("$call-invite", "m.call.invite", {"call_id": "call", "version": 1}, timestamp=38),
        _event_source(
            "$call-candidates",
            "m.call.candidates",
            {"call_id": "call", "candidates": [], "version": 1},
            timestamp=39,
        ),
        _event_source("$call-answer", "m.call.answer", {"call_id": "call", "version": 1}, timestamp=40),
        _event_source(
            "$call-select",
            "m.call.select_answer",
            {"call_id": "call", "party_id": "party", "selected_party_id": "party", "version": 1},
            timestamp=41,
        ),
        _event_source("$call-reject", "m.call.reject", {"call_id": "call", "version": 1}, timestamp=42),
        _event_source(
            "$call-negotiate",
            "m.call.negotiate",
            {"call_id": "call", "description": {"sdp": "", "type": "offer"}, "version": 1},
            timestamp=43,
        ),
        _event_source("$call-hangup", "m.call.hangup", {"call_id": "call", "version": 1}, timestamp=44),
        _event_source(
            "$rtc-member",
            "org.matrix.msc3401.call.member",
            {
                "application": "m.call",
                "call_id": "",
                "device_id": "DEVICE",
                "foci_active": [{"focus_selection": "oldest_membership", "type": "livekit"}],
                "focus_active": {"focus_selection": "oldest_membership", "type": "livekit"},
                "scope": "m.room",
            },
            timestamp=45,
            state_key="@user:localhost_DEVICE",
        ),
        _event_source(
            "$rtc-notification",
            "org.matrix.msc4075.rtc.notification",
            {"m.relates_to": {"event_id": "$rtc-member", "rel_type": "m.annotation"}, "type": "ring"},
            timestamp=46,
        ),
        _event_source(
            "$generic-timeline",
            "com.mindroom.cache.audit",
            {"value": "timeline"},
            timestamp=47,
        ),
        _event_source(
            "$thread-related-sticker",
            "m.sticker",
            {"body": "sticker", "m.relates_to": _thread_relation()},
            timestamp=48,
        ),
        _event_source(
            "$thread-related-poll",
            "m.poll.response",
            {
                "m.poll.response": {"answers": ["a"]},
                "m.relates_to": _thread_relation(),
            },
            timestamp=49,
        ),
        _event_source(
            "$thread-related-beacon",
            "m.beacon",
            {"m.relates_to": _thread_relation()},
            timestamp=50,
        ),
        _event_source(
            "$thread-related-state",
            "m.room.topic",
            {"m.relates_to": _thread_relation(), "topic": "topic"},
            timestamp=51,
            state_key="thread-related",
        ),
        _event_source(
            "$thread-related-call",
            "m.call.invite",
            {"call_id": "call", "m.relates_to": _thread_relation(), "version": 1},
            timestamp=52,
        ),
        _event_source(
            "$thread-related-rtc",
            "org.matrix.msc4075.rtc.notification",
            {"m.relates_to": _thread_relation(), "type": "ring"},
            timestamp=53,
        ),
    ]


def _build_sync_harness(
    event_cache: ConversationEventCache,
    *,
    fetch_event_info_override: Callable[[str, str], Awaitable[EventInfo | None]] | None = None,
) -> _SyncHarness:
    logger = get_logger("tests.matrix_cache_interaction_contract")
    coordinator = EventCacheWriteCoordinator(logger=logger)
    runtime = cast(
        "BotRuntimeView",
        _CacheRuntime(
            event_cache=event_cache,
            event_cache_write_coordinator=coordinator,
        ),
    )

    async def fetch_event_info(room_id: str, event_id: str) -> EventInfo | None:
        event = await event_cache.get_event(room_id, event_id)
        return None if event is None else EventInfo.from_event(event)

    resolver = ThreadMutationResolver(
        logger_getter=lambda: logger,
        runtime=runtime,
        fetch_event_info_for_thread_resolution=fetch_event_info_override or fetch_event_info,
    )
    cache_ops = ThreadMutationCacheOps(
        logger_getter=lambda: logger,
        runtime=runtime,
    )
    return _SyncHarness(
        policy=ThreadSyncWritePolicy(
            resolver=resolver,
            cache_ops=cache_ops,
        ),
    )


async def _seed_thread(event_cache: ConversationEventCache) -> None:
    await replace_thread_unconditionally(
        event_cache,
        _ROOM_ID,
        _THREAD_ID,
        [
            _message_source(_THREAD_ID, "m.text", timestamp=1, body="root"),
            _message_source(
                _THREAD_CHILD_ID,
                "m.text",
                timestamp=2,
                body="child",
                relation=_thread_relation(),
            ),
        ],
        validated_at=10.0,
    )


async def _seed_other_thread(event_cache: ConversationEventCache) -> None:
    await replace_thread_unconditionally(
        event_cache,
        _ROOM_ID,
        _OTHER_THREAD_ID,
        [
            _message_source(_OTHER_THREAD_ID, "m.text", timestamp=3, body="other root"),
            _message_source(
                "$other-thread-child",
                "m.text",
                timestamp=4,
                body="other child",
                relation=_thread_relation(_OTHER_THREAD_ID),
            ),
        ],
        validated_at=10.0,
    )


@pytest.mark.asyncio
async def test_joined_timeline_room_level_interaction_matrix(
    event_cache: ConversationEventCache,
) -> None:
    """Room-level joined-timeline families are point-cached without changing thread state."""
    await _seed_thread(event_cache)
    before_events = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
    before_state = await event_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID)
    sources = _room_level_timeline_sources()

    await _build_sync_harness(event_cache).apply(
        _sync_response([raw_nio_event(source) for source in sources]),
    )

    for source in sources:
        event_id = cast("str", source["event_id"])
        assert await event_cache.get_event(_ROOM_ID, event_id) == source
        assert await event_cache.get_thread_id_for_event(_ROOM_ID, event_id) is None
    assert await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID) == before_events
    assert await event_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID) == before_state


@pytest.mark.asyncio
async def test_redaction_envelopes_are_omitted_from_every_point_write(
    event_cache: ConversationEventCache,
) -> None:
    """Direct and batched point writes must enforce the redaction-envelope exclusion."""
    direct_redaction = _event_source(
        "$direct-redaction-envelope",
        "m.room.redaction",
        {"redacts": "$target"},
        timestamp=49,
    )
    batch_redaction = _event_source(
        "$batch-redaction-envelope",
        "m.room.redaction",
        {"redacts": "$target"},
        timestamp=50,
    )
    retained_message = _message_source("$retained-batch-message", "m.text", timestamp=51)

    await event_cache.store_event("$direct-redaction-envelope", _ROOM_ID, direct_redaction)
    await event_cache.store_events_batch(
        [
            ("$batch-redaction-envelope", _ROOM_ID, batch_redaction),
            ("$retained-batch-message", _ROOM_ID, retained_message),
        ],
    )

    assert await event_cache.get_event(_ROOM_ID, "$direct-redaction-envelope") is None
    assert await event_cache.get_event(_ROOM_ID, "$batch-redaction-envelope") is None
    assert await event_cache.get_event(_ROOM_ID, "$retained-batch-message") == retained_message


@pytest.mark.parametrize("relation_kind", ["reply", "reference"])
@pytest.mark.asyncio
async def test_message_relations_through_non_message_ancestors_fail_closed(
    event_cache: ConversationEventCache,
    relation_kind: str,
) -> None:
    """A message cannot inherit visible thread membership through a non-message relation."""
    await _seed_thread(event_cache)
    sticker = _event_source(
        "$thread-shaped-sticker",
        "m.sticker",
        {
            "body": "sticker",
            "m.relates_to": _thread_relation(),
            "url": "mxc://localhost/sticker",
        },
        timestamp=52,
    )
    relation = (
        _reply_relation("$thread-shaped-sticker")
        if relation_kind == "reply"
        else {"event_id": "$thread-shaped-sticker", "rel_type": "m.reference"}
    )
    dependent_message = _message_source(
        f"${relation_kind}-through-sticker",
        "m.text",
        timestamp=53,
        relation=relation,
    )

    await _build_sync_harness(event_cache).apply(
        _sync_response([raw_nio_event(sticker), raw_nio_event(dependent_message)]),
    )

    assert await event_cache.get_event(_ROOM_ID, "$thread-shaped-sticker") == sticker
    assert await event_cache.get_event(_ROOM_ID, cast("str", dependent_message["event_id"])) == dependent_message
    assert await event_cache.get_thread_id_for_event(_ROOM_ID, "$thread-shaped-sticker") is None
    assert (
        await event_cache.get_thread_id_for_event(
            _ROOM_ID,
            cast("str", dependent_message["event_id"]),
        )
        is None
    )
    state = await event_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID)
    assert state is not None
    assert state.room_invalidation_reason == "sync_thread_lookup_unavailable"
    assert thread_cache_rejection_reason(state) == "room_invalidated_after_validation"


@pytest.mark.asyncio
async def test_explicit_thread_snapshot_does_not_index_non_message_events(
    event_cache: ConversationEventCache,
) -> None:
    """Authoritative snapshot writes must preserve the non-message index boundary."""
    root = _message_source("$snapshot-root", "m.text", timestamp=54)
    child = _message_source(
        "$snapshot-child",
        "m.text",
        timestamp=55,
        relation={"event_id": "$snapshot-root", "rel_type": "m.thread"},
    )
    sticker = _event_source(
        "$snapshot-sticker",
        "m.sticker",
        {
            "body": "sticker",
            "m.relates_to": {"event_id": "$snapshot-root", "rel_type": "m.thread"},
            "url": "mxc://localhost/sticker",
        },
        timestamp=56,
    )

    await replace_thread_unconditionally(
        event_cache,
        _ROOM_ID,
        "$snapshot-root",
        [root, child, sticker],
    )

    assert await event_cache.get_thread_id_for_event(_ROOM_ID, "$snapshot-root") == "$snapshot-root"
    assert await event_cache.get_thread_id_for_event(_ROOM_ID, "$snapshot-child") == "$snapshot-root"
    assert await event_cache.get_thread_id_for_event(_ROOM_ID, "$snapshot-sticker") is None


@pytest.mark.asyncio
async def test_page_local_non_message_child_cannot_prove_thread_root(
    event_cache: ConversationEventCache,
) -> None:
    """A relation-shaped sticker cannot make a same-page relation-free message a thread root."""
    root = _message_source("$page-root", "m.text", timestamp=57)
    sticker = _event_source(
        "$page-sticker",
        "m.sticker",
        {
            "body": "sticker",
            "m.relates_to": {"event_id": "$page-root", "rel_type": "m.thread"},
            "url": "mxc://localhost/sticker",
        },
        timestamp=58,
    )
    reply = _message_source(
        "$page-root-reply",
        "m.text",
        timestamp=59,
        relation=_reply_relation("$page-root"),
    )

    await _build_sync_harness(event_cache).apply(
        _sync_response([raw_nio_event(root), raw_nio_event(sticker), raw_nio_event(reply)]),
    )

    for event_id in ("$page-root", "$page-sticker", "$page-root-reply"):
        assert await event_cache.get_thread_id_for_event(_ROOM_ID, event_id) is None


@pytest.mark.asyncio
async def test_cached_non_message_child_cannot_prove_thread_root(
    event_cache: ConversationEventCache,
) -> None:
    """A cached sticker cannot provide thread-root proof when its stale root index is unavailable."""
    root = _message_source("$cached-proof-root", "m.text", timestamp=60)
    sticker = _event_source(
        "$cached-proof-sticker",
        "m.sticker",
        {
            "body": "sticker",
            "m.relates_to": {"event_id": "$cached-proof-root", "rel_type": "m.thread"},
            "url": "mxc://localhost/sticker",
        },
        timestamp=61,
    )
    await replace_thread_unconditionally(
        event_cache,
        _ROOM_ID,
        "$cached-proof-root",
        [root, sticker],
    )
    real_lookup = event_cache.get_thread_id_for_event

    async def lookup_without_root_index(room_id: str, event_id: str) -> str | None:
        if event_id == "$cached-proof-root":
            return None
        return await real_lookup(room_id, event_id)

    with patch.object(
        event_cache,
        "get_thread_id_for_event",
        side_effect=lookup_without_root_index,
    ):
        await _build_sync_harness(event_cache).apply(
            _sync_response(
                [
                    raw_nio_event(
                        _message_source(
                            "$cached-proof-reply",
                            "m.text",
                            timestamp=62,
                            relation=_reply_relation("$cached-proof-root"),
                        ),
                    ),
                ],
            ),
        )

    assert await event_cache.get_thread_id_for_event(_ROOM_ID, "$cached-proof-reply") is None


@pytest.mark.parametrize("relation_kind", ["reply", "reference"])
@pytest.mark.asyncio
async def test_persisted_non_message_index_is_not_trusted_by_relation_walks(
    event_cache: ConversationEventCache,
    relation_kind: str,
) -> None:
    """Legacy index rows cannot let message relations inherit from non-message events."""
    await _seed_thread(event_cache)
    sticker = _event_source(
        "$legacy-index-sticker",
        "m.sticker",
        {
            "body": "sticker",
            "m.relates_to": _thread_relation(),
            "url": "mxc://localhost/sticker",
        },
        timestamp=63,
    )
    await event_cache.store_event("$legacy-index-sticker", _ROOM_ID, sticker)
    relation = (
        _reply_relation("$legacy-index-sticker")
        if relation_kind == "reply"
        else {"event_id": "$legacy-index-sticker", "rel_type": "m.reference"}
    )
    dependent = _message_source(
        f"$legacy-{relation_kind}-dependent",
        "m.text",
        timestamp=64,
        relation=relation,
    )
    real_lookup = event_cache.get_thread_id_for_event

    async def lookup_with_legacy_row(room_id: str, event_id: str) -> str | None:
        if event_id == "$legacy-index-sticker":
            return _THREAD_ID
        return await real_lookup(room_id, event_id)

    with patch.object(
        event_cache,
        "get_thread_id_for_event",
        side_effect=lookup_with_legacy_row,
    ):
        await _build_sync_harness(event_cache).apply(
            _sync_response([raw_nio_event(dependent)]),
        )

    assert await event_cache.get_thread_id_for_event(_ROOM_ID, cast("str", dependent["event_id"])) is None
    state = await event_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID)
    assert state is not None
    assert state.room_invalidation_reason == "sync_thread_lookup_unavailable"


@pytest.mark.asyncio
async def test_joined_timeline_thread_relations_indexes_edits_and_visible_history(
    event_cache: ConversationEventCache,
) -> None:
    """Thread, reply, edit, reference, and reaction families keep their distinct effects."""
    await _seed_thread(event_cache)
    explicit_child = _message_source(
        "$explicit-child",
        "m.text",
        timestamp=50,
        body="explicit child",
        relation=_thread_relation(),
    )
    plain_reply = _message_source(
        "$plain-reply",
        "m.text",
        timestamp=51,
        body="plain reply",
        relation=_reply_relation("$explicit-child"),
    )
    root_edit = _edit_source("$root-edit", _THREAD_ID, body="edited root", timestamp=52)
    child_edit = _edit_source(
        "$child-edit",
        "$explicit-child",
        body="edited child",
        timestamp=53,
        thread_id=_THREAD_ID,
    )
    reply_edit = _edit_source("$reply-edit", "$plain-reply", body="edited reply", timestamp=54)
    reference = _message_source(
        "$reference",
        "m.text",
        timestamp=55,
        body="reference",
        relation={"event_id": "$explicit-child", "rel_type": "m.reference"},
    )
    reaction = _event_source(
        "$reaction",
        "m.reaction",
        {"m.relates_to": {"event_id": "$explicit-child", "key": "👍", "rel_type": "m.annotation"}},
        timestamp=56,
    )
    threaded_sources = [
        explicit_child,
        plain_reply,
        root_edit,
        child_edit,
        reply_edit,
        reference,
        reaction,
    ]

    await _build_sync_harness(event_cache).apply(
        _sync_response([raw_nio_event(source) for source in threaded_sources]),
    )

    for source in threaded_sources:
        event_id = cast("str", source["event_id"])
        assert await event_cache.get_event(_ROOM_ID, event_id) == source
    for event_id in {
        _THREAD_ID,
        "$explicit-child",
        "$plain-reply",
        "$root-edit",
        "$child-edit",
        "$reply-edit",
        "$reference",
    }:
        assert await event_cache.get_thread_id_for_event(_ROOM_ID, event_id) == _THREAD_ID
    assert await event_cache.get_thread_id_for_event(_ROOM_ID, "$reaction") is None
    assert await event_cache.get_latest_edit(_ROOM_ID, _THREAD_ID) == root_edit
    assert await event_cache.get_latest_edit(_ROOM_ID, "$explicit-child") == child_edit
    assert await event_cache.get_latest_edit(_ROOM_ID, "$plain-reply") == reply_edit

    cached_sources = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
    assert cached_sources is not None
    assert {source["event_id"] for source in cached_sources} == {
        _THREAD_ID,
        _THREAD_CHILD_ID,
        "$explicit-child",
        "$plain-reply",
        "$root-edit",
        "$child-edit",
        "$reply-edit",
        "$reference",
    }
    state = await event_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID)
    assert state is not None
    assert state.validated_at is not None
    assert state.validated_at > 10.0
    assert thread_cache_rejection_reason(state) is None

    client = cast("nio.AsyncClient", object())
    history = await fetch_dispatch_thread_snapshot(
        client,
        _ROOM_ID,
        _THREAD_ID,
        event_cache,
    )
    assert [(message.event_id, message.latest_event_id, message.body) for message in history] == [
        (_THREAD_ID, "$root-edit", "edited root"),
        (_THREAD_CHILD_ID, _THREAD_CHILD_ID, "child"),
        ("$explicit-child", "$child-edit", "edited child"),
        ("$plain-reply", "$reply-edit", "edited reply"),
        ("$reference", "$reference", "reference"),
    ]


@pytest.mark.asyncio
async def test_encrypted_relation_bearing_events_are_point_cached_and_fail_the_thread_closed(
    event_cache: ConversationEventCache,
) -> None:
    """Opaque encrypted relations keep point evidence while the affected thread stays stale."""
    await _seed_thread(event_cache)
    seeded_thread_events = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
    encrypted_sources = [
        _event_source(
            "$encrypted-thread",
            "m.room.encrypted",
            {
                "algorithm": "m.megolm.v1.aes-sha2",
                "ciphertext": "opaque",
                "device_id": "DEVICE",
                "m.relates_to": _thread_relation(),
                "sender_key": "sender-key",
                "session_id": "session",
            },
            timestamp=60,
        ),
        _event_source(
            "$encrypted-reply",
            "m.room.encrypted",
            {
                "algorithm": "m.megolm.v1.aes-sha2",
                "ciphertext": "opaque",
                "device_id": "DEVICE",
                "m.relates_to": _reply_relation(_THREAD_CHILD_ID),
                "sender_key": "sender-key",
                "session_id": "session",
            },
            timestamp=61,
        ),
        _event_source(
            "$encrypted-edit",
            "m.room.encrypted",
            {
                "algorithm": "m.megolm.v1.aes-sha2",
                "ciphertext": "opaque",
                "device_id": "DEVICE",
                "m.relates_to": {"event_id": _THREAD_CHILD_ID, "rel_type": "m.replace"},
                "sender_key": "sender-key",
                "session_id": "session",
            },
            timestamp=62,
        ),
        _event_source(
            "$encrypted-reference",
            "m.room.encrypted",
            {
                "algorithm": "m.megolm.v1.aes-sha2",
                "ciphertext": "opaque",
                "device_id": "DEVICE",
                "m.relates_to": {"event_id": _THREAD_CHILD_ID, "rel_type": "m.reference"},
                "sender_key": "sender-key",
                "session_id": "session",
            },
            timestamp=63,
        ),
        _event_source(
            "$encrypted-reaction",
            "m.room.encrypted",
            {
                "algorithm": "m.megolm.v1.aes-sha2",
                "ciphertext": "opaque",
                "device_id": "DEVICE",
                "m.relates_to": {
                    "event_id": _THREAD_CHILD_ID,
                    "key": "👍",
                    "rel_type": "m.annotation",
                },
                "sender_key": "sender-key",
                "session_id": "session",
            },
            timestamp=64,
        ),
    ]

    await _build_sync_harness(event_cache).apply(
        _sync_response([raw_nio_event(source) for source in encrypted_sources]),
    )

    for source in encrypted_sources:
        event_id = cast("str", source["event_id"])
        assert await event_cache.get_event(_ROOM_ID, event_id) == source
    assert await event_cache.get_thread_id_for_event(_ROOM_ID, "$encrypted-thread") == _THREAD_ID
    for event_id in ("$encrypted-reply", "$encrypted-edit", "$encrypted-reference", "$encrypted-reaction"):
        assert await event_cache.get_thread_id_for_event(_ROOM_ID, event_id) is None
    assert await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID) == seeded_thread_events
    state = await event_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID)
    assert state is not None
    assert state.invalidation_reason == "sync_opaque_encrypted_event"
    assert state.room_invalidated_at is None
    assert thread_cache_rejection_reason(state) == "thread_invalidated_after_validation"


def _encrypted_source(
    event_id: str,
    *,
    timestamp: int,
    relation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    content: dict[str, Any] = {
        "algorithm": "m.megolm.v1.aes-sha2",
        "ciphertext": "opaque",
        "device_id": "DEVICE",
        "sender_key": "sender-key",
        "session_id": "session",
    }
    if relation is not None:
        content["m.relates_to"] = relation
    return _event_source(event_id, "m.room.encrypted", content, timestamp=timestamp)


@pytest.mark.asyncio
async def test_opaque_encrypted_thread_child_leaves_only_its_thread_stale(
    event_cache: ConversationEventCache,
) -> None:
    """A known-impact opaque encrypted child must not invalidate unrelated thread snapshots."""
    await _seed_thread(event_cache)
    await _seed_other_thread(event_cache)
    opaque_child = _encrypted_source("$opaque-child", timestamp=60, relation=_thread_relation())

    await _build_sync_harness(event_cache).apply(_sync_response([raw_nio_event(opaque_child)]))

    assert await event_cache.get_event(_ROOM_ID, "$opaque-child") == opaque_child
    affected_state = await event_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID)
    assert affected_state is not None
    assert affected_state.invalidation_reason == "sync_opaque_encrypted_event"
    assert thread_cache_rejection_reason(affected_state) == "thread_invalidated_after_validation"
    other_state = await event_cache.get_thread_cache_state(_ROOM_ID, _OTHER_THREAD_ID)
    assert other_state is not None
    assert thread_cache_rejection_reason(other_state) is None


@pytest.mark.asyncio
async def test_relationless_ciphertext_and_opaque_reaction_do_not_poison_thread_snapshots(
    event_cache: ConversationEventCache,
) -> None:
    """Ciphertext without thread-affecting relations must leave every thread snapshot trusted."""
    await _seed_thread(event_cache)
    before_state = await event_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID)
    relationless = _encrypted_source("$opaque-relationless", timestamp=60)
    reaction = _encrypted_source(
        "$opaque-reaction",
        timestamp=61,
        relation={"event_id": _THREAD_CHILD_ID, "key": "👍", "rel_type": "m.annotation"},
    )

    await _build_sync_harness(event_cache).apply(
        _sync_response([raw_nio_event(relationless), raw_nio_event(reaction)]),
    )

    assert await event_cache.get_event(_ROOM_ID, "$opaque-relationless") == relationless
    assert await event_cache.get_event(_ROOM_ID, "$opaque-reaction") == reaction
    assert await event_cache.get_thread_id_for_event(_ROOM_ID, "$opaque-relationless") is None
    assert await event_cache.get_thread_id_for_event(_ROOM_ID, "$opaque-reaction") is None
    state = await event_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID)
    assert state == before_state
    assert thread_cache_rejection_reason(state) is None


@pytest.mark.asyncio
async def test_later_clear_incremental_event_cannot_weaken_opaque_invalidation(
    event_cache: ConversationEventCache,
) -> None:
    """A plaintext append after opaque evidence must not revalidate the stale thread."""
    await _seed_thread(event_cache)
    harness = _build_sync_harness(event_cache)
    opaque_child = _encrypted_source("$opaque-child", timestamp=60, relation=_thread_relation())
    await harness.apply(_sync_response([raw_nio_event(opaque_child)]))

    clear_child = _message_source("$clear-child", "m.text", timestamp=61, relation=_thread_relation())
    await harness.apply(_sync_response([raw_nio_event(clear_child)]))

    cached_event_ids = {
        source["event_id"] for source in (await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID) or [])
    }
    assert "$clear-child" in cached_event_ids
    assert "$opaque-child" not in cached_event_ids
    state = await event_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID)
    assert state is not None
    assert state.invalidation_reason == "sync_opaque_encrypted_event"
    assert thread_cache_rejection_reason(state) == "thread_invalidated_after_validation"


@pytest.mark.asyncio
async def test_unknown_impact_opaque_encrypted_event_fails_closed_at_room_scope(
    event_cache: ConversationEventCache,
) -> None:
    """An opaque relation that cannot be resolved must invalidate the room's cached threads."""
    await _seed_thread(event_cache)
    await _seed_other_thread(event_cache)
    opaque_reply = _encrypted_source(
        "$opaque-unknown-reply",
        timestamp=60,
        relation=_reply_relation("$never-seen-event"),
    )

    await _build_sync_harness(event_cache).apply(_sync_response([raw_nio_event(opaque_reply)]))

    assert await event_cache.get_event(_ROOM_ID, "$opaque-unknown-reply") == opaque_reply
    for thread_id in (_THREAD_ID, _OTHER_THREAD_ID):
        state = await event_cache.get_thread_cache_state(_ROOM_ID, thread_id)
        assert state is not None
        assert state.room_invalidation_reason == "sync_thread_lookup_unavailable"
        assert thread_cache_rejection_reason(state) == "room_invalidated_after_validation"


@pytest.mark.asyncio
async def test_mark_thread_stale_upgrades_incremental_reason_to_full_refetch_reason(
    event_cache: ConversationEventCache,
) -> None:
    """A newer full-refetch marker must replace an incremental reason on both backends."""
    await _seed_thread(event_cache)
    await event_cache.mark_thread_stale(_ROOM_ID, _THREAD_ID, reason="sync_thread_mutation")
    await event_cache.mark_thread_stale(_ROOM_ID, _THREAD_ID, reason="sync_opaque_encrypted_event")

    state = await event_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID)
    assert state is not None
    assert state.invalidation_reason == "sync_opaque_encrypted_event"

    await event_cache.mark_thread_stale(_ROOM_ID, _THREAD_ID, reason="sync_thread_mutation")
    downgraded_state = await event_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID)
    assert downgraded_state is not None
    assert downgraded_state.invalidation_reason == "sync_opaque_encrypted_event"
    assert downgraded_state.invalidated_at is not None
    assert state.invalidated_at is not None
    assert downgraded_state.invalidated_at >= state.invalidated_at
    assert await event_cache.revalidate_thread_after_incremental_update(_ROOM_ID, _THREAD_ID) is False


@pytest.mark.asyncio
async def test_sync_opaque_stale_marker_failure_deletes_snapshot_instead_of_serving_it(
    event_cache: ConversationEventCache,
) -> None:
    """A failed opaque stale marker must remove the cached snapshot rather than leave it trusted."""
    await _seed_thread(event_cache)
    opaque_child = _encrypted_source("$opaque-child", timestamp=60, relation=_thread_relation())

    with patch.object(
        event_cache,
        "mark_thread_stale",
        AsyncMock(side_effect=RuntimeError("stale marker write refused")),
    ):
        result = await _build_sync_harness(event_cache).policy.cache_sync_timeline_for_certification(
            cast("nio.SyncResponse", _sync_response([raw_nio_event(opaque_child)])),
        )

    assert result.complete is False
    assert result.errors
    assert await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID) is None
    state = await event_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID)
    assert thread_cache_rejection_reason(state) is not None


@pytest.mark.asyncio
async def test_visible_thread_history_message_and_non_message_family_boundary(
    event_cache: ConversationEventCache,
) -> None:
    """All message msgtypes render, while non-message interaction families remain raw-only."""
    visible_sources = [
        _message_source(_THREAD_ID, "m.text", timestamp=70, body="root"),
        *[
            _message_source(
                f"$visible-{msgtype.removeprefix('m.')}",
                msgtype,
                timestamp=71 + index,
                relation=_thread_relation(),
                extra_content=extra_content,
            )
            for index, (msgtype, extra_content) in enumerate(
                (
                    ("m.notice", {}),
                    ("m.emote", {}),
                    ("m.location", {"geo_uri": "geo:51.5,-0.1"}),
                    ("m.file", {"url": "mxc://localhost/file"}),
                    ("m.image", {"url": "mxc://localhost/image"}),
                    (
                        "m.audio",
                        {
                            "org.matrix.msc1767.audio": {"duration": 20, "waveform": [0]},
                            "org.matrix.msc3245.voice": {},
                            "url": "mxc://localhost/audio",
                        },
                    ),
                    ("m.video", {"url": "mxc://localhost/video"}),
                ),
            )
        ],
    ]
    non_message_sources = [
        _event_source(
            "$raw-sticker",
            "m.sticker",
            {"body": "sticker", "m.relates_to": _thread_relation(), "url": "mxc://localhost/sticker"},
            timestamp=90,
        ),
        _event_source(
            "$raw-poll",
            "m.poll.start",
            {"m.poll.start": {}, "m.relates_to": _thread_relation()},
            timestamp=91,
        ),
        _event_source(
            "$raw-beacon",
            "m.beacon",
            {"m.relates_to": _thread_relation()},
            timestamp=92,
        ),
        _event_source(
            "$raw-state",
            "m.room.topic",
            {"m.relates_to": _thread_relation(), "topic": "topic"},
            timestamp=93,
            state_key="",
        ),
        _event_source(
            "$raw-call",
            "m.call.invite",
            {
                "call_id": "call",
                "lifetime": 60000,
                "m.relates_to": _thread_relation(),
                "offer": {"sdp": "", "type": "offer"},
                "version": 1,
            },
            timestamp=94,
        ),
        _event_source(
            "$raw-rtc",
            "org.matrix.msc3401.call.member",
            {"m.relates_to": _thread_relation()},
            timestamp=95,
            state_key="@user:localhost_DEVICE",
        ),
    ]
    await replace_thread_unconditionally(
        event_cache,
        _ROOM_ID,
        _THREAD_ID,
        [*visible_sources, *non_message_sources],
    )

    history = await fetch_dispatch_thread_snapshot(
        cast("nio.AsyncClient", object()),
        _ROOM_ID,
        _THREAD_ID,
        event_cache,
    )

    assert [message.event_id for message in history] == [source["event_id"] for source in visible_sources]
    assert history[6].to_dict()["msgtype"] == "m.audio"
    assert history[6].to_dict()["content"]["org.matrix.msc3245.voice"] == {}


def _excluded_sync_response(excluded_events: dict[str, nio.Event]) -> _SyncEnvelope:
    joined_room = _RoomSync(
        timeline=_Timeline(events=[excluded_events["joined_timeline"]]),
        state=_EventSection(events=[excluded_events["joined_state"]]),
        ephemeral=_EventSection(
            events=[
                excluded_events["typing"],
                excluded_events["receipt"],
            ],
        ),
        account_data=_EventSection(events=[excluded_events["room_account_data"]]),
    )
    return _SyncEnvelope(
        rooms=_SyncRooms(
            join={_ROOM_ID: joined_room},
            invite={
                "!invite:localhost": _RoomSync(
                    timeline=_Timeline(events=[excluded_events["invite_timeline"]]),
                ),
            },
            leave={
                "!leave:localhost": _RoomSync(
                    timeline=_Timeline(events=[excluded_events["leave_timeline"]]),
                ),
            },
        ),
        presence=_EventSection(events=[excluded_events["presence"]]),
        account_data=_EventSection(events=[excluded_events["global_account_data"]]),
        to_device=_EventSection(events=[excluded_events["to_device"]]),
        device_lists=_DeviceLists(
            changed=["@changed:localhost"],
            left=["@left:localhost"],
        ),
    )


@pytest.mark.asyncio
async def test_sync_categories_outside_joined_timeline_are_deliberately_excluded(
    event_cache: ConversationEventCache,
) -> None:
    """Timeline caching excludes sync families owned by other lifecycle collaborators."""
    await _seed_thread(event_cache)
    leave_room_id = "!leave:localhost"
    departed_room_event = _event_source(
        "$departed-room-event",
        "m.room.message",
        {"body": "departed", "msgtype": "m.text"},
        timestamp=100,
    )
    await event_cache.store_event(
        "$departed-room-event",
        leave_room_id,
        departed_room_event,
    )
    before_events = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
    before_state = await event_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID)

    excluded_sources = {
        "joined_timeline": _message_source("$joined-timeline", "m.text", timestamp=101),
        "joined_state": _event_source(
            "$joined-complete-state",
            "m.room.topic",
            {"topic": "complete state"},
            timestamp=102,
            state_key="",
        ),
        "invite_timeline": _message_source(
            "$invite-timeline",
            "m.text",
            timestamp=103,
            relation=_thread_relation("$invite-root"),
        ),
        "leave_timeline": _edit_source(
            "$leave-timeline",
            "$leave-original",
            body="leave edit",
            timestamp=104,
            thread_id="$leave-root",
        ),
        "typing": _event_source("$typing", "m.typing", {"user_ids": [_SENDER]}, timestamp=105),
        "receipt": _event_source("$receipt", "m.receipt", {}, timestamp=106),
        "presence": _event_source("$presence", "m.presence", {"presence": "online"}, timestamp=107),
        "room_account_data": _event_source("$room-account-data", "m.tag", {"tags": {}}, timestamp=108),
        "global_account_data": _event_source(
            "$global-account-data",
            "m.direct",
            {},
            timestamp=109,
        ),
        "to_device": _event_source(
            "$to-device",
            "m.room.encrypted",
            {"algorithm": "m.olm.v1.curve25519-aes-sha2", "ciphertext": {}},
            timestamp=110,
        ),
    }
    excluded_events = {name: raw_nio_event(source) for name, source in excluded_sources.items()}

    await _build_sync_harness(event_cache).apply(
        _excluded_sync_response(excluded_events),
    )

    assert await event_cache.get_event(_ROOM_ID, "$joined-timeline") == excluded_sources["joined_timeline"]
    excluded_room_ids = {
        "invite_timeline": "!invite:localhost",
        "leave_timeline": leave_room_id,
    }
    for category, source in excluded_sources.items():
        if category == "joined_timeline":
            continue
        event_id = cast("str", source["event_id"])
        room_id = excluded_room_ids.get(category, _ROOM_ID)
        assert await event_cache.get_event(room_id, event_id) is None
        assert await event_cache.get_thread_id_for_event(room_id, event_id) is None
    assert await event_cache.get_thread_events("!invite:localhost", "$invite-root") is None
    assert await event_cache.get_thread_cache_state("!invite:localhost", "$invite-root") is None
    assert await event_cache.get_thread_events(leave_room_id, "$leave-root") is None
    assert await event_cache.get_thread_cache_state(leave_room_id, "$leave-root") is None
    assert await event_cache.get_latest_edit(leave_room_id, "$leave-original") is None
    assert await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID) == before_events
    assert await event_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID) == before_state
    assert event_cache.room_departure_epoch(leave_room_id) == 0
    assert await event_cache.get_event(leave_room_id, "$departed-room-event") == departed_room_event


@pytest.mark.asyncio
async def test_reaction_redaction_is_point_only_and_tombstoned(
    event_cache: ConversationEventCache,
) -> None:
    """Reaction redaction removes the annotation without touching the target thread."""
    await _seed_thread(event_cache)
    harness = _build_sync_harness(event_cache)
    reaction = _event_source(
        "$reaction-to-redact",
        "m.reaction",
        {"m.relates_to": {"event_id": _THREAD_CHILD_ID, "key": "👍", "rel_type": "m.annotation"}},
        timestamp=120,
    )
    await harness.apply(_sync_response([raw_nio_event(reaction)]))
    before_events = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
    before_state = await event_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID)
    redaction_source = _event_source(
        "$reaction-redaction",
        "m.room.redaction",
        {"reason": "contract"},
        timestamp=121,
    )

    await harness.apply(
        _sync_response(
            [
                raw_nio_redaction(
                    redaction_source,
                    redacts="$reaction-to-redact",
                ),
            ],
        ),
    )

    assert await event_cache.get_event(_ROOM_ID, "$reaction-to-redact") is None
    assert await event_cache.get_event(_ROOM_ID, "$reaction-redaction") is None
    assert await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID) == before_events
    assert await event_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID) == before_state
    await event_cache.store_event("$reaction-to-redact", _ROOM_ID, reaction)
    assert await event_cache.get_event(_ROOM_ID, "$reaction-to-redact") is None


@pytest.mark.asyncio
async def test_non_message_reference_redactions_are_point_only_and_tombstoned(
    event_cache: ConversationEventCache,
) -> None:
    """Poll and beacon redactions cannot invalidate visible thread history."""
    await _seed_thread(event_cache)
    harness = _build_sync_harness(event_cache)
    targets = [
        _event_source(
            "$poll-response-to-redact",
            "m.poll.response",
            {
                "m.poll.response": {"answers": ["a"]},
                "m.relates_to": {"event_id": _THREAD_CHILD_ID, "rel_type": "m.reference"},
            },
            timestamp=122,
        ),
        _event_source(
            "$poll-end-to-redact",
            "m.poll.end",
            {
                "m.poll.end": {"m.text": "Closed"},
                "m.relates_to": {"event_id": _THREAD_CHILD_ID, "rel_type": "m.reference"},
            },
            timestamp=123,
        ),
        _event_source(
            "$beacon-to-redact",
            "m.beacon",
            {
                "m.relates_to": {"event_id": _THREAD_CHILD_ID, "rel_type": "m.reference"},
                "org.matrix.msc3488.location": {"uri": "geo:51.5,-0.1"},
            },
            timestamp=124,
        ),
    ]
    await harness.apply(_sync_response([raw_nio_event(target) for target in targets]))
    before_events = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
    before_state = await event_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID)

    await harness.apply(
        _sync_response(
            [
                raw_nio_redaction(
                    _event_source(
                        f"$redaction-{target['event_id']}",
                        "m.room.redaction",
                        {"reason": "contract"},
                        timestamp=125 + index,
                    ),
                    redacts=cast("str", target["event_id"]),
                )
                for index, target in enumerate(targets)
            ],
        ),
    )

    for target in targets:
        event_id = cast("str", target["event_id"])
        assert await event_cache.get_event(_ROOM_ID, event_id) is None
        assert await event_cache.get_thread_id_for_event(_ROOM_ID, event_id) is None
        await event_cache.store_event(event_id, _ROOM_ID, target)
        assert await event_cache.get_event(_ROOM_ID, event_id) is None
    assert await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID) == before_events
    assert await event_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID) == before_state


@pytest.mark.asyncio
async def test_unknown_redaction_without_cached_target_is_a_thread_state_noop(
    event_cache: ConversationEventCache,
) -> None:
    """A metadata-less redaction cannot invalidate threads when no cached target was removed."""
    await _seed_thread(event_cache)
    before_state = await event_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID)

    await _build_sync_harness(event_cache).apply(
        _sync_response(
            [
                raw_nio_redaction(
                    _event_source(
                        "$unknown-target-redaction",
                        "m.room.redaction",
                        {"reason": "target absent"},
                        timestamp=129,
                    ),
                    redacts="$unknown-target",
                ),
            ],
        ),
    )

    assert await event_cache.get_event(_ROOM_ID, "$unknown-target") is None
    assert await event_cache.get_thread_id_for_event(_ROOM_ID, "$unknown-target") is None
    assert await event_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID) == before_state


@pytest.mark.asyncio
async def test_unknown_redaction_of_cached_target_fails_closed_room_wide(
    event_cache: ConversationEventCache,
) -> None:
    """Removing a cached target with unavailable metadata invalidates every room thread."""
    await _seed_thread(event_cache)
    await _seed_other_thread(event_cache)
    target = _event_source(
        "$metadata-unavailable-target",
        "m.poll.response",
        {"m.poll.response": {"answers": ["a"]}},
        timestamp=130,
    )
    await event_cache.store_event("$metadata-unavailable-target", _ROOM_ID, target)

    async def fail_metadata_lookup(_room_id: str, _event_id: str) -> EventInfo | None:
        msg = "metadata unavailable"
        raise RuntimeError(msg)

    await _build_sync_harness(
        event_cache,
        fetch_event_info_override=fail_metadata_lookup,
    ).apply(
        _sync_response(
            [
                raw_nio_redaction(
                    _event_source(
                        "$metadata-unavailable-redaction",
                        "m.room.redaction",
                        {"reason": "lookup failure"},
                        timestamp=131,
                    ),
                    redacts="$metadata-unavailable-target",
                ),
            ],
        ),
    )

    assert await event_cache.get_event(_ROOM_ID, "$metadata-unavailable-target") is None
    for thread_id in (_THREAD_ID, _OTHER_THREAD_ID):
        state = await event_cache.get_thread_cache_state(_ROOM_ID, thread_id)
        assert state is not None
        assert state.room_invalidation_reason == "sync_redaction_lookup_unavailable"
        assert thread_cache_rejection_reason(state) == "room_invalidated_after_validation"


@pytest.mark.asyncio
async def test_advisory_stale_fallback_is_labeled_and_dispatch_rejects_it(
    event_cache: ConversationEventCache,
) -> None:
    """Only advisory reads may return stale rows after a failed homeserver refill."""
    await _seed_thread(event_cache)
    await event_cache.mark_thread_stale(
        _ROOM_ID,
        _THREAD_ID,
        reason="contract_fallback",
    )
    fetch_error = RuntimeError("deterministic homeserver failure")
    client = cast("nio.AsyncClient", object())

    with patch(
        "mindroom.matrix.client_thread_history._fetch_thread_history_with_events",
        AsyncMock(side_effect=fetch_error),
    ) as fetch_from_homeserver:
        advisory_history = await fetch_thread_history(
            client,
            _ROOM_ID,
            _THREAD_ID,
            event_cache,
        )
        with pytest.raises(RuntimeError, match="deterministic homeserver failure"):
            await fetch_dispatch_thread_snapshot(
                client,
                _ROOM_ID,
                _THREAD_ID,
                event_cache,
            )

    assert [message.event_id for message in advisory_history] == [_THREAD_ID, _THREAD_CHILD_ID]
    assert advisory_history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_STALE_CACHE
    assert advisory_history.diagnostics[THREAD_HISTORY_DEGRADED_DIAGNOSTIC] is True
    assert advisory_history.diagnostics[THREAD_HISTORY_ERROR_DIAGNOSTIC] == str(fetch_error)
    assert (
        advisory_history.diagnostics[THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC]
        == "thread_invalidated_after_validation"
    )
    assert fetch_from_homeserver.await_count == 2


@pytest.mark.parametrize(
    ("case", "redacted_event_id"),
    [
        pytest.param("message", _THREAD_CHILD_ID, id="message"),
        pytest.param("original_with_dependent_edit", _THREAD_CHILD_ID, id="original-and-edit"),
        pytest.param("edit_only", "$child-edit-to-redact", id="edit-only"),
    ],
)
@pytest.mark.asyncio
async def test_message_and_edit_redaction_contract(
    event_cache: ConversationEventCache,
    case: str,
    redacted_event_id: str,
) -> None:
    """Message, original-plus-edit, and edit-only redactions remove exactly their visible rows."""
    await _seed_thread(event_cache)
    child_edit = _edit_source(
        "$child-edit-to-redact",
        _THREAD_CHILD_ID,
        body="edited child",
        timestamp=130,
        thread_id=_THREAD_ID,
    )
    if case != "message":
        await _build_sync_harness(event_cache).apply(
            _sync_response([raw_nio_event(child_edit)]),
        )
    redaction_source = _event_source(
        f"$redaction-{case}",
        "m.room.redaction",
        {"reason": case},
        timestamp=131,
    )

    await _build_sync_harness(event_cache).apply(
        _sync_response(
            [
                raw_nio_redaction(
                    redaction_source,
                    redacts=redacted_event_id,
                ),
            ],
        ),
    )

    assert await event_cache.get_event(_ROOM_ID, redacted_event_id) is None
    assert await event_cache.get_event(_ROOM_ID, cast("str", redaction_source["event_id"])) is None
    assert await event_cache.get_thread_id_for_event(_ROOM_ID, redacted_event_id) is None
    state = await event_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID)
    assert state is not None
    assert state.invalidation_reason == "sync_redaction"
    if case == "edit_only":
        assert await event_cache.get_event(_ROOM_ID, _THREAD_CHILD_ID) is not None
        assert await event_cache.get_thread_id_for_event(_ROOM_ID, _THREAD_CHILD_ID) == _THREAD_ID
    else:
        assert await event_cache.get_event(_ROOM_ID, _THREAD_CHILD_ID) is None
        assert await event_cache.get_thread_id_for_event(_ROOM_ID, _THREAD_CHILD_ID) is None
    if case == "original_with_dependent_edit":
        assert await event_cache.get_event(_ROOM_ID, "$child-edit-to-redact") is None
        assert await event_cache.get_thread_id_for_event(_ROOM_ID, "$child-edit-to-redact") is None
        await event_cache.store_event(
            "$child-edit-to-redact",
            _ROOM_ID,
            child_edit,
        )
        assert await event_cache.get_event(_ROOM_ID, "$child-edit-to-redact") is None
    assert await event_cache.get_latest_edit(_ROOM_ID, _THREAD_CHILD_ID) is None
