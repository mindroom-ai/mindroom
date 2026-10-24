"""A turn that runs the model more than once still owes the room one answer.

A next-turn model switch makes the turn continue: the model runs again with a
continuation prompt, and only the last attempt has an answer worth showing.
The outbox is where that has to hold, and it holds asymmetrically. An
unattempted ``FINAL`` row is overwritten by whatever comes next, so an early
enqueue is harmless; an *attempted* one is kept, and the later enqueue is
discarded in silence. A claim taken between attempts therefore freezes an
intermediate answer as the turn's final one and leaves the real answer with
nowhere to go -- no error, no recovery, and a room that disagrees with the
durable result forever.

So these tests drive real multi-attempt turns through ``ResponseRunner``
against the real journal-backed outbox and a homeserver that deduplicates by
transaction ID, and watch the outbox calls interleaved with the model runs
that caused them. The model is the scripted provider; the switch is the real
``thread_model`` tool.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.config.models import ModelConfig
from mindroom.event_journal import DeliveryStage
from mindroom.turn_continuation import TURN_CONTINUATION_LIMIT
from tests.conftest import install_model_providers, unwrap_extracted_collaborator
from tests.fakes.scripted_model import ScriptedModel, ScriptStep, answer, calls, tool_call
from tests.response_runner_helpers import _bot, _plain_request, _target

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.event_journal import (
        DeliveryAcknowledgement,
        MatrixDelivery,
        MatrixDeliveryView,
        PermanentDeliveryFailure,
        ProjectedEvent,
        TerminalTurnWrite,
    )
    from mindroom.event_journal.replies import PreparedReplyRow, ReplyRowEnqueue, ReplyRowRequest, ReplyStore
    from mindroom.llm.provider import ModelRequest, StreamEvent
    from mindroom.response_runner import ResponseRunner

pytestmark = pytest.mark.asyncio

_SOURCE = "$event"
_INTERMEDIATE = "Switching models now."
_ANSWER = "The real answer."


def _switch(index: int, *, content: str | None) -> list[ScriptStep]:
    """Return a turn that switches to the ``large`` model for the next turn, which forces one more attempt."""
    return calls(
        tool_call("switch_thread_model", f"call-switch-{index}", model_name="large", when="next-turn"),
        content=content,
    )


@dataclass
class _WatchedOutbox:
    """The real outbox, with the calls that matter written onto a timeline.

    Wrapping rather than substituting keeps the store's own rules in play --
    the primary key, and the ``attempted = 0`` guard that decides whether a
    re-enqueue is honoured or silently dropped -- while making the order of
    enqueues and claims observable against the model runs between them.
    """

    inner: MatrixDeliveryView
    timeline: list[str]

    @property
    def principal_id(self) -> str:
        """Return the wrapped delivery principal."""
        return self.inner.principal_id

    @property
    def replies(self) -> ReplyStore:
        """Return the wrapped principal's reply records."""
        return self.inner.replies

    async def membership_epoch(self, room_id: str) -> int:
        """Return the current room membership without timeline noise."""
        return await self.inner.membership_epoch(room_id)

    async def enqueue_reply_row(self, request: ReplyRowRequest, prepared: PreparedReplyRow) -> ReplyRowEnqueue | None:
        """Record one reply write, noting the stage its rule chose on the timeline."""
        enqueued = await self.inner.enqueue_reply_row(request, prepared)
        if enqueued is not None and enqueued.stage is not None:
            self.timeline.append(f"enqueue:{enqueued.stage.value}")
        return enqueued

    async def unresolved_reply_rows(
        self,
        reply_id: str,
        *,
        before_sequence: int | None = None,
    ) -> tuple[tuple[str, DeliveryStage], ...]:
        """Return a reply's rows whose Matrix outcome is unknown, in write order."""
        return await self.inner.unresolved_reply_rows(reply_id, before_sequence=before_sequence)

    async def enqueue_matrix_delivery(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        event_type: str = "m.room.message",
        room_id: str,
        thread_id: str | None,
        payload: Mapping[str, object],
        result: Mapping[str, object] | None = None,
        edits_event_id: str | None = None,
        settle_source_event_ids: tuple[str, ...] = (),
        permanent_failure_reason: str | None = None,
    ) -> str | None:
        """Record intent, noting the stage on the timeline first."""
        self.timeline.append(f"enqueue:{stage.value}")
        return await self.inner.enqueue_matrix_delivery(
            delivery_id=delivery_id,
            stage=stage,
            event_type=event_type,
            room_id=room_id,
            thread_id=thread_id,
            payload=payload,
            result=result,
            edits_event_id=edits_event_id,
            settle_source_event_ids=settle_source_event_ids,
            permanent_failure_reason=permanent_failure_reason,
        )

    async def claim_matrix_delivery(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        sending_device_id: str | None = None,
    ) -> MatrixDelivery | None:
        """Freeze one delivery, noting the claim on the timeline first."""
        self.timeline.append(f"claim:{stage.value}")
        return await self.inner.claim_matrix_delivery(
            delivery_id=delivery_id,
            stage=stage,
            sending_device_id=sending_device_id,
        )

    async def record_matrix_delivery_device(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        device_id: str | None,
    ) -> None:
        """Record the device namespace this delivery is about to send under."""
        await self.inner.record_matrix_delivery_device(delivery_id=delivery_id, stage=stage, device_id=device_id)

    async def turn_membership_is_current(self, *, turn_id: str, room_id: str) -> bool:
        """Answer the membership check from the real store, off the timeline.

        The streaming gate asks this between progress edits, so recording it
        would bury the enqueue and claim ordering this double exists to show.
        It still has to be forwarded rather than stubbed: the store's answer
        for a turn the journal never admitted is what keeps a stream running,
        and a stub returning `True` would hide the fence never being consulted.
        """
        return await self.inner.turn_membership_is_current(turn_id=turn_id, room_id=room_id)

    async def load_matrix_delivery(self, *, delivery_id: str, stage: DeliveryStage) -> MatrixDelivery | None:
        """Return one delivery without claiming it."""
        return await self.inner.load_matrix_delivery(delivery_id=delivery_id, stage=stage)

    async def record_permanent_matrix_delivery_failure(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        reason: str,
    ) -> PermanentDeliveryFailure:
        """Stop retrying one definitively refused immutable payload, or report its ACK."""
        return await self.inner.record_permanent_matrix_delivery_failure(
            delivery_id=delivery_id,
            stage=stage,
            reason=reason,
        )

    async def retire_matrix_delivery(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        room_id: str,
        membership_epoch: int,
    ) -> str | None:
        """Retain an obsolete delivery as an identity tombstone."""
        return await self.inner.retire_matrix_delivery(
            delivery_id=delivery_id,
            stage=stage,
            room_id=room_id,
            membership_epoch=membership_epoch,
        )

    async def acknowledge_matrix_delivery(
        self,
        *,
        delivery_id: str,
        stage: DeliveryStage,
        event_id: str,
        delivered_projections: tuple[ProjectedEvent, ...],
        terminal_turn: TerminalTurnWrite | None = None,
    ) -> DeliveryAcknowledgement:
        """Record the Matrix event one claimed delivery produced, and the turn it completes."""
        return await self.inner.acknowledge_matrix_delivery(
            delivery_id=delivery_id,
            stage=stage,
            event_id=event_id,
            delivered_projections=delivered_projections,
            terminal_turn=terminal_turn,
        )

    async def unacknowledged_matrix_deliveries(
        self,
        *,
        event_type: str = "m.room.message",
        limit: int = 256,
        after: tuple[int, str, str] | None = None,
    ) -> tuple[MatrixDelivery, ...]:
        """Return deliveries whose Matrix outcome is unknown, oldest first."""
        return await self.inner.unacknowledged_matrix_deliveries(event_type=event_type, limit=limit, after=after)


