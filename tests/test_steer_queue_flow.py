"""Steer, Queue and Auto across two real turns: the stored run, the finished reply, and the follow-up's request.

The first reply runs through the response runner; the waiting message is reserved as ingress reserves it, while the
reply's tool runs, and then handed to the turn dispatcher with that reservation, as the coalescing gate hands it on
once the reply ends. Both replies answer in one room conversation, so one stored session holds both runs.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom import reply_lifecycle as rl
from mindroom.approval import manager as approval_manager
from mindroom.approval.tool_approval import shutdown_approval_runtime
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.judgment import LLMJudgmentConfig
from mindroom.config.mid_turn import MidTurnConfig
from mindroom.config.models import ModelConfig
from mindroom.constants import STREAM_STATUS_KEY
from mindroom.dispatch_handoff import PreparedIngress
from mindroom.event_journal import EventClass, EventKind, InboundEvent
from mindroom.llm.runs import RunStatus, run_tool_executions
from mindroom.matrix.thread_history_result import thread_history_result
from mindroom.mid_turn import MID_TURN_MODE_KEY, MidTurnMode
from mindroom.prompts import QUEUED_MESSAGE_NOTICE_TEXT
from mindroom.response_payload_preparation import _STEER_CONTINUATION_LINE, _steered_follow_up_prompt
from mindroom.tool_system.agno_toolkits import Toolkit
from mindroom.turn_record import TurnRecord
from mindroom.turns.queued_notice import queued_notice_reservation_if_busy
from tests.conftest import (
    SETTLE_TIMEOUT_SECONDS,
    handle_card_response_for_test,
    install_model_providers,
    make_visible_message,
    message_origin,
    unwrap_extracted_collaborator,
)
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call
from tests.response_runner_helpers import _envelope, _plain_request, _target
from tests.scripted_agent_helpers import registered_toolkit
from tests.test_reply_endings_characterization import (
    _REQUESTER,
    _ROOM,
    _SOURCE,
    _new_bot,
    _require_echo_approval,
    _respond_auto_approved,
    _team_reply,
    _team_scope,
    _trace_events,
    _Turn,
    _turn,
)
from tests.turn_dispatch_helpers import dispatch_test_turn

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
    from pathlib import Path

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelRequest
    from mindroom.reply_turn.request import ResponseRequest
    from mindroom.response_lifecycle import QueuedHumanNoticeReservation
    from tests.fakes.scripted_model import ScriptStep

pytestmark = pytest.mark.asyncio

_TOOL = "steer_flow_echo"
_AGENT_ID = "@mindroom_general:localhost"
_STEER = "$steer"
_STEER_TEXT = "Also print the date"
_FIRST_BATCH = calls(tool_call("echo", "call-1", text="hi"), content="Looking. ")
# The judge's question is whether the reply should stop for the queue.
_WAIT = '{"decision": false}'
_CANNOT_WAIT = '{"decision": true}'
_APPROVAL_WAIT = "Waiting for approval: `echo`"
# What a resume that ends with no text of its own shows, so its approval finishes through its acknowledged FINAL.
_APPROVAL_FALLBACK = "Tool approval continuation completed"


class _Tools(Toolkit):
    """One quick tool; ``during`` runs while it does, as a person's message can arrive while a tool runs."""

    during: ClassVar[Callable[[], Awaitable[None]] | None] = None

    def __init__(self) -> None:
        super().__init__(name="echo", tools=[self.echo])

    async def echo(self, text: str) -> str:
        """Echo the text back.

        Args:
            text: What to echo.

        """
        if type(self).during is not None:
            await type(self).during()
        return f"echoed {text}"


@pytest.fixture
def tools() -> Iterator[type[_Tools]]:
    """Register the echo toolkit for one test."""
    _Tools.during = None
    with registered_toolkit(_TOOL, _Tools):
        yield _Tools


async def _flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    scripts: Mapping[str, Sequence[Sequence[ScriptStep]]],
    show_tool_calls: bool = True,
    judge: bool = False,
    streaming: bool = True,
) -> _Turn:
    """Build the bot, its agent answering in the room, with a mid-turn judge playing the ``judge`` script if asked."""
    turn = await _turn(
        tmp_path,
        monkeypatch,
        streaming=streaming,
        show_tool_calls=show_tool_calls,
        tools=[_TOOL],
        scripts=scripts,
    )
    agent = turn.bot.config.agents["general"]
    agent.thread_mode = "room"
    if judge:
        turn.bot.config.models["judge"] = ModelConfig(provider="openai", id="judge")
        agent.mid_turn = MidTurnConfig(judgment=LLMJudgmentConfig(provider="llm", model="judge"))
    return turn


