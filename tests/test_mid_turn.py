"""Opt-in judgments decide whether queued messages need a mid-turn handoff."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Literal, cast

import pytest
from agno.media import Image as AgnoImage
from agno.tools.function import ToolResult
from pydantic import ValidationError
from structlog.testing import capture_logs

from mindroom.agent_loop.model_loop import EMPTY_REQUEST, LoopHooks
from mindroom.agent_loop.run import resume_agent, run_agent
from mindroom.ai_runtime import (
    queued_message_signal_context,
    queued_notice_after_tool_batch,
    queued_notice_on_resume_entry,
    told_to_pause,
)
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.mid_turn import MidTurnConfig
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.constants import ATTACHMENT_IDS_KEY, ORIGINAL_SENDER_KEY
from mindroom.credentials.manager import get_runtime_shared_credentials_manager
from mindroom.custom_tools.job import JobTools
from mindroom.event_journal import ApprovalCall, approval_arguments_digest
from mindroom.history.request_messages import build_resume_request
from mindroom.judgment.client import JudgmentClient
from mindroom.judgment.conversation import conversation_context_before_sources
from mindroom.judgment.state import MAX_REQUEST_BYTES, JudgmentMessage
from mindroom.judgment.typesafe import _PINNED_MODEL
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.message_target import MessageTarget
from mindroom.mid_turn import (
    MID_TURN_MODE_KEY,
    MidTurnGate,
    MidTurnMode,
    MidTurnOutcome,
    QueuedMessage,
    mid_turn_mode,
)
from mindroom.mid_turn_judgment import create_mid_turn_gate
from mindroom.model_defaults import OPENAI_DECISIONS_MODEL
from mindroom.redaction import redact_sensitive_text
from mindroom.response_lifecycle import QueuedHumanNoticeReservation, ResponseLifecycleCoordinator, _QueuedMessageState
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_jobs.consumption import set_consumption_storage
from mindroom.tool_jobs.conversation import ConversationKey
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.follow_ups import follow_up_jobs
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import BackgroundOutcome, register_background_runtime
from mindroom.tool_system.events import ToolTraceEntry, build_tool_trace_content
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.agent_loop.helpers import collect, context, external, gated, spec, toolkit
from tests.conftest import install_model_providers, make_visible_message, request_envelope, test_runtime_paths
from tests.cpu_budget_helpers import cpu_budget
from tests.delegation_helpers import _delegate_runtime_context, _runtime_paths
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    always_owned,
    answer_of,
    job_actor,
    job_tool,
    lookup,
    pending_outcomes,
    run_job_turn,
    start_job,
    tool_job_runtime,
    toolkit_job_tools,
    wait_for_status,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

    from mindroom.judgment.state import JudgmentRequest
    from mindroom.session_storage.store import ConversationStore
    from mindroom.tool_jobs.execution_authority import JobTool
    from mindroom.tool_jobs.runtime import ToolJobRuntime
    from mindroom.tool_system.tool_function import AdaptedToolkit

_NOTICE = "WRAP UP NOW"
_NOTICE_HOOKS = LoopHooks(
    after_tool_batch=(queued_notice_after_tool_batch(_NOTICE),),
    on_resume_entry=(queued_notice_on_resume_entry(_NOTICE),),
)
# The hooks of a run that owns its reply: a requester's Steer or a judge's "cannot wait" ends it at a completed batch.
_STEERING_HOOKS = LoopHooks(
    after_tool_batch=(queued_notice_after_tool_batch(_NOTICE, steering=True),),
    on_resume_entry=(queued_notice_on_resume_entry(_NOTICE, steering=True),),
)


def _has_notice(messages: list[Message] | tuple[Message, ...]) -> bool:
    return any(message.content == _NOTICE for message in messages)


class _ResumedRequests:
    """Resumed requests as the queued-notice resume hook leaves them for the provider."""

    def __init__(self) -> None:
        self.sent: list[list[Message]] = []
        self._hook = queued_notice_on_resume_entry(_NOTICE)

    async def send(self, messages: list[Message] | None = None) -> list[Message]:
        request = messages if messages is not None else _resumed_messages()
        await self._hook(request, ())
        self.sent.append(request)
        return request


async def _run_turn(
    model: ScriptedModel,
    *toolkits: AdaptedToolkit,
    persist: Callable[[RunRecord], Awaitable[None]] | None = None,
    hooks: LoopHooks = _NOTICE_HOOKS,
) -> RunRecord:
    """Run one turn of "Do the task" on the owned loop with the queued-notice hooks a reply installs."""
    _, record = await collect(
        run_agent(
            spec(model, *toolkits),
            [Message(role="user", content="Do the task")],
            ctx=context(),
            hooks=hooks,
            persist=persist,
            request_template=EMPTY_REQUEST,
        ),
    )
    return record


async def _resume_turn(
    model: ScriptedModel,
    paused: RunRecord,
    *toolkits: AdaptedToolkit,
    external_results: Mapping[str, str],
    approved: Sequence[ApprovalCall] = (),
    hooks: LoopHooks = _NOTICE_HOOKS,
) -> RunRecord:
    """Resume a paused turn with the results of its externally executed calls and the approvals it waited for."""
    _, record = await collect(
        resume_agent(
            spec(model, *toolkits),
            paused,
            build_resume_request(paused=paused.messages, summary=None, history=[]),
            approved=approved,
            denied={},
            external_results=external_results,
            context=lambda run_id: context(run_id=run_id),
            hooks=hooks,
            persist=None,
            request_template=EMPTY_REQUEST,
        ),
    )
    return record


def _resumed_messages() -> list[Message]:
    return [
        Message(role="user", content="Do the task"),
        Message(
            role="assistant",
            tool_calls=[
                {"id": "call", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            ],
        ),
        Message(role="tool", content="Private result", tool_call_id="call", tool_name="read_file"),
    ]


@pytest.mark.parametrize("backend", ["llm", "typesafe"])
def test_mid_turn_config_accepts_both_backends_and_validates_model_aliases(backend: str) -> None:
    """Mid-turn settings belong to an agent, not the room or another responder."""
    judgment = {"provider": backend, **({"model": "default"} if backend == "llm" else {})}
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "synthetic", "id": "test"}},
            "agents": {
                "helper": {"display_name": "Helper", "mid_turn": {"judgment": judgment}},
                "ordinary": {"display_name": "Ordinary"},
            },
        },
    )
    assert config.agents["helper"].mid_turn is not None
    assert config.agents["helper"].mid_turn.judgment.provider == backend
    assert config.agents["ordinary"].mid_turn is None
    with pytest.raises(ValidationError, match="Unknown judgment model for agent"):
        Config.model_validate(
            {
                "agents": {
                    "helper": {
                        "display_name": "Helper",
                        "mid_turn": {"judgment": {"provider": "llm", "model": "missing"}},
                    },
                },
            },
        )
    with pytest.raises(ValidationError):
        Config.model_validate({"agents": {"helper": {"display_name": "Helper", "mid_turn": {}}}})


@pytest.mark.parametrize(
    "settings",
    [
        None,
        {"judgment": {"provider": "typesafe"}},
        {"judgment": {"provider": "llm", "model": "default"}, "defer_reaction": "👀"},
    ],
)
def test_mid_turn_survives_authored_config_round_trip(settings: dict[str, object] | None) -> None:
    """Saving config preserves explicit disables and the agent's judgment settings."""
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "synthetic", "id": "test"}},
            "agents": {"helper": {"display_name": "Helper", "mid_turn": settings}},
        },
    )
    authored = config.authored_model_dump()
    assert authored["agents"]["helper"]["mid_turn"] == settings
    assert Config.model_validate(authored).agents["helper"].mid_turn == config.agents["helper"].mid_turn


def test_retired_room_mid_turn_is_rejected() -> None:
    """An old room configuration must not silently enable every agent in the room."""
    with pytest.raises(ValidationError, match="room_mid_turn"):
        Config.model_validate({"room_mid_turn": {"lobby": {"judgment": {"provider": "typesafe"}}}})


@pytest.mark.parametrize("room", ["!first:localhost", "!adhoc:localhost"])
@pytest.mark.parametrize("agent", ["helper", "ordinary", "team", "missing"])
def test_mid_turn_follows_only_the_configured_agent(tmp_path: Path, room: str, agent: str) -> None:
    """Agent opt-in follows its Matrix user across rooms without recruiting other agents or teams."""
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "synthetic", "id": "test"}},
            "agents": {
                "helper": {
                    "display_name": "Helper",
                    "mid_turn": {"judgment": {"provider": "llm", "model": "default"}, "defer_reaction": "👀"},
                },
                "ordinary": {"display_name": "Ordinary"},
            },
            "teams": {"team": {"display_name": "Team", "role": "Work together", "agents": ["helper"]}},
        },
    )
    gate = create_mid_turn_gate(
        config,
        test_runtime_paths(tmp_path),
        request_envelope(room_id=room, agent_name=agent),
        prompt="Do the task",
        has_media=False,
    )
    assert (gate is not None) is (agent == "helper")


@pytest.mark.parametrize(
    ("content", "mode"),
    [
        pytest.param(None, MidTurnMode.AUTO, id="no-content"),
        pytest.param("steer", MidTurnMode.AUTO, id="content-not-a-mapping"),
        pytest.param({"body": "hi"}, MidTurnMode.AUTO, id="missing"),
        pytest.param({MID_TURN_MODE_KEY: "STEER"}, MidTurnMode.AUTO, id="uppercase"),
        pytest.param({MID_TURN_MODE_KEY: " steer"}, MidTurnMode.AUTO, id="padded"),
        pytest.param({MID_TURN_MODE_KEY: "later"}, MidTurnMode.AUTO, id="unknown"),
        pytest.param({MID_TURN_MODE_KEY: 1}, MidTurnMode.AUTO, id="integer"),
        pytest.param({MID_TURN_MODE_KEY: ["steer"]}, MidTurnMode.AUTO, id="list"),
        pytest.param({MID_TURN_MODE_KEY: {"mode": "steer"}}, MidTurnMode.AUTO, id="dict"),
        pytest.param({MID_TURN_MODE_KEY: None}, MidTurnMode.AUTO, id="null"),
        pytest.param({MID_TURN_MODE_KEY: "steer"}, MidTurnMode.STEER, id="steer"),
        pytest.param({MID_TURN_MODE_KEY: "queue"}, MidTurnMode.QUEUE, id="queue"),
        pytest.param({MID_TURN_MODE_KEY: "auto"}, MidTurnMode.AUTO, id="auto"),
    ],
)
def test_mid_turn_mode_reads_known_values_and_rejects_malformed_content(
    content: object,
    mode: MidTurnMode,
) -> None:
    """Only an exact known value picks a mode; anything else a client sends is Auto."""
    assert MID_TURN_MODE_KEY == "io.mindroom.mid_turn"
    # A client can send any JSON, so the reader gets content its type does not promise.
    assert mid_turn_mode(cast("Mapping[str, object] | None", content)) is mode
    # The queued entry defaults to Auto, so a mode-less admission keeps today's behavior.
    assert QueuedMessage("$event", "hi").mode is MidTurnMode.AUTO


def test_queued_snapshot_keeps_text_until_exact_event_is_consumed() -> None:
    """The judge needs a stable snapshot without taking ownership of queued dispatch."""
    state = _QueuedMessageState()
    assert state.add_waiting_human_message("$first", text="First")
    assert not state.add_waiting_human_message("$first", text="Duplicate")
    assert state.add_waiting_human_message("$second", text="Second")
    snapshot = state.pending_message_snapshot()
    assert [(message.event_id, message.text) for message in snapshot] == [("$first", "First"), ("$second", "Second")]
    state.consume_waiting_human_message("$first")
    assert [message.text for message in state.pending_message_snapshot()] == ["Second"]
    assert len(snapshot) == 2
    assert state.has_pending_human_messages()


@pytest.mark.parametrize("reaction", ["", "   ", "\n", "x" * 65, 123])
def test_defer_reaction_rejects_invalid_keys(reaction: object) -> None:
    """Deferred acknowledgements use bounded, nonblank Matrix reaction keys."""
    with pytest.raises(ValidationError):
        MidTurnConfig.model_validate({"judgment": {"provider": "typesafe"}, "defer_reaction": reaction})


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["finish", "wrap_up", "error", "queue_changed", "cancel", "reaction_error"])
async def test_defer_reaction_requires_accepted_current_decision(outcome: str) -> None:
    """Never acknowledge a failed, cancelled, negative, or superseded judgment."""
    state = _QueuedMessageState()
    state.add_waiting_human_message("$first", text="Thanks")
    acknowledgements: list[str] = []

    async def acknowledge(event_id: str) -> None:
        acknowledgements.append(event_id)
        if outcome == "reaction_error":
            msg = "Matrix unavailable"
            raise RuntimeError(msg)

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        if outcome == "error":
            raise TimeoutError
        if outcome == "cancel":
            raise asyncio.CancelledError
        if outcome == "queue_changed":
            state.add_waiting_human_message("$correction", text="Stop")
        return outcome != "wrap_up"

    gate = MidTurnGate(active_text="Do the task", evaluate=evaluate, on_defer=acknowledge)
    resumed = _ResumedRequests()
    with queued_message_signal_context(state, mid_turn_gate=gate):
        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await resumed.send()
        else:
            await resumed.send()
            await resumed.send()
            if outcome == "finish":
                state.add_waiting_human_message("$second", text="No rush")
                await resumed.send()
    assert acknowledgements == (
        ["$first", "$second"] if outcome == "finish" else ["$first"] if outcome == "reaction_error" else []
    )
    if outcome == "reaction_error":
        assert not any(_has_notice(request) for request in resumed.sent)
    assert state.has_pending_human_messages()


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [("Thanks",), ("Thanks", "No rush")])
@pytest.mark.parametrize("change", ["correction", "handoff"])
async def test_queue_change_during_reaction_still_requests_wrap_up(
    queued: tuple[str, ...],
    change: Literal["correction", "handoff"],
) -> None:
    """A correction or a concurrent handoff while Matrix acknowledges any reaction must still cause handoff."""
    state = _QueuedMessageState()
    for index, text in enumerate(queued):
        state.add_waiting_human_message(f"$queued{index}", text=text)
    acknowledgements: list[str] = []

    async def acknowledge(event_id: str) -> None:
        acknowledgements.append(event_id)
        if change == "correction":
            state.add_waiting_human_message("$correction", text="Stop")
        else:
            context.notice_fired = True

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        return True

    resumed = _ResumedRequests()
    with queued_message_signal_context(
        state,
        mid_turn_gate=MidTurnGate(active_text="Do the task", evaluate=evaluate, on_defer=acknowledge),
    ) as context:
        await resumed.send()
        assert context.notice_fired
    assert acknowledgements == ["$queued0"]
    assert _has_notice(resumed.sent[0])