@dataclass
class _Homeserver:
    """A Matrix server that deduplicates by transaction ID, like a real one."""

    events: dict[str, str] = field(default_factory=dict)
    bodies: list[str] = field(default_factory=list)

    async def room_send(
        self,
        *,
        room_id: str,
        message_type: str,
        content: dict[str, Any],
        tx_id: str | None = None,
        **_kwargs: object,
    ) -> nio.RoomSendResponse:
        """Accept one send, collapsing a repeated transaction ID onto one event."""
        del message_type
        event_id = self.events.setdefault(tx_id or f"anon-{len(self.events)}", f"$sent{len(self.events)}")
        self.bodies.append(_visible_body(content))
        return nio.RoomSendResponse(event_id=event_id, room_id=room_id)


def _visible_body(content: Mapping[str, object]) -> str:
    """Return the body a reader would see, following an edit into its replacement."""
    replacement = content.get("m.new_content")
    if isinstance(replacement, Mapping):
        return str(replacement.get("body", ""))
    return str(content.get("body", ""))


@dataclass
class _AttemptingModel(ScriptedModel):
    """The scripted provider, noting each model run on the timeline."""

    timeline: list[str] = field(default_factory=list)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.timeline.append("attempt")
        async for event in super().stream(request):
            yield event


@dataclass
class _Turn:
    """One turn driven against a real outbox and a deduplicating homeserver."""

    runner: ResponseRunner
    outbox: _WatchedOutbox
    homeserver: _Homeserver
    timeline: list[str]

    async def final(self) -> MatrixDelivery | None:
        """Return the turn's durable final delivery, without claiming it."""
        return await self.outbox.load_matrix_delivery(delivery_id=_SOURCE, stage=DeliveryStage.FINAL)

    @property
    def attempts(self) -> int:
        """Return how many times the model actually ran."""
        return self.timeline.count("attempt")

    @property
    def final_claim_follows_every_attempt(self) -> bool:
        """Return whether nothing froze a `FINAL` payload while attempts remained."""
        if "claim:final" not in self.timeline:
            return False
        return self.timeline.index("claim:final") > _last_index(self.timeline, "attempt")

    def room_showed(self, text: str) -> bool:
        """Return whether any send would have put *text* in front of a reader."""
        return any(text in body for body in self.homeserver.bodies)


