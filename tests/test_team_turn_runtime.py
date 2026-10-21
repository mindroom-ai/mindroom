"""Team turns on the owned runtime, through ``team_response_stream``, ``team_response``, and the approval resume.

The seam is ``run_team`` fed by scripted providers behind the production member and leader builders: what a team
reply streams and renders, what it stores, how a member's approval pauses and resumes it, which scope its members'
tools address, and how it ends when a member is stopped. The last test drives a turn through ``ResponseRunner``, the
lifecycle owner that writes the interrupted-turn record after the loop stored its own terminal record.
"""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import AsyncMock

import pytest
from agno.tools import Toolkit

from mindroom import agents, response_runner
from mindroom.agent_loop.model_loop import LoopHooks
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.delegation.lifecycle import child_execution_identity
from mindroom.delegation.state import DelegationState
from mindroom.event_journal import ApprovalCall, approval_arguments_digest
from mindroom.history.session_context import create_scope_session_storage
from mindroom.history.storage import read_scope_seen_event_ids
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import HistoryScope
from mindroom.hooks import MessageEnvelope
from mindroom.llm.errors import ProviderError
from mindroom.llm.provider import TextDelta, UsageReported
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.response_runner import ResponseRequest
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import CompletedApprovalRun, ResponsePausedForApproval
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.teams import envelope
from mindroom.teams.envelope import continue_paused_team_run, team_response, team_response_stream
from mindroom.teams.spec import TeamMode
from mindroom.tool_system.declarations import ToolFileAccess
from mindroom.tool_system.events import StructuredStreamChunk
from mindroom.tool_system.metadata import TOOL_METADATA, TOOL_REGISTRY, ToolCategory
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tool_system.runtime_context import (
    build_execution_identity_from_runtime_context,
    get_tool_runtime_context,
    tool_runtime_context,
)
from mindroom.usage.storage import quote_identifier
from tests.access_schema_support import with_current_room_member_access, with_responder_access
from tests.ai_user_id_helpers import _team_orchestrator
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.bot_helpers import make_test_team_bot, unique_room_send_responses
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_journal_support,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_relation_lookup,
    make_turn_context,
    message_origin,
    patch_response_runner_module,
    test_runtime_paths,
)
from tests.delegation_helpers import _saved_approval_calls
from tests.fakes.scripted_model import Await, ScriptedModel, ScriptStep, answer, calls, tool_call, usage
from tests.identity_helpers import entity_ids
from tests.response_attempt_helpers import install_direct_response_admission

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.response_turn import ResponseTurnContext
    from mindroom.session_storage.store import ConversationStore
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

ROOM_ID = "!room:localhost"
THREAD_ID = "$thread:localhost"
REQUESTER_ID = "@user:localhost"
EVENT_ID = "$current:localhost"
SESSION_ID = f"{ROOM_ID}:{THREAD_ID}"
PROBE_TOOL = "team_turn_probe"
TEAM_SCOPE = HistoryScope(kind="team", scope_id="squad")
HEADER = "🤝 **Team Response** (Writer, Checker):\n\n"


class _ProbeToolkit(Toolkit):
    """Two member tools the tests watch: one records its calls, one reports the history scope its runtime context names."""

    calls: ClassVar[list[str]] = []
    scopes: ClassVar[list[str | None]] = []

    def __init__(self) -> None:
        super().__init__(name="probe", tools=[self.touch, self.scope, self.dig])

    async def touch(self, label: str) -> str:
        """Record one call.

        Args:
            label: What to record.

        """
        type(self).calls.append(label)
        return f"touched {label}"

    async def scope(self) -> str:
        """Report the history scope the running member stores to."""
        context = get_tool_runtime_context()
        key = context.history_scope.key if context is not None and context.history_scope is not None else None
        type(self).scopes.append(key)
        return key or "no scope"

    async def dig(self) -> str:
        """Return one large result, so a member's tool loop outgrows its context window."""
        return "r" * 4000


@contextmanager
def registered_probe() -> Iterator[type[_ProbeToolkit]]:
    """Register the probe toolkit and restore the registries afterwards."""
    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()

    @register_tool_with_metadata(
        name=PROBE_TOOL,
        file_access=ToolFileAccess.NONE,
        display_name="Team Turn Probe",
        description="Records the calls a team member makes.",
        category=ToolCategory.DEVELOPMENT,
    )
    def probe_factory() -> type[_ProbeToolkit]:
        return _ProbeToolkit

    _ProbeToolkit.calls.clear()
    _ProbeToolkit.scopes.clear()
    try:
        yield _ProbeToolkit
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)