@pytest.mark.asyncio
async def test_a_wait_judges_again_a_queue_that_changed_during_its_defer_reaction() -> None:
    """A message that arrives while Matrix acknowledges another is judged with it, so one that can wait ends no wait."""
    state = _QueuedMessageState()
    judged: list[tuple[str | None, ...]] = []

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        judged.append(tuple(message.text for message in state.pending_message_snapshot()))
        return "Stop" not in judged[-1]

    async def acknowledge(event_id: str) -> None:
        if event_id == "$thanks":
            state.add_waiting_human_message("$norush", text="No rush")

    state.mid_turn_gate = MidTurnGate(active_text="Do the task", evaluate=evaluate, on_defer=acknowledge)
    waiter = asyncio.create_task(state.wait())
    state.add_waiting_human_message("$thanks", text="Thanks")
    for _ in range(100):
        await asyncio.sleep(0)
    assert not waiter.done()
    assert judged == [("Thanks",), ("Thanks", "No rush")]
    state.add_waiting_human_message("$stop", text="Stop")
    await asyncio.wait_for(waiter, 5)


@pytest.mark.asyncio
async def test_a_wait_judges_again_a_queue_that_lost_a_message_during_its_judgment() -> None:
    """A decision counts only for the queue it judged, so a queue that shrank meanwhile is judged again at once."""
    state = _QueuedMessageState()
    judged: list[tuple[str | None, ...]] = []

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        judged.append(tuple(message.text for message in state.pending_message_snapshot()))
        if judged[-1] == ("Thanks", "Stop"):
            # The thanks no longer waits for this reply, as when its own turn is dropped.
            state.consume_waiting_human_message("$thanks")
        return "Stop" not in judged[-1]

    state.mid_turn_gate = MidTurnGate(active_text="Do the task", evaluate=evaluate)
    waiter = asyncio.create_task(state.wait())
    state.add_waiting_human_message("$thanks", text="Thanks")
    for _ in range(100):
        await asyncio.sleep(0)
    state.add_waiting_human_message("$stop", text="Stop")
    for _ in range(100):
        await asyncio.sleep(0)
    assert waiter.done()
    assert judged == [("Thanks",), ("Thanks", "Stop"), ("Stop",)]


@pytest.mark.asyncio
async def test_queued_message_freezes_visible_progress_at_admission() -> None:
    """Later stream edits must not change the context of an earlier human message."""
    evidence: list[str] = []

    async def evaluate(request: JudgmentRequest) -> bool | None:
        assert request.body is not None
        evidence.append(json.loads(request.body)["state"]["conversation"][0]["text"])
        return True

    gate = MidTurnGate(active_text="Do the task", evaluate=evaluate)
    state = _QueuedMessageState(mid_turn_gate=gate)
    read = ToolTraceEntry(
        type="tool_call_completed",
        tool_name="read_file",
        args_preview="path=a.py",
        result_preview="ok",
    )
    gate.record_visible_response("I found the file.\n🔧 `read_file` [1]", (read,))
    state.add_waiting_human_message("$first", text="Use that file")
    first = state.pending_message_snapshot()
    gate.record_visible_response("I found the file. Now editing it.", ())
    assert await gate.decide(first, current=lambda: True) is MidTurnOutcome.WAIT
    assert evidence[0] == (
        "<active_request>\nDo the task\n</active_request>\n"
        "<queued_message>\nUse that file\n</queued_message>\n"
        '<visible_response>\nI found the file.\n<tool name="read_file" input="path=a.py" output="ok"/>\n</visible_response>'
    )
    assert await gate.decide(first, current=lambda: True) is MidTurnOutcome.WAIT
    assert len(evidence) == 1
    state.add_waiting_human_message("$second", text="Stop editing")
    assert await gate.decide(state.pending_message_snapshot(), current=lambda: True) is MidTurnOutcome.WAIT
    assert evidence[1].endswith("<visible_response>\nI found the file. Now editing it.\n</visible_response>")
    assert "completed_tools" not in evidence[0]


@pytest.mark.asyncio
async def test_visible_progress_keeps_untraced_markers_and_multiline_tool_output() -> None:
    """Untraced marker-shaped lines are the agent's text, and redacted multi-line tool output stays judgeable."""
    evidence: list[str] = []

    async def evaluate(request: JudgmentRequest) -> bool | None:
        assert request.body is not None
        evidence.append(json.loads(request.body)["state"]["conversation"][-1]["text"])
        return True

    example = "Example:\n\n🔧 `shell` [1]\n\nThe number identifies a tool."
    output = "services:\n  db:\n    environment:\n      POSTGRES_PASSWORD: hunter2\n      POSTGRES_USER: app\n"
    shell = ToolTraceEntry(
        type="tool_call_completed",
        tool_name="shell",
        args_preview="command=cat docker-compose.yml",
        result_preview=redact_sensitive_text(output),
    )
    for text, trace in ((example, ()), ("Reading.\n\n🔧 `shell` [1]\n\nDone.", (shell,))):
        gate = MidTurnGate(active_text="Do the task", evaluate=evaluate, conversation_context=())
        gate.record_visible_response(text, trace)
        state = _QueuedMessageState(mid_turn_gate=gate)
        state.add_waiting_human_message("$queued", text="Thanks")
        assert await gate.decide(state.pending_message_snapshot(), current=lambda: True) is MidTurnOutcome.WAIT
    assert example in evidence[0]
    assert "POSTGRES_USER: app" in evidence[1]
    assert "hunter2" not in evidence[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "progress",
    [None, '{"password": "fake_secret_for_test"}'],
    ids=["unknown", "secret"],
)
async def test_unknown_or_unsafe_visible_progress_keeps_wrap_up(progress: str | None) -> None:
    """Unavailable or sensitive user-visible context must not leave the runtime."""

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        pytest.fail("Incomplete visible progress must not reach inference")

    gate = MidTurnGate(active_text="Do the task", evaluate=evaluate)
    gate.visible_response_text = progress
    state = _QueuedMessageState(mid_turn_gate=gate)
    state.add_waiting_human_message("$queued", text="Thanks")
    assert await gate.decide(state.pending_message_snapshot(), current=lambda: True) is MidTurnOutcome.PAUSE


@pytest.mark.asyncio
async def test_finish_reuses_exact_queue_but_rechecks_a_new_message() -> None:
    """A later correction must not inherit approval given to unrelated chatter."""
    requests: list[dict] = []

    async def evaluate(request: JudgmentRequest) -> bool | None:
        assert request.body is not None
        requests.append(json.loads(request.body))
        return len(requests) == 1

    gate = MidTurnGate(active_text="Install the dependencies", evaluate=evaluate)
    first = (QueuedMessage("$one", "Thanks"),)
    assert await gate.decide(first, current=lambda: True) is MidTurnOutcome.WAIT
    assert await gate.decide(first, current=lambda: True) is MidTurnOutcome.WAIT
    assert len(requests) == 1
    assert (
        await gate.decide(
            (*first, QueuedMessage("$two", "Use the other version")),
            current=lambda: True,
        )
        is MidTurnOutcome.CANNOT_WAIT
    )
    assert len(requests) == 2
    evidence = requests[0]["state"]["conversation"][0]["text"]
    assert "Install the dependencies" in evidence
    assert "Thanks" in evidence
    assert "$one" not in evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [None, "", "x" * 16001, "[attachments: report.pdf]", "api_key=sk-" + "x" * 48])
async def test_incomplete_or_sensitive_queue_keeps_wrap_up(text: str | None) -> None:
    """Missing or unsafe queued evidence must never authorize continued tool use."""

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        pytest.fail("Incomplete context must not reach inference")

    gate = MidTurnGate(active_text="Do the original task", evaluate=evaluate)
    assert await gate.decide((QueuedMessage("$one", text),), current=lambda: True) is MidTurnOutcome.PAUSE


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [False, None])
async def test_nonapproval_keeps_wrap_up(decision: bool | None) -> None:
    """A negative or abstaining judge cannot suppress the default notice."""

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        return decision

    gate = MidTurnGate(active_text="Do the task", evaluate=evaluate)
    outcome = await gate.decide((QueuedMessage("$one", "Please change it"),), current=lambda: True)
    assert outcome is (MidTurnOutcome.CANNOT_WAIT if decision is False else MidTurnOutcome.PAUSE)


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
async def test_json_credentials_are_checked_before_evidence_encoding(*, active: bool) -> None:
    """JSON escaping must not hide credentials from the redaction boundary."""

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        pytest.fail("Credentials must not reach the judgment backend")

    credential_text = '{"password": "fake_secret_for_test"}'
    gate = MidTurnGate(active_text=credential_text if active else "Do the task", evaluate=evaluate)
    assert (
        await gate.decide(
            (QueuedMessage("$new", "Thanks" if active else credential_text),),
            current=lambda: True,
        )
        is MidTurnOutcome.PAUSE
    )


@pytest.mark.asyncio
async def test_judgment_cancellation_propagates_without_caching_approval() -> None:
    """Stopping the active response must stop its judge too."""
    entered = asyncio.Event()

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError

    gate = MidTurnGate(active_text="Do the task", evaluate=evaluate)
    task = asyncio.create_task(gate.decide((QueuedMessage("$one", "Thanks"),), current=lambda: True))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", [False, True])
