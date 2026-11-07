"""Every agent turn has one ID that its hooks and tool calls carry, and a delegated child's turn names its caller's."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.ai import ai_response
from mindroom.config.agent import AgentConfig
from mindroom.config.models import ModelConfig
from mindroom.delegation.state import DelegationState
from mindroom.delivery_gateway import ResponseIdentity
from mindroom.hooks import (
    EVENT_CONTACT_BEFORE_SEND,
    EVENT_MESSAGE_AFTER_RESPONSE,
    EVENT_MESSAGE_BEFORE_RESPONSE,
    EVENT_MESSAGE_CANCELLED,
    EVENT_MESSAGE_ENRICH,
    EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM,
    EVENT_SESSION_STARTED,
    EVENT_SYSTEM_ENRICH,
    EVENT_TOOL_AFTER_CALL,
    EVENT_TOOL_BEFORE_CALL,
    EVENT_TOOL_ENVIRONMENT,
    HookContext,
    HookRegistry,
    ToolAfterCallContext,
    ToolBeforeCallContext,
    hook,
)
from mindroom.inbound_turn_normalizer import DispatchPayload
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.reply_turn.request import ResponseRequest
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt
from mindroom.tool_system.runtime_context import emit_custom_event, get_tool_runtime_context, tool_runtime_context
from tests.bot_helpers import _noop_typing_indicator
from tests.conftest import (
    delivered_matrix_side_effect,
    install_model_providers,
    make_turn_context,
    patch_response_runner_module,
    request_envelope,
)
from tests.contacts.test_policy import _check, _tool_context
from tests.contacts.test_policy import _config as _contact_config
from tests.delegation_helpers import (
    ScriptedAgents,
    _delegate_runtime_context,
    _runtime_paths,
    _saved_approval_calls,
    approval_executor,
    native_delegation_config,
    registered_report_tool,
    respond,
    resume,
)
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids
from tests.response_runner_helpers import _target
from tests.scripted_agent_helpers import PROBE_TOOL, registered_probe_tools
from tests.test_cancelled_response_hook import _envelope, _response_hook_service, _team_bot
from tests.test_delegation_direct_audit import _identity
from tests.test_delegation_execution import _config as _delegation_config
from tests.test_delegation_execution import _leader_identity, _parent_record
from tests.test_queued_message_notify import _bot
from tests.test_response_payload_preparation import _preparation, _request
from tests.test_response_runner_session_lifecycle import _session_runner, _thread_request
from tests.test_tool_hooks import _plugin, _tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.response_turn import ResponseTurnContext, TurnSinks
    from mindroom.teams.envelope import TeamTurnSpec

_LIFECYCLE_EVENTS = (
    EVENT_MESSAGE_BEFORE_RESPONSE,
    EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM,
    EVENT_MESSAGE_AFTER_RESPONSE,
    EVENT_MESSAGE_CANCELLED,
)


def _recording_hooks(events: tuple[str, ...], seen: list[tuple[str, str | None, str | None]]) -> list[object]:
    """Return one hook per event that records the event with the turn IDs its context carries."""

    def recorder(event_name: str) -> object:
        @hook(event_name, name=f"record_{event_name.replace(':', '_')}")
        async def record(ctx: HookContext) -> None:
            seen.append((ctx.event_name, ctx.turn_id, ctx.parent_turn_id))

        return record

    return [recorder(event_name) for event_name in events]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_a_reply_s_hooks_and_tool_calls_carry_its_request_s_turn_id_and_the_next_reply_another(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    """One reply's lifecycle, tool, and session hooks share its request's turn ID; the next reply has its own."""
    seen: list[tuple[str, str | None, str | None]] = []
    callbacks = _recording_hooks(
        (
            *_LIFECYCLE_EVENTS,
            EVENT_TOOL_BEFORE_CALL,
            EVENT_TOOL_ENVIRONMENT,
            EVENT_TOOL_AFTER_CALL,
            EVENT_SESSION_STARTED,
        ),
        seen,
    )
    model = ScriptedModel(
        [
            calls(tool_call("read_file", "call-1", path="one.txt")),
            answer("One."),
            calls(tool_call("read_file", "call-2", path="two.txt")),
            answer("Two."),
        ],
    )
    install_model_providers(monkeypatch, {"default": model})
    first_request = _thread_request()
    second_request = _thread_request("Hello again", "$user_msg2")

    with registered_probe_tools():
        coordinator, _storage = _session_runner(tmp_path, callbacks, streaming=streaming, tools=[PROBE_TOOL])
        await coordinator.generate_response(first_request)
        await coordinator.generate_response(second_request)

    first_end = [event for event, _turn, _parent in seen].index(EVENT_MESSAGE_AFTER_RESPONSE) + 1
    first, second = seen[:first_end], seen[first_end:]
    expected_first = {
        EVENT_TOOL_BEFORE_CALL,
        EVENT_TOOL_ENVIRONMENT,
        EVENT_TOOL_AFTER_CALL,
        EVENT_SESSION_STARTED,
        EVENT_MESSAGE_AFTER_RESPONSE,
    } | (set() if streaming else {EVENT_MESSAGE_BEFORE_RESPONSE})
    assert {event for event, _turn, _parent in first} >= expected_first
    assert {event for event, _turn, _parent in second} >= expected_first - {EVENT_SESSION_STARTED}
    assert {(turn, parent) for _event, turn, parent in first} == {(first_request.turn_id, None)}
    assert {(turn, parent) for _event, turn, parent in second} == {(second_request.turn_id, None)}
    assert first_request.turn_id != second_request.turn_id


@pytest.mark.asyncio
async def test_a_delegated_child_s_tool_calls_carry_its_own_turn_id_and_its_caller_s_as_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller's delegation call carries the caller's turn; the child's calls carry a new turn naming the caller's."""
    seen: list[tuple[str, str, str, str | None, str | None]] = []

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(ctx: ToolBeforeCallContext) -> None:
        seen.append(("before", ctx.agent_name, ctx.tool_name, ctx.turn_id, ctx.parent_turn_id))

    @hook(EVENT_TOOL_AFTER_CALL)
    async def after(ctx: ToolAfterCallContext) -> None:
        seen.append(("after", ctx.agent_name, ctx.tool_name, ctx.turn_id, ctx.parent_turn_id))

    config = native_delegation_config(
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["child"]),
            "child": AgentConfig(display_name="Child", tools=["calculator"]),
        },
    )
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    identity = _identity()
    parent = ScriptedModel(
        [calls(tool_call("run_subagent", "delegate", agent_name="child", task="Add one and two")), answer("Done")],
    )
    child = ScriptedModel([calls(tool_call("add", "add", a=1, b=2)), answer("Three")])
    ScriptedAgents({"child": child}, by_session={identity.session_id: parent}).install(monkeypatch)
    registry = HookRegistry.from_plugins([_plugin("turn-ids", [before, after])])
    runtime = replace(
        _delegate_runtime_context(config, paths, execution_identity=identity, hook_registry=registry),
        turn_id="turn-leader",
    )
    turn = replace(
        make_turn_context(
            "leader",
            session_id=identity.session_id,
            run_id="leader-run",
            correlation_id="corr-leader",
            room_id=identity.room_id,
            thread_id=identity.thread_id,
            requester_id=identity.requester_id,
        ),
        prompt="Delegate",
        execution_identity=runtime.execution_identity,
        supports_native_tool_approval=True,
    )

    with tool_runtime_context(runtime):
        result = await ai_response(turn, paths, config)

    assert result.text.endswith("Done")
    [child_turn] = {turn_id for _phase, agent, _tool, turn_id, _parent in seen if agent == "child"}
    assert child_turn not in {None, "turn-leader"}
    assert seen == [
        ("before", "leader", "run_subagent", "turn-leader", None),
        ("before", "child", "add", child_turn, "turn-leader"),
        ("after", "child", "add", child_turn, "turn-leader"),
        ("after", "leader", "run_subagent", "turn-leader", None),
    ]


