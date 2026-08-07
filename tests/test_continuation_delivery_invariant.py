"""A turn that runs the model more than once still owes the room one answer.

A dynamic-tool call makes the turn continue: the model runs again with a
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
that caused them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from agno.models.message import Message
from agno.models.response import ToolExecution
from agno.run.agent import RunContentEvent, RunOutput, ToolCallCompletedEvent
from agno.run.team import TeamRunOutput

from mindroom.ai import _PreparedAgentRun
from mindroom.dynamic_tool_continuation import DYNAMIC_TOOL_CONTINUATION_LIMIT
from mindroom.event_journal import DeliveryStage
from mindroom.history.types import PreparedHistoryState
from mindroom.knowledge.utils import _KnowledgeResolution
from tests.conftest import unwrap_extracted_collaborator
from tests.identity_helpers import fixture_entity_matrix_id
from tests.response_runner_helpers import _bot, _plain_request, _target
from tests.test_team_media_fallback import _make_test_agent, _make_test_team

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from agno.team import Team as AgnoTeam

    from mindroom.bot import AgentBot
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.event_journal import (
        DeliveryAcknowledgement,
        OutboxDelivery,
        OutboxView,
        TerminalTurnWrite,
    )
    from mindroom.response_runner import ResponseRunner

pytestmark = pytest.mark.asyncio

_SOURCE = "$event"
_INTERMEDIATE = "Loading the tool now."
_ANSWER = "The real answer."


def _load_tool_execution() -> ToolExecution:
    """Return the dynamic-tool call that forces one more attempt."""
    return ToolExecution(
        tool_call_id="call-load",
        tool_name="load_tool",
        tool_args={"tool_name": "sleep"},
        result=json.dumps({"status": "loaded", "tool": "dynamic_tools", "tool_name": "sleep"}),
        stop_after_tool_call=True,
    )


@dataclass
class _WatchedOutbox:
    """The real outbox, with the calls that matter written onto a timeline.

    Wrapping rather than substituting keeps the store's own rules in play --
    the primary key, and the ``attempted = 0`` guard that decides whether a
    re-enqueue is honoured or silently dropped -- while making the order of
    enqueues and claims observable against the model runs between them.
    """

    inner: OutboxView
    timeline: list[str]

    async def enqueue_delivery(
        self,
        *,
        turn_id: str,
        stage: DeliveryStage,
        room_id: str,
        thread_id: str | None,
        payload: Mapping[str, object],
        edits_event_id: str | None = None,
        settle_source_event_ids: tuple[str, ...] = (),
    ) -> str | None:
        """Record intent, noting the stage on the timeline first."""
        self.timeline.append(f"enqueue:{stage.value}")
        return await self.inner.enqueue_delivery(
            turn_id=turn_id,
            stage=stage,
            room_id=room_id,
            thread_id=thread_id,
            payload=payload,
            edits_event_id=edits_event_id,
            settle_source_event_ids=settle_source_event_ids,
        )

    async def claim_delivery(self, *, turn_id: str, stage: DeliveryStage) -> OutboxDelivery | None:
        """Freeze one delivery, noting the claim on the timeline first."""
        self.timeline.append(f"claim:{stage.value}")
        return await self.inner.claim_delivery(turn_id=turn_id, stage=stage)

    async def record_sending_device(
        self,
        *,
        turn_id: str,
        stage: DeliveryStage,
        device_id: str | None,
    ) -> None:
        """Record the device namespace this delivery is about to send under."""
        await self.inner.record_sending_device(turn_id=turn_id, stage=stage, device_id=device_id)

    async def turn_membership_is_current(self, *, turn_id: str, room_id: str) -> bool:
        """Answer the membership check from the real store, off the timeline.

        The streaming gate asks this between progress edits, so recording it
        would bury the enqueue and claim ordering this double exists to show.
        It still has to be forwarded rather than stubbed: the store's answer
        for a turn the journal never admitted is what keeps a stream running,
        and a stub returning `True` would hide the fence never being consulted.
        """
        return await self.inner.turn_membership_is_current(turn_id=turn_id, room_id=room_id)

    async def load_delivery(self, *, turn_id: str, stage: DeliveryStage) -> OutboxDelivery | None:
        """Return one delivery without claiming it."""
        return await self.inner.load_delivery(turn_id=turn_id, stage=stage)

    async def acknowledge_delivery(
        self,
        *,
        turn_id: str,
        stage: DeliveryStage,
        event_id: str,
        terminal_turn: TerminalTurnWrite | None = None,
    ) -> DeliveryAcknowledgement:
        """Record the Matrix event one claimed delivery produced, and the turn it completes."""
        return await self.inner.acknowledge_delivery(
            turn_id=turn_id,
            stage=stage,
            event_id=event_id,
            terminal_turn=terminal_turn,
        )

    async def unacknowledged_deliveries(
        self,
        *,
        limit: int = 256,
        after: tuple[int, str, str] | None = None,
    ) -> tuple[OutboxDelivery, ...]:
        """Return deliveries whose Matrix outcome is unknown, oldest first."""
        return await self.inner.unacknowledged_deliveries(limit=limit, after=after)


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
class _Turn:
    """One turn driven against a real outbox and a deduplicating homeserver."""

    runner: ResponseRunner
    outbox: _WatchedOutbox
    homeserver: _Homeserver
    timeline: list[str]

    async def final(self) -> OutboxDelivery | None:
        """Return the turn's durable final delivery, without claiming it."""
        return await self.outbox.load_delivery(turn_id=_SOURCE, stage=DeliveryStage.FINAL)

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