@pytest.mark.parametrize("terminal", [False, True])
async def test_real_tool_loop_judges_before_next_provider_request(
    *,
    finish: bool,
    terminal: bool,
) -> None:
    """Both provider paths must receive a notice only when the judge requests wrap-up."""
    state = _QueuedMessageState()
    evidence: list[dict] = []

    def queue_followup() -> str:
        """Queue a follow-up while the turn works."""
        gate.record_visible_response(
            "Work started.\n🔧 `queue_followup` [1]",
            (ToolTraceEntry(type="tool_call_started", tool_name="queue_followup"),),
        )
        state.add_waiting_human_message("$new", text="Thanks for doing this")
        return "Private tool result must not reach the judge"

    async def evaluate(request: JudgmentRequest) -> bool | None:
        assert request.body is not None
        evidence.append(json.loads(request.body))
        return finish

    gate = MidTurnGate(active_text="Do the task", evaluate=evaluate)
    state.mid_turn_gate = gate
    model = ScriptedModel([calls(tool_call("queue_followup", "call_1")), answer("Done")])
    tools = toolkit(queue_followup, stop_after_call=True) if terminal else toolkit(queue_followup)
    with queued_message_signal_context(state, mid_turn_gate=gate) as context:
        await _run_turn(model, tools)
        assert context.notice_fired is (not finish and not terminal)
    assert len(evidence) == (0 if terminal else 1)
    assert "Private tool result" not in json.dumps(evidence)
    assert ("queue_followup" in json.dumps(evidence)) is (not terminal)
    assert len(model.requests) == (1 if terminal else 2)
    assert _has_notice(model.requests[-1].messages) is (not finish and not terminal)
    assert {message.event_id for message in state.pending_message_snapshot()} == {"$new"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mixed", [False, True])
async def test_pending_approval_is_not_judged_as_completed_work(*, mixed: bool) -> None:
    """Unresolved tool calls must not become evidence of completed actions."""
    executed: list[str] = []
    judgments: list[JudgmentRequest] = []
    state = _QueuedMessageState()
    state.add_waiting_human_message("$queued", text="Thanks")

    def approved_action() -> str:
        """Act once approved."""
        executed.append("approved_action")
        return "Done"

    def ordinary_action() -> str:
        """Read something."""
        executed.append("ordinary_action")
        return "Read complete"

    async def evaluate(request: JudgmentRequest) -> bool | None:
        judgments.append(request)
        return True

    names = ["ordinary_action", "approved_action"] if mixed else ["approved_action"]
    model = ScriptedModel([calls(*(tool_call(name, name) for name in names))])
    with queued_message_signal_context(state, mid_turn_gate=MidTurnGate(active_text="Do the task", evaluate=evaluate)):
        record = await _run_turn(model, gated(approved_action), toolkit(ordinary_action))
    assert record.status == RunStatus.paused
    assert executed == (["ordinary_action"] if mixed else [])
    assert judgments == []


@pytest.mark.asyncio
@pytest.mark.parametrize("remove", [False, True])
async def test_queue_change_during_judgment_cannot_approve_unseen_input(*, remove: bool) -> None:
    """Only the exact judged queue can finish; a cancelled queue needs no notice."""
    state = _QueuedMessageState()
    state.add_waiting_human_message("$first", text="Thanks")

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        if remove:
            state.consume_waiting_human_message("$first")
        else:
            state.add_waiting_human_message("$correction", text="Use a different version")
        return True

    resumed = _ResumedRequests()
    with queued_message_signal_context(state, mid_turn_gate=MidTurnGate(active_text="Do the task", evaluate=evaluate)):
        await resumed.send()
    assert _has_notice(resumed.sent[0]) is (not remove)


@pytest.mark.asyncio
async def test_wrap_up_remains_settled_after_later_queue_changes() -> None:
    """Once told to wrap up, later acknowledgements must not reverse the handoff."""
    state = _QueuedMessageState()
    state.add_waiting_human_message("$first", text="Change the task")
    judged: list[JudgmentRequest] = []

    async def evaluate(request: JudgmentRequest) -> bool | None:
        judged.append(request)
        return len(judged) > 1

    resumed = _ResumedRequests()
    with queued_message_signal_context(state, mid_turn_gate=MidTurnGate(active_text="Do the task", evaluate=evaluate)):
        await resumed.send()
        state.add_waiting_human_message("$second", text="Thanks")
        await resumed.send()
    assert len(judged) == 1
    assert all(_has_notice(request) for request in resumed.sent)


@pytest.mark.asyncio
async def test_resumed_tools_do_not_expose_invisible_execution_details() -> None:
    """Approval results do not add model-private execution details to visible context."""
    state = _QueuedMessageState()
    state.add_waiting_human_message("$queued", text="Thanks")
    evidence: list[str] = []

    async def evaluate(request: JudgmentRequest) -> bool | None:
        assert request.body is not None
        evidence.append(json.loads(request.body)["state"]["conversation"][0]["text"])
        return False

    messages = _resumed_messages()
    messages[-1].tool_call_error = True
    messages[-1].content = "Denied"
    resumed = _ResumedRequests()
    with queued_message_signal_context(state, mid_turn_gate=MidTurnGate(active_text="Do the task", evaluate=evaluate)):
        await resumed.send(messages)
    assert "completed_tools" not in evidence[0]
    assert _has_notice(resumed.sent[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["llm", "typesafe"])
@pytest.mark.parametrize("outcome", ["finish", "wrap_up", "invalid", "timeout"])
async def test_configured_backends_control_resumed_turns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    outcome: str,
) -> None:
    """Backend errors retain the actual notice; valid approvals suppress it."""

    async def post(_client: JudgmentClient, _body: bytes) -> bytes:
        if outcome == "timeout":
            await asyncio.Event().wait()
        return json.dumps(
            {
                "model": _PINNED_MODEL,
                "usage": {"input_tokens": 20, "output_tokens": 1},
                "answers": {
                    "interrupt_current_turn": {
                        "type": "noul",
                        "noul": ("bad" if outcome == "invalid" else 0.1 if outcome == "finish" else 0.9),
                    },
                },
            },
        ).encode()

    monkeypatch.setattr(JudgmentClient, "_post", post)

    async def time_out() -> None:
        raise TimeoutError

    judge = ScriptedModel(
        [
            [Await(time_out)]
            if outcome == "timeout"
            else answer("invalid" if outcome == "invalid" else json.dumps({"decision": outcome != "finish"})),
        ],
    )
    install_model_providers(monkeypatch, {"cheap": judge})
    settings = {"provider": backend, "timeout_seconds": 0.02}
    if backend == "llm":
        settings["model"] = "cheap"
    config = Config.model_validate(
        {
            "models": {"cheap": {"provider": "synthetic", "id": "test"}},
            "agents": {"test_agent": {"display_name": "Test", "mid_turn": {"judgment": settings}}},
        },
    )
    paths = replace(test_runtime_paths(tmp_path), process_env={"TYPESAFE_API_KEY": "synthetic"})
    gate = create_mid_turn_gate(
        config,
        paths,
        request_envelope(prompt="Do the task"),
        prompt="Do the task",
        has_media=False,
    )
    assert gate is not None
    state = _QueuedMessageState()
    state.add_waiting_human_message("$new", text="Thanks")
    resumed = _ResumedRequests()
    with queued_message_signal_context(state, mid_turn_gate=gate):
        await resumed.send()
    assert _has_notice(resumed.sent[0]) is (outcome != "finish")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_existing_tool_checkpoint_survives_judgment_and_cancellation(*, cancel: bool) -> None:
    """Adding a judgment must not discard the stored results of executed tools."""
    state = _QueuedMessageState()
    entered = asyncio.Event()
    stored: list[RunRecord] = []

    def work() -> str:
        """Do the work."""
        state.add_waiting_human_message("$new", text="Change the task")
        return "Completed action"

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        return False

    async def persist(record: RunRecord) -> None:
        stored.append(record)

    model = ScriptedModel([calls(tool_call("work", "call")), answer("Done")])

    async def run() -> None:
        with queued_message_signal_context(
            state,
            mid_turn_gate=MidTurnGate(active_text="Do the task", evaluate=evaluate),
        ):
            await _run_turn(model, toolkit(work), persist=persist)

    task = asyncio.create_task(run())
    await entered.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    [record] = stored
    assert record.status == (RunStatus.cancelled if cancel else RunStatus.completed)
    assert "Completed action" in [message.get_content_string() for message in record.messages]
    assert len(model.requests) == (1 if cancel else 2)
    assert _has_notice(model.requests[-1].messages) is (not cancel)


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", [False, True])
@pytest.mark.parametrize("media", [False, True])
async def test_batch_with_paused_delegation_follows_the_judgment(*, interrupt: bool, media: bool) -> None:
    """A delegation paused beside other calls must not request a wrap-up the judge did not ask for."""
    state = _QueuedMessageState()
    judgments: list[JudgmentRequest] = []

    def work() -> ToolResult:
        """Do the work."""
        state.add_waiting_human_message("$new", text="Thanks")
        images = [AgnoImage(content=b"image", mime_type="image/png")] if media else None
        return ToolResult(content="Completed action", images=images)

    def delegate() -> str:
        """Hand the subtask to a child."""
        return "Child result"

    async def evaluate(request: JudgmentRequest) -> bool | None:
        judgments.append(request)
        return not interrupt

    tools = (toolkit(work), external(delegate))
    model = ScriptedModel([calls(tool_call("work", "work"), tool_call("delegate", "child")), answer("Done")])
    with queued_message_signal_context(
        state,
        mid_turn_gate=MidTurnGate(active_text="Do the task", evaluate=evaluate),
    ):
        # The loop runs the ordinary call and pauses the delegation for its external driver.
        paused = await _run_turn(model, *tools)
        assert paused.status == RunStatus.paused
        assert len(model.requests) == 1
        assert judgments == []
        assert not any(_has_notice([message]) for message in paused.messages)
        # Generated media follow the ordinary call's result as a user-role message.
        assert (paused.messages[-1].role == "user") is media
        # The driver resumes the run with the child's result, completing the batch for the judge.
        await _resume_turn(model, paused, *tools, external_results={"child": "Child result"})
    assert len(judgments) == 1
    assert _has_notice(model.requests[-1].messages) is interrupt


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["judge", "defer_reaction"])
@pytest.mark.parametrize("resumed", [False, True])
async def test_a_failing_judge_or_defer_reaction_costs_no_reply(*, failure: str, resumed: bool) -> None:
    """A judge that errors wraps the turn up and a defer reaction Matrix refuses is skipped; the reply finishes.

    The judgment runs after a completed tool batch, or, when the batch paused for a delegation, once the resume
    completes it.
    """
    state = _QueuedMessageState()
    reactions: list[str] = []
    judgments: list[JudgmentRequest] = []

    def work() -> str:
        """Do the work."""
        state.add_waiting_human_message("$new", text="Thanks")
        return "Completed action"

    def delegate() -> str:
        """Hand the subtask to a child."""
        return "Child result"

    async def evaluate(request: JudgmentRequest) -> bool | None:
        judgments.append(request)
        if failure == "judge":
            msg = "judgment backend unavailable"
            raise RuntimeError(msg)
        return True

    async def react(event_id: str) -> None:
        reactions.append(event_id)
        msg = "Matrix unavailable"
        raise RuntimeError(msg)

    tools = (toolkit(work), external(delegate))
    batch = (tool_call("work", "work"), *((tool_call("delegate", "child"),) if resumed else ()))
    model = ScriptedModel([calls(*batch), answer("Done")])
    gate = MidTurnGate(active_text="Do the task", evaluate=evaluate, on_defer=react)
    with queued_message_signal_context(state, mid_turn_gate=gate):
        record = await _run_turn(model, *tools)
        if resumed:
            assert record.status == RunStatus.paused
            record = await _resume_turn(model, record, *tools, external_results={"child": "Child result"})
    assert record.status == RunStatus.completed
    assert record.content == "Done"
    assert len(judgments) == 1
    assert reactions == ([] if failure == "judge" else ["$new"])
    # A failed judgment wraps the turn up; a decision to finish stands when only its reaction failed.
    assert _has_notice(model.requests[-1].messages) is (failure == "judge")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("probability", "threshold", "finish"),
    [
        (0.05, 0.8, True),
        (0.2, 0.8, True),
        (0.21, 0.8, False),
        (0.7, 0.8, False),
        (0.9, 0.8, False),
        (0.1, 0.95, False),
        (0.0, 1.0, True),
        (1.0, 0.0, True),
    ],
)
@pytest.mark.parametrize("provider", ["typesafe", "openai_decisions"])
async def test_interrupt_probability_preserves_continuation_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    probability: float,
    threshold: float,
    *,
    finish: bool,
) -> None:
    """A low-confidence no-interruption answer must still request a handoff on every probability backend."""

    async def post(_client: JudgmentClient, body: bytes) -> bytes:
        questions = json.loads(body)["questions"]
        if provider == "typesafe":
            question_id = next(iter(questions))
            model, answers = _PINNED_MODEL, {question_id: {"type": "noul", "noul": probability}}
        else:
            question_id = questions[0]["name"]
            model = OPENAI_DECISIONS_MODEL
            answers = [{"type": "predicate", "name": question_id, "probability": probability}]
        assert question_id == "interrupt_current_turn"
        return json.dumps(
            {"model": model, "usage": {"input_tokens": 20, "output_tokens": 1}, "answers": answers},
        ).encode()

    monkeypatch.setattr(JudgmentClient, "_post", post)
    config = Config.model_validate(
        {
            "agents": {
                "test_agent": {
                    "display_name": "Test",
                    "mid_turn": {
                        "judgment": {"provider": provider, "threshold": threshold},
                    },
                },
            },
        },
    )
    paths = replace(test_runtime_paths(tmp_path), process_env={"TYPESAFE_API_KEY": "synthetic"})
    get_runtime_shared_credentials_manager(paths).save_credentials("openai", {"api_key": "synthetic"})
    gate = create_mid_turn_gate(config, paths, request_envelope(), prompt="Do the task", has_media=False)
    assert gate is not None
    assert (
        await gate.decide((QueuedMessage("$new", "Thanks"),), current=lambda: True) is MidTurnOutcome.WAIT
    ) is finish


@pytest.mark.asyncio
async def test_mid_turn_preserves_longer_public_conversation_within_byte_budget() -> None:
    """Earlier instructions survive repeated continuations without truncating to the last few turns."""
    context = tuple(JudgmentMessage("user", f"Accepted task constraint {i}") for i in range(12))
    captured = []

    async def evaluate(request: JudgmentRequest) -> bool | None:
        assert request.body is not None
        captured.append(json.loads(request.body)["state"]["conversation"])
        return True

    gate = MidTurnGate(active_text="Continue", evaluate=evaluate, conversation_context=context)
    assert await gate.decide((QueuedMessage("$new", "Thanks"),), current=lambda: True) is MidTurnOutcome.WAIT
    assert captured[0][:-1] == [{"role": "user", "text": message.text} for message in context]


def test_traced_agent_reply_reaches_the_judge_with_tool_tags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Judges read what each tool call did, never the bare display markers."""
    monkeypatch.setattr(
        "mindroom.judgment.conversation.current_internal_sender_ids",
        lambda *_: frozenset({"@mindroom_code:localhost"}),
    )
    trace = [
        ToolTraceEntry(
            type="tool_call_completed",
            tool_name="shell",
            args_preview="cmd=pytest",
            result_preview="3 failed",
        ),
    ]
    history = [
        make_visible_message(body="Fix the tests", event_id="$root"),
        make_visible_message(
            sender="@mindroom_code:localhost",
            body="Running them.\n\n🔧 `shell` [1]\n\nThree fail.",
            event_id="$reply",
            content={"msgtype": "m.text", **(build_tool_trace_content(trace) or {})},
        ),
        make_visible_message(body="Continue", event_id="$source"),
    ]
    context = conversation_context_before_sources(
        history,
        source_event_ids=("$source",),
        thread_id="$root",
        config=Config(),
        runtime_paths=test_runtime_paths(tmp_path),
    )
    assert context is not None
    assert (
        context[1].text == 'Running them.\n\n<tool name="shell" input="cmd=pytest" output="3 failed"/>\n\nThree fail.'
    )


def test_relayed_messages_keep_their_original_author_role(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A thread move copy of an agent reply stays an assistant turn; a relayed person or the internal user stays a user."""
    agents = {"@mindroom_router:localhost", "@mindroom_research:localhost"}
    monkeypatch.setattr(
        "mindroom.judgment.conversation.current_internal_sender_ids",
        lambda *_: frozenset({*agents, "@mindroom_user:localhost"}),
    )
    monkeypatch.setattr("mindroom.judgment.conversation.mindroom_user_id", lambda *_: "@mindroom_user:localhost")
    history = [
        make_visible_message(
            sender="@mindroom_router:localhost",
            body="Find the release notes",
            event_id="$root",
            content={"msgtype": "m.text", ORIGINAL_SENDER_KEY: "@alice:localhost"},
        ),
        make_visible_message(
            sender="@mindroom_router:localhost",
            body="I found the release notes.",
            event_id="$copy",
            content={"msgtype": "m.text", ORIGINAL_SENDER_KEY: "@mindroom_research:localhost"},
        ),
        make_visible_message(
            sender="@mindroom_router:localhost",
            body="Daily: check the open PRs",
            event_id="$automation",
            content={"msgtype": "m.text", ORIGINAL_SENDER_KEY: "@mindroom_user:localhost"},
        ),
        make_visible_message(body="Continue", event_id="$source"),
    ]
    assert conversation_context_before_sources(
        history,
        source_event_ids=("$source",),
        thread_id="$root",
        config=Config(),
        runtime_paths=test_runtime_paths(tmp_path),
    ) == (
        JudgmentMessage("user", "Find the release notes"),
        JudgmentMessage("assistant", "I found the release notes."),
        JudgmentMessage("user", "Daily: check the open PRs"),
    )


