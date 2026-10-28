"""How every agent and team reply ends, streamed or delivered whole.

Each test drives one real turn through ``ResponseRunner.generate_response`` or
``generate_team_response_helper`` with the scripted provider, the real
journal-backed outbox and reply records, and a homeserver that deduplicates by
transaction ID. Whether the turn streams is decided by the requester's presence,
as in production. A Stop goes through the user-stop reconciler a 🛑 reaction
reaches, and a process shutdown cancels the response task the way the inbox
drain does.

The tests read only what a reader of the reply sees: the reply record and its
decoded presentation, the room's writes, the durable ``FINAL`` row, the hooks
that fired, the stored conversation history, and the turn's return value.

An assertion preceded by ``# CHARACTERIZATION:`` pins behavior that looks like a
bug; flip it deliberately when it is fixed.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from structlog.testing import capture_logs

from mindroom import reply_lifecycle as rl
from mindroom.approval.tool_approval import shutdown_approval_runtime
from mindroom.cancellation import request_task_cancel
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import STREAM_STATUS_KEY
from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.event_journal import DeliveryStage, EventClass, EventKind, InboundEvent
from mindroom.history.interrupted_replay import is_interrupted_replay_run, persist_interrupted_replay_snapshot
from mindroom.history.session_context import create_scope_session_storage, resolve_bound_team_scope
from mindroom.hooks import (
    EVENT_MESSAGE_AFTER_RESPONSE,
    EVENT_MESSAGE_BEFORE_RESPONSE,
    EVENT_MESSAGE_CANCELLED,
    EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM,
    AfterResponseContext,
    BeforeResponseContext,
    CancelledResponseContext,
    FinalResponseTransformContext,
    HookRegistry,
    hook,
)
from mindroom.llm.provider import TextDelta
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.matrix.users import AgentMatrixUser
from mindroom.reply_presentation import TEAM_PLACEHOLDER, Presentation, decode_presentation, render_body
from mindroom.tool_system.agno_toolkits import Toolkit
from mindroom.turn_policy import ResponseAction
from mindroom.turn_record import TurnRecord
from tests.access_schema_support import with_current_room_member_access
from tests.ai_user_id_helpers import team_orchestrator
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    initialize_approval_store_for_test,
    install_model_providers,
    install_runtime_journal_support,
    make_matrix_client_mock,
    message_origin,
    runtime_paths_for,
    test_runtime_paths,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)
from tests.fakes.scripted_model import Await, ScriptedModel, ScriptStep, answer, calls, tool_call
from tests.response_attempt_helpers import install_direct_response_admission
from tests.response_runner_helpers import _plain_request, _target
from tests.scripted_agent_helpers import registered_toolkit, stored_runs

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.event_journal import MatrixDelivery
    from mindroom.history.types import HistoryScope
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.response_runner import ResponseRequest, ResponseRunner

pytestmark = pytest.mark.asyncio

_SOURCE = "$event"
_ROOM = "!room:localhost"
_REQUESTER = "@user:localhost"
_ECHO_TOOL = "reply_endings_echo"
_MEMBERS = ("writer", "checker")
_CANCEL_NOTE = "**[Response cancelled by user]**"
_MODEL_ERROR = "[general] ⚠️ Error: model exploded"
# What a stored run of this turn holds as its user message: the model prompt, and the bare message tag.
_MODEL_PROMPT = f'Current message:\n<msg event_id="{_SOURCE}" from="{_REQUESTER}"><![CDATA[hello]]></msg>'
_BARE_PROMPT = f'<msg event_id="{_SOURCE}" from="{_REQUESTER}"><![CDATA[hello]]></msg>'
_TEAM_HEADER = "🤝 **Team Response** (Writer, Checker):\n\n"
_EDIT_INTERVAL = 0.0
_TEAM_MEMBERS = "**Writer**: Writer fact.\n\n**Checker**: Checker fact."


class _EchoTools(Toolkit):
    """One quick tool; ``calls`` records each call so a test can tell whether it ran."""

    calls: ClassVar[list[str]] = []

    def __init__(self) -> None:
        super().__init__(name="echo", tools=[self.echo])

    async def echo(self, text: str) -> str:
        """Echo the text back.

        Args:
            text: What to echo.

        """
        type(self).calls.append(text)
        return f"echoed {text}"


@pytest.fixture
def echo_tool() -> Iterator[type[_EchoTools]]:
    """Register the echo toolkit for one test."""
    _EchoTools.calls.clear()
    with registered_toolkit(_ECHO_TOOL, _EchoTools):
        yield _EchoTools


@dataclass
class _Homeserver:
    """A Matrix server that deduplicates by transaction ID and can hold one message write until released.

    Only ``m.room.message`` writes are kept: the Stop button's reaction is not part of the reply's text.
    """

    events: dict[str, str] = field(default_factory=dict)
    writes: list[dict[str, Any]] = field(default_factory=list)
    hold_text: str | None = None
    held: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    # Text whose first write the server fails, as an unreachable homeserver does.
    fail_text: str | None = None

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
        if message_type == "m.room.message" and self.fail_text is not None and self.fail_text in _body(content):
            self.fail_text = None
            return nio.RoomSendError(message="homeserver unavailable", status_code="M_UNKNOWN")
        event_id = self.events.setdefault(tx_id or f"anon-{len(self.events)}", f"$sent{len(self.events)}")
        if message_type == "m.room.message":
            self.writes.append(content)
            if self.hold_text is not None and self.hold_text in _body(content):
                self.hold_text = None
                self.held.set()
                await self.release.wait()
        return nio.RoomSendResponse(event_id=event_id, room_id=room_id)

    @property
    def bodies(self) -> list[str]:
        """Return each write's visible body, in order."""
        return [_body(content) for content in self.writes]

    @property
    def statuses(self) -> list[str | None]:
        """Return each write's ``io.mindroom.stream_status``, in order."""
        return [content.get(STREAM_STATUS_KEY) for content in self.writes]