def _arrival(
    turn: _Turn,
    *,
    mode: MidTurnMode,
) -> tuple[list[QueuedHumanNoticeReservation], Callable[[], Awaitable[None]]]:
    """Return a callback that reserves the requester's ``$steer`` once, as its ingress does, and its reservations."""
    notices: list[QueuedHumanNoticeReservation] = []

    async def arrive() -> None:
        if notices:
            return
        target = _target(reply_to_event_id=_STEER)
        envelope = replace(
            _envelope(target, source_event_id=_STEER),
            body=_STEER_TEXT,
            origin=message_origin(sender_id=_REQUESTER, requester_id=_REQUESTER),
        )
        notice = await queued_notice_reservation_if_busy(turn.runner, target=target, envelope=envelope, mode=mode)
        assert notice is not None
        notices.append(notice)

    return notices, arrive


def _queue_while_tool_runs(
    turn: _Turn,
    tools: type[_Tools],
    *,
    mode: MidTurnMode,
) -> list[QueuedHumanNoticeReservation]:
    """Reserve the requester's ``$steer`` while the reply's tool runs; return its reservations."""
    notices, tools.during = _arrival(turn, mode=mode)
    return notices


def _first_request() -> ResponseRequest:
    """Return the request for ``$event``, with the full room history a judge reads before it: none."""
    history = thread_history_result(
        [make_visible_message(sender=_REQUESTER, body="hello", event_id=_SOURCE)],
        is_full_history=True,
    )
    return replace(_plain_request(_target()), thread_history=history)


async def _respond_person_approved(turn: _Turn, request: ResponseRequest) -> None:
    """Answer ``request``, whose ``echo`` call waits for a person: the requester approves its card and the reply resumes.

    The pause shows its waiting text, unlike one a policy grants at once. The approved source then runs again, as its
    woken source does, and the resume runs inside that response.
    """
    _require_echo_approval(turn)
    try:
        await turn.runner.generate_response(request)
        assert await wait_for_background_tasks(timeout=SETTLE_TIMEOUT_SECONDS, owner=turn.bot._runtime_view)
        assert (await _reply(turn, _SOURCE)).state is rl.ReplyState.PAUSED
        assert (turn.homeserver.bodies[-1], turn.homeserver.statuses[-1]) == (_APPROVAL_WAIT, "approval_pending")
        approved = await handle_card_response_for_test(
            approval_manager.get_approval_store(),
            room_id=_ROOM,
            sender_id=_REQUESTER,
            card_event_id="$approval-card",
            status="approved",
            authorize_responder=lambda _entity_name: True,
        )
        assert approved.consumed
        assert await wait_for_background_tasks(timeout=SETTLE_TIMEOUT_SECONDS, owner=turn.bot._runtime_view)
        await turn.runner.generate_response(request)
        assert await wait_for_background_tasks(timeout=SETTLE_TIMEOUT_SECONDS, owner=turn.bot._runtime_view)
    finally:
        await shutdown_approval_runtime()


def _room() -> nio.MatrixRoom:
    room = nio.MatrixRoom(_ROOM, _AGENT_ID)
    room.add_member(_REQUESTER, "User", None)
    room.add_member(_AGENT_ID, "General", None)
    return room


async def _admit(turn: _Turn, event_id: str = _STEER) -> None:
    """Admit the message to the journal, as its ingress does."""
    await turn.bot.journal_principal().admit(
        InboundEvent(
            event_id=event_id,
            room_id=_ROOM,
            thread_id=None,
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender=_REQUESTER,
            origin_server_ts=2,
            source={},
        ),
    )