def test_earlier_media_reaches_the_judge_as_a_placeholder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A thread holding files or images is still judged, each shown only by its file name and caption."""
    history = [
        make_visible_message(body="report.pdf", event_id="$root", content={"msgtype": "m.file"}),
        make_visible_message(body="Summarize this report", event_id="$ask"),
        make_visible_message(
            sender="@mindroom_helper:localhost",
            body="Here is the chart",
            event_id="$chart",
            content={"msgtype": "m.image", "filename": "chart.png"},
        ),
        make_visible_message(event_id="$clip", content={"msgtype": "m.video"}),
        make_visible_message(event_id="$photo", content={"msgtype": "m.image", "filename": "photo.png"}),
        make_visible_message(body="x", event_id="$odd", content={"msgtype": ["m.text"]}),
        make_visible_message(body="Use these too", event_id="$more", content={ATTACHMENT_IDS_KEY: ["att_1"]}),
        make_visible_message(body="Continue", event_id="$source"),
    ]
    monkeypatch.setattr(
        "mindroom.judgment.conversation.current_internal_sender_ids",
        lambda *_: frozenset({"@mindroom_helper:localhost"}),
    )

    def context() -> tuple[JudgmentMessage, ...] | None:
        return conversation_context_before_sources(
            history,
            source_event_ids=("$source",),
            thread_id="$root",
            config=Config(),
            runtime_paths=test_runtime_paths(tmp_path),
        )

    assert context() == (
        JudgmentMessage("user", "[file: report.pdf]"),
        JudgmentMessage("user", "Summarize this report"),
        JudgmentMessage("assistant", "[image: chart.png]\nHere is the chart"),
        JudgmentMessage("user", "[video]"),
        JudgmentMessage("user", "[image: photo.png]"),
        JudgmentMessage("user", "[media: x]"),
        JudgmentMessage("user", "Use these too\n[with attachments]"),
    )
    # An empty text message or a long-text preview whose full text was never fetched is still unreadable history.
    for unreadable in (
        make_visible_message(event_id="$ask"),
        make_visible_message(
            body="Preview",
            event_id="$ask",
            content={
                "msgtype": "m.file",
                "url": "mxc://localhost/full-text",
                "io.mindroom.long_text": {"version": 2, "encoding": "matrix_event_content_json"},
            },
        ),
    ):
        history[1] = unreadable
        assert context() is None


@pytest.mark.asyncio
async def test_earlier_attachment_annotation_does_not_block_judgment() -> None:
    """An earlier reply that repeats an attachment annotation is background, like a media placeholder."""
    requests: list[JudgmentRequest] = []

    async def evaluate(request: JudgmentRequest) -> bool | None:
        requests.append(request)
        return True

    annotation = '[attachments: att_1 (file, "report.pdf")]'
    gate = MidTurnGate(
        active_text="Continue",
        evaluate=evaluate,
        conversation_context=(JudgmentMessage("assistant", f"Saved the report {annotation}"),),
    )
    assert await gate.decide((QueuedMessage("$new", "Thanks"),), current=lambda: True) is MidTurnOutcome.WAIT
    assert len(requests) == 1
    # The same annotation in a queued message still means an attachment the judge cannot open.
    assert await gate.decide((QueuedMessage("$newer", annotation),), current=lambda: True) is MidTurnOutcome.PAUSE


def _skip_reasons(logs: list[dict]) -> list[str]:
    return [log["reason"] for log in logs if log["event"] == "Mid-turn judgment skipped"]


@pytest.mark.asyncio
async def test_long_background_reaches_the_judge_as_its_start_and_end() -> None:
    """A long earlier reply or reply snapshot no longer disables the judge; the request and queued text stay whole."""
    requests: list[dict] = []

    async def evaluate(request: JudgmentRequest) -> bool | None:
        assert request.body is not None
        requests.append(json.loads(request.body))
        return True

    gate = MidTurnGate(
        active_text="Triage the next batch",
        evaluate=evaluate,
        conversation_context=(
            JudgmentMessage("user", "Triage the batch"),
            JudgmentMessage("assistant", "Report start. " + "x" * 80_000 + " Report end."),
        ),
    )
    state = _QueuedMessageState(mid_turn_gate=gate)
    gate.record_visible_response("Progress start. " + "z" * 50_000 + " Progress end.", ())
    queued = "Is this still running? " + "q" * 3_000
    state.add_waiting_human_message("$queued", text=queued)
    with capture_logs() as logs:
        assert await gate.decide(state.pending_message_snapshot(), current=lambda: True) is MidTurnOutcome.WAIT
    assert _skip_reasons(logs) == []
    conversation = requests[0]["state"]["conversation"]
    evidence = conversation[-1]["text"]
    assert conversation[0]["text"] == "Triage the batch"
    assert evidence.startswith(
        f"<active_request>\nTriage the next batch\n</active_request>\n<queued_message>\n{queued}\n",
    )
    visible = evidence.split("<visible_response>\n")[1].removesuffix("\n</visible_response>")
    clipped = (conversation[1]["text"], visible)
    for text, label in zip(clipped, ("Report", "Progress"), strict=True):
        assert text.startswith(f"{label} start.")
        assert text.endswith(f"{label} end.")
        assert "characters omitted" in text
        assert len(text) < 2_100


@pytest.mark.asyncio
@pytest.mark.parametrize(("count", "size"), [(100, 10), (30, 1_900)], ids=["many", "large"])
async def test_judge_sees_the_newest_conversation_that_fits(count: int, size: int) -> None:
    """Older messages give way to newer ones under the message cap and the request byte limit."""
    context = tuple(JudgmentMessage("user", f"{i:03} " + "x" * size) for i in range(count))
    requests: list[JudgmentRequest] = []

    async def evaluate(request: JudgmentRequest) -> bool | None:
        requests.append(request)
        return True

    gate = MidTurnGate(active_text="Continue", evaluate=evaluate, conversation_context=context)
    assert await gate.decide((QueuedMessage("$new", "Thanks"),), current=lambda: True) is MidTurnOutcome.WAIT
    assert requests[0].body is not None
    assert len(requests[0].body) <= MAX_REQUEST_BYTES
    sent = [message["text"] for message in json.loads(requests[0].body)["state"]["conversation"][:-1]]
    assert sent == [message.text for message in context[-len(sent) :]]
    assert len(sent) == 63 if count == 100 else 0 < len(sent) < count


@pytest.mark.asyncio
async def test_credential_across_a_clip_cut_keeps_wrap_up() -> None:
    """Redaction scans past each clip cut, so the kept half of a split credential never reaches the judge."""

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        pytest.fail("Credentials must not reach the judgment backend")

    # The kept end starts hundreds of characters into the token, where it no longer looks like a credential.
    token = "Authorization: Bearer eyJ" + "".join(chr(ord("a") + i * 7 % 26) for i in range(900))
    text = "x" * 20_000 + token + "x" * 600
    gate = MidTurnGate(
        active_text="Do the task",
        evaluate=evaluate,
        conversation_context=(JudgmentMessage("user", text),),
    )
    with capture_logs() as logs:
        assert await gate.decide((QueuedMessage("$new", "Thanks"),), current=lambda: True) is MidTurnOutcome.PAUSE
    assert _skip_reasons(logs) == ["history_unjudgeable"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [False, True], ids=["history", "reply"])
async def test_megabyte_texts_cost_only_what_the_judge_sees(*, reply: bool) -> None:
    """Huge earlier messages and reply snapshots are scanned only around their clipped text, keeping the loop free."""
    requests: list[JudgmentRequest] = []

    async def evaluate(request: JudgmentRequest) -> bool | None:
        requests.append(request)
        return True

    body = "a=" * 1_000_000
    gate = MidTurnGate(
        active_text="Do the task",
        evaluate=evaluate,
        conversation_context=() if reply else tuple(JudgmentMessage("user", body) for _ in range(7)),
    )
    state = _QueuedMessageState(mid_turn_gate=gate)
    gate.record_visible_response(body if reply else "", ())
    state.add_waiting_human_message("$queued", text="Thanks")
    with cpu_budget(0.25):
        assert await gate.decide(state.pending_message_snapshot(), current=lambda: True) is MidTurnOutcome.WAIT
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("newest", [False, True], ids=["gave-way", "kept"])
async def test_only_conversation_that_reaches_the_judge_is_checked_for_credentials(*, newest: bool) -> None:
    """A credential in older conversation that gives way to newer messages no longer forces a wrap-up."""
    requests: list[JudgmentRequest] = []

    async def evaluate(request: JudgmentRequest) -> bool | None:
        requests.append(request)
        return True

    credential = JudgmentMessage("user", '{"password": "fake_secret_for_test"}')
    newer = tuple(JudgmentMessage("user", f"{i:03} " + "x" * 1_900) for i in range(30))
    gate = MidTurnGate(
        active_text="Continue",
        evaluate=evaluate,
        conversation_context=(*newer, credential) if newest else (credential, *newer),
    )
    outcome = await gate.decide((QueuedMessage("$new", "Thanks"),), current=lambda: True)
    assert outcome is (MidTurnOutcome.PAUSE if newest else MidTurnOutcome.WAIT)
    assert all(request.body is not None and b"fake_secret_for_test" not in request.body for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ({"conversation_context": None}, "history_unavailable"),
        ({"pending": tuple(QueuedMessage(f"${i}", "Thanks") for i in range(9))}, "queued_message_count"),
        ({"pending": (QueuedMessage("$new", None),)}, "queued_message_unjudgeable"),
        ({"pending": (QueuedMessage("$new", "x" * 16_001),)}, "queued_message_unjudgeable"),
        ({"active_text": None}, "active_request_unjudgeable"),
        ({"active_text": "x" * 16_001}, "active_request_unjudgeable"),
        ({"pending": (QueuedMessage("$new", "Thanks", None),)}, "visible_response_unavailable"),
        ({"pending": (QueuedMessage("$new", "x" * 15_000),)}, "essential_input_too_large"),
        ({"evaluate_error": True}, "judge_error"),
    ],
    ids=["history", "count", "missing", "oversized", "media", "long-request", "progress", "request", "error"],
)
async def test_skipped_judgment_logs_its_reason(setup: dict, reason: str) -> None:
    """Every wrap-up without a judge decision says why, once per queue snapshot."""

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        if setup.get("evaluate_error"):
            msg = "private input"
            raise RuntimeError(msg)
        pytest.fail("A skipped judgment must not reach inference")

    gate = MidTurnGate(
        active_text=setup.get("active_text", "Do the task"),
        evaluate=evaluate,
        conversation_context=setup.get("conversation_context", ()),
    )
    pending = setup.get("pending", (QueuedMessage("$new", "Thanks"),))
    with capture_logs() as logs:
        assert await gate.decide(pending, current=lambda: True) is MidTurnOutcome.PAUSE
        assert await gate.decide(pending, current=lambda: True) is MidTurnOutcome.PAUSE
    assert _skip_reasons(logs) == [reason]
    assert "private input" not in str(logs)


_REQUESTER = "@alice:localhost"
_OTHER = "@bob:localhost"


def _between_people(
    event_id: str,
    text: str | None = "Did you get the slides?",
    visible: str | None = "",
) -> QueuedMessage:
    """Another person's message that mentions nobody."""
    return QueuedMessage(event_id, text, visible, _OTHER, mentions_nobody=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ({"conversation_context": None}, "history_unavailable"),
        ({"count": 9}, "queued_message_count"),
        ({"active_text": None}, "active_request_unjudgeable"),
        ({"text": None}, "queued_message_unjudgeable"),
        ({"visible": None}, "visible_response_unavailable"),
        ({"conversation_context": (JudgmentMessage("user", "api_key=sk-" + "x" * 48),)}, "history_unjudgeable"),
        ({"text": "x" * 15_000}, "essential_input_too_large"),
        ({"verdict": "error"}, "judge_error"),
        ({"verdict": None}, "no_answer"),
        ({"judge": False}, "only_waiting_on_jobs"),
    ],
    ids=[
        "history",
        "count",
        "active",
        "queued",
        "progress",
        "unjudgeable-history",
        "request",
        "error",
        "no-answer",
        "jobs",
    ],
)
@pytest.mark.parametrize("queue", ["others", "with_requester", "with_tagged", "requester_not_a_person"])
async def test_an_unjudged_queue_of_other_peoples_untagged_messages_waits(
    setup: dict,
    reason: str,
    queue: str,
) -> None:
    """Unjudged, the reply wraps up for a queue unless only other people's messages that mention nobody wait in it.

    A message of the reply's requester, or one that mentions the agent, wraps the reply up as before, and so does any
    queue of a reply no person asked for, and of a reply that only waits on its jobs.
    """

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        if setup.get("verdict") == "error":
            msg = "private input"
            raise RuntimeError(msg)
        if "verdict" in setup:
            return setup["verdict"]
        pytest.fail("A skipped judgment must not reach inference")

    pending = [
        _between_people(f"${i}", setup.get("text", "Did you get the slides?"), setup.get("visible", ""))
        for i in range(setup.get("count", 2))
    ]
    if queue == "with_requester":
        pending[-1] = replace(pending[-1], requester_id=_REQUESTER)
    if queue == "with_tagged":
        pending[-1] = replace(pending[-1], mentions_nobody=False)
    gate = MidTurnGate(
        active_text=setup.get("active_text", "Do the task"),
        evaluate=evaluate,
        conversation_context=setup.get("conversation_context", ()),
        requester_id=None if queue == "requester_not_a_person" else _REQUESTER,
    )
    judge = setup.get("judge", True)
    waits = queue == "others" and judge
    with capture_logs() as logs:
        outcome = await gate.decide(tuple(pending), current=lambda: True, judge=judge)
        # No explicit verdict is fabricated: a skipped or failed judgment never reads as one.
        assert outcome is (MidTurnOutcome.WAIT if waits else MidTurnOutcome.PAUSE)
    skips = [log for log in logs if log["event"] == "Mid-turn judgment skipped"]
    assert [(log["reason"], log["outcome"]) for log in skips] == [(reason, "waits" if waits else "wraps_up")]
    assert "private input" not in str(logs)