def _body(content: Mapping[str, Any]) -> str:
    """Return the body a reader would see, following an edit into its replacement."""
    replacement = content.get("m.new_content")
    return str((replacement if isinstance(replacement, dict) else content).get("body", ""))


@dataclass
class _Hooks:
    """The response hooks that fired, in order, with their key payload fields."""

    fired: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    suppress: bool = False
    # Text the final transform appends to the answer.
    transform_suffix: str = ""

    @property
    def names(self) -> list[str]:
        """Return the hook events that fired, in order."""
        return [name for name, _payload in self.fired]

    def payload(self, name: str) -> dict[str, Any]:
        """Return the payload of the one hook of ``name`` that fired."""
        [payload] = [payload for fired, payload in self.fired if fired == name]
        return payload


def _hook_registry(hooks: _Hooks) -> HookRegistry:
    """Return a registry whose response hooks record into ``hooks``; the before hook suppresses when asked."""

    @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
    async def before(ctx: BeforeResponseContext) -> None:
        hooks.fired.append(("before", {"text": ctx.draft.response_text, "kind": ctx.draft.response_kind}))
        if hooks.suppress:
            ctx.draft.suppress = True

    @hook(EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM)
    async def transform(ctx: FinalResponseTransformContext) -> None:
        hooks.fired.append(("transform", {"text": ctx.draft.response_text, "kind": ctx.draft.response_kind}))
        ctx.draft.response_text += hooks.transform_suffix

    @hook(EVENT_MESSAGE_AFTER_RESPONSE)
    async def after(ctx: AfterResponseContext) -> None:
        result = ctx.result
        hooks.fired.append(
            (
                "after",
                {
                    "text": result.response_text,
                    "event_id": result.response_event_id,
                    "delivery_kind": result.delivery_kind,
                    "kind": result.response_kind,
                },
            ),
        )

    @hook(EVENT_MESSAGE_CANCELLED)
    async def cancelled(ctx: CancelledResponseContext) -> None:
        info = ctx.info
        hooks.fired.append(
            (
                "cancelled",
                {
                    "event_id": info.visible_response_event_id,
                    "failure_reason": info.failure_reason,
                    "kind": info.response_kind,
                },
            ),
        )

    plugin = type(
        "PluginStub",
        (),
        {
            "name": "reply-endings",
            "discovered_hooks": (before, transform, after, cancelled),
            "discovered_automations": (),
            "entry_config": PluginEntryConfig(path="./plugins/reply-endings"),
            "plugin_order": 0,
        },
    )()
    return HookRegistry.from_plugins([plugin])


def _config(tmp_path: Path) -> Config:
    """Return the bot's config: the answering agent, and two members an ad hoc team of it can run."""
    return bind_runtime_paths(
        with_current_room_member_access(
            Config(
                agents={
                    "general": AgentConfig(display_name="General", rooms=[_ROOM]),
                    "writer": AgentConfig(display_name="Writer", role="Writes.", model="writer-model"),
                    "checker": AgentConfig(display_name="Checker", role="Checks.", model="checker-model"),
                },
                models={
                    "default": ModelConfig(provider="openai", id="test-model"),
                    "writer-model": ModelConfig(provider="openai", id="writer-model"),
                    "checker-model": ModelConfig(provider="openai", id="checker-model"),
                },
                # A finished response would otherwise persist memory through a real embedding request.
                memory={"backend": "none"},
            ),
        ),
        test_runtime_paths(tmp_path),
    )


def _new_bot(tmp_path: Path) -> AgentBot:
    config = _config(tmp_path)
    agent_user = AgentMatrixUser(
        agent_name="general",
        password=TEST_PASSWORD,
        display_name="General",
        user_id="@mindroom_general:localhost",
    )
    bot = make_test_agent_bot(agent_user, tmp_path, config, runtime_paths_for(config), rooms=[_ROOM])
    install_direct_response_admission(bot)
    bot.client = make_matrix_client_mock(user_id="@mindroom_general:localhost")
    install_runtime_journal_support(bot)
    wrap_extracted_collaborators(bot)
    return bot


@dataclass
class _Turn:
    """One bot wired to the scripted models, a fake homeserver, and recording hooks."""

    bot: AgentBot
    runner: ResponseRunner
    homeserver: _Homeserver
    hooks: _Hooks
    models: dict[str, ScriptedModel]

    async def reply(self) -> rl.Reply:
        """Return the reply record the turn's source reached."""
        reply = await self.bot.journal_principal().replies.for_sources((_SOURCE,))
        assert reply is not None
        return reply

    async def span_outcomes(self) -> list[rl.SpanOutcome | None]:
        """Return how each span of the reply ended, None for one that never ended."""
        reply = await self.reply()
        return [span.outcome for span in await self.bot.journal_principal().replies.spans(reply.reply_id)]

    async def presentation(self) -> Presentation:
        """Return the reply's decoded presentation."""
        return decode_presentation((await self.reply()).presentation)

    async def final(self) -> MatrixDelivery | None:
        """Return the turn's durable ``FINAL`` row, without claiming it."""
        return await self.bot.journal_principal().load_matrix_delivery(delivery_id=_SOURCE, stage=DeliveryStage.FINAL)

    async def pending(self) -> bool:
        """Return whether the turn's source is still pending in the journal."""
        return await self.bot.journal_principal().is_pending(_SOURCE)

    def agent_runs(self) -> list[AnyRunRecord]:
        """Return the runs stored in the agent's session for this conversation."""
        return stored_runs(self.bot.config, self.bot.runtime_paths, _target().session_id)

    def team_runs(self) -> list[AnyRunRecord]:
        """Return the runs stored in the ad hoc team's session for this conversation."""
        storage = create_scope_session_storage(
            scope=_team_scope(self),
            config=self.bot.config,
            runtime_paths=self.bot.runtime_paths,
            execution_identity=None,
        )
        session = storage.load_session(_target().session_id, "team")
        return [] if session is None else session.runs