def _last_answer(delivery: OutboxDelivery | None) -> str:
    """Return the body the durable final delivery would put in the room."""
    assert delivery is not None, "the turn recorded no final delivery"
    return _visible_body(delivery.payload)


def _watch(bot: AgentBot, *, streaming: bool) -> _Turn:
    """Wire one test bot to a watched real outbox and a fake homeserver.

    The outbox goes in through the gateway's dependency record rather than by
    patching a method, so every production caller reaches the same store.
    Which driver runs is decided by the requester's presence, exactly as it is
    in production, so ``streaming`` sets that rather than short-circuiting the
    decision.
    """
    runner = unwrap_extracted_collaborator(bot._response_runner)
    gateway: DeliveryGateway = runner.deps.delivery_gateway
    timeline: list[str] = []
    outbox = _WatchedOutbox(inner=gateway.deps.outbox, timeline=timeline)
    object.__setattr__(gateway, "deps", replace(gateway.deps, outbox=outbox))

    homeserver = _Homeserver()
    bot.client.room_send = AsyncMock(side_effect=homeserver.room_send)
    presence = MagicMock(spec=nio.PresenceGetResponse)
    presence.presence = "online" if streaming else "offline"
    presence.last_active_ago = 0
    bot.client.get_presence = AsyncMock(return_value=presence)
    return _Turn(runner=runner, outbox=outbox, homeserver=homeserver, timeline=timeline)


def _model_agent() -> MagicMock:
    """Return an agent stand-in the real preparation path can carry."""
    agent = MagicMock()
    agent.model = MagicMock()
    agent.model.id = "test-model"
    agent.add_history_to_context = False
    return agent


def _orchestrator_for(bot: AgentBot) -> SimpleNamespace:
    """Return the orchestrator surface the team driver reads."""
    return SimpleNamespace(
        config=bot.config,
        runtime_paths=bot.runtime_paths,
        knowledge_managers={},
        agent_bots={"general": SimpleNamespace(running=True)},
        knowledge_refresh_scheduler=SimpleNamespace(
            schedule_refresh=lambda _base_id: None,
            is_refreshing=lambda _base_id: False,
        ),
        hook_matrix_admin=lambda: None,
        hook_room_state_querier=lambda: None,
        hook_room_state_putter=lambda: None,
    )


def _prepared(agent: object) -> _PreparedAgentRun:
    """Return the minimal prepared run the agent drivers need."""
    return _PreparedAgentRun(
        agent=agent,  # type: ignore[arg-type]
        messages=(Message(role="user", content="hello"),),
        unseen_event_ids=[],
        prepared_history=PreparedHistoryState(),
        runtime_model_name="default",
    )


def _runs(timeline: list[str], outputs: list[RunOutput]) -> AsyncMock:
    """Return a model stand-in that hands back *outputs*, one recorded run each."""
    remaining = iter(outputs)

    async def run(*_args: object, **_kwargs: object) -> RunOutput:
        timeline.append("attempt")
        return next(remaining)

    return AsyncMock(side_effect=run)