async def _dispatch(
    turn: _Turn,
    notice: QueuedHumanNoticeReservation | None,
    *,
    mode: MidTurnMode = MidTurnMode.STEER,
    event_id: str = _STEER,
    body: str = _STEER_TEXT,
) -> None:
    """Hand the requester's message to the turn dispatcher, as the gate does once the reply ends; wait for its reply."""
    await _admit(turn, event_id)
    content = {"msgtype": "m.text", "body": body, MID_TURN_MODE_KEY: mode.value}
    event = PreparedIngress(
        sender=_REQUESTER,
        event_id=event_id,
        body=body,
        source={"type": "m.room.message", "event_id": event_id, "sender": _REQUESTER, "content": content},
        server_timestamp=2,
    )
    await dispatch_test_turn(turn.bot._turn_dispatcher, _room(), event, _REQUESTER, queued_notice_reservation=notice)
    await turn.runner.inbox.drain_inbox_responses()


def _wire(messages: Sequence[Message]) -> list[tuple[object, ...]]:
    """Return what a provider body carries of each message: role, content, calls and the call a result answers."""
    return [(m.role, m.content, m.tool_calls, m.tool_call_id) for m in messages]


def _last_edit(turn: _Turn, event_id: str) -> dict[str, Any]:
    """Return the last write that edits the reply event ``event_id``."""
    return [c for c in turn.homeserver.writes if c.get("m.relates_to", {}).get("event_id") == event_id][-1]


def _answered_sources(turn: _Turn) -> list[str]:
    """Return the message each new reply event answers, in the order the replies were sent."""
    relations = [content.get("m.relates_to", {}) for content in turn.homeserver.writes]
    return [
        relation["io.mindroom.in_reply_to"]["event_id"]
        for relation in relations
        if "io.mindroom.in_reply_to" in relation
    ]


async def _assert_settled(turn: _Turn, source_event_id: str) -> None:
    """Assert the source's own turn completed, the journal holds it settled, and nobody still claims it."""
    record = await turn.bot._turn_store.get_turn_record(source_event_id)
    assert record is not None
    assert record.source_event_ids == (source_event_id,)
    assert record.completed
    assert not await turn.bot.journal_principal().is_pending(source_event_id)
    assert not turn.bot._turn_store.has_live_turn_claim(source_event_id)


async def _reply(turn: _Turn, source_event_id: str) -> rl.Reply:
    reply = await turn.bot.journal_principal().replies.for_sources((source_event_id,))
    assert reply is not None
    return reply


def _contents(request: ModelRequest) -> list[str]:
    return [str(message.content) for message in request.messages]