async def _turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    streaming: bool,
    scripts: Mapping[str, Sequence[Sequence[ScriptStep]]],
    show_tool_calls: bool = True,
    tools: Sequence[str] = (),
) -> _Turn:
    """Build one bot whose models play ``scripts``, with ``$event`` admitted and its turn pending, as ingress leaves it.

    ``streaming`` sets the requester's presence, which decides the driver as it does in production.
    """
    bot = _new_bot(tmp_path)
    bot.config.agents["general"].show_tool_calls = show_tool_calls
    bot.config.agents["general"].tools = list(tools)
    # Startup makes this bot instance the owner of its replies, so a Stop finds its spans live.
    await bot._reply_runtime.start()
    await bot.journal_principal().admit(
        InboundEvent(
            event_id=_SOURCE,
            room_id=_ROOM,
            thread_id=None,
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender=_REQUESTER,
            origin_server_ts=1,
            source={},
        ),
    )
    await bot._turn_store.record_pending_turn(
        bot._turn_store.attach_response_context(
            TurnRecord.create([_SOURCE], requester_id=_REQUESTER),
            history_scope=bot._turn_store.response_history_scope(ResponseAction(kind="individual")),
            conversation_target=_target(),
        ),
    )
    # Send a progress edit for every chunk: the throttle has no idle timer, so with any interval a chunk that lands
    # just after an edit finished would wait for a later chunk that a blocked model never sends.
    progress = bot.config.defaults.streaming
    progress.update_interval = _EDIT_INTERVAL
    progress.min_update_interval = _EDIT_INTERVAL
    progress.max_idle = _EDIT_INTERVAL
    models = {name: ScriptedModel([list(steps) for steps in turns]) for name, turns in scripts.items()}
    install_model_providers(monkeypatch, models)
    homeserver = _Homeserver()
    bot.client.room_send = AsyncMock(side_effect=homeserver.room_send)
    presence = MagicMock(spec=nio.PresenceGetResponse)
    presence.presence = "online" if streaming else "offline"
    presence.last_active_ago = 0
    bot.client.get_presence = AsyncMock(return_value=presence)
    hooks = _Hooks()
    bot.hook_registry = _hook_registry(hooks)
    orchestrator = team_orchestrator(bot.config, bot.runtime_paths)
    orchestrator.agent_bots = {name: MagicMock(running=True) for name in bot.config.agents}
    orchestrator.automations = MagicMock()
    bot.orchestrator = orchestrator
    return _Turn(
        bot=bot,
        runner=unwrap_extracted_collaborator(bot._response_runner),
        homeserver=homeserver,
        hooks=hooks,
        models=models,
    )


def _team_scope(turn: _Turn) -> HistoryScope:
    return resolve_bound_team_scope(
        member_names=_MEMBERS,
        config=turn.bot.config,
        team_name="general",
        requester_user_id=_REQUESTER,
    )


async def _team_reply(turn: _Turn) -> str | None:
    return await turn.runner.generate_team_response_helper(
        _plain_request(_target()),
        member_names=list(_MEMBERS),
        team_mode="collaborate",
    )


def _team_scripts(*, consensus: Sequence[ScriptStep]) -> dict[str, list[list[ScriptStep]]]:
    """Return a collaborate team turn: the leader asks both members, they answer, and the leader plays ``consensus``."""
    return {
        "default": [calls(tool_call("delegate_task_to_members", "call-all", task="One fact")), list(consensus)],
        "writer-model": [answer("Writer fact.")],
        "checker-model": [answer("Checker fact.")],
    }


async def _wait_shown(turn: _Turn, text: str) -> None:
    """Wait until a write to the room shows ``text``."""

    async def shown() -> None:
        while not any(text in body for body in turn.homeserver.bodies):  # noqa: ASYNC110
            await asyncio.sleep(0.005)

    try:
        await asyncio.wait_for(shown(), timeout=10)
    except TimeoutError:
        msg = f"{text!r} never reached the room, which saw {turn.homeserver.bodies!r}"
        raise AssertionError(msg) from None


async def _stop(turn: _Turn) -> bool:
    """Stop the reply's event as a 🛑 reaction does: through the turn's durable user-stop reconciliation."""
    reply = await turn.reply()
    assert reply.event_id is not None
    return await turn.bot._user_stop_reconciler.finalize(reply.event_id, 7, room_id=_ROOM)


async def _past_edit_interval() -> None:
    """Yield once, so the previous chunk's edit is under way before the next chunk arrives."""
    await asyncio.sleep(0)


def _streams_then_blocks(text: str, running: asyncio.Event) -> list[ScriptStep]:
    """Return a model turn that streams ``text`` as its own edit, then waits until it is cancelled."""

    async def block() -> None:
        running.set()
        await asyncio.Event().wait()

    return [Await(_past_edit_interval), TextDelta(text), Await(block)]


async def _explode() -> None:
    msg = "model exploded"
    raise RuntimeError(msg)


def _interrupted(runs: Sequence[AnyRunRecord]) -> AnyRunRecord:
    """Return the one interrupted-replay row among ``runs``."""
    [run] = [run for run in runs if is_interrupted_replay_run(run)]
    return run


def _original_status(run: AnyRunRecord) -> str:
    return (run.metadata or {})["mindroom_original_status"]


def _messages(run: AnyRunRecord) -> list[tuple[str, object]]:
    return [(message.role, message.content) for message in run.messages or []]