@pytest.mark.asyncio
@pytest.mark.parametrize("requester", [_REQUESTER, None], ids=["person", "not-a-person"])
async def test_the_judge_sees_who_sent_each_queued_message_by_role_only(requester: str | None) -> None:
    """Each queued message reaches the judge labelled as the requester's or another person's, never with a user ID."""
    bodies: list[str] = []

    async def evaluate(request: JudgmentRequest) -> bool | None:
        assert request.body is not None
        bodies.append(request.body.decode())
        return True

    gate = MidTurnGate(active_text="Do the task", evaluate=evaluate, requester_id=requester)
    pending = (QueuedMessage("$mine", "Thanks", "", _REQUESTER), _between_people("$theirs"))
    assert await gate.decide(pending, current=lambda: True) is MidTurnOutcome.WAIT
    (body,) = bodies
    evidence = json.loads(body)["state"]["conversation"][0]["text"]
    if requester is None:
        assert "<queued_message>\nThanks" in evidence
        assert "<queued_message>\nDid you get the slides?" in evidence
    else:
        assert '<queued_message from="requester">\nThanks' in evidence
        assert '<queued_message from="another person">\nDid you get the slides?' in evidence
    assert "alice" not in body
    assert "bob" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [True, False], ids=["can-wait", "cannot-wait"])
async def test_a_judged_message_from_another_person_follows_the_judge(*, verdict: bool) -> None:
    """Another person's message that the judge says cannot wait still interrupts; one that can wait waits."""

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        return verdict

    gate = MidTurnGate(active_text="Do the task", evaluate=evaluate, requester_id=_REQUESTER)
    with capture_logs() as logs:
        assert await gate.decide((_between_people("$stop", "Stop, that is the wrong file"),), current=lambda: True) is (
            MidTurnOutcome.WAIT if verdict else MidTurnOutcome.CANNOT_WAIT
        )
    assert _skip_reasons(logs) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "outcome"),
    [
        ("can_wait", MidTurnOutcome.WAIT),
        ("cannot_wait", MidTurnOutcome.CANNOT_WAIT),
        ("no_answer", MidTurnOutcome.PAUSE),
        ("error", MidTurnOutcome.PAUSE),
        ("timeout", MidTurnOutcome.PAUSE),
        ("incomplete", MidTurnOutcome.PAUSE),
        ("redacted", MidTurnOutcome.PAUSE),
        ("unjudgeable", MidTurnOutcome.PAUSE),
        ("skip", MidTurnOutcome.PAUSE),
    ],
)
async def test_mid_turn_outcome_distinguishes_negative_from_no_answer_error_timeout_and_skip(
    case: str,
    outcome: MidTurnOutcome,
) -> None:
    """Only the judge's own "cannot wait" is one; a requester's message without a verdict pauses the reply."""
    verdicts = {"can_wait": True, "cannot_wait": False, "no_answer": None}

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        if case == "error":
            msg = "private input"
            raise RuntimeError(msg)
        if case == "timeout":
            raise TimeoutError
        if case in verdicts:
            return verdicts[case]
        pytest.fail("A skipped judgment must not reach inference")

    text = {"incomplete": None, "redacted": "api_key=sk-" + "x" * 48}.get(case, "Stop, wrong file")
    message = QueuedMessage("$msg", text, None if case == "unjudgeable" else "", _REQUESTER, mentions_nobody=True)
    gate = MidTurnGate(active_text="Do the task", evaluate=evaluate, requester_id=_REQUESTER)
    with capture_logs() as logs:
        assert await gate.decide((message,), current=lambda: True, judge=case != "skip") is outcome
    assert "private input" not in str(logs)


@pytest.mark.asyncio
async def test_explicit_outcome_survives_cache_and_only_waiting_shortcut_does_not_fabricate_it() -> None:
    """A cached "cannot wait" stays one, while a reply only waiting on jobs releases its wait without any verdict."""
    verdicts = [False, True]

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        return verdicts.pop(0)

    gate = MidTurnGate(active_text="Do the task", evaluate=evaluate, requester_id=_REQUESTER)
    stop = (QueuedMessage("$stop", "Stop, wrong file", "", _REQUESTER),)
    assert await gate.decide(stop, current=lambda: True) is MidTurnOutcome.CANNOT_WAIT
    assert await gate.decide(stop, current=lambda: True) is MidTurnOutcome.CANNOT_WAIT
    assert verdicts == [True]
    # The release asks no judge and is no verdict, and it leaves the verdict on the same queue as it was.
    assert await gate.decide(stop, current=lambda: True, judge=False) is MidTurnOutcome.PAUSE
    assert await gate.decide(stop, current=lambda: True) is MidTurnOutcome.CANNOT_WAIT
    thanks = (QueuedMessage("$thanks", "Thanks", "", _REQUESTER),)
    assert await gate.decide(thanks, current=lambda: True) is MidTurnOutcome.WAIT
    assert verdicts == []
    # A queue that could wait for the work that ran then is not let wait by a reply that only waits on jobs.
    assert await gate.decide(thanks, current=lambda: True, judge=False) is MidTurnOutcome.PAUSE
    assert await gate.decide(thanks, current=lambda: True) is MidTurnOutcome.PAUSE
    # Nor is a queue no judge ever read.
    unread = (QueuedMessage("$unread", "Thanks again", "", _REQUESTER),)
    assert await gate.decide(unread, current=lambda: True, judge=False) is MidTurnOutcome.PAUSE
    assert await gate.decide(unread, current=lambda: True) is MidTurnOutcome.PAUSE


@pytest.mark.asyncio
async def test_acknowledging_a_queued_message_is_one_best_effort_attempt_per_event() -> None:
    """A failed reaction costs no reply and is not retried, and each event is acknowledged at most once."""
    attempts: list[str] = []

    async def acknowledge(event_id: str) -> None:
        attempts.append(event_id)
        msg = "Matrix unavailable"
        raise RuntimeError(msg)

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        pytest.fail("Acknowledging asks no judge")

    gate = MidTurnGate(active_text="Do the task", evaluate=evaluate, on_defer=acknowledge)
    with capture_logs() as logs:
        await gate.acknowledge_queued(QueuedMessage("$queued", "Later, please"))
        await gate.acknowledge_queued(QueuedMessage("$queued", "Later, please, edited"))
        await gate.acknowledge_queued(QueuedMessage("$other", "Also later"))
    assert attempts == ["$queued", "$other"]
    assert [log["event"] for log in logs] == ["mid_turn_reaction_failed"] * 2


@pytest.mark.asyncio
async def test_report_stop_preserves_as_read_one_sender_and_previously_deferred_rules() -> None:
    """A "cannot wait" publishes, only at the wrap-up, the judged messages still waiting as read, none it let wait.

    A message appended during the judgment, which the judge never read, and one edited after it, are not marked.
    """
    published: list[tuple[QueuedMessage, ...]] = []
    queue = _QueuedMessageState()

    def on_stop(judged: tuple[QueuedMessage, ...]) -> None:
        published.append(judged)
        queue.mark_stopped_reply(judged)

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        if len(queue.pending_message_snapshot()) == 1:
            return True
        queue.add_waiting_human_message("$appended", text="and the tests", requester_id=_REQUESTER)
        return False

    gate = MidTurnGate(active_text="Do the task", evaluate=evaluate, requester_id=_REQUESTER, on_stop=on_stop)
    queue.mid_turn_gate = gate
    queue.add_waiting_human_message("$waited", text="Thanks", requester_id=_REQUESTER)
    assert await gate.decide(queue.pending_message_snapshot(), current=lambda: True) is MidTurnOutcome.WAIT
    queue.add_waiting_human_message("$stop", text="Stop, wrong file", requester_id=_REQUESTER)
    queue.add_waiting_human_message("$edited", text="Use the other file", requester_id=_REQUESTER)
    judged = queue.pending_message_snapshot()
    assert await gate.decide(judged, current=lambda: judged == queue.pending_message_snapshot()) is (
        MidTurnOutcome.CANNOT_WAIT
    )
    # A decision a job wait asks for publishes nothing.
    assert published == []
    queue.update_waiting_human_message("$edited", text="Never mind", mentions_nobody=False)
    gate.report_stop()
    (stopping,) = published
    assert {message.event_id for message in stopping} == {"$stop", "$edited"}
    assert queue.stopped_reply_event_ids == {"$stop"}


async def _wraps_up_after_tool_batch(queue: _QueuedMessageState) -> bool:
    """Run the judgment a reply makes over ``queue`` after a tool batch, and return whether the reply wraps up."""
    messages = _resumed_messages()
    with queued_message_signal_context(queue, mid_turn_gate=queue.mid_turn_gate):
        await queued_notice_after_tool_batch(_NOTICE)(messages, ())
    return _has_notice(messages)


@pytest.mark.asyncio
async def test_a_later_verdict_that_lets_the_queue_wait_leaves_no_mark() -> None:
    """A stop verdict the reply did not act on, because a newer message made the queue wait, marks nothing."""
    calls: list[int] = []
    judged_again = asyncio.Event()
    queue = _QueuedMessageState()

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        calls.append(len(calls))
        if len(calls) == 1:
            queue.add_waiting_human_message(
                "$nvm",
                text="Never mind, the file is right, keep going",
                requester_id=_REQUESTER,
                mentions_nobody=True,
            )
            return False
        judged_again.set()
        return True

    queue.mid_turn_gate = MidTurnGate(
        active_text="Do the task",
        evaluate=evaluate,
        requester_id=_REQUESTER,
        on_stop=queue.mark_stopped_reply,
    )
    queue.add_waiting_human_message("$stop", text="Stop, wrong file", requester_id=_OTHER, mentions_nobody=True)
    stop = QueuedHumanNoticeReservation(queue, "$stop")
    # A tool call waiting on its job asks the queue, and keeps waiting once the judge lets the changed queue wait.
    waiting = asyncio.create_task(queue.wait())
    await asyncio.wait_for(judged_again.wait(), timeout=5)
    assert not waiting.done()
    assert not await _wraps_up_after_tool_batch(queue)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert not stop.stopped_reply


@pytest.mark.asyncio
async def test_another_persons_message_during_a_job_wait_judgment_keeps_the_correction_counted() -> None:
    """A correction the judge stopped the reply for stays counted when another person writes during the judgment.

    The job wait judges the changed queue again; its verdict is explained by the correction, so the newer message from
    someone else is not counted.
    """
    calls: list[int] = []
    queue = _QueuedMessageState()

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        calls.append(len(calls))
        if len(calls) == 1:
            queue.add_waiting_human_message("$bob", text="Lunch anyone?", requester_id=_OTHER, mentions_nobody=True)
        return False

    queue.mid_turn_gate = MidTurnGate(
        active_text="Do the task",
        evaluate=evaluate,
        requester_id=_REQUESTER,
        on_stop=queue.mark_stopped_reply,
    )
    queue.add_waiting_human_message("$stop", text="Stop, wrong file", requester_id=_REQUESTER, mentions_nobody=True)
    stop = QueuedHumanNoticeReservation(queue, "$stop")
    bob = QueuedHumanNoticeReservation(queue, "$bob")
    await asyncio.wait_for(queue.wait(), timeout=5)
    assert len(calls) == 2
    assert await _wraps_up_after_tool_batch(queue)
    assert (stop.stopped_reply, bob.stopped_reply) == (True, False)