@pytest.mark.parametrize(
    ("mode", "judge", "approval"),
    [
        pytest.param(MidTurnMode.STEER, None, False, id="steer-no-judge"),
        pytest.param(MidTurnMode.STEER, [answer(_WAIT)], False, id="steer-judge"),
        pytest.param(MidTurnMode.AUTO, [answer(_CANNOT_WAIT)], False, id="auto-cannot-wait"),
        pytest.param(MidTurnMode.STEER, None, True, id="approval-resume"),
    ],
)
async def test_steer_finishes_old_reply_and_followup_replays_its_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: type[_Tools],
    mode: MidTurnMode,
    judge: list[list[ScriptStep]] | None,
    approval: bool,
) -> None:
    """The reply ends after its tool batch as it stands; the message's own reply carries on from the stored run.

    The cut-off run stores its calls and results completed, with no notice or summary and no further request, and the
    follow-up's first request extends that run's last request whole, so a prompt cache reuses it. The follow-up reads
    the message once and then the carry-on line. An explicit Steer is never judged; a judge's "cannot wait" ends the
    reply the same way. An approval resume that owns the reply ends at its approved batch.
    """
    scripts = {"default": [_FIRST_BATCH, answer("Carried on.")], **({"judge": judge} if judge is not None else {})}
    turn = await _flow(tmp_path, monkeypatch, scripts=scripts, judge=judge is not None)
    notices = _queue_while_tool_runs(turn, tools, mode=mode)

    if approval:
        _require_echo_approval(turn)
        await _respond_auto_approved(turn, _first_request())
    else:
        await turn.runner.generate_response(_first_request())
    [notice] = notices
    assert notice.stopped_reply

    # The old reply finished as it stood, completed, in its own event, with its completed tool call.
    old_reply = await _reply(turn, _SOURCE)
    assert old_reply.state is rl.ReplyState.COMPLETED
    assert old_reply.event_id == "$sent0"
    final = _last_edit(turn, old_reply.event_id)
    assert final[STREAM_STATUS_KEY] == "completed"
    assert [(e["tool_name"], e["type"], e["result_preview"]) for e in _trace_events(final)] == [
        ("echo", "tool_call_completed", "echoed hi"),
    ]
    model = turn.models["default"]
    assert len(model.requests) == 1
    [cut_off] = turn.agent_runs()
    assert cut_off.status is RunStatus.completed

    await _dispatch(turn, notice, mode=mode)

    # Each turn made one request: no summary, no empty-response retry, no continuation attempt.
    first, follow_up = model.requests
    if judge is not None:
        assert len(turn.models["judge"].requests) == (1 if mode is MidTurnMode.AUTO else 0)
    old_run, new_run = turn.agent_runs()
    assert old_run.run_id == cut_off.run_id != new_run.run_id
    assert (old_run.status, new_run.status) == (RunStatus.completed, RunStatus.completed)
    # The cut-off run stores what its request sent, turn context included, then its call and result: no notice, no
    # summary, and no stop flag, which would drop that context for a model switch's continuation to send again.
    sent = [(m.role, m.content) for m in first.messages if m.role != "system"]
    assert [(m.role, m.content) for m in old_run.messages] == [*sent, ("assistant", "Looking. "), ("tool", "echoed hi")]
    assert not any(tool.stop_after_call for tool in run_tool_executions(old_run))
    assert not any(m.stop_after_tool_call for m in old_run.messages)
    # The follow-up's request extends the cut-off run's last request whole, then the call and result, then the message.
    *replayed, prompt = follow_up.messages[len(first.messages) :]
    assert _wire(follow_up.messages)[: len(first.messages)] == _wire(first.messages)
    assert _wire(replayed) == _wire(old_run.messages[-2:])
    assert prompt.role == "user"
    text = str(prompt.content)
    assert sum(content.count(_STEER_TEXT) for content in _contents(follow_up)) == 1
    assert sum(content.count(_STEER_CONTINUATION_LINE) for content in _contents(follow_up)) == 1
    assert text.index(_STEER_TEXT) < text.index(_STEER_CONTINUATION_LINE)

    # Two replies, each answering its own message, each source settled once by its own completed turn.
    new_reply = await _reply(turn, _STEER)
    assert new_reply.reply_id != old_reply.reply_id
    assert new_reply.state is rl.ReplyState.COMPLETED
    assert new_reply.event_id not in {None, old_reply.event_id}
    assert _answered_sources(turn) == [_SOURCE, _STEER]
    assert _last_edit(turn, new_reply.event_id)[STREAM_STATUS_KEY] == "completed"
    await _assert_settled(turn, _SOURCE)
    await _assert_settled(turn, _STEER)