def _last_index(timeline: list[str], entry: str) -> int:
    """Return the index of the final occurrence of *entry*."""
    return len(timeline) - 1 - timeline[::-1].index(entry)


def _last_answer(delivery: MatrixDelivery | None) -> str:
    """Return the body the durable final delivery would put in the room."""
    assert delivery is not None, "the turn recorded no final delivery"
    return _visible_body(delivery.payload)


def _watch(bot: AgentBot, monkeypatch: pytest.MonkeyPatch, turns: list[list[ScriptStep]], *, streaming: bool) -> _Turn:
    """Wire one test bot to a watched real outbox, a fake homeserver, and the scripted model.

    The outbox goes in through the gateway's dependency record rather than by
    patching a method, so every production caller reaches the same store.
    Which driver runs is decided by the requester's presence, exactly as it is
    in production, so ``streaming`` sets that rather than short-circuiting the
    decision. The agent gets the ``thread_model`` tool and a ``large`` model to
    switch to; both models are the one scripted provider.
    """
    bot.config.agents["general"].tools = ["thread_model"]
    bot.config.models["large"] = ModelConfig(provider="openai", id="large-model")
    runner = unwrap_extracted_collaborator(bot._response_runner)
    gateway: DeliveryGateway = runner.deps.delivery_gateway
    timeline: list[str] = []
    outbox = _WatchedOutbox(inner=gateway.deps.outbox, timeline=timeline)
    object.__setattr__(gateway, "deps", replace(gateway.deps, outbox=outbox))
    model = _AttemptingModel(turns, timeline=timeline)
    install_model_providers(monkeypatch, {"default": model})

    homeserver = _Homeserver()
    bot.client.room_send = AsyncMock(side_effect=homeserver.room_send)
    presence = MagicMock(spec=nio.PresenceGetResponse)
    presence.presence = "online" if streaming else "offline"
    presence.last_active_ago = 0
    bot.client.get_presence = AsyncMock(return_value=presence)
    return _Turn(runner=runner, outbox=outbox, homeserver=homeserver, timeline=timeline)