@pytest.mark.asyncio
async def test_only_messages_the_judge_did_not_let_wait_count_as_stopping_the_reply() -> None:
    """A queue the judge says cannot wait marks the messages it had not let wait before.

    A released notice keeps its mark for its own follow-up, and the queue forgets it, so the same event queued again
    starts unmarked.
    """
    verdicts = [True, False]

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        return verdicts.pop(0)

    queue = _QueuedMessageState()
    queue.mid_turn_gate = MidTurnGate(
        active_text="Do the task",
        evaluate=evaluate,
        requester_id=_REQUESTER,
        on_stop=queue.mark_stopped_reply,
    )
    queue.add_waiting_human_message(
        "$remark",
        text="Did you get the slides?",
        requester_id=_OTHER,
        mentions_nobody=True,
    )
    remark = QueuedHumanNoticeReservation(queue, "$remark")
    assert not await _wraps_up_after_tool_batch(queue)
    queue.add_waiting_human_message("$stop", text="Stop, wrong file", requester_id=_OTHER, mentions_nobody=True)
    stop = QueuedHumanNoticeReservation(queue, "$stop")
    assert await _wraps_up_after_tool_batch(queue)
    assert (remark.stopped_reply, stop.stopped_reply) == (False, True)
    stop.release()
    remark.release()
    assert (remark.stopped_reply, stop.stopped_reply) == (False, True)
    queue.add_waiting_human_message("$stop", text="Stop, wrong file", requester_id=_OTHER, mentions_nobody=True)
    assert not QueuedHumanNoticeReservation(queue, "$stop").stopped_reply


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", ["waited_into_stop", "stop_into_small_talk"])
async def test_an_edit_counts_only_with_a_verdict_on_its_new_text(edit: str) -> None:
    """A message the judge let wait and then edited into a correction stops the reply when the judge says so.

    A correction edited after its verdict loses the mark, since no verdict covered the edited text.
    """
    verdicts = [True, False] if edit == "waited_into_stop" else [False]

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        return verdicts.pop(0)

    queue = _QueuedMessageState()
    queue.mid_turn_gate = MidTurnGate(
        active_text="Do the task",
        evaluate=evaluate,
        requester_id=_REQUESTER,
        on_stop=queue.mark_stopped_reply,
    )
    first, edited = ("Thanks", "Stop, wrong file") if edit == "waited_into_stop" else ("Stop, wrong file", "Thanks")
    queue.add_waiting_human_message("$msg", text=first, requester_id=_OTHER, mentions_nobody=True)
    message = QueuedHumanNoticeReservation(queue, "$msg")
    if edit == "waited_into_stop":
        assert not await _wraps_up_after_tool_batch(queue)
    else:
        assert await _wraps_up_after_tool_batch(queue)
        assert message.stopped_reply
    queue.update_waiting_human_message("$msg", text=edited, mentions_nobody=True)
    if edit == "waited_into_stop":
        assert await _wraps_up_after_tool_batch(queue)
    assert message.stopped_reply is (edit == "waited_into_stop")


@pytest.mark.asyncio
async def test_an_edit_after_release_drops_the_kept_verdict() -> None:
    """A correction whose notice a requester split released, then edited, has no verdict on its edited text."""

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        return False

    queue = _QueuedMessageState()
    queue.mid_turn_gate = MidTurnGate(
        active_text="Do the task",
        evaluate=evaluate,
        requester_id=_REQUESTER,
        on_stop=queue.mark_stopped_reply,
    )
    queue.add_waiting_human_message("$stop", text="Stop, wrong file", requester_id=_OTHER, mentions_nobody=True)
    notice = QueuedHumanNoticeReservation(queue, "$stop")
    assert await _wraps_up_after_tool_batch(queue)
    notice.release()
    assert notice.stopped_reply
    notice.update(request_envelope(reply_to_event_id="$stop", prompt="Thanks, carry on"), mentions_nobody=True)
    assert not notice.stopped_reply
    assert not queue.has_pending_human_messages()


@pytest.mark.asyncio
async def test_one_verdict_for_several_peoples_new_messages_marks_none() -> None:
    """The judge answers once for a queue, so new messages from two people leave both to participation."""

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        return False

    queue = _QueuedMessageState()
    queue.mid_turn_gate = MidTurnGate(
        active_text="Do the task",
        evaluate=evaluate,
        requester_id=_REQUESTER,
        on_stop=queue.mark_stopped_reply,
    )
    queue.add_waiting_human_message("$remark", text="Lunch anyone?", requester_id=_OTHER, mentions_nobody=True)
    queue.add_waiting_human_message("$stop", text="Stop, wrong file", requester_id=_REQUESTER, mentions_nobody=True)
    assert await _wraps_up_after_tool_batch(queue)
    assert queue.stopped_reply_event_ids == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [False, True], ids=["cannot-wait", "can-wait"])
async def test_a_verdict_after_a_tool_batch_marks_the_waiting_message(*, verdict: bool) -> None:
    """The judgment after a tool batch, where a reply wraps up, marks a message the reply cannot wait for."""

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        return verdict

    lifecycle = ResponseLifecycleCoordinator()
    target = MessageTarget.resolve("!room:localhost", "$thread", "$active")
    hook = queued_notice_after_tool_batch(_NOTICE)
    notices = []

    async def turn(_target: MessageTarget) -> list[Message]:
        follow_up = request_envelope(
            target=MessageTarget.resolve("!room:localhost", "$thread", "$stop"),
            reply_to_event_id="$stop",
            prompt="Stop, wrong file",
        )
        notices.append(lifecycle.reserve_waiting_human_message(target=follow_up.target, response_envelope=follow_up))
        messages = _resumed_messages()
        await hook(messages, ())
        return messages

    messages = await lifecycle.run_locked_response(
        target=target,
        response_envelope=request_envelope(target=target, reply_to_event_id="$active", prompt="Do the task"),
        pipeline_timing=None,
        locked_operation=turn,
        mid_turn_gate=MidTurnGate(active_text="Do the task", evaluate=evaluate, conversation_context=()),
    )
    (notice,) = notices
    assert notice is not None
    assert _has_notice(messages) is not verdict
    assert notice.stopped_reply is not verdict


@pytest.mark.asyncio
async def test_a_message_arriving_during_the_judgment_keeps_the_judged_mark() -> None:
    """A newer message that arrives while the judge decides still lets the reply wrap up and the judged one count.

    The newer message, which the judge never read, is not marked.
    """
    lifecycle = ResponseLifecycleCoordinator()
    target = MessageTarget.resolve("!room:localhost", "$thread", "$active")
    hook = queued_notice_after_tool_batch(_NOTICE)
    notices: dict[str, QueuedHumanNoticeReservation | None] = {}

    def queue(event_id: str, prompt: str) -> None:
        envelope = request_envelope(
            target=MessageTarget.resolve("!room:localhost", "$thread", event_id),
            reply_to_event_id=event_id,
            prompt=prompt,
        )
        notices[event_id] = lifecycle.reserve_waiting_human_message(target=envelope.target, response_envelope=envelope)

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        queue("$more", "and use the new file")
        return False

    async def turn(_target: MessageTarget) -> list[Message]:
        queue("$stop", "Stop, wrong file")
        messages = _resumed_messages()
        await hook(messages, ())
        await hook(messages, ())
        return messages

    messages = await lifecycle.run_locked_response(
        target=target,
        response_envelope=request_envelope(target=target, reply_to_event_id="$active", prompt="Do the task"),
        pipeline_timing=None,
        locked_operation=turn,
        mid_turn_gate=MidTurnGate(active_text="Do the task", evaluate=evaluate, conversation_context=()),
    )
    assert _has_notice(messages)
    assert {event_id: notice is not None and notice.stopped_reply for event_id, notice in notices.items()} == {
        "$stop": True,
        "$more": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("requester", [_REQUESTER, None], ids=["person", "not-a-person"])
async def test_only_another_persons_untagged_waiting_message_gets_no_defer_reaction(requester: str | None) -> None:
    """Whether the agent answers another person's untagged message is decided after the reply, so it shows no deferral."""
    acknowledgements: list[str] = []

    async def acknowledge(event_id: str) -> None:
        acknowledgements.append(event_id)

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        return True

    gate = MidTurnGate(active_text="Do the task", evaluate=evaluate, requester_id=requester, on_defer=acknowledge)
    pending = (
        QueuedMessage("$mine", "Thanks", "", _REQUESTER, mentions_nobody=True),
        _between_people("$theirs"),
        QueuedMessage("$tagged", "Nice work", "", _OTHER),
    )
    assert await gate.decide(pending, current=lambda: True) is MidTurnOutcome.WAIT
    assert acknowledgements == (["$mine", "$tagged"] if requester is not None else ["$mine", "$theirs", "$tagged"])


@pytest.mark.parametrize(
    ("source_kind", "requester"),
    [("message", "@user:localhost"), ("scheduled", None), ("hook", None)],
)
def test_the_gate_knows_the_requester_only_of_a_reply_a_person_asked_for(
    tmp_path: Path,
    source_kind: str,
    requester: str | None,
) -> None:
    """A scheduled or hook reply has no requester to tell apart from other people."""
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "synthetic", "id": "test"}},
            "agents": {
                "test_agent": {
                    "display_name": "Test",
                    "mid_turn": {"judgment": {"provider": "llm", "model": "default"}},
                },
            },
        },
    )
    gate = create_mid_turn_gate(
        config,
        test_runtime_paths(tmp_path),
        request_envelope(source_kind=source_kind),
        prompt="Do the task",
        has_media=False,
    )
    assert gate is not None
    assert gate.requester_id == requester


_QUESTION = "What is the capital of France?"


def _message_waiting() -> bool:
    """Return whether a tool wait of the reply listens for a newer message now."""
    return any(task.get_coro().__qualname__ == "_QueuedMessageState.wait" for task in asyncio.all_tasks())


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(JOB_TEST_TIMEOUT):
        while not condition():  # noqa: ASYNC110 - Polls conditions no event signals, such as a task existing.
            await asyncio.sleep(0.001)


class _JobReply:
    """An agent's reply with background jobs, run under the response lifecycle every Matrix reply runs under."""

    def __init__(self, tmp_path: Path, runtime: ToolJobRuntime) -> None:
        config = Config(
            background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
            agents={"leader": AgentConfig(display_name="Leader")},
        )
        self.config = config
        self.paths = _runtime_paths(tmp_path)
        self.context = _delegate_runtime_context(config, self.paths)
        self.owner = self.context.execution_identity
        assert self.owner is not None
        self.runtime = runtime
        pin_background_tool_jobs(self.context.config, self.paths)
        register_background_runtime(self.paths, runtime)
        self.lifecycle = ResponseLifecycleCoordinator()
        self.target = MessageTarget.resolve("!room:localhost", "$thread", "$active")
        self.queue = self.lifecycle._get_or_create_queued_signal(self.target)
        self.job_done = asyncio.Event()

    async def start_job(self) -> None:
        """Start the long background job "work" an earlier step of the reply started."""

        async def operation() -> BackgroundOutcome:
            await self.job_done.wait()
            return BackgroundOutcome("completed", "job result")

        await start_job(
            self.runtime,
            "work",
            tool_name="long_task",
            depth=0,
            adapter={},
            owner=self.owner,
            operation=operation,
        )

    def run(
        self,
        model: ScriptedModel,
        gate: MidTurnGate | None,
        *tools: JobTool,
        run_id: str = "run",
    ) -> asyncio.Task[RunRecord]:
        """Start the reply ``run_id`` to "Do the task", offering ``tools`` beside the job controls."""
        controls = toolkit_job_tools(JobTools(self.paths), authored_name="job").values()
        actor = job_actor(*tools, *controls)

        def storage() -> ConversationStore:
            return create_session_storage("leader", self.config, self.paths, self.owner)

        @owned_tool_execution(enabled=always_owned)
        async def turn(_target: MessageTarget) -> RunRecord:
            # The reply saves its run, so a result it read stays read.
            set_consumption_storage(storage)
            return await run_job_turn(
                actor,
                model,
                "Do the task",
                run_id=run_id,
                session_id=self.context.session_id,
                store=storage(),
                queued_notice=_NOTICE,
            )

        with tool_runtime_context(self.context):
            return asyncio.create_task(
                self.lifecycle.run_locked_response(
                    target=self.target,
                    response_envelope=request_envelope(
                        target=self.target,
                        reply_to_event_id="$active",
                        prompt="Do the task",
                    ),
                    pipeline_timing=None,
                    locked_operation=turn,
                    mid_turn_gate=gate,
                ),
            )

    def send(self) -> None:
        """Deliver a person's unrelated question to the running reply, as ingress queues it."""
        envelope = request_envelope(target=self.target, reply_to_event_id="$question", prompt=_QUESTION)
        assert self.lifecycle.reserve_waiting_human_message(target=self.target, response_envelope=envelope) is not None

    def key(self) -> ConversationKey:
        assert self.owner.room_id is not None
        assert self.owner.requester_id is not None
        return ConversationKey(
            recipient=self.owner.recipient,
            room_id=self.owner.room_id,
            thread_id=self.owner.resolved_thread_id,
            requester_id=self.owner.requester_id,
            silent=False,
        )


def _judge(verdict: bool, judged: list[JudgmentRequest]) -> MidTurnGate:
    """Return a gate whose judge answers ``verdict``, True letting queued messages wait, and records each request."""

    async def evaluate(request: JudgmentRequest) -> bool:
        judged.append(request)
        return verdict

    return MidTurnGate(active_text="Do the task", evaluate=evaluate)


def _job_wait(call_id: str = "wait") -> dict[str, object]:
    return tool_call("job", call_id, action="wait", job_id="work")