async def _run_blocking_agent_turn(tmp_path: Path, outputs: list[RunOutput]) -> _Turn:
    """Drive one blocking agent turn whose model produces *outputs* in order."""
    bot = _bot(tmp_path)
    # Visible tool calls route the "non-streaming" agent turn through the
    # collected-stream driver instead; the blocking driver is the one here.
    bot.config.agents["general"].show_tool_calls = False
    turn = _watch(bot, streaming=False)
    agent = _model_agent()

    with (
        patch("mindroom.ai._prepare_agent_and_prompt", new=AsyncMock(return_value=_prepared(agent))),
        patch("mindroom.ai.ai_runtime.cached_agent_run", new=_runs(turn.timeline, outputs)),
    ):
        await turn.runner.generate_response(_plain_request(_target()))
    return turn


def _one_continuation() -> list[RunOutput]:
    """Return a dynamic-tool attempt, then the attempt that actually answers.

    The first attempt says something of its own, so a driver that delivered it
    would produce a plausible-looking answer rather than an obvious blank --
    the failure a weaker assertion would let through.
    """
    return [
        RunOutput(run_id="run-1", content=_INTERMEDIATE, tools=[_load_tool_execution()]),
        RunOutput(run_id="run-2", content=_ANSWER),
    ]


class TestAgentContinuation:
    """The blocking agent driver: one answer, from the attempt that produced it."""

    async def test_one_final_delivery_carries_the_last_attempt(self, tmp_path: Path) -> None:
        """Two attempts, one `FINAL`, and it holds what the second attempt said."""
        turn = await _run_blocking_agent_turn(tmp_path, _one_continuation())

        assert turn.attempts == 2, "the dynamic-tool call did not force a second attempt"
        assert turn.timeline.count("enqueue:final") == 1
        assert turn.timeline.count("claim:final") == 1
        assert _last_answer(await turn.final()) == _ANSWER
        # A claim is what freezes the payload, so a claim taken while attempts
        # remain is the defect -- not merely an early enqueue.
        assert turn.final_claim_follows_every_attempt
        assert not turn.room_showed(_INTERMEDIATE)

    async def test_the_placeholder_is_sent_once_not_once_per_attempt(self, tmp_path: Path) -> None:
        """The `INITIAL` stage belongs to the turn, not to an attempt."""
        turn = await _run_blocking_agent_turn(tmp_path, _one_continuation())

        assert turn.attempts == 2
        assert turn.timeline.count("enqueue:initial") == 1
        assert turn.timeline.count("claim:initial") == 1
        # A placeholder and the edit that answers it: two events, one message.
        assert len(turn.homeserver.events) == 2

    async def test_exhausting_the_continuation_budget_still_delivers_one_final(
        self,
        tmp_path: Path,
    ) -> None:
        """A turn that never converges answers with the limit message, once.

        The budget is the one path that ends without the model ever producing
        an answer, so it is also where a second `FINAL` -- or one frozen from
        an earlier attempt -- would be easiest to miss.
        """
        never_converges = [
            RunOutput(run_id=f"run-{index}", content="", tools=[_load_tool_execution()])
            for index in range(DYNAMIC_TOOL_CONTINUATION_LIMIT + 1)
        ]

        turn = await _run_blocking_agent_turn(tmp_path, never_converges)

        assert turn.attempts == DYNAMIC_TOOL_CONTINUATION_LIMIT + 1
        assert turn.timeline.count("enqueue:final") == 1
        assert turn.timeline.count("claim:final") == 1
        assert turn.final_claim_follows_every_attempt
        assert "did not produce a final answer" in _last_answer(await turn.final())