def _one_continuation() -> list[list[ScriptStep]]:
    """Return a model-switch attempt, then the attempt that actually answers.

    The first attempt says something of its own, so a driver that delivered it
    would produce a plausible-looking answer rather than an obvious blank --
    the failure a weaker assertion would let through.
    """
    return [_switch(1, content=_INTERMEDIATE), answer(_ANSWER)]


async def _run_blocking_agent_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    turns: list[list[ScriptStep]],
) -> _Turn:
    """Drive one blocking agent turn whose model plays *turns* in order."""
    bot = _bot(tmp_path)
    # Visible tool calls route the "non-streaming" agent turn through the
    # collected-stream driver instead; the blocking driver is the one here.
    bot.config.agents["general"].show_tool_calls = False
    turn = _watch(bot, monkeypatch, turns, streaming=False)
    await turn.runner.generate_response(_plain_request(_target()))
    return turn


class TestAgentContinuation:
    """The blocking agent driver: one answer, from the attempt that produced it."""

    async def test_one_final_delivery_carries_the_last_attempt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two attempts, one `FINAL`, and it holds what the second attempt said."""
        turn = await _run_blocking_agent_turn(tmp_path, monkeypatch, _one_continuation())

        assert turn.attempts == 2, "the model switch did not force a second attempt"
        assert turn.timeline.count("enqueue:final") == 1
        assert turn.timeline.count("claim:final") == 1
        assert _last_answer(await turn.final()) == _ANSWER
        # A claim is what freezes the payload, so a claim taken while attempts
        # remain is the defect -- not merely an early enqueue.
        assert turn.final_claim_follows_every_attempt
        assert not turn.room_showed(_INTERMEDIATE)

    async def test_the_placeholder_is_sent_once_not_once_per_attempt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The `INITIAL` stage belongs to the turn, not to an attempt."""
        turn = await _run_blocking_agent_turn(tmp_path, monkeypatch, _one_continuation())

        assert turn.attempts == 2
        assert turn.timeline.count("enqueue:initial") == 1
        assert turn.timeline.count("claim:initial") == 1
        # A placeholder and the edit that answers it: two events, one message.
        assert len(turn.homeserver.events) == 2

    async def test_exhausting_the_continuation_budget_still_delivers_one_final(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A turn that never converges answers with the limit message, once.

        The budget is the one path that ends without the model ever producing
        an answer, so it is also where a second `FINAL` -- or one frozen from
        an earlier attempt -- would be easiest to miss.
        """
        never_converges = [_switch(index, content=None) for index in range(TURN_CONTINUATION_LIMIT + 1)]

        turn = await _run_blocking_agent_turn(tmp_path, monkeypatch, never_converges)

        assert turn.attempts == TURN_CONTINUATION_LIMIT + 1
        assert turn.timeline.count("enqueue:final") == 1
        assert turn.timeline.count("claim:final") == 1
        assert turn.final_claim_follows_every_attempt
        assert "did not produce a final answer" in _last_answer(await turn.final())


class TestStreamedAgentContinuation:
    """The streamed turn's terminal edit is its one durable delivery."""

    async def test_one_final_delivery_carries_the_last_attempt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The stream edits its placeholder once, after the last attempt.

        A streamed attempt writes into the visible message as it goes, so the
        narration that preceded the tool call is already in the room and stays
        there -- once. The continuation handoff must not put it back a second
        time on the way out, because that text is inside the payload the
        outbox freezes and would then be the answer a reader is left with.
        """
        bot = _bot(tmp_path)
        turn = _watch(bot, monkeypatch, _one_continuation(), streaming=True)

        await turn.runner.generate_response(_plain_request(_target()))

        assert turn.attempts == 2, "the model switch did not force a second attempt"
        assert turn.timeline.count("enqueue:final") == 1
        assert turn.timeline.count("claim:final") == 1
        assert turn.final_claim_follows_every_attempt
        answer_body = _last_answer(await turn.final())
        # A streamed attempt's narration is already in the room when the tool
        # call supersedes it, and it stays there: the visible document
        # accumulates across attempts, so the delivered answer holds both
        # attempts' text, each once.
        assert answer_body.count(_INTERMEDIATE) == 1
        assert answer_body.endswith(_ANSWER)