@pytest.mark.asyncio
async def test_a_reply_only_waiting_on_jobs_answers_a_newer_message_without_the_judge(tmp_path: Path) -> None:
    """A message reaching a reply blocked in a job wait releases it at once, unjudged, and the job runs on."""
    runtime = await tool_job_runtime(tmp_path)
    reply = _JobReply(tmp_path, runtime)
    judged: list[JudgmentRequest] = []
    model = ScriptedModel([calls(_job_wait()), answer("Wrapping up.")])
    try:
        await reply.start_job()
        pending = reply.run(model, _judge(True, judged))
        await _until(_message_waiting)
        reply.send()
        await _until(lambda: pending.done() or bool(judged))
        assert judged == []
        record = await asyncio.wait_for(pending, JOB_TEST_TIMEOUT)
        assert record.tools is not None
        [wait] = record.tools
        assert json.loads(str(wait.result))["status"] == "running"
        # The reply wraps up as for a message that cannot wait, and the question is answered next.
        assert _has_notice(model.requests[-1].messages)
        assert [message.event_id for message in reply.queue.pending_message_snapshot()] == ["$question"]
        assert (await lookup(runtime, "work", owner=reply.owner, depth=0)).status == "running"
    finally:
        reply.job_done.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_released_jobs_result_arrives_once_after_the_reply(tmp_path: Path) -> None:
    """The job a released wait left running ends later and reaches the conversation once, by a follow-up."""
    runtime = await tool_job_runtime(tmp_path)
    reply = _JobReply(tmp_path, runtime)
    judged: list[JudgmentRequest] = []
    model = ScriptedModel([calls(_job_wait()), answer("Wrapping up.")])
    try:
        await reply.start_job()
        pending = reply.run(model, _judge(True, judged))
        await _until(_message_waiting)
        reply.send()
        await _until(lambda: pending.done() or bool(judged))
        assert judged == []
        record = await asyncio.wait_for(pending, JOB_TEST_TIMEOUT)
        assert record.tools is not None
        assert "job result" not in str(record.tools[0].result)
        reply.job_done.set()
        await wait_for_status(runtime, "work", "completed")
        [ended] = follow_up_jobs(await runtime.ended_jobs(reply.key()))
        assert ended.job_id == "work"
        # The follow-up's reply reads the result; nothing is left to deliver again.
        reply.queue.consume_waiting_human_message("$question")
        follow_up = ScriptedModel([calls(_job_wait("read")), answer("Here is the result.")])
        record = await asyncio.wait_for(reply.run(follow_up, None, run_id="follow-up"), JOB_TEST_TIMEOUT)
        assert record.tools is not None
        assert record.tools[0].result == "job result"
        assert await runtime.ended_jobs(reply.key()) == ()
        assert pending_outcomes(runtime) == []
    finally:
        reply.job_done.set()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [True, False])
@pytest.mark.parametrize("batch", ["tool", "tool_beside_wait"])
async def test_a_reply_running_a_tool_leaves_a_newer_message_to_the_judge(
    tmp_path: Path,
    *,
    verdict: bool,
    batch: str,
) -> None:
    """A foreground tool still running, alone or beside a job wait, keeps today's rule: the judge decides.

    A wait beside it that the judge let go on is released, unjudged, once the tool ends and the reply only waits.
    """
    runtime = await tool_job_runtime(tmp_path)
    reply = _JobReply(tmp_path, runtime)
    judged: list[JudgmentRequest] = []
    started, finish = asyncio.Event(), asyncio.Event()
    beside_wait = batch == "tool_beside_wait"

    async def fetch_report() -> str:
        started.set()
        await finish.wait()
        return "report"

    fetch = tool_call("fetch_report", "fetch", background_after=None)
    model = ScriptedModel([calls(fetch, *([_job_wait()] if beside_wait else [])), answer("Done.")])
    try:
        await reply.start_job()
        pending = reply.run(model, _judge(verdict, judged), job_tool(fetch_report))
        await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
        await _until(_message_waiting)
        reply.send()
        await _until(lambda: bool(judged))
        if verdict:
            # The message can wait, so the reply works on until its tool ends.
            for _ in range(100):
                await asyncio.sleep(0)
            assert not pending.done()
            finish.set()
        record = await asyncio.wait_for(pending, JOB_TEST_TIMEOUT)
        assert len(judged) == 1
        assert record.tools is not None
        results = {tool.tool_call_id: str(tool.result) for tool in record.tools}
        assert (results["fetch"] == "report") is verdict
        if beside_wait:
            assert json.loads(results["wait"])["status"] == "running"
        assert _has_notice(model.requests[-1].messages) is (not verdict or beside_wait)
        assert (await lookup(runtime, "work", owner=reply.owner, depth=0)).status == "running"
    finally:
        finish.set()
        reply.job_done.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_reply_left_only_waiting_after_judged_work_answers_the_waiting_message(tmp_path: Path) -> None:
    """A message the judge let wait during a tool releases the job wait that follows; a second wait returns at once."""
    runtime = await tool_job_runtime(tmp_path)
    reply = _JobReply(tmp_path, runtime)
    judged: list[JudgmentRequest] = []
    started, finish = asyncio.Event(), asyncio.Event()

    async def fetch_report() -> str:
        started.set()
        await finish.wait()
        return "report"

    model = ScriptedModel(
        [
            calls(tool_call("fetch_report", "fetch", background_after=None)),
            calls(_job_wait("wait")),
            calls(_job_wait("again")),
            answer("Wrapping up."),
        ],
    )
    try:
        await reply.start_job()
        pending = reply.run(model, _judge(True, judged), job_tool(fetch_report))
        await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
        await _until(_message_waiting)
        reply.send()
        await _until(lambda: bool(judged))
        finish.set()
        record = await asyncio.wait_for(pending, JOB_TEST_TIMEOUT)
        assert len(judged) == 1
        assert record.tools is not None
        results = {tool.tool_call_id: str(tool.result) for tool in record.tools}
        assert results["fetch"] == "report"
        assert json.loads(results["wait"])["status"] == "running"
        assert json.loads(results["again"])["status"] == "running"
        # The judged work's batch gave no notice; each wait's batch gave the one wrap-up notice.
        assert [_has_notice(request.messages) for request in model.requests] == [False, False, True, True]
        assert sum(message.content == _NOTICE for message in model.requests[-1].messages) == 1
        assert (await lookup(runtime, "work", owner=reply.owner, depth=0)).status == "running"
    finally:
        finish.set()
        reply.job_done.set()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [True, False])
async def test_a_message_during_a_model_step_is_left_to_the_judge(tmp_path: Path, *, verdict: bool) -> None:
    """A message arriving while the model steps is judged at the wait of the tool call that step starts.

    The tool runs until the judge has answered, so its call is still waiting when the judge decides.
    """
    runtime = await tool_job_runtime(tmp_path)
    reply = _JobReply(tmp_path, runtime)
    judged: list[JudgmentRequest] = []
    finish = asyncio.Event()

    async def send() -> None:
        reply.send()

    async def check_disk() -> str:
        await finish.wait()
        return "plenty of space"

    model = ScriptedModel(
        [[Await(send), *calls(tool_call("check_disk", "disk"))], answer("Done.")],
    )
    try:
        pending = reply.run(model, _judge(verdict, judged), job_tool(check_disk))
        await _until(lambda: bool(judged))
        if verdict:
            # The message can wait, so the call keeps waiting for its tool.
            for _ in range(100):
                await asyncio.sleep(0)
            assert not pending.done()
            finish.set()
        record = await asyncio.wait_for(pending, JOB_TEST_TIMEOUT)
        assert len(judged) == 1
        assert record.tools is not None
        result = str(record.tools[0].result)
        if verdict:
            assert result == "plenty of space"
        else:
            assert json.loads(result)["status"] == "running"
        assert _has_notice(model.requests[-1].messages) is not verdict
    finally:
        finish.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_without_mid_turn_any_newer_message_releases_a_job_wait(tmp_path: Path) -> None:
    """Without ``mid_turn``, a newer message releases a job wait and asks the reply to wrap up, as before."""
    runtime = await tool_job_runtime(tmp_path)
    reply = _JobReply(tmp_path, runtime)
    model = ScriptedModel([calls(_job_wait()), answer("Wrapping up.")])
    try:
        await reply.start_job()
        pending = reply.run(model, None)
        await _until(_message_waiting)
        reply.send()
        record = await asyncio.wait_for(pending, JOB_TEST_TIMEOUT)
        assert record.tools is not None
        assert json.loads(str(record.tools[0].result))["status"] == "running"
        assert _has_notice(model.requests[-1].messages)
        assert (await lookup(runtime, "work", owner=reply.owner, depth=0)).status == "running"
    finally:
        reply.job_done.set()
        await runtime.shutdown()


def _acknowledgements(
    *event_ids: str,
) -> tuple[dict[str, asyncio.Event], Callable[[str], Awaitable[None]]]:
    """Return one event per message, set once its defer reaction is attempted, and the reaction that sets them."""
    attempted = {event_id: asyncio.Event() for event_id in event_ids}

    async def acknowledge(event_id: str) -> None:
        attempted[event_id].set()

    return attempted, acknowledge


@pytest.mark.asyncio
async def test_queue_is_absent_from_judgment_notice_pause_and_wait_release() -> None:
    """A Queue message is never judged, gives no notice or pause, and leaves even a reply only waiting on jobs waiting."""
    queue = _QueuedMessageState()
    queue.bind_active_requester(_REQUESTER)
    judged: list[JudgmentRequest] = []

    async def evaluate(request: JudgmentRequest) -> bool | None:
        judged.append(request)
        return False

    attempted, acknowledge = _acknowledgements("$queued", "$later", "$last")
    gate = MidTurnGate(active_text="Do the task", evaluate=evaluate, requester_id=_REQUESTER, on_defer=acknowledge)
    answered = Message(role="assistant", content="Working on it")
    # An earlier message's notice is still in the request, so only the queue decides whether it tells the reply to pause.
    queue.add_waiting_human_message("$earlier", text="Stop", requester_id=_REQUESTER)
    messages = _resumed_messages()
    with queued_message_signal_context(queue):
        await queued_notice_after_tool_batch(_NOTICE)(messages, ())
        assert told_to_pause([*messages, answered])
        queue.consume_waiting_human_message("$earlier")
        queue.add_waiting_human_message("$queued", text="Also do X", requester_id=_REQUESTER, mode=MidTurnMode.QUEUE)
        assert not told_to_pause([*messages, answered])
        await queued_notice_after_tool_batch(_NOTICE)(messages, ())
        assert not _has_notice(messages)

    queue.mid_turn_gate = gate
    queue.calls.running = queue.calls.waiting = 1
    assert queue.calls.only_waiting()
    assert not await _wraps_up_after_tool_batch(queue)
    with queued_message_signal_context(queue, mid_turn_gate=gate):
        assert not _has_notice(await _ResumedRequests().send())
    waiter = asyncio.create_task(queue.wait())
    queue.add_waiting_human_message("$later", text="And Y", requester_id=_OTHER, mode=MidTurnMode.QUEUE)
    await asyncio.wait_for(attempted["$later"].wait(), JOB_TEST_TIMEOUT)
    # The wait acknowledges a message on its next look at the queue, so this one shows the last look left it waiting.
    queue.add_waiting_human_message("$last", text="And Z", requester_id=_REQUESTER, mode=MidTurnMode.QUEUE)
    await asyncio.wait_for(attempted["$last"].wait(), JOB_TEST_TIMEOUT)
    assert not waiter.done()
    assert judged == []
    # An Auto message still releases a reply only waiting on jobs at once, without asking the judge.
    queue.add_waiting_human_message("$auto", text="Thanks", requester_id=_REQUESTER)
    await asyncio.wait_for(waiter, JOB_TEST_TIMEOUT)
    assert judged == []
    assert queue.pending_human_messages == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True], ids=["delivered", "delivery_error"])
async def test_queue_defer_reaction_is_attempted_once(*, fails: bool) -> None:
    """The configured defer reaction goes once onto a Queue message, however often the queue is looked at."""
    queue = _QueuedMessageState()
    queue.bind_active_requester(_REQUESTER)
    reactions: list[tuple[str, str]] = []
    second_attempted = asyncio.Event()
    judged: list[JudgmentRequest] = []

    async def react(reaction: str, event_id: str) -> None:
        reactions.append((reaction, event_id))
        if event_id == "$second":
            second_attempted.set()
        if fails:
            msg = "Matrix unavailable"
            raise RuntimeError(msg)

    async def evaluate(request: JudgmentRequest) -> bool | None:
        judged.append(request)
        return True

    queue.mid_turn_gate = MidTurnGate(
        active_text="Do the task",
        evaluate=evaluate,
        requester_id=_REQUESTER,
        on_defer=partial(react, "⏳"),
    )
    queue.add_waiting_human_message("$queued", text="Also do X", requester_id=_REQUESTER, mode=MidTurnMode.QUEUE)
    resumed = _ResumedRequests()
    for _ in range(2):
        assert not await _wraps_up_after_tool_batch(queue)
        with queued_message_signal_context(queue, mid_turn_gate=queue.mid_turn_gate):
            assert not _has_notice(await resumed.send())
    waiter = asyncio.create_task(queue.wait())
    queue.add_waiting_human_message("$second", text="And Y", requester_id=_REQUESTER, mode=MidTurnMode.QUEUE)
    await asyncio.wait_for(second_attempted.wait(), JOB_TEST_TIMEOUT)
    assert not await _wraps_up_after_tool_batch(queue)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    assert reactions == [("⏳", "$queued"), ("⏳", "$second")]
    assert judged == []