def _silent_schedule(request: ResponseRequest) -> ResponseRequest:
    return replace(
        request,
        response_envelope=replace(
            request.response_envelope,
            origin=message_origin(
                sender_id=_REQUESTER,
                requester_id=_REQUESTER,
                source_kind=SILENT_SCHEDULE_SOURCE_KIND,
            ),
        ),
    )


@pytest.mark.usefixtures("echo_tool")
@pytest.mark.parametrize("show_tool_calls", [True, False], ids=["tools-shown", "tools-hidden"])
@pytest.mark.parametrize("streaming", [True, False], ids=["streamed", "whole"])
async def test_a_completed_agent_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
    show_tool_calls: bool,
) -> None:
    """Pins the agent turn that calls a tool and answers: one completed reply, its answer frozen, one completed run."""
    turn = await _turn(
        tmp_path,
        monkeypatch,
        streaming=streaming,
        show_tool_calls=show_tool_calls,
        tools=[_ECHO_TOOL],
        scripts={
            "default": [calls(tool_call("echo", "call-1", text="hi"), content="Looking. "), answer("The answer.")],
        },
    )

    assert await turn.runner.generate_response(_plain_request(_target())) == "$sent0"

    # A collected reply shows what the stream shows, tool calls hidden or not.
    body = "Looking. \n\n🔧 `echo` [1]\n\nThe answer." if show_tool_calls else "Looking. \n\nThe answer."
    reply = await turn.reply()
    assert reply.state is rl.ReplyState.COMPLETED
    assert reply.event_id == "$sent0"
    assert await turn.span_outcomes() == [rl.SpanOutcome.COMPLETED]
    presentation = await turn.presentation()
    assert [(segment.kind, segment.text) for segment in presentation.segments] == [("answer", body)]
    assert presentation.trailing_note is None
    assert turn.homeserver.bodies[0] == "Thinking..."
    assert turn.homeserver.bodies[-1] == body
    if streaming:
        assert turn.homeserver.statuses[0] == "pending"
        assert set(turn.homeserver.statuses[1:-1]) == {"streaming"}
        assert turn.homeserver.statuses[-1] == "completed"
    else:
        assert turn.homeserver.statuses == ["pending", "completed"]
    final = await turn.final()
    assert final is not None
    assert (_body(final.payload), final.payload[STREAM_STATUS_KEY]) == (body, "completed")
    # A streamed reply runs final_response_transform and skips before_response; a reply delivered whole the reverse.
    assert turn.hooks.names == (["transform", "after"] if streaming else ["before", "after"])
    assert turn.hooks.payload("after") == {"text": body, "event_id": "$sent0", "delivery_kind": "edited", "kind": "ai"}
    [run] = turn.agent_runs()
    assert isinstance(run, RunRecord)
    assert run.status is RunStatus.completed
    assert not is_interrupted_replay_run(run)
    assert run.content == "Looking.\n\nThe answer."
    assert not await turn.pending()


async def test_a_transformed_streamed_reply_reports_the_text_the_room_shows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a final transform reshapes a streamed answer, after_response reports the reshaped text it delivered."""
    turn = await _turn(tmp_path, monkeypatch, streaming=True, scripts={"default": [answer("The answer.")]})
    turn.hooks.transform_suffix = " (checked)"

    assert await turn.runner.generate_response(_plain_request(_target())) == "$sent0"

    assert turn.homeserver.bodies[-1] == "The answer. (checked)"
    assert turn.hooks.payload("after")["text"] == "The answer. (checked)"


@pytest.mark.usefixtures("echo_tool")
async def test_a_reply_keeps_the_tool_call_visibility_it_was_claimed_with(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config reload after the reply is claimed does not change whether that turn shows its tool calls."""
    turn = await _turn(
        tmp_path,
        monkeypatch,
        streaming=True,
        show_tool_calls=True,
        tools=[_ECHO_TOOL],
        scripts={
            "default": [calls(tool_call("echo", "call-1", text="hi"), content="Looking. "), answer("The answer.")],
        },
    )
    replies = turn.runner.deps.replies
    claim = replies.claim

    async def claim_then_reload(**kwargs: Any) -> object:  # noqa: ANN401
        handle = await claim(**kwargs)
        turn.bot.config.agents["general"].show_tool_calls = False
        return handle

    monkeypatch.setattr(replies, "claim", claim_then_reload)

    assert await turn.runner.generate_response(_plain_request(_target())) == "$sent0"

    assert turn.homeserver.bodies[-1] == "Looking. \n\n🔧 `echo` [1]\n\nThe answer."