@pytest.fixture
def probe() -> Iterator[type[_ProbeToolkit]]:
    """Register the probe toolkit for the test."""
    with registered_probe() as toolkit:
        yield toolkit


@dataclass
class _Team:
    """One team turn's config, its scripted models by model name, its recorder, store, and runtime."""

    config: Config
    runtime_paths: RuntimePaths
    models: dict[str, ScriptedModel]
    recorder: TurnRecorder
    storage: ConversationStore
    identity: ToolExecutionIdentity
    runtime: ToolRuntimeContext
    orchestrator: SimpleNamespace

    @property
    def leader(self) -> ScriptedModel:
        return self.models["default"]

    @property
    def writer(self) -> ScriptedModel:
        return self.models["writer-model"]

    @property
    def checker(self) -> ScriptedModel:
        return self.models["checker-model"]

    def member_ids(self) -> list[Any]:
        ids = entity_ids(self.config, self.runtime_paths)
        return [ids["writer"], ids["checker"]]


def _team(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    leader: Sequence[Sequence[ScriptStep]],
    writer: Sequence[Sequence[ScriptStep]] = (),
    checker: Sequence[Sequence[ScriptStep]] = (),
    helper: Sequence[Sequence[ScriptStep]] | None = None,
    mode: str = "coordinate",
    approval_rules: Sequence[ApprovalRuleConfig] = (),
    context_window: int | None = None,
    max_tool_calls_per_turn: int | None = None,
) -> _Team:
    """Build the ``squad`` team of a writer and a checker whose models are scripted providers.

    ``helper`` scripts the ``helper`` agent, outside the team, that the writer may then delegate to. A
    ``context_window`` turns on compaction with a ``summary`` model, which the test supplies.
    """
    authored = {
        "defaults": {"tools": []},
        "agents": {
            "writer": {"display_name": "Writer", "role": "Writes.", "model": "writer-model", "tools": [PROBE_TOOL]},
            "checker": {"display_name": "Checker", "role": "Checks.", "model": "checker-model", "tools": [PROBE_TOOL]},
            "helper": {"display_name": "Helper", "role": "Helps.", "model": "helper-model", "tools": [PROBE_TOOL]},
        },
        "teams": {
            "squad": {"display_name": "Squad", "role": "Answers.", "agents": ["writer", "checker"], "mode": mode},
        },
        "memory": {"backend": "none"},
        "models": {
            name: {"provider": "openai", "id": "test-model", "extra_kwargs": {"api_key": "test-key"}}
            for name in ("default", "writer-model", "checker-model", "helper-model")
        },
        "timezone": "UTC",
    }
    if helper is not None:
        authored["agents"]["writer"]["delegate_to"] = ["helper"]
    if max_tool_calls_per_turn is not None:
        authored["teams"]["squad"]["max_tool_calls_per_turn"] = max_tool_calls_per_turn
    if context_window is not None:
        for model in authored["models"].values():
            model["context_window"] = context_window
        authored["models"]["summary"] = {"provider": "openai", "id": "summary-model", "context_window": 100_000}
        authored["defaults"]["compaction"] = {"model": "summary", "reserve_tokens": 500}
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(Config.model_validate(authored), runtime_paths)
    with_current_room_member_access(config)
    with_responder_access(config, "helper", users=[REQUESTER_ID])
    if approval_rules:
        config.tool_approval = ToolApprovalConfig(rules=list(approval_rules))
    models = {
        "default": ScriptedModel([list(turn) for turn in leader]),
        "writer-model": ScriptedModel([list(turn) for turn in writer]),
        "checker-model": ScriptedModel([list(turn) for turn in checker]),
        "helper-model": ScriptedModel([list(turn) for turn in helper or ()]),
    }
    monkeypatch.setattr(agents, "_build_agent_model_provider", lambda _config, _paths, name, _identity: models[name])
    monkeypatch.setattr(envelope, "build_model_provider", lambda _config, _paths, name, **_kwargs: models[name])
    runtime = make_test_tool_runtime_context(
        agent_name="squad",
        target=MessageTarget(
            room_id=ROOM_ID,
            source_thread_id=THREAD_ID,
            resolved_thread_id=THREAD_ID,
            reply_to_event_id=EVENT_ID,
            session_id=SESSION_ID,
        ),
        requester_id=REQUESTER_ID,
        client=SimpleNamespace(user_id="@mindroom_squad:localhost"),
        config=config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        correlation_id="correlation-1",
        membership_turn_id=EVENT_ID,
    )
    identity = build_execution_identity_from_runtime_context(runtime)
    storage = create_scope_session_storage(
        agent_name="checker",
        scope=TEAM_SCOPE,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
    )
    orchestrator = _team_orchestrator(config, runtime_paths)
    orchestrator.agent_bots = {name: SimpleNamespace(running=True) for name in ("writer", "checker", "helper")}
    return _Team(
        config,
        runtime_paths,
        models,
        TurnRecorder(user_message="Hello"),
        storage,
        identity,
        runtime,
        orchestrator,
    )


