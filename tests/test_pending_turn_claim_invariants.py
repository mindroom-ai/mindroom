"""Pending-turn-claim invariants for the inbound turn pipeline.

A live inbound turn claims its source events in the ``TurnStore``
(``TurnStore.claim``) *before* any text or media normalization runs, and every
acquired claim handle ends exactly once on every path: released when ingress
does not hand it on (ignored, consumed, failed, or cancelled ingress), or
retired when dispatch merges it into the claim on the whole turn, which the
response task then releases.

These are characterization tests for the turn-pipeline lifecycle refactor:
they pin the current leak-free behavior through the focused ``TurnIngress``
harness (real ``TurnStore``, ``CoalescingGate``, and ingress lanes).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import nio
import pytest

from mindroom.coalescing.gate import IngressAdmissionClosedError
from mindroom.coalescing.ingress_lanes import ReceiptLaneKey
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.turn_record import TurnRecord
from mindroom.turn_store import ClaimContended, ClaimRefused, TurnClaim
from tests.conftest import bind_runtime_paths, test_runtime_paths
from tests.test_turns_focused import (
    _ROOM_ID,
    _SENDER,
    _THREAD_ROOT,
    _build_harness,
    _entity_user_id,
    _image_event,
    _replace_harness_deps,
    _room_with_members,
    _serve_newer_requester_message,
    _text_event,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.dispatch_handoff import PreparedIngress
    from mindroom.inbound_turn_normalizer import InboundTurnNormalizer, TextNormalizationRequest
    from mindroom.turn_store import TurnStore
    from tests.test_turns_focused import _Harness


@dataclass
class _ClaimLedger:
    """Ordered observations of claim handles over one ``TurnStore``.

    Acquisitions are ``claim`` (ingress) and ``merge`` (the claim on the whole
    turn at dispatch); a handle ends by ``release`` while live, or by
    ``retire`` when a merge replaces it. A release of a handle that already
    ended does nothing and is not recorded. Normalization spies may interleave
    ``("normalize", None)`` markers to pin claim/normalization ordering.
    """

    events: list[tuple[str, TurnClaim | None]] = field(default_factory=list)

    def kinds(self) -> list[str]:
        """Return the ordered claim event kinds."""
        return [kind for kind, _claim in self.events]

    def ends_of(self, claim: TurnClaim) -> list[str]:
        """Return how one exact claim handle ended: each release or retirement of it while it was live."""
        return [kind for kind, seen in self.events if kind in {"release", "retire"} and seen is claim]


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
        probe = await self.turn_store.claim(TurnRecord.create([self.source_event_id], completed=False))
        if isinstance(probe, TurnClaim):
            probe.release()
        self.claim_live_at_resolve.append(isinstance(probe, ClaimContended))
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


def _is_live(claim: TurnClaim) -> bool:
    return claim in claim._registry._live


def _install_claim_ledger(monkeypatch: pytest.MonkeyPatch, turn_store: TurnStore) -> _ClaimLedger:
    """Wrap claim acquisition, merging, and handle release to record each handle's life."""
    ledger = _ClaimLedger()
    real_claim = turn_store.claim
    real_merge = turn_store.merge_claims
    real_release = TurnClaim.release

    async def claim(record: TurnRecord) -> TurnClaim | ClaimRefused | ClaimContended:
        result = await real_claim(record)
        if isinstance(result, TurnClaim):
            ledger.events.append(("claim", result))
        return result

    async def merge_claims(claims: Sequence[TurnClaim], record: TurnRecord) -> TurnClaim | None:
        live = [handle for handle in claims if _is_live(handle)]
        merged = await real_merge(claims, record)
        if merged is not None:
            ledger.events.extend(("retire", handle) for handle in live)
            ledger.events.append(("merge", merged))
        return merged

    def release(handle: TurnClaim) -> None:
        if _is_live(handle):
            ledger.events.append(("release", handle))
        real_release(handle)

    monkeypatch.setattr(turn_store, "claim", claim)
    monkeypatch.setattr(turn_store, "merge_claims", merge_claims)
    monkeypatch.setattr(TurnClaim, "release", release)
    return ledger


def _assert_claims_released_exactly_once(ledger: _ClaimLedger) -> None:
    """Every acquired handle ended exactly once: no leak, and no owner gave up a handle it no longer held."""
    acquired = [handle for kind, handle in ledger.events if kind in {"claim", "merge"} and handle is not None]
    assert acquired, "expected the turn to acquire a pending claim"
    for handle in acquired:
        assert len(ledger.ends_of(handle)) == 1, (
            f"claim on {sorted(handle.source_event_ids)} ended {ledger.ends_of(handle)}"
        )
        assert not _is_live(handle)


async def _assert_no_live_claim(turn_store: TurnStore, *source_event_ids: str) -> None:
    """Prove no pending claim owns these sources by claiming and releasing a probe."""
    assert not any(map(turn_store.has_live_turn_claim, source_event_ids))
    probe = await turn_store.claim(TurnRecord.create(source_event_ids, completed=False))
    assert isinstance(probe, TurnClaim), f"a pending claim still owns {source_event_ids}"
    probe.release()


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
    """Swap the turn owners' normalizer for a typed test double."""
    _replace_harness_deps(
        harness,
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
        inner=harness.ingress.deps.normalizer,
        turn_store=harness.turn_store,
        ledger=ledger,
        source_event_id=event.event_id,
    )
    _install_normalizer(harness, spy)

    await harness.deliver(room, event)

    assert spy.claim_live_at_resolve, "text normalization never ran"
    assert all(spy.claim_live_at_resolve), "normalization ran without a live claim for the source"
    assert ledger.kinds() == ["claim", "normalize", "retire", "merge", "release"]
    _assert_claims_released_exactly_once(ledger)
    await _assert_no_live_claim(harness.turn_store, event.event_id)