async def test_a_user_stop_mid_stream_cancels_the_streamed_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins a Stop while the streamed model waits after showing text: cancelled reply, stopped history row."""
    running = asyncio.Event()
    turn = await _turn(
        tmp_path,
        monkeypatch,
        streaming=True,
        scripts={"default": [_streams_then_blocks("Partial answer", running)]},
    )
    response = asyncio.create_task(turn.runner.generate_response(_plain_request(_target())))
    await asyncio.wait_for(running.wait(), timeout=10)
    await _wait_shown(turn, "Partial answer")

    assert await _stop(turn)
    assert await asyncio.wait_for(response, timeout=10) == "$sent0"

    shown = f"Partial answer\n\n{_CANCEL_NOTE}"
    reply = await turn.reply()
    assert reply.state is rl.ReplyState.CANCELLED
    assert await turn.span_outcomes() == [rl.SpanOutcome.CANCELLED]
    presentation = await turn.presentation()
    # The span exit writes the cancel note as a note segment below what the stream showed.
    assert [(segment.kind, segment.text) for segment in presentation.segments] == [("answer", "Partial answer")]
    assert presentation.trailing_note is not None
    assert (presentation.trailing_note.note, presentation.trailing_note.text) == (rl.NoteKind.CANCELLED, _CANCEL_NOTE)
    assert (turn.homeserver.bodies[-1], turn.homeserver.statuses[-1]) == (shown, "cancelled")
    final = await turn.final()
    assert final is not None
    assert (_body(final.payload), final.payload[STREAM_STATUS_KEY]) == (shown, "cancelled")
    # A stopped stream writes no final answer of its own, so final_response_transform does not run on it.
    assert turn.hooks.fired == [
        ("cancelled", {"event_id": "$sent0", "failure_reason": "cancelled_by_user", "kind": "ai"}),
    ]
    [run] = turn.agent_runs()
    assert is_interrupted_replay_run(run)
    assert _original_status(run) == "cancelled"
    assert run.content == "Partial answer\n\n(turn stopped before completion)"
    # CHARACTERIZATION: current behavior; an interrupted row stores the bare message tag, not the model prompt.
    assert _messages(run) == [("user", _BARE_PROMPT), ("assistant", run.content)]
    assert not await turn.pending()


async def test_a_stopped_turn_holds_its_conversation_until_its_interrupted_record_is_stored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stopped turn's record can take longer to store than the stream's bounded unwind; the turn waits for it.

    The conversation's next turn reads history once this turn ends, so it must find the stopped turn there.
    """
    running = asyncio.Event()
    turn = await _turn(
        tmp_path,
        monkeypatch,
        streaming=True,
        scripts={"default": [_streams_then_blocks("Partial answer", running)]},
    )
    release = threading.Event()
    store = persist_interrupted_replay_snapshot

    def slow_store(**kwargs: Any) -> None:  # noqa: ANN401
        release.wait(timeout=30)
        store(**kwargs)

    monkeypatch.setattr("mindroom.response_turn.persist_interrupted_replay_snapshot", slow_store)
    response = asyncio.create_task(turn.runner.generate_response(_plain_request(_target())))
    try:
        await asyncio.wait_for(running.wait(), timeout=10)
        await _wait_shown(turn, "Partial answer")
        assert await _stop(turn)

        async def reply_ended() -> None:
            while "cancelled" not in turn.hooks.names:  # noqa: ASYNC110
                await asyncio.sleep(0.005)

        await asyncio.wait_for(reply_ended(), timeout=10)
        done, _pending = await asyncio.wait({response}, timeout=1)
        assert response not in done
        assert not any(is_interrupted_replay_run(run) for run in turn.agent_runs())
    finally:
        release.set()

    assert await asyncio.wait_for(response, timeout=10) == "$sent0"
    [run] = turn.agent_runs()
    assert is_interrupted_replay_run(run)