def _ctx(team: _Team, *, run_id: str = "run-1") -> ResponseTurnContext:
    del team
    return make_turn_context(
        "squad",
        session_id=SESSION_ID,
        run_id=run_id,
        correlation_id="correlation-1",
        reply_to_event_id=EVENT_ID,
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        requester_id=REQUESTER_ID,
    )


def _delegate(call_id: str, member_id: str, task: str) -> list[ScriptStep]:
    return calls(tool_call("delegate_task_to_member", call_id, member_id=member_id, task=task), metrics=usage(40, 4))


async def _stream(team: _Team, *, mode: TeamMode = TeamMode.COORDINATE, **kwargs: Any) -> list[Any]:  # noqa: ANN401
    """Stream one team reply as the response runner does and return its chunks."""
    with tool_runtime_context(team.runtime):
        return [
            chunk
            async for chunk in team_response_stream(
                agent_ids=team.member_ids(),
                message="Hello",
                orchestrator=team.orchestrator,
                execution_identity=team.identity,
                ctx=_ctx(team),
                mode=mode,
                user_id=REQUESTER_ID,
                configured_team_name="squad",
                turn_recorder=team.recorder,
                **kwargs,
            )
        ]


async def _respond(team: _Team, *, mode: TeamMode = TeamMode.COORDINATE, **kwargs: Any) -> str:  # noqa: ANN401
    """Run one blocking team reply and return its text."""
    with tool_runtime_context(team.runtime):
        return await team_response(
            agent_names=["writer", "checker"],
            mode=mode,
            message="Hello",
            orchestrator=team.orchestrator,
            execution_identity=team.identity,
            ctx=_ctx(team),
            user_id=REQUESTER_ID,
            configured_team_name="squad",
            turn_recorder=team.recorder,
            **kwargs,
        )


def _stored_runs(team: _Team) -> tuple[TeamRunRecord, list[RunRecord]]:
    session = team.storage.load_session(SESSION_ID, "team")
    assert session is not None
    [record] = [run for run in session.runs if isinstance(run, TeamRunRecord)]
    members = [run for run in session.runs if isinstance(run, RunRecord)]
    return record, members


def _usage_rows(storage: ConversationStore, run_id: str) -> list[dict[str, Any]]:
    with storage.transaction(write=False) as connection:
        rows = connection.execute(
            f"SELECT usage_data FROM {quote_identifier(storage.names.usage)} WHERE run_id = ?",  # noqa: S608
            (run_id,),
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


@pytest.mark.asyncio
async def test_a_streamed_team_reply_renders_the_members_and_the_consensus_and_stores_their_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """The document grows member by member and ends with the consensus; the team and member rows are stored once."""
    team = _team(
        tmp_path,
        monkeypatch,
        leader=[_delegate("call-team", "writer", "Report"), answer("Consensus answer.", usage(50, 5))],
        writer=[calls(tool_call("touch", "call-touch", label="a"), metrics=usage(10, 1)), answer("Member answer.")],
    )

    chunks = await _stream(team, show_tool_calls=False)

    assert probe.calls == ["a"]
    assert all(isinstance(chunk, StructuredStreamChunk) for chunk in chunks)
    assert chunks[-1].content == f"{HEADER}**Writer**: Member answer.\n\n\n**Team Consensus**:\n\nConsensus answer."
    # Hidden tool calls still reach the reply's tool trace: the member's in its slot, the delegation in the team's.
    assert [(entry.tool_name, entry.scope_key) for entry in chunks[-1].tool_trace or []] == [
        ("touch", "agent:writer"),
        ("delegate_task_to_member", "team"),
    ]
    assert team.recorder.outcome == "completed"
    assert team.recorder.assistant_text == chunks[-1].content
    record, members = _stored_runs(team)
    assert (record.status, record.content, record.team_id) == (RunStatus.completed, "Consensus answer.", "squad")
    [member] = members
    assert (member.agent_id, member.parent_run_id, member.status) == ("writer", record.run_id, RunStatus.completed)
    assert [message.role for message in member.messages] == ["user", "assistant", "tool", "assistant"]
    assert record.tools is not None
    assert [(tool.tool_name, tool.result) for tool in record.tools] == [("delegate_task_to_member", "Member answer.")]
    # The leader's prompt names both members and the mode; the member got the task alone.
    leader_system = team.leader.requests[0].messages[0].content
    assert isinstance(leader_system, str)
    assert '<member id="writer" name="Writer">' in leader_system
    assert "coordinate mode" in leader_system
    assert [message.content for message in team.writer.requests[0].messages[1:]] == ["Report"]
    assert team.checker.requests == []
    session = team.storage.load_session(SESSION_ID, "team")
    assert session is not None
    assert EVENT_ID in read_scope_seen_event_ids(team.storage, session, TEAM_SCOPE)


@pytest.mark.asyncio
@pytest.mark.parametrize("show_tool_calls", [True, False])
async def test_the_streamed_document_keeps_the_canonical_member_and_consensus_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
    show_tool_calls: bool,
) -> None:
    """The final document is the member slots then the consensus; shown tool calls mark each slot, the record never."""
    del probe
    team = _team(
        tmp_path,
        monkeypatch,
        leader=[_delegate("call-team", "writer", "Report"), answer("Consensus answer.", usage(50, 5))],
        writer=[calls(tool_call("touch", "call-touch", label="a"), metrics=usage(10, 1)), answer("Member answer.")],
    )

    chunks = await _stream(team, show_tool_calls=show_tool_calls)

    canonical = f"{HEADER}**Writer**: Member answer.\n\n\n**Team Consensus**:\n\nConsensus answer."
    shown = (
        f"{HEADER}**Writer**:\n\n🔧 `touch` [1]\n\nMember answer.\n\n\n"
        "**Team Consensus**:\n\n🔧 `delegate_task_to_member` [2]\n\nConsensus answer."
    )
    assert chunks[-1].content == (shown if show_tool_calls else canonical)
    assert team.recorder.assistant_text == canonical