@pytest.mark.asyncio
async def test_requester_steer_releases_wait_during_an_unresolved_auto_judgment() -> None:
    """The requester's Steer ends a wait at once, while the judge still weighs an earlier Auto message.

    The obsolete judgment is cancelled and drained before the wait returns, so it can publish no mark afterwards.
    """
    queue = _QueuedMessageState()
    queue.bind_active_requester(_REQUESTER)
    judging, answer_judgment, judgment_cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        judging.set()
        try:
            await answer_judgment.wait()
        except asyncio.CancelledError:
            judgment_cancelled.set()
            raise
        return False

    gate = MidTurnGate(
        active_text="Do the task",
        evaluate=evaluate,
        requester_id=_REQUESTER,
        on_stop=queue.mark_stopped_reply,
    )
    queue.mid_turn_gate = gate
    waiter = asyncio.create_task(queue.wait())
    queue.add_waiting_human_message("$auto", text="Stop, wrong file", requester_id=_REQUESTER)
    await asyncio.wait_for(judging.wait(), JOB_TEST_TIMEOUT)
    queue.add_waiting_human_message(
        "$steer",
        text="Use the other file",
        requester_id=_REQUESTER,
        mode=MidTurnMode.STEER,
    )
    await asyncio.wait_for(waiter, JOB_TEST_TIMEOUT)
    assert not answer_judgment.is_set()
    assert judgment_cancelled.is_set()
    assert [task for task in asyncio.all_tasks() if "_QueuedMessageState" in task.get_coro().__qualname__] == []
    answer_judgment.set()
    gate.report_stop()
    assert queue.stopped_reply_event_ids == set()
    assert queue.requester_steer_event_ids == {"$steer"}


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [False, True], ids=["cannot_wait", "can_wait"])
async def test_a_judgment_that_absorbs_its_steer_cancellation_leaves_no_evidence(*, verdict: bool) -> None:
    """A judgment the requester's Steer cancels commits nothing, even when its evaluator absorbs the cancellation.

    An HTTP client whose own timeout fires with the cancellation can take it for that timeout and still answer.
    """
    queue = _QueuedMessageState()
    queue.bind_active_requester(_REQUESTER)
    judging, absorbed = asyncio.Event(), asyncio.Event()
    judgments = 0

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        nonlocal judgments
        judgments += 1
        if judgments > 1:
            return False
        judging.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            absorbed.set()
        return verdict

    gate = MidTurnGate(
        active_text="Do the task",
        evaluate=evaluate,
        requester_id=_REQUESTER,
        on_stop=queue.mark_stopped_reply,
    )
    queue.mid_turn_gate = gate
    queue.add_waiting_human_message("$bob-auto", text="Stop, wrong file", requester_id=_OTHER)
    waiter = asyncio.create_task(queue.wait())
    await asyncio.wait_for(judging.wait(), JOB_TEST_TIMEOUT)
    queue.add_waiting_human_message("$alice-steer", text="Use mine", requester_id=_REQUESTER, mode=MidTurnMode.STEER)
    await asyncio.wait_for(waiter, JOB_TEST_TIMEOUT)
    assert absorbed.is_set()
    assert [task for task in asyncio.all_tasks() if "_QueuedMessageState" in task.get_coro().__qualname__] == []
    gate.report_stop()
    assert queue.stopped_reply_event_ids == set()
    steer = QueuedHumanNoticeReservation(queue, "$alice-steer")
    steer.release()
    assert steer.stopped_reply
    # The judge is asked again, and its "cannot wait" names Bob's message, which no cancelled verdict let wait.
    assert await _wraps_up_after_tool_batch(queue)
    assert judgments == 2
    assert queue.stopped_reply_event_ids == {"$bob-auto"}


@pytest.mark.asyncio
async def test_other_requesters_steer_uses_auto_wait_policy() -> None:
    """Another person's Steer is weighed as Auto: a judge that lets it wait leaves the wait blocked, with no bypass."""
    queue = _QueuedMessageState()
    queue.bind_active_requester(_REQUESTER)
    judged: list[JudgmentRequest] = []

    async def evaluate(request: JudgmentRequest) -> bool | None:
        judged.append(request)
        return True

    attempted, acknowledge = _acknowledgements("$bob", "$later")
    queue.mid_turn_gate = MidTurnGate(
        active_text="Do the task",
        evaluate=evaluate,
        requester_id=_REQUESTER,
        on_defer=acknowledge,
        on_stop=queue.mark_stopped_reply,
    )
    waiter = asyncio.create_task(queue.wait())
    queue.add_waiting_human_message("$bob", text="Stop, use mine", requester_id=_OTHER, mode=MidTurnMode.STEER)
    bob = QueuedHumanNoticeReservation(queue, "$bob")
    await asyncio.wait_for(attempted["$bob"].wait(), JOB_TEST_TIMEOUT)
    # The wait acknowledges a message on its next look at the queue, so this one shows the verdict left it waiting.
    queue.add_waiting_human_message("$later", text="Also do X", requester_id=_REQUESTER, mode=MidTurnMode.QUEUE)
    await asyncio.wait_for(attempted["$later"].wait(), JOB_TEST_TIMEOUT)
    assert not waiter.done()
    assert len(judged) == 1
    assert queue.requester_steer_snapshot() == ()
    assert queue.requester_steer_event_ids == set()
    queue.mid_turn_gate.report_stop()
    assert not bob.stopped_reply
    bob.release()
    assert not bob.stopped_reply
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)


def test_binding_a_requester_latches_only_their_waiting_steers() -> None:
    """A reply that takes the lock, like an approval resuming, makes its requester's waiting Steers direct ones."""
    queue = _QueuedMessageState()
    queue.add_waiting_human_message("$mine", text="Use X", requester_id=_REQUESTER, mode=MidTurnMode.STEER)
    queue.add_waiting_human_message("$theirs", text="Use Y", requester_id=_OTHER, mode=MidTurnMode.STEER)
    queue.add_waiting_human_message("$auto", text="Use Z", requester_id=_REQUESTER)
    # Admitted while no reply holds the lock, a Steer is not yet anyone's direct one.
    assert queue.requester_steer_event_ids == set()
    assert queue.requester_steer_snapshot() == ()
    queue.bind_active_requester(_REQUESTER)
    assert queue.requester_steer_event_ids == {"$mine"}
    assert [message.event_id for message in queue.requester_steer_snapshot()] == ["$mine"]
    queue.bind_active_requester(None)
    assert queue.requester_steer_snapshot() == ()
    assert QueuedHumanNoticeReservation(queue, "$mine").stopped_reply


async def _steers_after_tool_batch(queue: _QueuedMessageState) -> tuple[bool | None, list[Message]]:
    """Run a reply-owning run's after-batch hook over ``queue``; return whether it ends the run, and the messages."""
    messages = _resumed_messages()
    with queued_message_signal_context(queue, mid_turn_gate=queue.mid_turn_gate):
        ended = await queued_notice_after_tool_batch(_NOTICE, steering=True)(messages, ())
    return ended, messages


@pytest.mark.asyncio
@pytest.mark.parametrize("judge", [False, True], ids=["no_judge", "judge"])
async def test_requester_steer_returns_batch_stop_without_notice_or_queue_consumption(*, judge: bool) -> None:
    """The requester's Steer ends an owning run once its batch's results are recorded, unannounced and unjudged.

    The run stores its tool calls and results as any finished run does, its results keep their own outcomes, and the
    Steer stays queued with its bypass for the reply that follows.
    """
    queue = _QueuedMessageState()
    queue.bind_active_requester(_REQUESTER)
    judged: list[JudgmentRequest] = []

    async def evaluate(request: JudgmentRequest) -> bool | None:
        judged.append(request)
        return True

    if judge:
        queue.mid_turn_gate = MidTurnGate(
            active_text="Do the task",
            evaluate=evaluate,
            requester_id=_REQUESTER,
            on_stop=queue.mark_stopped_reply,
        )

    def work() -> str:
        """Do the work."""
        queue.add_waiting_human_message(
            "$steer",
            text="Also print the date",
            requester_id=_REQUESTER,
            mode=MidTurnMode.STEER,
        )
        return "Completed action"

    model = ScriptedModel([calls(tool_call("work", "work")), answer("Never requested")])
    with queued_message_signal_context(queue, mid_turn_gate=queue.mid_turn_gate):
        record = await _run_turn(model, toolkit(work), hooks=_STEERING_HOOKS)
    assert record.status == RunStatus.completed
    assert len(model.requests) == 1
    assert not _has_notice(record.messages or ())
    assert answer_of(record, "work").content == "Completed action"
    assert [tool.tool_name for tool in record.tools or ()] == ["work"]
    assert not any(message.stop_after_tool_call for message in record.messages or ())
    assert not any(tool.stop_after_call for tool in record.tools or ())
    assert [message.event_id for message in queue.pending_message_snapshot()] == ["$steer"]
    assert QueuedHumanNoticeReservation(queue, "$steer").stopped_reply

    # Each hook of an owning run, after a batch and at a resume's entry, ends it for the same reason.
    ended, messages = await _steers_after_tool_batch(queue)
    assert ended is True
    assert not _has_notice(messages)
    resumed = _resumed_messages()
    with queued_message_signal_context(queue, mid_turn_gate=queue.mid_turn_gate):
        assert await queued_notice_on_resume_entry(_NOTICE, steering=True)(resumed, ()) is True
    assert not _has_notice(resumed)
    assert judged == []
    assert [message.event_id for message in queue.pending_message_snapshot()] == ["$steer"]


_VERDICTS: dict[str, bool | None] = {
    "cannot_wait": False,
    "can_wait": True,
    "no_answer": None,
    "another_persons_steer": True,
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "ends", "notice"),
    [
        ("cannot_wait", True, False),
        ("can_wait", None, False),
        ("no_answer", None, True),
        ("error", None, True),
        ("timeout", None, True),
        ("unjudgeable", None, True),
        ("edited", None, True),
        ("deleted", None, False),
        ("appended", True, False),
        ("another_persons_steer", None, False),
    ],
)
async def test_only_explicit_current_negative_verdict_ends_run(case: str, ends: bool | None, *, notice: bool) -> None:
    """Only the judge's "cannot wait" on the queue as it still waits ends an owning run, without a notice.

    A verdict on a message edited or removed during the judgment is obsolete, and a message appended meanwhile does
    not cost the judged correction its count. Without a verdict, the run gets the pause notice and no bypass.
    """
    queue = _QueuedMessageState()
    queue.bind_active_requester(_REQUESTER)
    judgments: list[JudgmentRequest] = []

    async def evaluate(request: JudgmentRequest) -> bool | None:
        judgments.append(request)
        if case == "error":
            msg = "judgment backend unavailable"
            raise RuntimeError(msg)
        if case == "timeout":
            raise TimeoutError
        if case == "edited":
            queue.update_waiting_human_message("$stop", text="Never mind, keep going", mentions_nobody=False)
        elif case == "deleted":
            queue.consume_waiting_human_message("$stop")
        elif case == "appended":
            queue.add_waiting_human_message("$bob", text="Lunch anyone?", requester_id=_OTHER, mentions_nobody=True)
        return _VERDICTS.get(case, False)

    queue.mid_turn_gate = MidTurnGate(
        active_text="Do the task",
        evaluate=evaluate,
        requester_id=_REQUESTER,
        on_stop=queue.mark_stopped_reply,
    )
    sender, mode = (_OTHER, MidTurnMode.STEER) if case == "another_persons_steer" else (_REQUESTER, MidTurnMode.AUTO)
    text = None if case == "unjudgeable" else "Stop, wrong file"
    queue.add_waiting_human_message("$stop", text=text, requester_id=sender, mode=mode)
    stop = QueuedHumanNoticeReservation(queue, "$stop")
    ended, messages = await _steers_after_tool_batch(queue)
    assert ended is ends
    assert _has_notice(messages) is notice
    assert len(judgments) == (0 if case == "unjudgeable" else 1)
    assert stop.stopped_reply is (ends is True)
    assert QueuedHumanNoticeReservation(queue, "$bob").stopped_reply is False


@pytest.mark.asyncio
@pytest.mark.parametrize("pause", ["delegation", "approval"])
@pytest.mark.parametrize("reason", ["steer", "cannot_wait"])
async def test_unresolved_batch_is_not_steered_before_resume(pause: str, reason: str) -> None:
    """A batch waiting on a delegation's or an approval's result is not ended; its resume ends it once complete.

    The owner stays paused with the message still queued, and the resumed batch ends the run before another request.
    """
    queue = _QueuedMessageState()
    queue.bind_active_requester(_REQUESTER)
    judgments: list[JudgmentRequest] = []

    async def evaluate(request: JudgmentRequest) -> bool | None:
        judgments.append(request)
        return False

    queue.mid_turn_gate = MidTurnGate(
        active_text="Do the task",
        evaluate=evaluate,
        requester_id=_REQUESTER,
        on_stop=queue.mark_stopped_reply,
    )

    def work() -> str:
        """Do the work."""
        mode = MidTurnMode.STEER if reason == "steer" else MidTurnMode.AUTO
        queue.add_waiting_human_message("$new", text="Stop, wrong file", requester_id=_REQUESTER, mode=mode)
        return "Completed action"

    def child() -> str:
        """Hand the subtask to a child, or run once approved."""
        return "Child result"

    held = external(child) if pause == "delegation" else gated(child)
    model = ScriptedModel([calls(tool_call("work", "work"), tool_call("child", "child")), answer("Never requested")])
    with queued_message_signal_context(queue, mid_turn_gate=queue.mid_turn_gate):
        paused = await _run_turn(model, toolkit(work), held, hooks=_STEERING_HOOKS)
        assert paused.status == RunStatus.paused
        assert judgments == []
        assert [message.event_id for message in queue.pending_message_snapshot()] == ["$new"]
        approved = [
            ApprovalCall(
                tool_call_id="child",
                tool_name="child",
                invoking_agent="code",
                expires_at_ns=2**62,
                toolkit_name="gated",
                arguments_digest=approval_arguments_digest({}),
            ),
        ]
        record = await _resume_turn(
            model,
            paused,
            toolkit(work),
            held,
            external_results={"child": "Child result"} if pause == "delegation" else {},
            approved=approved if pause == "approval" else (),
            hooks=_STEERING_HOOKS,
        )
    assert record.status == RunStatus.completed
    assert len(model.requests) == 1
    assert answer_of(record, "child").content == "Child result"
    assert not _has_notice(record.messages or ())
    assert len(judgments) == (1 if reason == "cannot_wait" else 0)
    assert [message.event_id for message in queue.pending_message_snapshot()] == ["$new"]
    assert QueuedHumanNoticeReservation(queue, "$new").stopped_reply