@pytest.mark.asyncio
async def test_admitted_text_turn_releases_pre_gate_claim_before_dispatch_reclaim(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admitted text turn hands its claim through the gate exactly once.

    Sequence: the pre-gate claim is acquired at ingress, dispatch merges it into
    the claim on the whole turn in one step, which retires it, and the response
    task releases that claim when it ends.
    """
    harness = _build_harness(config, tmp_path)
    ledger = _install_claim_ledger(monkeypatch, harness.turn_store)
    room = _room_with_members(config, "general")
    event = _text_event("ordinary admitted turn")

    outcome = await harness.ingress.handle_text_event(room, event)
    await harness.gate.drain_all()
    await harness.runner.settle_inbox_responses()

    assert outcome is TurnDispatchOutcome.DEFERRED
    assert len(harness.runner.requests) == 1
    assert ledger.kinds() == ["claim", "retire", "merge", "release"]
    _assert_claims_released_exactly_once(ledger)
    await _assert_no_live_claim(harness.turn_store, event.event_id)


@pytest.mark.asyncio
async def test_command_control_input_releases_ingress_claim_exactly_once(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command-control shortcut hands its ingress claim straight to the command dispatch.

    A non-router agent silently consumes ``!help``: the command dispatch merges
    the ingress claim (taken from the ticket, so the ticket's close must not
    release it again), and the merged claim is released when the consumed
    command settles.
    """
    harness = _build_harness(config, tmp_path)
    ledger = _install_claim_ledger(monkeypatch, harness.turn_store)
    room = _room_with_members(config, "general")
    event = _text_event("!help", event_id="$command-claim:localhost")

    await harness.deliver(room, event)

    assert harness.gate_batches == []
    assert harness.ignored_dispatch_sources == [(event.event_id,)]
    assert ledger.kinds() == ["claim", "retire", "merge", "release"]
    _assert_claims_released_exactly_once(ledger)
    await _assert_no_live_claim(harness.turn_store, event.event_id)


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

    outcome = await harness.ingress.handle_text_event(room, event)

    assert outcome is TurnDispatchOutcome.INTENTIONALLY_IGNORED
    assert resolve_spy.calls == 0, "the router skip must happen before normalization"
    assert harness.gate_batches == []
    assert ledger.kinds() == ["claim", "release"]
    _assert_claims_released_exactly_once(ledger)
    await _assert_no_live_claim(harness.turn_store, event.event_id)


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

    ingress = asyncio.create_task(harness.ingress.handle_text_event(room, event))
    await blocker.started.wait()
    ingress.cancel()
    with pytest.raises(asyncio.CancelledError):
        await ingress

    assert ledger.kinds() == ["claim", "release"]
    _assert_claims_released_exactly_once(ledger)
    await _assert_no_live_claim(harness.turn_store, event.event_id)


@pytest.mark.asyncio
async def test_abandoned_lane_slot_releases_claim_exactly_once(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admission through an abandoned lane slot closes the claim exactly once.

    When the lane worker dies, ``_settle_abandoned_lane`` releases the slot and
    the late submit raises ``IngressAdmissionClosedError``. The claim was
    already moved into the pending event (and taken off the ticket), so closing
    the event releases it, and the ticket's close finds nothing left to release.
    """
    harness = _build_harness(config, tmp_path)
    ledger = _install_claim_ledger(monkeypatch, harness.turn_store)
    room = _room_with_members(config, "general")
    event = _text_event("admit through abandoned lane")

    ticket = harness.ingress.reserve_prompt_ingress_order(room, _SENDER)
    worker = harness.gate.lanes._workers.get(ReceiptLaneKey(room_id=room.room_id, sender_id=_SENDER))
    assert worker is not None
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    await asyncio.wait_for(ticket.slot.settled.wait(), timeout=1.0)

    # The callback's own reservation is the slot whose lane died.
    monkeypatch.setattr(harness.ingress, "reserve_prompt_ingress_order", lambda *_args, **_kwargs: ticket)
    with pytest.raises(IngressAdmissionClosedError):
        await harness.ingress.handle_text_event(room, event)

    assert ledger.kinds() == ["claim", "release"]
    _assert_claims_released_exactly_once(ledger)
    await _assert_no_live_claim(harness.turn_store, event.event_id)
    ticket.close()


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
        await harness.ingress.handle_media_event(room, event)

    assert ledger.kinds() == ["claim", "release"]
    _assert_claims_released_exactly_once(ledger)
    await _assert_no_live_claim(harness.turn_store, event.event_id)


@pytest.mark.asyncio
async def test_superseded_turn_releases_every_claim(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn superseded by a newer unresponded message leaves no live claim.

    Dispatch merges the pre-gate claim into the claim on the whole turn, and
    releases that claim itself because the replay guard settles the turn as
    ignored before any response takes it.
    """
    harness = _build_harness(config, tmp_path)
    await _serve_newer_requester_message(harness, config, mentions=None, voice=False, history_degraded=False)
    ledger = _install_claim_ledger(monkeypatch, harness.turn_store)
    room = _room_with_members(config, "general")
    event = _text_event("older superseded message", thread_id=_THREAD_ROOT, origin_server_ts=1_000_000)

    await harness.deliver(room, event)

    assert harness.policy.plan_turn_calls == 0
    assert harness.ignored_dispatch_sources == [(event.event_id,)]
    assert ledger.kinds() == ["claim", "retire", "merge", "release"]
    _assert_claims_released_exactly_once(ledger)
    await _assert_no_live_claim(harness.turn_store, event.event_id)