@pytest.mark.asyncio
async def test_a_blocking_team_reply_sends_non_streaming_requests_and_renders_the_same_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    del probe
    team = _team(
        tmp_path,
        monkeypatch,
        leader=[_delegate("call-team", "checker", "Check"), answer("All good.", usage(50, 5))],
        checker=[answer("Checked.")],
    )

    text = await _respond(team, show_tool_calls=False)

    assert text == f"{HEADER}**Checker**: Checked.\n\n\n**Team Consensus**:\n\nAll good."
    assert all(not request.stream for request in team.leader.requests)
    record, members = _stored_runs(team)
    assert record.status is RunStatus.completed
    assert [member.agent_id for member in members] == ["checker"]


@pytest.mark.asyncio
async def test_a_collaborate_team_puts_the_task_to_every_member_and_reads_them_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    del probe
    team = _team(
        tmp_path,
        monkeypatch,
        leader=[
            calls(tool_call("delegate_task_to_members", "call-all", task="One fact"), metrics=usage(40, 4)),
            answer("Two facts.", usage(50, 5)),
        ],
        writer=[answer("Writer fact.")],
        checker=[answer("Checker fact.")],
        mode="collaborate",
    )

    chunks = await _stream(team, mode=TeamMode.COLLABORATE, show_tool_calls=False)

    assert chunks[-1].content == (
        f"{HEADER}**Writer**: Writer fact.\n\n**Checker**: Checker fact.\n\n\n**Team Consensus**:\n\nTwo facts."
    )
    record, members = _stored_runs(team)
    assert record.tools is not None
    assert record.tools[0].result == "Writer fact.Checker fact."
    assert sorted(member.agent_id or "" for member in members) == ["checker", "writer"]
    assert "broadcast mode" in str(team.leader.requests[0].messages[0].content)


@pytest.mark.asyncio
async def test_a_member_tool_addresses_the_team_history_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """A member's tools read the team scope as the scope the running entity stores to."""
    team = _team(
        tmp_path,
        monkeypatch,
        leader=[_delegate("call-team", "writer", "Which scope?"), answer("Done.", usage(50, 5))],
        writer=[calls(tool_call("scope", "call-scope"), metrics=usage(10, 1)), answer("The team's.")],
    )

    await _stream(team)

    assert probe.scopes == ["team:squad"]
    _, [member] = _stored_runs(team)
    assert member.tools is not None
    assert member.tools[0].result == "team:squad"


