"""Opt-in judgments decide whether queued messages need a mid-turn handoff."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.tools.function import Function
from pydantic import ValidationError

from mindroom import model_loading
from mindroom.ai_runtime import install_queued_message_notice_hook, queued_message_signal_context
from mindroom.config.main import Config
from mindroom.config.mid_turn import MidTurnConfig
from mindroom.judgment.client import PINNED_MODEL, SystemOneClient
from mindroom.judgment.state import JudgmentMessage
from mindroom.mid_turn import MidTurnGate, QueuedMessage
from mindroom.mid_turn_judgment import create_mid_turn_gate
from mindroom.response_lifecycle import _QueuedMessageState
from tests.conftest import request_envelope, test_runtime_paths
from tests.participation_helpers import ParticipationModel

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.judgment.state import JudgmentRequest


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
    model = ParticipationModel(ModelResponse(content="Done"))
    install_queued_message_notice_hook(model, notice_text="WRAP UP NOW")
    with queued_message_signal_context(state, mid_turn_gate=gate):
        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await model.aresponse(_resumed_messages())
        else:
            await model.aresponse(_resumed_messages())
            await model.aresponse(_resumed_messages())
            if outcome == "finish":
                state.add_waiting_human_message("$second", text="No rush")
                await model.aresponse(_resumed_messages())
    assert acknowledgements == (
        ["$first", "$second"] if outcome == "finish" else ["$first"] if outcome == "reaction_error" else []
    )
    if outcome == "reaction_error":
        assert all(not any(message.content == "WRAP UP NOW" for message in call["messages"]) for call in model.requests)
    assert state.has_pending_human_messages()


@pytest.mark.asyncio
async def test_queue_change_during_reaction_still_requests_wrap_up() -> None:
    """A correction arriving while Matrix acknowledges a reaction must still cause handoff."""
    state = _QueuedMessageState()
    state.add_waiting_human_message("$first", text="Thanks")
    state.add_waiting_human_message("$second", text="No rush")
    acknowledgements: list[str] = []

    async def acknowledge(event_id: str) -> None:
        acknowledgements.append(event_id)
        state.add_waiting_human_message("$correction", text="Stop")

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        return True

    model = ParticipationModel(ModelResponse(content="Done"))
    install_queued_message_notice_hook(model, notice_text="WRAP UP NOW")
    with queued_message_signal_context(
        state,
        mid_turn_gate=MidTurnGate(active_text="Do the task", evaluate=evaluate, on_defer=acknowledge),
    ) as context:
        await model.aresponse(_resumed_messages())
        assert context.notice_fired
    assert acknowledgements == ["$first"]
    assert any(message.content == "WRAP UP NOW" for message in model.requests[0]["messages"])


@pytest.mark.asyncio
async def test_queued_message_freezes_visible_progress_at_admission() -> None:
    """Later stream edits must not change the context of an earlier human message."""
    evidence: list[dict] = []

    async def evaluate(request: JudgmentRequest) -> bool | None:
        assert request.body is not None
        evidence.append(json.loads(json.loads(request.body)["state"]["conversation"][0]["text"]))
        return True

    gate = MidTurnGate(active_text="Do the task", evaluate=evaluate)
    state = _QueuedMessageState(mid_turn_gate=gate)
    gate.record_visible_response("I found the file.\n🔧 `read_file` [1]")
    state.add_waiting_human_message("$first", text="Use that file")
    first = state.pending_message_snapshot()
    gate.record_visible_response("I found the file. Now editing it.")
    assert await gate.should_finish(first)
    assert evidence[0]["queued_messages"] == [
        {"text": "Use that file", "visible_response": "I found the file.\n🔧 `read_file` [1]"},
    ]
    assert await gate.should_finish(first)
    assert len(evidence) == 1
    state.add_waiting_human_message("$second", text="Stop editing")
    assert await gate.should_finish(state.pending_message_snapshot())
    assert evidence[1]["queued_messages"][1]["visible_response"] == "I found the file. Now editing it."
    assert "completed_tools" not in evidence[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "progress",
    [None, "x" * 16001, '{"password": "fake_secret_for_test"}'],
    ids=["unknown", "oversized", "secret"],
)
async def test_unknown_or_unsafe_visible_progress_keeps_wrap_up(progress: str | None) -> None:
    """Unavailable or sensitive user-visible context must not leave the runtime."""

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        pytest.fail("Incomplete visible progress must not reach inference")

    gate = MidTurnGate(active_text="Do the task", evaluate=evaluate)
    gate.visible_response_text = progress
    state = _QueuedMessageState(mid_turn_gate=gate)
    state.add_waiting_human_message("$queued", text="Thanks")
    assert not await gate.should_finish(state.pending_message_snapshot())


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
    assert await gate.should_finish(first)
    assert await gate.should_finish(first)
    assert len(requests) == 1
    assert not await gate.should_finish(
        (*first, QueuedMessage("$two", "Use the other version")),
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
    assert not await gate.should_finish((QueuedMessage("$one", text),))


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [False, None])
async def test_nonapproval_keeps_wrap_up(decision: bool | None) -> None:
    """A negative or abstaining judge cannot suppress the default notice."""

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        return decision

    gate = MidTurnGate(active_text="Do the task", evaluate=evaluate)
    assert not await gate.should_finish((QueuedMessage("$one", "Please change it"),))


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
async def test_json_credentials_are_checked_before_evidence_encoding(*, active: bool) -> None:
    """JSON escaping must not hide credentials from the redaction boundary."""

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        pytest.fail("Credentials must not reach the judgment backend")

    credential_text = '{"password": "fake_secret_for_test"}'
    gate = MidTurnGate(active_text=credential_text if active else "Do the task", evaluate=evaluate)
    assert not await gate.should_finish(
        (QueuedMessage("$new", "Thanks" if active else credential_text),),
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
    task = asyncio.create_task(gate.should_finish((QueuedMessage("$one", "Thanks"),)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("finish", [False, True])
@pytest.mark.parametrize("terminal", [False, True])
async def test_real_tool_loop_judges_before_next_provider_request(
    *,
    stream: bool,
    finish: bool,
    terminal: bool,
) -> None:
    """Both provider paths must receive a notice only when the judge requests wrap-up."""
    state = _QueuedMessageState()
    evidence: list[dict] = []

    def queue_followup() -> str:
        gate.record_visible_response("Work started.\n🔧 `queue_followup` [1]")
        state.add_waiting_human_message("$new", text="Thanks for doing this")
        return "Private tool result must not reach the judge"

    async def evaluate(request: JudgmentRequest) -> bool | None:
        assert request.body is not None
        evidence.append(json.loads(request.body))
        return finish

    gate = MidTurnGate(active_text="Do the task", evaluate=evaluate)
    state.mid_turn_gate = gate
    model = ParticipationModel(
        ModelResponse(
            tool_calls=[
                {"id": "call_1", "type": "function", "function": {"name": "queue_followup", "arguments": "{}"}},
            ],
        ),
    )
    install_queued_message_notice_hook(model, notice_text="WRAP UP NOW")
    function = Function.from_callable(queue_followup)
    function.stop_after_tool_call = terminal
    agent = Agent(model=model, tools=[function], telemetry=False)
    with queued_message_signal_context(state, mid_turn_gate=gate) as context:
        if stream:
            async for _ in agent.arun("Do the task", stream=True):
                pass
        else:
            await agent.arun("Do the task")
        assert context.notice_fired is (not finish and not terminal)
    assert len(evidence) == (0 if terminal else 1)
    assert "Private tool result" not in json.dumps(evidence)
    assert ("queue_followup" in json.dumps(evidence)) is (not terminal)
    assert len(model.requests) == (1 if terminal else 2)
    assert any(message.content == "WRAP UP NOW" for message in model.requests[-1]["messages"]) is (
        not finish and not terminal
    )
    assert {message.event_id for message in state.pending_message_snapshot()} == {"$new"}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
async def test_pending_approval_is_not_judged_as_completed_work(*, stream: bool, mixed: bool) -> None:
    """Unresolved tool calls must not become evidence of completed actions."""
    executed: list[str] = []
    judgments: list[JudgmentRequest] = []
    state = _QueuedMessageState()
    state.add_waiting_human_message("$queued", text="Thanks")

    def approved_action() -> str:
        executed.append("approved_action")
        return "Done"

    def ordinary_action() -> str:
        executed.append("ordinary_action")
        return "Read complete"

    async def evaluate(request: JudgmentRequest) -> bool | None:
        judgments.append(request)
        return True

    names = ["ordinary_action", "approved_action"] if mixed else ["approved_action"]
    model = ParticipationModel(
        ModelResponse(
            tool_calls=[
                {"id": name, "type": "function", "function": {"name": name, "arguments": "{}"}} for name in names
            ],
        ),
    )
    install_queued_message_notice_hook(model, notice_text="WRAP UP NOW")
    agent = Agent(
        model=model,
        tools=[
            Function(name="approved_action", entrypoint=approved_action, requires_confirmation=True),
            ordinary_action,
        ],
        telemetry=False,
    )
    with queued_message_signal_context(state, mid_turn_gate=MidTurnGate(active_text="Do the task", evaluate=evaluate)):
        if stream:
            async for _ in agent.arun("Do the task", stream=True):
                pass
        else:
            await agent.arun("Do the task")
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

    model = ParticipationModel(ModelResponse(content="Done"))
    install_queued_message_notice_hook(model, notice_text="WRAP UP NOW")
    with queued_message_signal_context(state, mid_turn_gate=MidTurnGate(active_text="Do the task", evaluate=evaluate)):
        await model.aresponse(_resumed_messages())
    assert any(message.content == "WRAP UP NOW" for message in model.requests[0]["messages"]) is (not remove)


@pytest.mark.asyncio
async def test_wrap_up_remains_settled_after_later_queue_changes() -> None:
    """Once told to wrap up, later acknowledgements must not reverse the handoff."""
    state = _QueuedMessageState()
    state.add_waiting_human_message("$first", text="Change the task")
    judged: list[JudgmentRequest] = []

    async def evaluate(request: JudgmentRequest) -> bool | None:
        judged.append(request)
        return len(judged) > 1

    model = ParticipationModel(ModelResponse(content="Done"))
    install_queued_message_notice_hook(model, notice_text="WRAP UP NOW")
    with queued_message_signal_context(state, mid_turn_gate=MidTurnGate(active_text="Do the task", evaluate=evaluate)):
        await model.aresponse(_resumed_messages())
        state.add_waiting_human_message("$second", text="Thanks")
        await model.aresponse(_resumed_messages())
    assert len(judged) == 1
    assert all(any(message.content == "WRAP UP NOW" for message in call["messages"]) for call in model.requests)


@pytest.mark.asyncio
async def test_resumed_tools_do_not_expose_invisible_execution_details() -> None:
    """Approval results do not add model-private execution details to visible context."""
    state = _QueuedMessageState()
    state.add_waiting_human_message("$queued", text="Thanks")
    evidence: list[dict] = []

    async def evaluate(request: JudgmentRequest) -> bool | None:
        assert request.body is not None
        evidence.append(json.loads(json.loads(request.body)["state"]["conversation"][0]["text"]))
        return False

    messages = _resumed_messages()
    messages[-1].tool_call_error = True
    messages[-1].content = "Denied"
    model = ParticipationModel(ModelResponse(content="Done"))
    install_queued_message_notice_hook(model, notice_text="WRAP UP NOW")
    with queued_message_signal_context(state, mid_turn_gate=MidTurnGate(active_text="Do the task", evaluate=evaluate)):
        await model.aresponse(messages)
    assert "completed_tools" not in evidence[0]
    assert any(message.content == "WRAP UP NOW" for message in model.requests[0]["messages"])


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

    async def post(_client: SystemOneClient, _body: bytes) -> bytes:
        if outcome == "timeout":
            await asyncio.Event().wait()
        return json.dumps(
            {
                "model": PINNED_MODEL,
                "usage": {"input_tokens": 20, "output_tokens": 1},
                "answers": {
                    "interrupt_current_turn": {
                        "type": "noul",
                        "noul": ("bad" if outcome == "invalid" else 0.1 if outcome == "finish" else 0.9),
                    },
                },
            },
        ).encode()

    monkeypatch.setattr(SystemOneClient, "_post", post)
    judge = ParticipationModel(
        TimeoutError()
        if outcome == "timeout"
        else ModelResponse(
            content=("invalid" if outcome == "invalid" else json.dumps({"decision": outcome != "finish"})),
        ),
    )
    monkeypatch.setattr(model_loading, "get_model_instance", lambda *_: judge)
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
    model = ParticipationModel(ModelResponse(content="Done"))
    install_queued_message_notice_hook(model, notice_text="WRAP UP NOW")
    with queued_message_signal_context(state, mid_turn_gate=gate):
        await model.aresponse(_resumed_messages())
    assert any(message.content == "WRAP UP NOW" for message in model.requests[0]["messages"]) is (outcome != "finish")


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
async def test_existing_tool_checkpoint_survives_judgment_and_cancellation(*, stream: bool, cancel: bool) -> None:
    """Adding a judgment must not discard durable checkpoints of executed tools."""
    state = _QueuedMessageState()
    entered = asyncio.Event()
    checkpoints: list[list[str]] = []
    messages = [Message(role="user", content="Do the task")]

    def work() -> str:
        state.add_waiting_human_message("$new", text="Change the task")
        return "Completed action"

    async def evaluate(_request: JudgmentRequest) -> bool | None:
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        return False

    async def checkpoint(_result: ModelResponse) -> None:
        checkpoints.append([message.get_content_string() for message in messages if message.role in {"tool", "user"}])

    model = ParticipationModel(
        ModelResponse(
            tool_calls=[
                {"id": "call", "type": "function", "function": {"name": "work", "arguments": "{}"}},
            ],
        ),
    )
    install_queued_message_notice_hook(model, notice_text="WRAP UP NOW")

    async def run() -> None:
        with queued_message_signal_context(
            state,
            mid_turn_gate=MidTurnGate(active_text="Do the task", evaluate=evaluate),
        ):
            if stream:
                async for _ in model.aresponse_stream(
                    messages,
                    tools=[Function.from_callable(work)],
                    after_tool_results=checkpoint,
                ):
                    pass
            else:
                await model.aresponse(messages, tools=[Function.from_callable(work)], after_tool_results=checkpoint)

    task = asyncio.create_task(run())
    await entered.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    assert len(checkpoints) == 1
    assert "Completed action" in checkpoints[0]
    assert ("WRAP UP NOW" in checkpoints[0]) is (not cancel)
    assert len(model.requests) == (1 if cancel else 2)


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
async def test_interrupt_probability_preserves_continuation_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probability: float,
    threshold: float,
    *,
    finish: bool,
) -> None:
    """A low-confidence no-interruption answer must still request a handoff."""

    async def post(_client: SystemOneClient, body: bytes) -> bytes:
        question_id = next(iter(json.loads(body)["questions"]))
        assert question_id == "interrupt_current_turn"
        return json.dumps(
            {
                "model": PINNED_MODEL,
                "usage": {"input_tokens": 20, "output_tokens": 1},
                "answers": {question_id: {"type": "noul", "noul": probability}},
            },
        ).encode()

    monkeypatch.setattr(SystemOneClient, "_post", post)
    config = Config.model_validate(
        {
            "agents": {
                "test_agent": {
                    "display_name": "Test",
                    "mid_turn": {
                        "judgment": {"provider": "typesafe", "threshold": threshold},
                    },
                },
            },
        },
    )
    paths = replace(test_runtime_paths(tmp_path), process_env={"TYPESAFE_API_KEY": "synthetic"})
    gate = create_mid_turn_gate(config, paths, request_envelope(), prompt="Do the task", has_media=False)
    assert gate is not None
    assert await gate.should_finish((QueuedMessage("$new", "Thanks"),)) is finish


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
    assert await gate.should_finish((QueuedMessage("$new", "Thanks"),))
    assert captured[0][:-1] == [{"role": "user", "text": message.text} for message in context]
