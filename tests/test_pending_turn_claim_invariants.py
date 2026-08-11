"""Pending-turn-claim invariants for the inbound turn pipeline.

A live inbound turn claims its source events in the ``TurnStore``
(``TurnStore.try_claim_turn``) *before* any text or media normalization runs,
and every acquired claim is released exactly once on every path that does not
transfer the claim into the coalescing gate (ignored, consumed, failed, or
cancelled ingress). When the gate admits the event, the pre-gate claim travels
as ``PendingDispatchMetadata`` and is closed at batch flush, before dispatch
re-claims the same sources for response execution.

These are characterization tests for the turn-pipeline lifecycle refactor:
they pin the current leak-free behavior through the focused ``TurnController``
harness (real ``TurnStore``, ``CoalescingGate``, and ingress lanes).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import nio
import pytest

from mindroom.coalescing import IngressAdmissionClosedError
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.handled_turns import TurnRecord
from mindroom.ingress_lanes import ReceiptLaneKey
from mindroom.matrix.thread_history_result import thread_history_result
from tests.conftest import bind_runtime_paths, make_visible_message, test_runtime_paths
from tests.test_turn_controller_focused import (
    _ROOM_ID,
    _SENDER,
    _THREAD_ROOT,
    _build_harness,
    _entity_user_id,
    _image_event,
    _room_with_members,
    _text_event,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.dispatch_handoff import PreparedIngress
    from mindroom.inbound_turn_normalizer import InboundTurnNormalizer, TextNormalizationRequest
    from mindroom.turn_store import TurnStore
    from tests.test_turn_controller_focused import _Harness


@dataclass
class _ClaimLedger:
    """Ordered claim/release observations over one ``TurnStore``.

    Only successful acquisitions are recorded as claims; every release call is
    recorded, so a double release of the same record shows up as two entries.
    Normalization spies may interleave ``("normalize", None)`` markers to pin
    claim/normalization ordering.
    """

    events: list[tuple[str, TurnRecord | None]] = field(default_factory=list)

    def kinds(self) -> list[str]:
        """Return the ordered claim/release event kinds."""
        return [kind for kind, _record in self.events]

    def releases_of(self, record: TurnRecord) -> int:
        """Return how many times one exact claim record was released."""
        return sum(1 for kind, seen in self.events if kind == "release" and seen is record)


@dataclass
class _ResolveClaimSpy:
    """Delegating normalizer that records claim state at every resolve call.

    Appends a ``normalize`` marker to the shared ledger (for ordering pins),
    then probes whether a live claim currently owns the source event (a
    competing claim must fail while the turn's claim is held).
    """

    inner: InboundTurnNormalizer
    turn_store: TurnStore
    ledger: _ClaimLedger
    source_event_id: str
    claim_live_at_resolve: list[bool] = field(default_factory=list)

    async def resolve_text_event(self, request: TextNormalizationRequest) -> PreparedIngress:
        """Record the claim state at this resolve, then delegate."""
        self.ledger.events.append(("normalize", None))
        probe = TurnRecord.create([self.source_event_id], completed=False)
        claim_live = not self.turn_store.try_claim_turn(probe)
        if not claim_live:
            self.turn_store.release_pending_turn_claim(probe)
        self.claim_live_at_resolve.append(claim_live)
        return await self.inner.resolve_text_event(request)


@dataclass
class _BlockingNormalizer:
    """Normalizer stand-in that blocks inside resolution until cancelled."""

    started: asyncio.Event
    calls: int = 0

    async def resolve_text_event(self, _request: TextNormalizationRequest) -> PreparedIngress:
        """Signal that resolution started, then wait for cancellation."""
        self.calls += 1
        self.started.set()
        await asyncio.Event().wait()
        msg = "blocked normalization must only finish through cancellation"
        raise AssertionError(msg)


@dataclass
class _FailingNormalizer:
    """Normalizer stand-in that fails if resolution is attempted."""

    calls: int = 0

    async def resolve_text_event(self, _request: TextNormalizationRequest) -> PreparedIngress:
        """Resolution must never run on the path under test."""
        self.calls += 1
        msg = "normalization must not run on this path"
        raise AssertionError(msg)


def _install_claim_ledger(monkeypatch: pytest.MonkeyPatch, turn_store: TurnStore) -> _ClaimLedger:
    """Wrap ``try_claim_turn``/``release_pending_turn_claim`` to record claim ownership."""
    ledger = _ClaimLedger()
    real_try_claim = turn_store.try_claim_turn
    real_release = turn_store.release_pending_turn_claim

    def try_claim(record: TurnRecord) -> bool:
        acquired = real_try_claim(record)
        if acquired:
            ledger.events.append(("claim", record))
        return acquired

    def release(record: TurnRecord) -> None:
        ledger.events.append(("release", record))
        real_release(record)

    monkeypatch.setattr(turn_store, "try_claim_turn", try_claim)
    monkeypatch.setattr(turn_store, "release_pending_turn_claim", release)
    return ledger


def _assert_claims_released_exactly_once(ledger: _ClaimLedger) -> None:
    """Every acquired claim was released exactly once: no leak, no double release."""
    claimed = [record for kind, record in ledger.events if kind == "claim"]
    assert claimed, "expected the turn to acquire a pending claim"
    for record in claimed:
        assert ledger.releases_of(record) == 1, (
            f"claim for {record.source_event_ids} was released {ledger.releases_of(record)} times"
        )


def _assert_no_live_claim(turn_store: TurnStore, *source_event_ids: str) -> None:
    """Prove no pending claim owns these sources by claiming and releasing a probe."""
    probe = TurnRecord.create(source_event_ids, completed=False)
    assert turn_store.try_claim_turn(probe), f"a pending claim still owns {source_event_ids}"
    turn_store.release_pending_turn_claim(probe)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Two-agent config bound to isolated runtime paths."""
    return bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General"),
                "research": AgentConfig(display_name="Research"),
            },
        ),
        test_runtime_paths(tmp_path / "runtime"),
    )