@pytest.mark.asyncio
async def test_the_leader_runs_with_compaction_and_receipt_hooks_bound_to_the_team_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """The leader loop gets mid-turn compaction then the receipt before each request, and the notices around tools."""
    del probe
    team = _team(tmp_path, monkeypatch, leader=[answer("Alone.", usage(50, 5))])
    captured: dict[str, Any] = {}
    real_run_team = envelope.run_team
    real_compaction_hook = envelope.mid_turn_compaction_hook

    def capture_run_team(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        captured["hooks"] = kwargs["hooks"]
        return real_run_team(*args, **kwargs)

    def capture_compaction_hook(**kwargs: Any) -> Any:  # noqa: ANN401
        captured["compaction"] = kwargs
        return real_compaction_hook(**kwargs)

    monkeypatch.setattr(envelope, "run_team", capture_run_team)
    monkeypatch.setattr(envelope, "mid_turn_compaction_hook", capture_compaction_hook)

    await _stream(team)

    hooks = captured["hooks"]
    assert isinstance(hooks, LoopHooks)
    assert len(hooks.before_request) == 2
    assert (len(hooks.after_tool_batch), len(hooks.on_resume_entry)) == (1, 1)
    assert hooks.tool_middleware == ()
    compaction = captured["compaction"]
    assert (compaction["is_team"], compaction["scope_id"], compaction["entity_name"]) == (True, "squad", "squad")
    assert compaction["scope"] == TEAM_SCOPE
    assert compaction["model"] is team.leader
    assert compaction["prompt_id"] is not None


def _approval(call_id: str, label: str) -> ApprovalCall:
    return ApprovalCall(
        tool_call_id=call_id,
        tool_name="touch",
        invoking_agent="checker",
        toolkit_name=PROBE_TOOL,
        expires_at_ns=2**62,
        arguments_digest=approval_arguments_digest({"label": label}),
    )


async def _pause_on_the_checker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_Team, ResponsePausedForApproval]:
    team = _team(
        tmp_path,
        monkeypatch,
        leader=[_delegate("call-team", "checker", "Touch it")],
        checker=[calls(tool_call("touch", "call-touch", label="dangerous"), metrics=usage(10, 1))],
        approval_rules=[ApprovalRuleConfig(match="touch", action="require_approval")],
    )
    with pytest.raises(ResponsePausedForApproval) as raised:
        await _stream(team, show_tool_calls=False)
    return team, raised.value


@pytest.mark.asyncio
async def test_a_member_approval_pauses_the_team_with_the_member_s_identity_and_toolkit_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)

    paused = raised.paused
    assert probe.calls == []
    [requirement] = paused.requirements
    assert (requirement.member_agent_id, requirement.member_agent_name) == ("checker", "Checker")
    assert requirement.tool_execution.tool_call_id == "call-touch"
    assert paused.toolkit_owners[("checker", "touch")] == PROBE_TOOL
    assert ("writer", "touch") in paused.toolkit_owners
    assert paused.approval_agent_name is None
    assert paused.runtime_model_name == "default"
    assert paused.team_member_model_names == (("checker", "checker-model"), ("writer", "writer-model"))
    assert [member["config_name"] for member in paused.response_presentation_state["members"]] == [
        "writer",
        "checker",
    ]
    assert [(entry.tool_name, entry.scope_key) for entry in paused.tool_trace] == [
        ("touch", "agent:checker"),
        ("delegate_task_to_member", "team"),
    ]
    # With tool calls hidden and no member text yet, the pause shows no document of its own.
    assert paused.response_text == ""
    record, [member] = _stored_runs(team)
    assert (record.status, member.status) == (RunStatus.paused, RunStatus.paused)
    assert record.member_responses[0].run_id == member.run_id
    assert record.content == "Team run paused. The following require input:\n- Checker: touch requires confirmation"
    assert team.recorder.outcome == "suspended"


async def _resume(team: _Team, paused: Any, *, approved: bool, calls_: Sequence[ApprovalCall]) -> Any:  # noqa: ANN401
    with tool_runtime_context(team.runtime):
        return await continue_paused_team_run(
            member_names=("writer", "checker"),
            mode=TeamMode.COORDINATE,
            config=team.config,
            runtime_paths=team.runtime_paths,
            execution_identity=team.identity,
            session_id=SESSION_ID,
            run_id=paused.run_id,
            user_id=REQUESTER_ID,
            configured_team_name="squad",
            model_name="default",
            decisions=dict.fromkeys((call.tool_call_id for call in calls_), approved),
            denial_reasons=dict.fromkeys((call.tool_call_id for call in calls_), "Declined by requester"),
            refresh_scheduler=None,
            approval_calls=calls_,
            history_scope=TEAM_SCOPE,
            prior_tool_trace=paused.tool_trace,
            prior_presentation_state=paused.response_presentation_state,
            show_tool_calls=False,
            progress=None,
        )