@pytest.mark.asyncio
async def test_a_child_resumed_after_approval_keeps_its_turn_id_when_a_model_switch_continues_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resumed run and the continuation a model switch starts are one child turn, so their calls share its ID."""
    seen: list[tuple[str, str | None]] = []

    @hook(EVENT_TOOL_BEFORE_CALL, agents=["code"])
    async def before(ctx: ToolBeforeCallContext) -> None:
        seen.append((ctx.tool_name, ctx.turn_id))

    paths = _runtime_paths(tmp_path)
    config = _delegation_config(paths, child_tools=("report", "thread_model"))
    config.models["alternate"] = ModelConfig(provider="openai", id="alternate-model")
    leader = ScriptedModel(
        [calls(tool_call("run_subagent", "delegate-0", agent_name="code", task="Write report")), answer("Parent done")],
    )
    switch = tool_call("switch_thread_model", "call-switch", model_name="alternate", when="after-toolcall")
    later = tool_call("switch_thread_model", "call-later", model_name="default", when="next-turn")
    code = ScriptedModel([calls(tool_call("write_report", "same-call")), calls(switch), calls(later), answer("Done")])
    ScriptedAgents({"leader": leader, "code": code}).install(monkeypatch)
    registry = HookRegistry.from_plugins([_plugin("turn-ids", [before])])
    identity = _leader_identity()
    options = {"config": config, "runtime_paths": paths, "identity": identity, "hook_registry": registry}

    with registered_report_tool():
        paused = await respond("leader", "delegate", **options, run_id="parent-run")
        assert isinstance(paused, PausedAttempt)
        state = DelegationState.from_metadata(_parent_record(config, paths, identity, "parent-run").metadata)
        [pending] = state.pending_tools
        result = await resume(
            approval_executor(config, paths),
            paused,
            _saved_approval_calls(state),
            agent_name="leader",
            **options,
            decisions={str(pending["tool_call_id"]): True},
        )

    assert isinstance(result, CompletedApprovalRun)
    assert [name for name, _turn in seen] == ["write_report", "switch_thread_model", "switch_thread_model"]
    [turn] = {turn for _name, turn in seen}
    assert turn is not None


@pytest.mark.asyncio
async def test_a_custom_event_from_tool_code_carries_the_turn_ids_of_its_call(tmp_path: Path) -> None:
    """A custom event a tool emits carries the turn and parent turn of the context the tool runs in."""
    seen: list[tuple[str, str | None, str | None]] = []
    registry = HookRegistry.from_plugins([_plugin("turn-ids", _recording_hooks(("demo:turn_probe",), seen))])
    context = replace(
        _tool_runtime_context(tmp_path, hook_registry=registry),
        turn_id="turn-child",
        parent_turn_id="turn-parent",
    )

    with tool_runtime_context(context):
        await emit_custom_event("turn-ids", "demo:turn_probe", {})

    assert seen == [("demo:turn_probe", "turn-child", "turn-parent")]


@pytest.mark.asyncio
async def test_contact_before_send_carries_the_turn_ids_of_the_call_that_sends(tmp_path: Path) -> None:
    """The contact policy hook of a send carries the turn and parent turn of the tool call making it."""
    seen: list[tuple[str, str | None, str | None]] = []
    config, paths = _contact_config(tmp_path)
    context = _tool_context(
        config,
        paths,
        *_recording_hooks((EVENT_CONTACT_BEFORE_SEND,), seen),
        turn_id="turn-child",
        parent_turn_id="turn-parent",
    )

    await _check(context)

    assert seen == [(EVENT_CONTACT_BEFORE_SEND, "turn-child", "turn-parent")]


@pytest.mark.asyncio
async def test_enrich_hooks_carry_the_turn_id_of_the_reply_they_prepare(tmp_path: Path) -> None:
    """Message and system enrichment run under the lock with the turn ID of the reply they prepare."""
    seen: list[tuple[str, str | None, str | None]] = []
    bot = _bot(tmp_path)
    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins(
        [_plugin("turn-ids", _recording_hooks((EVENT_MESSAGE_ENRICH, EVENT_SYSTEM_ENRICH), seen))],
    )
    target = _target()
    request = _request(target, _preparation(target), thread_history=ThreadHistoryResult([], is_full_history=True))

    with patch.object(
        bot._inbound_turn_normalizer,
        "build_dispatch_payload_with_attachments",
        new=AsyncMock(return_value=DispatchPayload(prompt="hello")),
    ):
        await bot._request_payload_preparer.prepare(request)

    assert seen == [(EVENT_MESSAGE_ENRICH, request.turn_id, None), (EVENT_SYSTEM_ENRICH, request.turn_id, None)]


@pytest.mark.asyncio
async def test_each_reply_lifecycle_hook_carries_its_reply_s_turn_id(tmp_path: Path) -> None:
    """The before, final-transform, after, and cancelled hooks of a reply carry the turn ID its identity names."""
    seen: list[tuple[str, str | None, str | None]] = []
    _config, service = _response_hook_service(
        tmp_path,
        HookRegistry.from_plugins([_plugin("turn-ids", _recording_hooks(_LIFECYCLE_EVENTS, seen))]),
    )
    envelope = _envelope()
    identity = ResponseIdentity(
        response_kind="ai",
        response_envelope=envelope,
        correlation_id="corr-reply",
        turn_id="turn-reply",
        sources=ResponseSources((envelope.source_event_id,), (envelope.source_event_id,)),
    )

    await service.apply_before_response(identity=identity, response_text="Hi", tool_trace=None, extra_content=None)
    await service.apply_final_response_transform(identity=identity, response_text="Hi")
    await service._emit_after_response(
        identity=identity,
        response_text="Hi",
        response_event_id="$reply",
        delivery_kind="sent",
    )
    await service._emit_cancelled_response(identity=identity)

    assert seen == [(event_name, "turn-reply", None) for event_name in _LIFECYCLE_EVENTS]


@pytest.mark.asyncio
async def test_a_team_reply_s_tools_run_under_its_request_s_turn_id(tmp_path: Path) -> None:
    """A team reply's members call their tools in the tool context of the team's turn."""
    bot = _team_bot(tmp_path)
    observed: list[str | None] = []

    async def team_stream(_spec: TeamTurnSpec, _ctx: ResponseTurnContext, *, sinks: TurnSinks) -> AsyncIterator[str]:
        del sinks
        context = get_tool_runtime_context()
        observed.append(None if context is None else context.turn_id)
        yield "Team reply"

    request = ResponseRequest(
        sources=ResponseSources(pending_event_ids=("$event",), logical_source_event_ids=("$event",)),
        thread_history=[],
        prompt="team prompt",
        user_id="@user:localhost",
        response_envelope=request_envelope(
            room_id="!room:localhost",
            reply_to_event_id="$event",
            prompt="team prompt",
            user_id="@user:localhost",
            agent_name=bot.agent_name,
        ),
    )
    with (
        patch(
            "mindroom.delivery_gateway.send_message_outcome",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$team")),
        ),
        patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            should_use_streaming=AsyncMock(return_value=False),
            team_response_stream=MagicMock(side_effect=team_stream),
        ),
    ):
        await bot._response_runner.generate_team_response_helper(request, member_names=["code"], team_mode="coordinate")

    assert observed == [request.turn_id]