async def test_a_user_stop_while_a_whole_delivery_runs_the_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins a Stop while a reply delivered whole waits on the model: the placeholder becomes the cancel note."""
    running = asyncio.Event()
    turn = await _turn(
        tmp_path,
        monkeypatch,
        streaming=False,
        scripts={"default": [_streams_then_blocks("Partial answer", running)]},
    )
    response = asyncio.create_task(turn.runner.generate_response(_plain_request(_target())))
    await asyncio.wait_for(running.wait(), timeout=10)
    await _wait_shown(turn, "Thinking...")

    assert await _stop(turn)
    assert await asyncio.wait_for(response, timeout=10) == "$sent0"

    reply = await turn.reply()
    assert reply.state is rl.ReplyState.CANCELLED
    assert await turn.span_outcomes() == [rl.SpanOutcome.CANCELLED]
    presentation = await turn.presentation()
    # Nothing was shown, so the cancel note is the reply's only content.
    assert presentation.segments == ()
    assert presentation.trailing_note is not None
    assert (presentation.trailing_note.note, presentation.trailing_note.text) == (rl.NoteKind.CANCELLED, _CANCEL_NOTE)
    assert list(zip(turn.homeserver.bodies, turn.homeserver.statuses, strict=True)) == [
        ("Thinking...", "pending"),
        (_CANCEL_NOTE, "cancelled"),
    ]
    final = await turn.final()
    assert final is not None
    assert (_body(final.payload), final.payload[STREAM_STATUS_KEY]) == (_CANCEL_NOTE, "cancelled")
    assert turn.hooks.fired == [
        ("cancelled", {"event_id": "$sent0", "failure_reason": "cancelled_by_user", "kind": "ai"}),
    ]
    run = _interrupted(turn.agent_runs())
    assert _original_status(run) == "cancelled"
    # CHARACTERIZATION: current behavior; history keeps model text the room never showed.
    assert run.content == "Partial answer\n\n(turn stopped before completion)"
    assert _messages(run) == [("user", _BARE_PROMPT), ("assistant", run.content)]
    assert not await turn.pending()


async def test_a_stop_while_the_stream_waits_for_matrix_records_the_turn_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    echo_tool: type[_EchoTools],
) -> None:
    """Pins a Stop that lands while the stream consumer waits for a held room write, not for the model.

    With tool calls hidden, a tool start first flushes the text streamed so far,
    and that flush waits behind the held write. The model already asked for the
    tool, which never runs.
    """
    turn_requested_tool = asyncio.Event()

    async def tool_requested() -> None:
        turn_requested_tool.set()

    async def wait_for_hold() -> None:
        await turn.homeserver.held.wait()

    first = [
        TextDelta("Partial "),
        Await(wait_for_hold),
        *calls(tool_call("echo", "call-1", text="hi"), content="answer"),
        Await(tool_requested),
    ]
    turn = await _turn(
        tmp_path,
        monkeypatch,
        streaming=True,
        show_tool_calls=False,
        tools=[_ECHO_TOOL],
        scripts={"default": [first, answer("Never reached.")]},
    )
    turn.homeserver.hold_text = "Partial"
    response = asyncio.create_task(turn.runner.generate_response(_plain_request(_target())))
    await asyncio.wait_for(turn_requested_tool.wait(), timeout=10)

    assert await _stop(turn)
    turn.homeserver.release.set()
    assert await asyncio.wait_for(response, timeout=10) == "$sent0"

    assert echo_tool.calls == []
    reply = await turn.reply()
    assert reply.state is rl.ReplyState.CANCELLED
    assert (turn.homeserver.bodies[-1], turn.homeserver.statuses[-1]) == (
        f"Partial answer\n\n{_CANCEL_NOTE}",
        "cancelled",
    )
    run = _interrupted(turn.agent_runs())
    # The Stop lands while the consumer writes, outside the model's await, and the turn is still recorded stopped.
    assert _original_status(run) == "cancelled"
    assert run.content.startswith(
        "Partial answer\n\n(turn stopped before completion; 1 tool call(s) were still running)",
    )


@pytest.mark.parametrize(
    ("streaming", "text_first", "show_tool_calls"),
    [
        pytest.param(True, True, True, id="streamed-after-text"),
        pytest.param(True, False, True, id="streamed-before-text"),
        pytest.param(False, True, True, id="whole-tools-shown"),
        pytest.param(False, True, False, id="whole-tools-hidden"),
    ],
)
async def test_a_model_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
    text_first: bool,
    show_tool_calls: bool,
) -> None:
    """Pins a model that raises mid-turn: the error text becomes the answer of a completed reply."""
    turn_holder: list[_Turn] = []

    async def explode_once_shown() -> None:
        if streaming:
            await _wait_shown(turn_holder[0], "Partial answer")
        await _explode()

    script = [TextDelta("Partial answer"), Await(explode_once_shown)] if text_first else [Await(_explode)]
    turn = await _turn(
        tmp_path,
        monkeypatch,
        streaming=streaming,
        show_tool_calls=show_tool_calls,
        scripts={"default": [script]},
    )
    turn_holder.append(turn)

    assert await turn.runner.generate_response(_plain_request(_target())) == "$sent0"

    # A collected reply keeps the partial text the stream would have shown.
    partial = "Partial answer" if text_first else ""
    # The error starts a paragraph of its own below the partial text.
    shown = f"{partial}\n\n{_MODEL_ERROR}" if partial else _MODEL_ERROR
    reply = await turn.reply()
    # CHARACTERIZATION: current behavior; a failed model run ends the reply completed, its error inside the answer.
    assert reply.state is rl.ReplyState.COMPLETED
    assert await turn.span_outcomes() == [rl.SpanOutcome.COMPLETED]
    presentation = await turn.presentation()
    assert [(segment.kind, segment.text) for segment in presentation.segments] == [("answer", shown)]
    assert presentation.trailing_note is None
    # CHARACTERIZATION: current behavior; the room is told the failed reply completed.
    assert (turn.homeserver.bodies[-1], turn.homeserver.statuses[-1]) == (shown, "completed")
    final = await turn.final()
    assert final is not None
    assert final.payload[STREAM_STATUS_KEY] == "completed"
    # CHARACTERIZATION: current behavior; after_response, not message:cancelled, reports the failed reply.
    assert turn.hooks.names == (["transform", "after"] if streaming else ["before", "after"])
    assert turn.hooks.payload("after")["text"] == shown
    [run] = turn.agent_runs()
    assert is_interrupted_replay_run(run)
    assert _original_status(run) == "error"
    assert run.content == (f"{partial}\n\n" if partial else "") + "(turn failed before completion)"
    assert _messages(run) == [("user", _BARE_PROMPT), ("assistant", run.content)]
    assert not await turn.pending()


async def test_a_silent_schedule_that_reports_nothing_shows_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins a silent schedule whose model answers the no-report token: no placeholder, no write, a gone reply.

    The requester is online, so the presence check would stream; a silent schedule never does.
    """
    turn = await _turn(tmp_path, monkeypatch, streaming=True, scripts={"default": [answer("NO_REPLY")]})

    assert await turn.runner.generate_response(_silent_schedule(_plain_request(_target()))) is None

    reply = await turn.reply()
    assert reply.state is rl.ReplyState.GONE
    assert reply.event_id is None
    assert await turn.span_outcomes() == [rl.SpanOutcome.SUPPRESSED]
    assert turn.homeserver.writes == []
    assert await turn.final() is None
    assert turn.hooks.fired == [
        ("before", {"text": "NO_REPLY", "kind": "ai"}),
        ("cancelled", {"event_id": None, "failure_reason": "silent_no_report", "kind": "ai"}),
    ]
    [run] = turn.agent_runs()
    assert run.status is RunStatus.completed
    assert not is_interrupted_replay_run(run)
    assert run.content == "NO_REPLY"
    assert not await turn.pending()


async def test_a_run_whose_reply_a_hook_suppressed_still_counts_as_succeeded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run completed though delivery suppressed its reply, so memory and skill review still take it."""
    turn = await _turn(tmp_path, monkeypatch, streaming=False, scripts={"default": [answer("Hidden answer.")]})
    turn.hooks.suppress = True
    effects = AsyncMock(return_value=None)
    monkeypatch.setattr("mindroom.response_lifecycle.apply_post_response_effects", effects)

    await turn.runner.generate_response(_plain_request(_target()))

    effects.assert_awaited_once()
    final, outcome, _deps = effects.await_args.args
    assert final.suppressed
    assert outcome.run_succeeded is True


async def test_an_answer_the_homeserver_did_not_take_yet_applies_its_run_s_effects_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An answer left for the outbox to send finished its run, so memory and skill review take it as it settles.

    No delivery hook fires for it, and its sources go to the outbox with the answer.
    """
    turn = await _turn(tmp_path, monkeypatch, streaming=False, scripts={"default": [answer("Owed answer.")]})
    turn.homeserver.fail_text = "Owed answer."
    effects = AsyncMock(return_value=None)
    monkeypatch.setattr("mindroom.response_lifecycle.apply_post_response_effects", effects)
    request = replace(_plain_request(_target()), source_handoff=asyncio.Event())

    await turn.runner.generate_response(request)

    effects.assert_awaited_once()
    final, outcome, _deps = effects.await_args.args
    assert (final.terminal_status, outcome.run_succeeded) == ("suspended", True)
    assert turn.hooks.names == ["before"]
    assert request.source_handoff is not None
    assert request.source_handoff.is_set()