@pytest.mark.asyncio
async def test_an_approved_member_call_resumes_the_stored_team_run_and_renders_the_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel([answer("The checker touched it.", usage(50, 5))])
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])

    result = await _resume(team, raised.paused, approved=True, calls_=(_approval("call-touch", "dangerous"),))

    assert isinstance(result, CompletedApprovalRun)
    assert probe.calls == ["dangerous"]
    assert result.response_text == f"{HEADER}**Checker**: Touched.\n\n\n**Team Consensus**:\n\nThe checker touched it."
    assert result.metadata_content["io.mindroom.ai_run"]["status"] == "completed"
    record, [member] = _stored_runs(team)
    assert (record.status, member.status) == (RunStatus.completed, RunStatus.completed)
    assert record.requirements == []
    assert member.tools is not None
    assert (member.tools[0].confirmed, member.tools[0].result) == (True, "touched dangerous")
    # The leader reads the member's result where it read the pause notice.
    assert team.leader.requests[-1].messages[-1].content == (
        "Member results after human-in-the-loop resolution:\n[Checker]: Touched."
    )


@pytest.mark.asyncio
async def test_a_denied_member_call_never_runs_and_the_member_answers_the_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel([answer("The checker declined.", usage(50, 5))])
    team.models["checker-model"] = ScriptedModel([answer("I did not touch it.")])

    result = await _resume(team, raised.paused, approved=False, calls_=(_approval("call-touch", "dangerous"),))

    assert isinstance(result, CompletedApprovalRun)
    assert probe.calls == []
    assert team.checker.requests[-1].messages[-1].content == "Declined by requester"
    _, [member] = _stored_runs(team)
    assert member.tools is not None
    assert (member.tools[0].confirmed, member.tools[0].tool_call_error) == (False, True)


@pytest.mark.asyncio
async def test_a_failed_resume_stores_the_error_over_the_paused_row_and_ran_the_approved_tool_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """A leader request that fails after the approved call ran reaches the caller; the rows record the outcome."""
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)

    async def lose_the_provider() -> None:
        msg = "provider connection lost"
        raise ProviderError(msg, provider="Synthetic")

    team.models["default"] = ScriptedModel([[Await(lose_the_provider)]])
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])

    with pytest.raises(ProviderError, match="provider connection lost"):
        await _resume(team, raised.paused, approved=True, calls_=(_approval("call-touch", "dangerous"),))

    assert probe.calls == ["dangerous"]
    record, [member] = _stored_runs(team)
    assert (record.status, member.status) == (RunStatus.error, RunStatus.completed)
    assert "provider connection lost" in str(record.content)
    assert member.tools is not None
    assert (member.tools[0].confirmed, member.tools[0].result) == (True, "touched dangerous")


@pytest.mark.asyncio
async def test_a_resume_is_refused_before_anything_runs_when_the_approval_no_longer_fits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """A member that lost the approved toolkit, or an approval naming another call, leaves the paused row untouched."""
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel([])
    team.models["checker-model"] = ScriptedModel([])

    with pytest.raises(RuntimeError, match="no longer match the pending requirements"):
        await _resume(team, raised.paused, approved=True, calls_=(_approval("call-other", "dangerous"),))
    team.config.agents["checker"].tools = []
    with pytest.raises(ExceptionGroup) as refused:
        await _resume(team, raised.paused, approved=True, calls_=(_approval("call-touch", "dangerous"),))

    [error] = refused.value.exceptions
    assert str(error) == f"Saved approval toolkit {PROBE_TOOL!r} is no longer permitted; retry the request"
    assert probe.calls == []
    assert (team.leader.requests, team.checker.requests) == ([], [])
    record, _ = _stored_runs(team)
    assert record.status is RunStatus.paused


def _delegate_to_helper(task: str) -> list[ScriptStep]:
    return calls(tool_call("run_subagent", "call-sub", agent_name="helper", task=task), metrics=usage(10, 1))


def _delegation_state(team: _Team) -> tuple[TeamRunRecord, RunRecord, DelegationState]:
    record, [member] = _stored_runs(team)
    return record, member, DelegationState.from_metadata(record.metadata)


