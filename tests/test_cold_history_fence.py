"""Per-event Matrix provenance admission for timeline callbacks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import nio
import pytest

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.cold_history_fence import ColdHistoryFence
from mindroom.dispatch_admission import DispatchSourceAdmission
from mindroom.dispatch_obligations import (
    DispatchCallbackKind,
    DispatchObligationRunner,
    DispatchObligationStore,
)
from mindroom.dispatch_obligations.events import DispatchCallbackResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable
    from pathlib import Path


@dataclass
class _PendingObligations:
    pending: set[tuple[str, DispatchCallbackKind]] = field(default_factory=set)
    reads: list[tuple[str, DispatchCallbackKind]] = field(default_factory=list)

    @classmethod
    def with_keys(
        cls,
        keys: Iterable[tuple[str, DispatchCallbackKind]],
    ) -> _PendingObligations:
        return cls(pending=set(keys))

    def has_pending(
        self,
        source_event_id: str,
        callback_kind: DispatchCallbackKind,
    ) -> bool:
        self.reads.append((source_event_id, callback_kind))
        return (source_event_id, callback_kind) in self.pending


def _message(event_id: str) -> nio.RoomMessageText:
    event = nio.RoomMessageText.from_dict(
        {
            "type": "m.room.message",
            "event_id": event_id,
            "sender": "@user:example.org",
            "origin_server_ts": 1,
            "content": {"msgtype": "m.text", "body": "hello"},
        },
    )
    assert isinstance(event, nio.RoomMessageText)
    return event


def _sync_response(
    next_batch: str,
    event: nio.RoomMessageText,
) -> nio.SyncResponse:
    response = nio.SyncResponse.from_dict(
        {
            "next_batch": next_batch,
            "device_one_time_keys_count": {},
            "device_lists": {"changed": [], "left": []},
            "rooms": {
                "invite": {},
                "leave": {},
                "join": {
                    "!room:example.org": {
                        "timeline": {
                            "events": [event.source],
                            "limited": False,
                            "prev_batch": "p0",
                        },
                        "state": {"events": []},
                        "ephemeral": {"events": []},
                        "account_data": {"events": []},
                    },
                },
            },
            "to_device": {"events": []},
            "presence": {"events": []},
            "account_data": {"events": []},
        },
    )
    assert isinstance(response, nio.SyncResponse)
    return response


def _runner(
    store: DispatchObligationStore,
    fence: ColdHistoryFence,
    callback: Callable[
        [nio.MatrixRoom, nio.Event],
        Awaitable[DispatchCallbackResult],
    ],
) -> DispatchObligationRunner:
    return DispatchObligationRunner(
        store=store,
        callbacks={DispatchCallbackKind.MESSAGE: callback},
        room_for_id=lambda room_id: nio.MatrixRoom(room_id, "@code:example.org"),
        turn_is_terminal=lambda _event_id: False,
        source_admission=fence.admit_source,
        observe_event_provenance=fence.observe_event_provenance,
    )


@pytest.mark.asyncio
async def test_history_requires_one_exact_pending_obligation() -> None:
    """Historical delivery may retry only its exact durable callback."""
    obligations = _PendingObligations.with_keys(
        {("$pending", DispatchCallbackKind.REACTION)},
    )
    fence = ColdHistoryFence(obligations)

    assert (
        await fence.admit_source(
            "!room:example.org",
            "$pending",
            DispatchCallbackKind.REACTION,
            nio.TimelineEventProvenance.HISTORY,
        )
        is DispatchSourceAdmission.ACCEPTED
    )
    assert (
        await fence.admit_source(
            "!room:example.org",
            "$pending",
            DispatchCallbackKind.MESSAGE,
            nio.TimelineEventProvenance.HISTORY,
        )
        is DispatchSourceAdmission.COLD_HISTORY_FENCED
    )
    assert (
        await fence.admit_source(
            "!room:example.org",
            "$other",
            DispatchCallbackKind.REACTION,
            nio.TimelineEventProvenance.HISTORY,
        )
        is DispatchSourceAdmission.COLD_HISTORY_FENCED
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provenance",
    [nio.TimelineEventProvenance.LIVE, None],
)
async def test_live_and_direct_dispatch_do_not_read_pending_state(
    provenance: nio.TimelineEventProvenance | None,
) -> None:
    """Live nio delivery and explicit non-nio dispatch are current work."""
    obligations = _PendingObligations()
    fence = ColdHistoryFence(obligations)

    admission = await fence.admit_source(
        "!room:example.org",
        "$current",
        DispatchCallbackKind.MESSAGE,
        provenance,
    )

    assert admission is DispatchSourceAdmission.ACCEPTED
    assert obligations.reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provenance",
    [nio.TimelineEventProvenance.LIVE, nio.TimelineEventProvenance.HISTORY],
)
async def test_invite_and_decrypt_fences_override_timeline_provenance(
    provenance: nio.TimelineEventProvenance,
) -> None:
    """Invites stay current while pending joins keep decrypt notices fenced."""
    obligations = _PendingObligations()
    fence = ColdHistoryFence(
        obligations,
        decrypt_notice_is_fenced=lambda room_id: room_id == "!joining:localhost",
    )

    invite = await fence.admit_source(
        "!invited:localhost",
        "$invite",
        DispatchCallbackKind.INVITE,
        provenance,
    )
    decrypt = await fence.admit_source(
        "!joining:localhost",
        "$encrypted",
        DispatchCallbackKind.DECRYPTION_FAILURE,
        provenance,
    )

    assert invite is DispatchSourceAdmission.ACCEPTED
    assert decrypt is DispatchSourceAdmission.DECRYPT_NOTICE_FENCED
    assert obligations.reads == []


def test_best_effort_admission_requires_matching_live_event_provenance() -> None:
    """One live event cannot license unrelated best-effort callback work."""
    fence = ColdHistoryFence(_PendingObligations())

    fence.observe_event_provenance("$live", nio.TimelineEventProvenance.LIVE)
    assert fence.event_is_live("$live")
    assert not fence.event_is_live("$other")

    fence.observe_event_provenance("$history", nio.TimelineEventProvenance.HISTORY)
    assert not fence.event_is_live("$history")
    assert not fence.event_is_live("$live")


@pytest.mark.asyncio
async def test_history_cannot_create_the_obligation_that_admits_itself(
    tmp_path: Path,
) -> None:
    """Nio admission checks history before creating current durable work."""
    store = DispatchObligationStore(
        tracking_path=tmp_path,
        principal_id="@code:example.org",
        entity_name="code",
    )
    fence = ColdHistoryFence(store)
    attempts = 0

    async def callback(
        _room: nio.MatrixRoom,
        _event: nio.Event,
    ) -> DispatchCallbackResult:
        nonlocal attempts
        attempts += 1
        return DispatchCallbackResult.SUCCEEDED

    runner = _runner(store, fence, callback)
    room = nio.MatrixRoom("!room:example.org", "@code:example.org")
    event = _message("$history")

    await runner._admit_source_event(
        room,
        event,
        nio.TimelineEventProvenance.HISTORY,
    )

    assert attempts == 0
    assert not store.has_pending("$history", DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_live_admission_persists_before_callback_fanout(
    tmp_path: Path,
) -> None:
    """Live nio admission durably owns work before ordinary callbacks run."""
    store = DispatchObligationStore(
        tracking_path=tmp_path,
        principal_id="@code:example.org",
        entity_name="code",
    )
    fence = ColdHistoryFence(store)

    async def callback(
        _room: nio.MatrixRoom,
        _event: nio.Event,
    ) -> DispatchCallbackResult:
        return DispatchCallbackResult.SUCCEEDED

    runner = _runner(store, fence, callback)
    room = nio.MatrixRoom("!room:example.org", "@code:example.org")
    event = _message("$live")

    await runner._admit_source_event(
        room,
        event,
        nio.TimelineEventProvenance.LIVE,
    )

    assert store.has_pending("$live", DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_real_nio_initial_history_is_fenced_and_continuation_is_live(
    tmp_path: Path,
) -> None:
    """The public nio contract drives the aggregate admission owner end to end."""
    store = DispatchObligationStore(
        tracking_path=tmp_path,
        principal_id="@code:example.org",
        entity_name="code",
    )
    fence = ColdHistoryFence(store)
    seen: list[str] = []

    async def callback(
        _room: nio.MatrixRoom,
        event: nio.Event,
    ) -> DispatchCallbackResult:
        seen.append(event.event_id)
        return DispatchCallbackResult.SUCCEEDED

    runner = _runner(store, fence, callback)
    client = nio.AsyncClient(
        "https://example.org",
        "@code:example.org",
        config=nio.AsyncClientConfig(
            encryption_enabled=False,
            backfill_limited_timelines=True,
        ),
    )
    owner = object()
    runner.register_source_callbacks(client, owner=owner)

    try:
        await client.receive_response(_sync_response("s1", _message("$history")))
        await client.receive_response(_sync_response("s2", _message("$live")))
        await wait_for_background_tasks(timeout=1, owner=owner)
    finally:
        await client.close()

    assert seen == ["$live"]
    assert not store.has_pending("$history", DispatchCallbackKind.MESSAGE)
    assert not store.has_pending("$live", DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_direct_recovery_bypasses_timeline_provenance(
    tmp_path: Path,
) -> None:
    """Restart recovery replays durable work without a new nio delivery."""
    store = DispatchObligationStore(
        tracking_path=tmp_path,
        principal_id="@code:example.org",
        entity_name="code",
    )
    fence = ColdHistoryFence(store)

    async def failing_callback(
        _room: nio.MatrixRoom,
        _event: nio.Event,
    ) -> DispatchCallbackResult:
        msg = "callback failed"
        raise RuntimeError(msg)

    room = nio.MatrixRoom("!room:example.org", "@code:example.org")
    event = _message("$failed")
    with pytest.raises(RuntimeError, match="callback failed"):
        await _runner(store, fence, failing_callback).dispatch(
            room,
            event,
            DispatchCallbackKind.MESSAGE,
        )
    assert store.has_pending("$failed", DispatchCallbackKind.MESSAGE)

    recovered: list[str] = []

    async def succeeding_callback(
        _room: nio.MatrixRoom,
        recovered_event: nio.Event,
    ) -> DispatchCallbackResult:
        recovered.append(recovered_event.event_id)
        return DispatchCallbackResult.SUCCEEDED

    await _runner(store, fence, succeeding_callback).recover_pending()

    assert recovered == ["$failed"]
    assert not store.has_pending("$failed", DispatchCallbackKind.MESSAGE)