async def test_a_before_response_hook_that_suppresses_a_whole_reply_removes_its_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins a suppressing before_response hook: the placeholder is redacted, the answer kept only in history."""
    turn = await _turn(tmp_path, monkeypatch, streaming=False, scripts={"default": [answer("Hidden answer.")]})
    turn.hooks.suppress = True

    assert await turn.runner.generate_response(_plain_request(_target())) is None

    reply = await turn.reply()
    assert reply.state is rl.ReplyState.GONE
    assert await turn.span_outcomes() == [rl.SpanOutcome.SUPPRESSED]
    assert list(zip(turn.homeserver.bodies, turn.homeserver.statuses, strict=True)) == [("Thinking...", "pending")]
    assert [call.args[1] for call in turn.bot.client.room_redact.await_args_list] == ["$sent0"]
    assert await turn.final() is None
    assert turn.hooks.fired == [
        ("before", {"text": "Hidden answer.", "kind": "ai"}),
        ("cancelled", {"event_id": None, "failure_reason": "suppressed_by_hook", "kind": "ai"}),
    ]
    [run] = turn.agent_runs()
    assert run.status is RunStatus.completed
    assert run.content == "Hidden answer."
    assert not await turn.pending()


async def test_an_empty_prompt_ends_without_running_the_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins a whitespace-only prompt: the model never runs, nothing is written, and nothing is stored."""
    turn = await _turn(tmp_path, monkeypatch, streaming=True, scripts={"default": []})

    assert await turn.runner.generate_response(replace(_plain_request(_target()), prompt="   ")) is None

    assert turn.models["default"].requests == []
    reply = await turn.reply()
    assert reply.state is rl.ReplyState.GONE
    assert await turn.span_outcomes() == [rl.SpanOutcome.SUPPRESSED]
    assert turn.homeserver.writes == []
    assert await turn.final() is None
    assert turn.hooks.fired == [("cancelled", {"event_id": None, "failure_reason": "empty_prompt", "kind": "ai"})]
    assert turn.agent_runs() == []
    assert not await turn.pending()


async def test_a_process_shutdown_mid_stream_leaves_the_reply_for_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins an orderly process shutdown mid-stream: the reply stays unfinished and its source pending, for replay."""
    running = asyncio.Event()
    turn = await _turn(
        tmp_path,
        monkeypatch,
        streaming=True,
        scripts={"default": [_streams_then_blocks("Partial answer", running)]},
    )
    response = asyncio.create_task(turn.runner.generate_response(_plain_request(_target())))
    await asyncio.wait_for(running.wait(), timeout=10)
    await _wait_shown(turn, "Partial answer")

    request_task_cancel(response, process_shutdown=True)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(response, timeout=10)

    reply = await turn.reply()
    assert reply.state is rl.ReplyState.ACTIVE
    assert await turn.span_outcomes() == [None]
    assert (await turn.presentation()).segments == ()
    assert reply.possibly_shown is not None
    assert render_body(decode_presentation(reply.possibly_shown))[0] == "Partial answer"
    assert turn.homeserver.statuses[-1] == "streaming"
    assert await turn.final() is None
    assert turn.hooks.fired == []
    runs = turn.agent_runs()
    assert not any(is_interrupted_replay_run(run) for run in runs)
    # CHARACTERIZATION: current behavior; the agent loop still stores its own cancelled run, holding only the prompt.
    [run] = runs
    assert run.status is RunStatus.cancelled
    assert run.content is None
    assert _messages(run) == [("user", _MODEL_PROMPT)]
    assert await turn.pending()


@pytest.mark.parametrize("streaming", [True, False], ids=["streamed", "whole"])
async def test_a_completed_team_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    """Pins a collaborate team reply: each member's text under its label, in member order, then the consensus."""
    turn = await _turn(
        tmp_path,
        monkeypatch,
        streaming=streaming,
        scripts=_team_scripts(consensus=answer("Two facts.")),
    )

    assert await _team_reply(turn) == "$sent0"

    # A collected team reply is the streamed document.
    body = f"{_TEAM_HEADER}{_TEAM_MEMBERS}\n\n\n**Team Consensus**:\n\n🔧 `delegate_task_to_members` [1]\n\nTwo facts."
    reply = await turn.reply()
    assert reply.state is rl.ReplyState.COMPLETED
    assert await turn.span_outcomes() == [rl.SpanOutcome.COMPLETED]
    presentation = await turn.presentation()
    assert presentation.placeholder == TEAM_PLACEHOLDER
    assert [(segment.kind, segment.text) for segment in presentation.segments] == [("answer", body)]
    assert turn.homeserver.bodies[0] == TEAM_PLACEHOLDER
    assert (turn.homeserver.bodies[-1], turn.homeserver.statuses[-1]) == (body, "completed")
    final = await turn.final()
    assert final is not None
    assert (_body(final.payload), final.payload[STREAM_STATUS_KEY]) == (body, "completed")
    # A team reply follows the agent's hook rule: final_response_transform when streamed, before_response when whole.
    assert turn.hooks.names == (["transform", "after"] if streaming else ["before", "after"])
    assert turn.hooks.payload("after") == {
        "text": body,
        "event_id": "$sent0",
        "delivery_kind": "edited",
        "kind": "team",
    }
    runs = turn.team_runs()
    [team_run] = [run for run in runs if isinstance(run, TeamRunRecord)]
    assert team_run.status is RunStatus.completed
    assert team_run.content == "Two facts."
    assert sorted(run.content for run in runs if isinstance(run, RunRecord)) == ["Checker fact.", "Writer fact."]
    assert not await turn.pending()