@pytest.mark.asyncio
async def test_a_member_s_delegation_runs_its_child_and_the_team_continues_with_the_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """A member's ``run_subagent`` pauses the team run; the driver runs the child, then the member and the leader continue."""
    team = _team(
        tmp_path,
        monkeypatch,
        leader=[_delegate("call-team", "writer", "Research it"), answer("Consensus.", usage(50, 5))],
        writer=[_delegate_to_helper("Dig"), answer("Writer: the helper found it.")],
        helper=[calls(tool_call("touch", "call-helper", label="dug"), metrics=usage(5, 1)), answer("Helper found it.")],
    )

    chunks = await _stream(team)

    assert probe.calls == ["dug"]
    assert chunks[-1].content == (
        f"{HEADER}**Writer**:\n\n🔧 `run_subagent` [1]\n\nWriter: the helper found it.\n\n\n"
        "**Team Consensus**:\n\n🔧 `delegate_task_to_member` [2]\n\nConsensus."
    )
    # The driver's events about the member's call land in the member's slot, once.
    assert [(entry.tool_name, entry.scope_key) for entry in chunks[-1].tool_trace or []] == [
        ("run_subagent", "agent:writer"),
        ("delegate_task_to_member", "team"),
    ]
    assert team.recorder.outcome == "completed"
    record, member, state = _delegation_state(team)
    assert (record.status, record.content) == (RunStatus.completed, "Consensus.")
    assert (member.agent_id, member.status) == ("writer", RunStatus.completed)
    assert member.tools is not None
    assert member.tools[0].tool_name == "run_subagent"
    assert "Helper found it." in str(member.tools[0].result)
    [child] = state.children
    assert (child.caller_agent_name, child.child_agent_name, child.status) == ("writer", "helper", "completed")
    helper_store = create_session_storage("helper", team.config, team.runtime_paths, child_execution_identity(child))
    helper_run = helper_store.get_run(child.run_id)
    assert helper_run is not None
    assert (helper_run.status, helper_run.content) == (RunStatus.completed, "Helper found it.")
    # The writer read the helper's result where its call paused; the leader read the writer's answer.
    assert "Helper found it." in str(team.writer.requests[-1].messages[-1].content)
    assert team.leader.requests[-1].messages[-1].content == (
        "Member results after human-in-the-loop resolution:\n[Writer]: Writer: the helper found it."
    )


@pytest.mark.asyncio
async def test_a_team_paused_on_its_child_s_approval_resumes_through_the_delegation_driver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """The approval a member's child needs is asked through the team's pause; approving it resumes child, member, and leader."""
    team = _team(
        tmp_path,
        monkeypatch,
        leader=[_delegate("call-team", "writer", "Research it"), answer("Consensus.", usage(50, 5))],
        writer=[_delegate_to_helper("Dig"), answer("Writer: dug.")],
        helper=[calls(tool_call("touch", "call-helper", label="deep"), metrics=usage(5, 1)), answer("Dug deep.")],
        approval_rules=[ApprovalRuleConfig(match="touch", action="require_approval")],
    )
    with pytest.raises(ResponsePausedForApproval) as raised:
        await _stream(team, show_tool_calls=False)

    paused = raised.value.paused
    assert probe.calls == []
    record, member, state = _delegation_state(team)
    assert (record.status, member.status) == (RunStatus.paused, RunStatus.paused)
    [child] = state.children
    assert (child.caller_agent_name, child.child_agent_name, child.status) == ("writer", "helper", "paused")
    assert state.pending_child_id == child.delegation_id
    [tool] = paused.tools
    assert (tool.tool_name, tool.tool_call_id) == ("touch", f"{child.delegation_id}:call-helper")
    assert paused.toolkit_owners == {("helper", "touch"): PROBE_TOOL}
    assert team.recorder.outcome == "suspended"

    result = await _resume(team, paused, approved=True, calls_=_saved_approval_calls(state))

    assert isinstance(result, CompletedApprovalRun)
    assert probe.calls == ["deep"]
    assert result.response_text == f"{HEADER}**Writer**: Writer: dug.\n\n\n**Team Consensus**:\n\nConsensus."
    record, member, state = _delegation_state(team)
    assert (record.status, member.status) == (RunStatus.completed, RunStatus.completed)
    assert [child.status for child in state.children] == ["completed"]
    assert "Dug deep." in str(team.writer.requests[-1].messages[-1].content)