async def test_team_leader_steer_ends_its_run_and_the_team_followup_replays_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A team leader owns its reply: it ends after its members' batch, and the team's next reply replays its run.

    The Steer arrives while a member works. The member finishes; the leader records the batch and ends without its
    consensus request. Its run is stored in the team's shared scope, not the agent's or a member's, and the team's
    follow-up extends the leader's last request whole.
    """
    turn = await _flow(
        tmp_path,
        monkeypatch,
        scripts={
            "default": [
                calls(tool_call("delegate_task_to_members", "call-all", task="One fact")),
                answer("Team carried on."),
            ],
            "writer-model": [],
            "checker-model": [answer("Checker fact.")],
        },
    )
    notices, arrive = _arrival(turn, mode=MidTurnMode.STEER)

    async def arrive_while_member_works() -> None:
        await arrive()

    turn.models["writer-model"].turns.append([Await(arrive_while_member_works), *answer("Writer fact.")])

    await _team_reply(turn, _first_request())
    [notice] = notices
    assert notice.stopped_reply
    leader = turn.models["default"]
    assert len(leader.requests) == 1
    old_reply = await _reply(turn, _SOURCE)
    assert old_reply.state is rl.ReplyState.COMPLETED
    assert _last_edit(turn, old_reply.event_id)[STREAM_STATUS_KEY] == "completed"

    await _admit(turn)
    await turn.bot._turn_store.record_pending_turn(
        turn.bot._turn_store.attach_response_context(
            TurnRecord.create([_STEER], requester_id=_REQUESTER),
            history_scope=_team_scope(turn),
            conversation_target=_target(reply_to_event_id=_STEER),
        ),
    )
    follow_up_request = replace(
        _plain_request(_target(reply_to_event_id=_STEER), source_event_id=_STEER),
        prompt=_STEER_TEXT,
        # The marked prompt the payload preparer builds for a message that stopped the reply.
        model_prompt=_steered_follow_up_prompt(_STEER_TEXT),
    )
    await _team_reply(turn, follow_up_request)

    first, follow_up = leader.requests
    assert _wire(follow_up.messages)[: len(first.messages)] == _wire(first.messages)
    assistant, result, prompt = follow_up.messages[len(first.messages) :]
    assert [call["id"] for call in assistant.tool_calls or ()] == ["call-all"]
    assert (result.role, result.tool_call_id) == ("tool", "call-all")
    assert "Writer fact." in str(result.content)
    assert "Checker fact." in str(result.content)
    assert str(prompt.content).count(_STEER_CONTINUATION_LINE) == 1
    # The members answered once, for the cut-off run only.
    assert [len(turn.models[name].requests) for name in ("writer-model", "checker-model")] == [1, 1]
    old_run, new_run = [run for run in turn.team_runs() if run.parent_run_id is None]
    assert (old_run.status, new_run.status) == (RunStatus.completed, RunStatus.completed)
    assert old_run.messages[-1].role == "tool"
    assert not any(QUEUED_MESSAGE_NOTICE_TEXT in str(m.content) for m in old_run.messages)
    assert not any(tool.stop_after_call for tool in run_tool_executions(old_run))
    assert turn.agent_runs() == []
    new_reply = await _reply(turn, _STEER)
    assert new_reply.state is rl.ReplyState.COMPLETED
    assert _answered_sources(turn) == [_SOURCE, _STEER]


@pytest.mark.parametrize(
    ("mode", "judge"),
    [
        # A judge that answers nothing usable is no verdict: the reply pauses with the notice, as without a judge.
        pytest.param(MidTurnMode.AUTO, [answer("I am not sure.")], id="auto-failed-judgment"),
        # Queue never asks the judge, which would have said the reply cannot wait.
        pytest.param(MidTurnMode.QUEUE, [answer(_CANNOT_WAIT)], id="queue"),
    ],
)
async def test_auto_failure_and_queue_keep_distinct_request_traces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: type[_Tools],
    mode: MidTurnMode,
    judge: list[list[ScriptStep]],
) -> None:
    """A failed judgment pauses the reply with the notice-bearing summary request; Queue lets the reply finish.

    Neither stopped the reply for the message, so its own turn is an ordinary one: participation is not bypassed
    and the model reads no carry-on line.
    """
    turn = await _flow(
        tmp_path,
        monkeypatch,
        scripts={"default": [_FIRST_BATCH, answer("Wrapped up."), answer("Answered.")], "judge": judge},
        judge=True,
    )
    notices = _queue_while_tool_runs(turn, tools, mode=mode)

    await turn.runner.generate_response(_first_request())
    [notice] = notices
    model = turn.models["default"]
    _first, last = model.requests
    noticed = QUEUED_MESSAGE_NOTICE_TEXT in _contents(last)
    assert noticed is (mode is MidTurnMode.AUTO)
    assert len(turn.models["judge"].requests) == (1 if mode is MidTurnMode.AUTO else 0)
    [run] = turn.agent_runs()
    assert run.status is RunStatus.completed
    assert run.messages[-1].content == "Wrapped up."
    assert (await _reply(turn, _SOURCE)).state is rl.ReplyState.COMPLETED
    # What the dispatcher forwards as the follow-up's bypass, and the payload preparer as its carry-on line.
    assert not notice.stopped_reply

    await _dispatch(turn, notice, mode=mode)

    follow_up = model.requests[-1]
    assert len(model.requests) == 3
    assert not any(_STEER_CONTINUATION_LINE in content for content in _contents(follow_up))
    assert sum(content.count(_STEER_TEXT) for content in _contents(follow_up)) == 1
    assert (await _reply(turn, _STEER)).state is rl.ReplyState.COMPLETED
    await _assert_settled(turn, _STEER)


async def _restart(turn: _Turn, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, script: list[ScriptStep]) -> _Turn:
    """Start a new bot on the same storage and homeserver, as a process restart does; its model plays ``script``."""
    bot = _new_bot(tmp_path)
    bot.config.agents["general"].thread_mode = "room"
    bot.config.agents["general"].tools = [_TOOL]
    await bot._reply_runtime.start()
    model = ScriptedModel([script])
    install_model_providers(monkeypatch, {"default": model})
    bot.client.room_send = AsyncMock(side_effect=turn.homeserver.room_send)
    bot.client.get_presence = turn.bot.client.get_presence
    return replace(
        turn,
        bot=bot,
        runner=unwrap_extracted_collaborator(bot._response_runner),
        models={"default": model},
    )


async def test_restart_after_old_completion_replays_only_unanswered_followup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: type[_Tools],
) -> None:
    """After a restart, the stopped reply's message stays answered and the Steer gets one reply of its own.

    The process stops after the old reply completed and while the Steer still waits. The restarted bot dispatches
    both messages again: the old one is not answered again and its event is not rewritten, and the Steer, whose
    in-memory reservation went with the process, is answered once as an ordinary message that replays the old run.
    """
    turn = await _flow(tmp_path, monkeypatch, scripts={"default": [_FIRST_BATCH]})
    notices = _queue_while_tool_runs(turn, tools, mode=MidTurnMode.STEER)
    await turn.runner.generate_response(_first_request())
    assert notices[0].stopped_reply
    await _admit(turn)
    old_reply = await _reply(turn, _SOURCE)
    assert old_reply.state is rl.ReplyState.COMPLETED
    written_before = len(turn.homeserver.writes)

    restarted = await _restart(turn, monkeypatch, tmp_path, answer("Carried on."))
    await _dispatch(restarted, None, event_id=_SOURCE, body="hello")
    await _dispatch(restarted, None)

    [request] = restarted.models["default"].requests
    assert ("tool", "echoed hi") in [(m.role, m.content) for m in request.messages]
    assert sum(content.count(_STEER_TEXT) for content in _contents(request)) == 1
    after = turn.homeserver.writes[written_before:]
    assert not any(content.get("m.relates_to", {}).get("event_id") == old_reply.event_id for content in after)
    assert _answered_sources(turn) == [_SOURCE, _STEER]
    assert (await _reply(restarted, _SOURCE)).event_id == old_reply.event_id
    new_reply = await _reply(restarted, _STEER)
    assert new_reply.state is rl.ReplyState.COMPLETED
    assert _last_edit(turn, new_reply.event_id)[STREAM_STATUS_KEY] == "completed"
    await _assert_settled(restarted, _SOURCE)
    await _assert_settled(restarted, _STEER)


async def test_hidden_tool_only_steer_has_no_failed_or_stuck_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: type[_Tools],
) -> None:
    """A Steer that ends a reply which showed nothing but its placeholder removes it, with no failure note.

    The reply wrote no text and hid its tool calls, so it had nothing to show. Its run is stored completed with its
    call and result, its message counts as answered, and the Steer's reply answers as usual.
    """
    turn = await _flow(
        tmp_path,
        monkeypatch,
        scripts={"default": [calls(tool_call("echo", "call-1", text="hi")), answer("Carried on.")]},
        show_tool_calls=False,
    )
    notices = _queue_while_tool_runs(turn, tools, mode=MidTurnMode.STEER)

    await turn.runner.generate_response(_first_request())

    assert notices[0].stopped_reply
    # One request: a tool-only run is not empty, so nothing retries it.
    assert len(turn.models["default"].requests) == 1
    [run] = turn.agent_runs()
    assert run.status is RunStatus.completed
    assert [(m.role, m.content) for m in run.messages[1:]] == [("assistant", None), ("tool", "echoed hi")]
    old_reply = await _reply(turn, _SOURCE)
    assert old_reply.event_id == "$sent0"
    assert set(turn.homeserver.bodies) == {"Thinking..."}
    assert "error" not in turn.homeserver.statuses
    # The reply is gone: its placeholder is redacted, with no failure note ever written.
    assert old_reply.state is rl.ReplyState.GONE
    assert old_reply.event_id in [call.args[1] for call in turn.bot.client.room_redact.await_args_list]
    await _assert_settled(turn, _SOURCE)

    await _dispatch(turn, notices[0])

    new_reply = await _reply(turn, _STEER)
    assert new_reply.state is rl.ReplyState.COMPLETED
    assert turn.homeserver.bodies[-1] == "Carried on."
    assert _last_edit(turn, new_reply.event_id)[STREAM_STATUS_KEY] == "completed"
    await _assert_settled(turn, _STEER)


async def test_hidden_tool_only_steer_whose_progress_edits_failed_still_removes_its_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: type[_Tools],
) -> None:
    """Refused progress edits do not fail a reply (#4067), so a Steer still removes one that had nothing to show."""
    turn = await _flow(
        tmp_path,
        monkeypatch,
        scripts={"default": [calls(tool_call("echo", "call-1", text="hi"))]},
        show_tool_calls=False,
    )
    notices, tools.during = _arrival(turn, mode=MidTurnMode.STEER)
    # Every progress edit fails, whether it lands before or after the Steer arrives: which comes first is a race.
    turn.homeserver.refuse_streaming_text = "Thinking..."

    await turn.runner.generate_response(_first_request())

    assert notices[0].stopped_reply
    old_reply = await _reply(turn, _SOURCE)
    assert old_reply.state is rl.ReplyState.GONE
    assert old_reply.event_id in [call.args[1] for call in turn.bot.client.room_redact.await_args_list]
    assert "error" not in turn.homeserver.statuses


@pytest.mark.parametrize("streaming", [True, False], ids=["streamed", "whole"])
async def test_hidden_tool_only_steer_of_a_person_approved_resume_ends_with_its_completion_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: type[_Tools],
    streaming: bool,
) -> None:
    """Pins the accepted exception: a Steer that ends an approval resume with nothing to show keeps its completion text.

    A reply an approval holds ends only through an acknowledged FINAL, so its message is not removed as an ordinary
    turn's placeholder is: it completes with the text every tool-only approval resume shows, never a failure note.
    Its run is stored completed with its call and result, its source settles, and the Steer's reply carries on once.
    """
    turn = await _flow(
        tmp_path,
        monkeypatch,
        scripts={"default": [calls(tool_call("echo", "call-1", text="hi")), answer("Carried on.")]},
        show_tool_calls=False,
        streaming=streaming,
    )
    notices = _queue_while_tool_runs(turn, tools, mode=MidTurnMode.STEER)

    await _respond_person_approved(turn, _first_request())

    [notice] = notices
    assert notice.stopped_reply
    model = turn.models["default"]
    # One request: the Steer ended the resumed run after its approved call, so nothing asked the model again.
    assert len(model.requests) == 1
    [run] = turn.agent_runs()
    assert run.status is RunStatus.completed
    assert [(m.role, m.content) for m in run.messages if m.role in {"assistant", "tool"}] == [
        ("assistant", None),
        ("tool", "echoed hi"),
    ]
    old_reply = await _reply(turn, _SOURCE)
    assert old_reply.state is rl.ReplyState.COMPLETED
    assert old_reply.event_id == "$sent0"
    assert old_reply.event_id not in [call.args[1] for call in turn.bot.client.room_redact.await_args_list]
    assert set(turn.homeserver.bodies) == {"Thinking...", _APPROVAL_WAIT, _APPROVAL_FALLBACK}
    assert turn.homeserver.bodies[-1] == _APPROVAL_FALLBACK
    assert _last_edit(turn, old_reply.event_id)[STREAM_STATUS_KEY] == "completed"
    assert "error" not in turn.homeserver.statuses
    assert await turn.bot.journal_principal().approvals.approval_continuation_for_source(_SOURCE) is None
    await _assert_settled(turn, _SOURCE)

    await _dispatch(turn, notice)

    # The follow-up replies once, to the Steer, with the cut-off run's call and result and one carry-on line.
    _first, follow_up = model.requests
    assert [(m.role, m.content) for m in follow_up.messages if m.role == "tool"] == [("tool", "echoed hi")]
    text = str(follow_up.messages[-1].content)
    assert sum(content.count(_STEER_TEXT) for content in _contents(follow_up)) == 1
    assert sum(content.count(_STEER_CONTINUATION_LINE) for content in _contents(follow_up)) == 1
    assert text.index(_STEER_TEXT) < text.index(_STEER_CONTINUATION_LINE)
    new_reply = await _reply(turn, _STEER)
    assert new_reply.reply_id != old_reply.reply_id
    assert new_reply.state is rl.ReplyState.COMPLETED
    assert _answered_sources(turn) == [_SOURCE, _STEER]
    assert turn.homeserver.bodies[-1] == "Carried on."
    assert _last_edit(turn, new_reply.event_id)[STREAM_STATUS_KEY] == "completed"
    assert "error" not in turn.homeserver.statuses
    assert len(turn.agent_runs()) == 2
    await _assert_settled(turn, _STEER)