class TestStreamedAgentContinuation:
    """The streamed turn's terminal edit is its one durable delivery."""

    @staticmethod
    def _streams(timeline: list[str]) -> MagicMock:
        """Return an `arun` stand-in: narrate, load a tool, then answer."""

        async def first() -> AsyncIterator[object]:
            timeline.append("attempt")
            yield RunContentEvent(content=_INTERMEDIATE)
            yield ToolCallCompletedEvent(tool=_load_tool_execution())

        async def second() -> AsyncIterator[object]:
            timeline.append("attempt")
            yield RunContentEvent(content=_ANSWER)

        return MagicMock(side_effect=[first(), second()])

    async def test_one_final_delivery_carries_the_last_attempt(self, tmp_path: Path) -> None:
        """The stream edits its placeholder once, after the last attempt.

        A streamed attempt writes into the visible message as it goes, so the
        narration that preceded the tool call is already in the room and stays
        there -- once. The continuation handoff must not put it back a second
        time on the way out, because that text is inside the payload the
        outbox freezes and would then be the answer a reader is left with.
        """
        bot = _bot(tmp_path)
        turn = _watch(bot, streaming=True)
        agent = _model_agent()
        agent.arun = self._streams(turn.timeline)

        with patch("mindroom.ai._prepare_agent_and_prompt", new=AsyncMock(return_value=_prepared(agent))):
            await turn.runner.generate_response(_plain_request(_target()))

        assert turn.attempts == 2, "the dynamic-tool call did not force a second attempt"
        assert turn.timeline.count("enqueue:final") == 1
        assert turn.timeline.count("claim:final") == 1
        assert turn.final_claim_follows_every_attempt
        answer = _last_answer(await turn.final())
        # A streamed attempt's narration is already in the room when the tool
        # call supersedes it, and it stays there: the visible document
        # accumulates across attempts while the recorder is reset, so the
        # delivered answer is both attempts' text. That was investigated as a
        # possible room-versus-history divergence and found not to be one --
        # the projection stores this exact string, because the terminal edit
        # carries it and the projection reduces what the room holds.
        #
        # Equality rather than `endswith` plus a count, to pin the whole
        # string. Honest limit: no mutation was found that this kills and the
        # weaker pair did not, because the reachable ways to change this value
        # also change the narration's count. Prefixing `finalize`'s
        # `text_to_send` does not reach it at all.
        assert answer == _INTERMEDIATE + _ANSWER


class TestTeamContinuation:
    """The team driver continues the same way, and owes the same one answer."""

    @staticmethod
    def _team(timeline: list[str]) -> AgnoTeam:
        """Return a team stand-in: one member dynamic-tool load, then the answer."""
        remaining = iter(
            [
                TeamRunOutput(
                    content=_INTERMEDIATE,
                    member_responses=[RunOutput(agent_name="GeneralAgent", content="", tools=[_load_tool_execution()])],
                ),
                TeamRunOutput(content=_ANSWER),
            ],
        )

        async def run(*_args: object, **_kwargs: object) -> TeamRunOutput:
            timeline.append("attempt")
            return next(remaining)

        team = _make_test_team()
        team.arun = AsyncMock(side_effect=run)
        return team

    async def test_one_final_delivery_carries_the_last_attempt(self, tmp_path: Path) -> None:
        """A team turn that continues still enqueues exactly one `FINAL`."""
        bot = _bot(tmp_path)
        bot.config.agents["general"].show_tool_calls = False
        bot.orchestrator = _orchestrator_for(bot)
        turn = _watch(bot, streaming=False)
        team = self._team(turn.timeline)

        with (
            patch("mindroom.teams.create_agent", return_value=_make_test_agent("GeneralAgent")),
            patch("mindroom.teams.resolve_agent_knowledge_access", return_value=_KnowledgeResolution(knowledge=None)),
            patch("mindroom.teams._create_team_instance", return_value=team),
        ):
            await turn.runner.generate_team_response_helper(
                _plain_request(_target()),
                team_agents=[fixture_entity_matrix_id("general", "localhost", bot.runtime_paths)],
                team_mode="coordinate",
            )

        assert turn.attempts == 2, "the member dynamic-tool call did not force a second attempt"
        assert turn.timeline.count("enqueue:final") == 1
        assert turn.timeline.count("claim:final") == 1
        assert turn.final_claim_follows_every_attempt
        # The team renders its answer inside a consensus block, so the answer
        # is looked for inside it -- and the superseded attempt's text is not.
        answer = _last_answer(await turn.final())
        assert _ANSWER in answer
        assert _INTERMEDIATE not in answer
        assert not turn.room_showed(_INTERMEDIATE)