@pytest.mark.asyncio
async def test_a_stopped_member_keeps_its_usage_and_both_rows_end_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """A Stop while a member works stores the member's cut-off request and the team's row before the stop lands."""
    del probe
    streaming = asyncio.Event()

    async def wait_to_be_cancelled() -> None:
        streaming.set()
        await asyncio.Event().wait()

    team = _team(
        tmp_path,
        monkeypatch,
        leader=[_delegate("call-team", "writer", "Write")],
        writer=[[TextDelta("Dra"), UsageReported(usage(30, 2), final=False), Await(wait_to_be_cancelled)]],
    )
    task = asyncio.create_task(_stream(team))
    await asyncio.wait_for(streaming.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record, [member] = _stored_runs(team)
    assert (record.status, member.status) == (RunStatus.cancelled, RunStatus.cancelled)
    assert member.parent_run_id == record.run_id
    [member_usage] = _usage_rows(team.storage, member.run_id)
    assert member_usage["metrics"]["input_tokens"] == 30
    [leader_usage] = _usage_rows(team.storage, record.run_id)
    assert leader_usage["metrics"]["input_tokens"] == 40
    assert team.recorder.outcome == "interrupted"
    assert (
        team.recorder.assistant_text == "**Writer**: Dra\n\n\n*No team consensus - showing individual responses only*"
    )


def _runner(team: _Team, tmp_path: Path) -> Any:  # noqa: ANN401
    """Build the squad's team bot on the team's config, with a mocked Matrix client, and return its runner."""
    bot = make_test_team_bot(
        AgentMatrixUser(
            agent_name="squad",
            password=TEST_PASSWORD,
            display_name="Squad",
            user_id="@mindroom_squad:localhost",
        ),
        tmp_path,
        team.config,
        team.runtime_paths,
        rooms=[ROOM_ID],
    )
    install_direct_response_admission(bot)
    bot.client = make_matrix_client_mock(user_id="@mindroom_squad:localhost")
    unique_room_send_responses(bot.client)
    install_runtime_journal_support(bot)
    bot.orchestrator = team.orchestrator
    return bot._response_runner


def _request(prompt: str = "Hello") -> ResponseRequest:
    target = MessageTarget.resolve(room_id=ROOM_ID, thread_id=THREAD_ID, reply_to_event_id=EVENT_ID)
    return ResponseRequest(
        thread_history=[],
        prompt=prompt,
        user_id=REQUESTER_ID,
        response_envelope=MessageEnvelope(
            source_event_id=EVENT_ID,
            target=target,
            body=prompt,
            attachment_ids=(),
            mentioned_agents=(),
            agent_name="squad",
            origin=message_origin(sender_id=REQUESTER_ID, requester_id=REQUESTER_ID, source_kind="message"),
        ),
        sources=ResponseSources(pending_event_ids=(EVENT_ID,), logical_source_event_ids=(EVENT_ID,)),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
async def test_the_runner_writes_the_interrupted_record_after_the_loop_stored_the_cancelled_team_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
    streaming: bool,
) -> None:
    """Through ``ResponseRunner``, a stop mid-reply stores the ``CANCELLED`` team row before the interrupted record.

    The record the lifecycle owner writes replaces the cancelled row, and no later save of the run overwrites it.
    """
    del probe
    working = asyncio.Event()

    async def wait_to_be_cancelled() -> None:
        working.set()
        await asyncio.Event().wait()

    team = _team(
        tmp_path,
        monkeypatch,
        leader=[_delegate("call-team", "writer", "Write")],
        writer=[[TextDelta("Dra"), UsageReported(usage(30, 2), final=False), Await(wait_to_be_cancelled)]],
    )
    runner = _runner(team, tmp_path)
    statuses_when_interrupted_record_written: list[RunStatus | None] = []
    original = response_runner.persist_interrupted_replay_snapshot

    def spy(**kwargs: Any) -> None:  # noqa: ANN401
        stored = team.storage.get_run(kwargs["run_id"])
        statuses_when_interrupted_record_written.append(stored.status if stored is not None else None)
        original(**kwargs)

    monkeypatch.setattr(response_runner, "persist_interrupted_replay_snapshot", spy)

    with patch_response_runner_module(should_use_streaming=AsyncMock(return_value=streaming)):
        task = asyncio.create_task(
            runner.generate_team_response_helper(_request(), team_agents=team.member_ids(), team_mode="coordinate"),
        )
        await asyncio.wait_for(working.wait(), timeout=5)
        # A Stop cancels the reply span's attempt task, as the stop button does.
        spans = runner.deps.replies.spans
        assert [spans.cancel(span_id, cancel_source="user_stop") for span_id in spans.live_span_ids()] == [True]
        await asyncio.wait_for(task, timeout=5)

    assert statuses_when_interrupted_record_written == [RunStatus.cancelled]
    session = team.storage.load_session(SESSION_ID, "team")
    assert session is not None
    [record] = [run for run in session.runs if isinstance(run, TeamRunRecord)]
    assert record.metadata is not None
    assert record.metadata["mindroom_original_status"] == "cancelled"
    [member] = [run for run in session.runs if isinstance(run, RunRecord)]
    assert member.status is RunStatus.cancelled
    [row] = _usage_rows(team.storage, member.run_id)
    assert row["metrics"]["input_tokens"] == 30