@pytest.mark.parametrize("streaming", [True, False], ids=["streamed", "whole"])
async def test_a_team_leader_failure_shows_the_error_in_place_of_the_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    """Pins a team whose leader fails after its members answered: the error replaces the document, streamed or whole."""
    turn = await _turn(tmp_path, monkeypatch, streaming=streaming, scripts=_team_scripts(consensus=[Await(_explode)]))

    assert await _team_reply(turn) == "$sent0"

    body = turn.homeserver.bodies[-1]
    assert "model exploded" in body
    assert "Writer fact." not in body
    [team_run] = [run for run in turn.team_runs() if isinstance(run, TeamRunRecord)]
    assert is_interrupted_replay_run(team_run)
    assert _original_status(team_run) == "error"


async def test_a_user_stop_mid_stream_cancels_the_team_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins a Stop while the team leader streams its consensus: cancelled reply, stopped team history row."""
    running = asyncio.Event()
    turn = await _turn(
        tmp_path,
        monkeypatch,
        streaming=True,
        scripts=_team_scripts(consensus=_streams_then_blocks("Partial consensus", running)),
    )
    with capture_logs() as logs:
        response = asyncio.create_task(_team_reply(turn))
        await asyncio.wait_for(running.wait(), timeout=10)
        await _wait_shown(turn, "Partial consensus")

        assert await _stop(turn)
        assert await asyncio.wait_for(response, timeout=10) == "$sent0"

    shown = (
        f"{_TEAM_HEADER}{_TEAM_MEMBERS}\n\n\n**Team Consensus**:\n\n🔧 `delegate_task_to_members` [1]\n\n"
        f"Partial consensus\n\n{_CANCEL_NOTE}"
    )
    reply = await turn.reply()
    assert reply.state is rl.ReplyState.CANCELLED
    assert await turn.span_outcomes() == [rl.SpanOutcome.CANCELLED]
    presentation = await turn.presentation()
    # The span exit writes the cancel note as a note segment below the team document the stream showed.
    assert [(segment.kind, segment.text) for segment in presentation.segments] == [
        ("answer", shown.removesuffix(f"\n\n{_CANCEL_NOTE}")),
    ]
    assert presentation.trailing_note is not None
    assert presentation.trailing_note.note is rl.NoteKind.CANCELLED
    assert (turn.homeserver.bodies[-1], turn.homeserver.statuses[-1]) == (shown, "cancelled")
    assert turn.hooks.fired == [
        ("cancelled", {"event_id": "$sent0", "failure_reason": "cancelled_by_user", "kind": "team"}),
    ]
    run = _interrupted(turn.team_runs())
    assert isinstance(run, TeamRunRecord)
    assert _original_status(run) == "cancelled"
    assert run.content.startswith(
        f"{_TEAM_MEMBERS}\n\n\n**Team Consensus**:\n\nPartial consensus\n\n"
        "(turn stopped before completion; 1 tool call(s) had finished)",
    )
    assert _messages(run)[0] == ("user", _BARE_PROMPT)
    errors = [entry["event"] for entry in logs if entry["log_level"] == "error"]
    # A team Stop is a cancellation, as an agent's is, not an error.
    assert errors == []
    assert not await turn.pending()


@pytest.mark.usefixtures("echo_tool")
@pytest.mark.parametrize("streaming", [True, False], ids=["streamed", "whole"])
async def test_an_approval_pause_holds_the_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    """Pins a tool call that needs approval: the reply pauses waiting, its source stays pending, no hook fires."""
    turn = await _turn(
        tmp_path,
        monkeypatch,
        streaming=streaming,
        tools=[_ECHO_TOOL],
        scripts={"default": [calls(tool_call("echo", "call-1", text="hi"), content="Before approval")]},
    )
    turn.bot.config.tool_approval = ToolApprovalConfig(
        rules=[ApprovalRuleConfig(match="echo", action="require_approval")],
    )

    async def prepare_event(_room: str, _thread: str | None, content: dict) -> dict:
        return content

    async def send_card(_delivery: MatrixDelivery) -> str:
        return "$approval-card"

    initialize_approval_store_for_test(
        turn.bot.runtime_paths,
        prepare_event=prepare_event,
        send_delivery=send_card,
        resolve_delivery=AsyncMock(return_value=None),
        cards=turn.bot._journal_store.principal("router@shared"),
        transport_sender=lambda: "@router:localhost",
        sending_device=lambda: "DEVICE",
    )
    try:
        assert await turn.runner.generate_response(_plain_request(_target())) is None
    finally:
        await shutdown_approval_runtime()

    shown = "Before approval\n\n🔧 `echo` [1] ⏳"
    reply = await turn.reply()
    assert reply.state is rl.ReplyState.PAUSED
    assert await turn.span_outcomes() == [rl.SpanOutcome.PAUSED]
    presentation = await turn.presentation()
    assert [(segment.kind, segment.text) for segment in presentation.segments] == [("answer", shown)]
    assert presentation.trailing_note is not None
    assert (presentation.trailing_note.note, presentation.trailing_note.text) == (
        rl.NoteKind.APPROVAL_WAIT,
        "Waiting for approval: `echo`",
    )
    assert (turn.homeserver.bodies[-1].rstrip(), turn.homeserver.statuses[-1]) == (shown, "approval_pending")
    assert await turn.final() is None
    assert turn.hooks.fired == []
    [run] = turn.agent_runs()
    assert run.status is RunStatus.paused
    assert not is_interrupted_replay_run(run)
    assert run.content == "Before approval"
    assert await turn.pending()