def _mention_text_event(body: str, mentioned_user_id: str, *, event_id: str) -> nio.RoomMessageText:
    """Build one human text event that explicitly mentions another agent."""
    return nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": body,
                "msgtype": "m.text",
                "m.mentions": {"user_ids": [mentioned_user_id]},
            },
            "event_id": event_id,
            "sender": _SENDER,
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )


def _install_normalizer(harness: _Harness, normalizer: object) -> None:
    """Swap the controller's normalizer for a typed test double."""
    harness.controller.deps = replace(
        harness.controller.deps,
        normalizer=cast("InboundTurnNormalizer", normalizer),
    )


@pytest.mark.asyncio
async def test_text_claim_is_held_before_text_normalization(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live-turn claim is acquired before any text normalization runs.

    At every ``InboundTurnNormalizer.resolve_text_event`` call, a competing
    claim for the same source must fail because a live claim already owns it,
    and the claim event must precede the normalization marker in the claim
    ledger. Ordinary text is normalized exactly once per live turn, during
    lane-delivered admission; dispatch no longer re-normalizes.
    """
    harness = _build_harness(config, tmp_path)
    ledger = _install_claim_ledger(monkeypatch, harness.turn_store)
    room = _room_with_members(config, "general")
    event = _text_event("claim before normalization")
    spy = _ResolveClaimSpy(
        inner=harness.controller.deps.normalizer,
        turn_store=harness.turn_store,
        ledger=ledger,
        source_event_id=event.event_id,
    )
    _install_normalizer(harness, spy)

    await harness.deliver(room, event)

    assert spy.claim_live_at_resolve, "text normalization never ran"
    assert all(spy.claim_live_at_resolve), "normalization ran without a live claim for the source"
    assert ledger.kinds() == ["claim", "normalize", "release", "claim", "release"]
    _assert_claims_released_exactly_once(ledger)
    _assert_no_live_claim(harness.turn_store, event.event_id)


@pytest.mark.asyncio
async def test_admitted_text_turn_releases_pre_gate_claim_before_dispatch_reclaim(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admitted text turn hands its claim through the gate exactly once.

    Sequence: the pre-gate claim is acquired at ingress, closed at batch flush
    (via the dispatch metadata), the dispatch path then re-claims the same
    sources, and the response-owned claim is released after the terminal
    response record. The dispatch re-claim could never succeed if the pre-gate
    claim were still live, so a completed response also proves the handoff.
    """
    harness = _build_harness(config, tmp_path)
    ledger = _install_claim_ledger(monkeypatch, harness.turn_store)
    room = _room_with_members(config, "general")
    event = _text_event("ordinary admitted turn")

    outcome = await harness.controller.handle_text_event(room, event)
    await harness.gate.drain_all()
    await harness.runner.settle_inbox_responses()

    assert outcome is TurnDispatchOutcome.DEFERRED
    assert len(harness.runner.requests) == 1
    assert ledger.kinds() == ["claim", "release", "claim", "release"]
    _assert_claims_released_exactly_once(ledger)
    _assert_no_live_claim(harness.turn_store, event.event_id)


@pytest.mark.asyncio
async def test_command_control_input_releases_ingress_claim_exactly_once(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command-control shortcut releases the ingress claim before dispatch.

    A non-router agent silently consumes ``!help``: the pre-gate claim is
    released at the shortcut (and cleared from the reservation owner, so the
    leak guard must not release it a second time), the command dispatch
    re-claims the same source, and that claim is released when the consumed
    command settles. The command dispatch could only re-claim the source if
    the pre-gate claim had already been released.
    """
    harness = _build_harness(config, tmp_path)
    ledger = _install_claim_ledger(monkeypatch, harness.turn_store)
    room = _room_with_members(config, "general")
    event = _text_event("!help", event_id="$command-claim:localhost")

    await harness.deliver(room, event)

    assert harness.gate_batches == []
    assert harness.ignored_dispatch_sources == [(event.event_id,)]
    assert ledger.kinds() == ["claim", "release", "claim", "release"]
    _assert_claims_released_exactly_once(ledger)
    _assert_no_live_claim(harness.turn_store, event.event_id)


@pytest.mark.asyncio
async def test_router_pre_ingress_skip_releases_claim(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A router turn consumed before shared ingress work releases its claim.

    When the router skips a message routed to another agent, the turn is
    consumed after the claim but before normalization; the finally guard in
    the message handler must release the claim exactly once.
    """
    harness = _build_harness(config, tmp_path, agent_name=ROUTER_AGENT_NAME)
    ledger = _install_claim_ledger(monkeypatch, harness.turn_store)
    room = _room_with_members(config, ROUTER_AGENT_NAME, "general", "research")
    event = _mention_text_event(
        "@research investigate this",
        _entity_user_id(config, "research"),
        event_id="$router-skip-claim:localhost",
    )
    resolve_spy = _FailingNormalizer()
    _install_normalizer(harness, resolve_spy)

    outcome = await harness.controller.handle_text_event(room, event)

    assert outcome is TurnDispatchOutcome.INTENTIONALLY_IGNORED
    assert resolve_spy.calls == 0, "the router skip must happen before normalization"
    assert harness.gate_batches == []
    assert ledger.kinds() == ["claim", "release"]
    _assert_claims_released_exactly_once(ledger)
    _assert_no_live_claim(harness.turn_store, event.event_id)


@pytest.mark.asyncio
async def test_cancelled_text_ingress_releases_claim(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling ingress while it waits inside normalization releases the claim."""
    harness = _build_harness(config, tmp_path)
    ledger = _install_claim_ledger(monkeypatch, harness.turn_store)
    room = _room_with_members(config, "general")
    event = _text_event("cancel while normalizing")
    blocker = _BlockingNormalizer(started=asyncio.Event())
    _install_normalizer(harness, blocker)

    ingress = asyncio.create_task(harness.controller.handle_text_event(room, event))
    await blocker.started.wait()
    ingress.cancel()
    with pytest.raises(asyncio.CancelledError):
        await ingress

    assert ledger.kinds() == ["claim", "release"]
    _assert_claims_released_exactly_once(ledger)
    _assert_no_live_claim(harness.turn_store, event.event_id)


@pytest.mark.asyncio
async def test_abandoned_lane_slot_releases_claim_exactly_once(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admission through an abandoned lane slot closes the claim exactly once.

    When the lane worker dies, ``_settle_abandoned_lane`` releases the slot and
    the late submit raises ``IngressAdmissionClosedError``. The claim was
    already transferred into the pending event's dispatch metadata (and
    cleared from the reservation owner), so the metadata close releases it —
    and the message handler's leak guard must not release it again.
    """
    harness = _build_harness(config, tmp_path)
    ledger = _install_claim_ledger(monkeypatch, harness.turn_store)
    room = _room_with_members(config, "general")
    event = _text_event("admit through abandoned lane")

    reservation_owner = harness.controller.reserve_prompt_ingress_order(room, _SENDER)
    worker = harness.gate.lanes._workers.get(ReceiptLaneKey(room_id=room.room_id, physical_sender_id=_SENDER))
    assert worker is not None
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    await asyncio.wait_for(reservation_owner.slot.settled.wait(), timeout=1.0)

    with pytest.raises(IngressAdmissionClosedError):
        await harness.controller.handle_text_event(room, event, reservation_owner=reservation_owner)

    assert ledger.kinds() == ["claim", "release"]
    _assert_claims_released_exactly_once(ledger)
    _assert_no_live_claim(harness.turn_store, event.event_id)
    await reservation_owner.release()


@pytest.mark.asyncio
async def test_media_gate_admission_failure_releases_claim_exactly_once(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected media gate admission closes the transferred claim exactly once."""
    harness = _build_harness(config, tmp_path)
    ledger = _install_claim_ledger(monkeypatch, harness.turn_store)
    room = _room_with_members(config, "general")
    event = _image_event(event_id="$media-admission-claim:localhost")
    monkeypatch.setattr(
        harness.gate,
        "submit_lane_slot",
        MagicMock(side_effect=IngressAdmissionClosedError),
    )

    with pytest.raises(IngressAdmissionClosedError):
        await harness.controller.handle_media_event(room, event)

    assert ledger.kinds() == ["claim", "release"]
    _assert_claims_released_exactly_once(ledger)
    _assert_no_live_claim(harness.turn_store, event.event_id)


@pytest.mark.asyncio
async def test_superseded_turn_releases_every_claim(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn superseded by a newer unresponded message leaves no live claim.

    The pre-gate claim closes at batch flush; the dispatch-side claim is
    released by the dispatch finally because the replay guard settles the turn
    as ignored before any response ownership transfer.
    """
    newer_history = thread_history_result(
        [
            make_visible_message(
                sender=_SENDER,
                body="newer follow-up from the same requester",
                event_id="$newer:localhost",
                timestamp=2_000_000,
            ),
        ],
        is_full_history=True,
    )
    harness = _build_harness(config, tmp_path, thread_history=newer_history)
    ledger = _install_claim_ledger(monkeypatch, harness.turn_store)
    room = _room_with_members(config, "general")
    event = _text_event("older superseded message", thread_id=_THREAD_ROOT, origin_server_ts=1_000_000)

    await harness.deliver(room, event)

    assert harness.policy.plan_turn_calls == 0
    assert harness.ignored_dispatch_sources == [(event.event_id,)]
    assert ledger.kinds() == ["claim", "release", "claim", "release"]
    _assert_claims_released_exactly_once(ledger)
    _assert_no_live_claim(harness.turn_store, event.event_id)
