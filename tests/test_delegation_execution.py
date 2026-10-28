"""Durable delegated runs retain exact parent waits across child approvals, on the owned loop.

A parent reply that delegates pauses on the externally executed ``run_subagent`` call; the delegation driver runs
the child, asks for the child's approvals through the parent's cards, and continues the parent with the child's
result through ``resume_agent``. These tests drive real replies (``ai_response``) and the production approval
execution with one scripted model per agent.
"""

from __future__ import annotations

import asyncio
import json
import threading
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.ai import run_delegated_child_response
from mindroom.approval.response import require_ordered_pause_presentation
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.delegation import execution
from mindroom.delegation.lifecycle import authorize_delegation
from mindroom.delegation.records import DelegationRecordLocator, DelegationRecordOwner
from mindroom.delegation.recovery import cancel_approval_delegations
from mindroom.delegation.state import DELEGATION_STATE_KEY, DelegationState, child_execution_identity
from mindroom.history.storage import new_scope_session
from mindroom.llm.errors import ModelOutputLimitError, ProviderError
from mindroom.llm.events import ToolCompleted
from mindroom.llm.provider import TurnCompleted
from mindroom.llm.runs import RunRecord, RunRequirement, RunStatus, ToolExecution
from mindroom.llm.usage import RunMetrics, TokenCounts
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.session_storage.store import ConversationStore, persist_to
from mindroom.tool_system.progress import tool_progress_reporter
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, agent_workspace_root_path
from mindroom.usage.storage import quote_identifier
from tests.access_schema_support import with_responder_access
from tests.approval_continuation_helpers import approval_continuation, repeats_each_request
from tests.delegation_helpers import (
    RECALLED_MEMORY,
    ScriptedAgents,
    _delegate_runtime_context,
    _runtime_paths,
    _saved_approval_calls,
    approval_calls_for,
    approval_executor,
    pause_parent,
    recall_memories,
    registered_hand_off_tool,
    registered_metadata_probe_tool,
    registered_report_tool,
    respond,
    resume,
)
from tests.fakes.scripted_model import Await, ScriptedModel, ScriptStep, answer, calls, tool_call
from tests.identity_helpers import entity_ids
from tests.test_delegation_direct_audit import _identity
from tests.tool_approval_helpers import gated_functions

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild
    from mindroom.session_storage.store import SaveOutcome
    from mindroom.session_storage.types import ConversationSession

REQUESTER = "@alice:example.org"


def _config(paths: RuntimePaths, *, child_tools: Sequence[str] = ("report",), gate_delegation: bool = False) -> Config:
    """Return the leader that delegates to ``code``, which may delegate to itself; ``write_report`` needs approval."""
    rules = [{"match": "write_report", "action": "require_approval"}]
    if gate_delegation:
        rules.append({"match": "run_subagent", "action": "require_approval"})
    config = with_responder_access(
        Config(
            agents={
                "leader": AgentConfig(display_name="Leader", delegate_to=["code"]),
                "code": AgentConfig(display_name="Code", tools=list(child_tools), delegate_to=["code"]),
            },
            defaults=DefaultsConfig(tools=[]),
            memory={"backend": "none"},
            tool_approval={"rules": rules},
        ),
        "code",
        users=[REQUESTER],
    )
    entity_ids(config, paths)
    return config


def _leader_identity() -> ToolExecutionIdentity:
    return ToolExecutionIdentity("matrix", "leader", REQUESTER, "!room:example.org", None, None, "parent")


def _provider_failure(text: str) -> Await:
    async def fail() -> None:
        raise ProviderError(text, provider="Synthetic")

    return Await(fail)


def _child_script(*, nested: bool, retry: bool, siblings: int) -> list[list]:
    """Script the ``code`` model for every sibling: a gated ``write_report``, approved later, then its answer.

    A nested sibling delegates to another ``code`` first, which does the gated write; the same model answers both.
    A retried sibling answers nothing first, so the reply discards that run and tries again.
    """
    turns: list[list] = []
    for _ in range(siblings):
        if retry:
            turns.append(answer(""))
        if nested:
            turns.append(calls(tool_call("run_subagent", "delegate-0", agent_name="code", task="Nested report")))
        turns.append(calls(tool_call("write_report", "same-call")))
        turns.append(answer("child result"))
        if nested:
            turns.append(answer("child result"))
    return turns


def _record_files(tmp_path: Path) -> list[dict[str, object]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(tmp_path.glob("agents/*/workspace/.mindroom/delegations/*/*/run.json"))
    ]


def _record_events(tmp_path: Path) -> list[dict[str, object]]:
    [path] = tmp_path.glob("agents/*/workspace/.mindroom/delegations/*/*/events.jsonl")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _parent_record(config: Config, paths: RuntimePaths, identity: ToolExecutionIdentity, run_id: str) -> RunRecord:
    record = create_session_storage("leader", config, paths, identity).get_run(run_id)
    assert isinstance(record, RunRecord)
    return record


def _child_identity(child: DelegationChild) -> ToolExecutionIdentity:
    return ToolExecutionIdentity("matrix", "code", REQUESTER, "!room:example.org", None, None, child.session_id)


def _child_record(child: DelegationChild, config: Config, paths: RuntimePaths) -> RunRecord:
    record = create_session_storage("code", config, paths, _child_identity(child)).get_run(child.run_id)
    assert isinstance(record, RunRecord)
    return record


def _distinct_markers(paused: PausedAttempt) -> int:
    """Return how many tool markers a pause shows, after checking that each names a different call."""
    call_ids = [entry.tool_call_id for entry in paused.tool_trace]
    assert len(call_ids) == len(set(call_ids)), call_ids
    assert paused.response_text.count("🔧") == len(call_ids)
    return len(call_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["approve", "deny", "cancel", "cancel_removed", "cancel_completed", "revoke"])
@pytest.mark.parametrize(
    ("siblings", "nested", "retry"),
    [(1, False, False), (2, False, False), (1, True, False), (1, False, True)],
)
async def test_child_approval_survives_parent_reconstruction(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    siblings: int,
    nested: bool,
    retry: bool,
) -> None:
    """Approval pauses the child before its side effect and resumes the exact parent through the production resume."""
    paths = _runtime_paths(tmp_path)
    config = _config(paths)
    identity = _leader_identity()
    if outcome == "cancel_removed":
        config.agents["code"].private = AgentPrivateConfig(per="user", root="private_notes")
        config.agents["leader"].private = AgentPrivateConfig(per="user", root="private_notes")
    leader = ScriptedModel(
        [
            calls(
                *(
                    tool_call("run_subagent", f"delegate-{index}", agent_name="code", task="Write report")
                    for index in range(siblings)
                ),
            ),
            answer("parent result"),
        ],
    )
    code = ScriptedModel(_child_script(nested=nested, retry=retry, siblings=siblings))
    ScriptedAgents({"leader": leader, "code": code}).install(monkeypatch)
    executor = approval_executor(config, paths)

    with registered_report_tool() as report:
        paused = await respond(
            "leader",
            "delegate",
            config=config,
            runtime_paths=paths,
            identity=identity,
            run_id="parent-run",
        )
        assert isinstance(paused, PausedAttempt)
        stored = _parent_record(config, paths, identity, "parent-run")
        state = DelegationState.from_metadata(stored.metadata)
        assert stored.status is RunStatus.paused
        assert report.written == []
        assert state.pending_agent_name == "code"
        assert paused.approval_agent_name == "code"
        # The card names the child's call under the delegation that owns it, ``delegation_id:call_id``, and a
        # nested child's call under every delegation between it and this parent.
        card_id = str(state.pending_tools[0]["tool_call_id"])
        assert card_id.startswith(f"{state.children[0].delegation_id}:")
        assert card_id.endswith(":same-call")
        assert card_id.count(":") == (2 if nested else 1)
        assert [tool.tool_call_id for tool in paused.tools] == [state.pending_tools[0]["tool_call_id"]]
        assert paused.toolkit_owners[("code", "write_report")] == "report"

        if outcome in {"cancel", "cancel_removed", "cancel_completed"}:
            original_config = config.model_copy(update={"agents": dict(config.agents)})
            child = state.children[0]
            child_identity = _child_identity(child)
            if outcome == "cancel_completed":
                # A crash after the child committed completion but before its parent and audit record saw it.
                storage = create_session_storage("code", config, paths, child_identity)
                session = storage.load_session(child.session_id, "agent")
                assert session is not None
                completed = deepcopy(storage.get_run(child.run_id))
                assert isinstance(completed, RunRecord)
                completed.status = RunStatus.completed
                completed.content = "Saved result"
                completed.tools = []
                completed.requirements = []
                storage.save_run(session, completed, record_usage=False)
            if outcome == "cancel_removed":
                config.agents.clear()
            await cancel_approval_delegations(
                approval_continuation(
                    run_id="parent-run",
                    session_id="parent",
                    entity_name="leader",
                    room_id=identity.room_id,
                    thread_id=None,
                    requester_id=REQUESTER,
                    response_event_id="$response",
                    state="failing",
                    execution_identity=replace(
                        child_execution_identity(child),
                        agent_name="leader",
                        session_id="parent",
                    ),
                    delegation_storage_bindings=paused.delegation_storage_bindings,
                ),
                config=config,
                runtime_paths=paths,
                reason="Cancelled by requester",
            )
            expected = RunStatus.completed if outcome == "cancel_completed" else RunStatus.cancelled
            storage = create_session_storage("code", original_config, paths, child_identity)
            assert storage.get_run(child.run_id).status is expected  # type: ignore[union-attr]
            record = await DelegationRecordOwner(original_config, paths).reopen(
                DelegationRecordLocator.from_dict(child.record_locator),
            )
            run_file = json.loads((record.child_workspace / record.scoped_path / "run.json").read_text())
            assert run_file["status"] == ("completed" if outcome == "cancel_completed" else "cancelled")
            # The parent's stored state settles the child too, so no later entry asks about it again.
            parent_state = DelegationState.from_metadata(
                _parent_record(original_config, paths, identity, "parent-run").metadata,
            )
            assert [item.status for item in parent_state.children] == [run_file["status"]]
            assert parent_state.pending_child_id is None
            assert report.written == []
            return
        if outcome == "revoke":
            config.agents["leader"].delegate_to = []
        observed_ids: set[str] = set()
        result: CompletedApprovalRun | PausedAttempt = paused
        shown = _distinct_markers(paused)
        for index in range(siblings):
            assert isinstance(result, PausedAttempt)
            stored = _parent_record(config, paths, identity, "parent-run")
            state = DelegationState.from_metadata(stored.metadata)
            call_id = str(state.pending_tools[0]["tool_call_id"])
            assert call_id not in observed_ids
            observed_ids.add(call_id)
            assert sum(child.status == "completed" for child in state.children) == index
            result = await resume(
                executor,
                result,
                _saved_approval_calls(state),
                agent_name="leader",
                identity=identity,
                config=config,
                runtime_paths=paths,
                decisions={call_id: outcome == "approve"},
                denial_reasons={call_id: None},
                approval_id=f"card-{index}",
            )
            if isinstance(result, CompletedApprovalRun):
                break
            require_ordered_pause_presentation(result, show_tool_calls=True)
            # A sibling answered on an earlier entry keeps its one completed marker; nothing restarts or repeats.
            shown = _distinct_markers(result)
            assert any(
                entry.tool_call_id == call_id and entry.type == "tool_call_completed" for entry in result.tool_trace
            )
            assert len(report.written) == (index + 1 if outcome == "approve" else 0)
        assert isinstance(result, CompletedApprovalRun)
        assert result.response_text.endswith("parent result")
        # The last entry announces no call the last pause had not shown.
        assert result.response_text.count("🔧") == shown
        assert report.written == (["written"] * siblings if outcome == "approve" else [])
        resumed = _parent_record(config, paths, identity, "parent-run")
        assert resumed.status is RunStatus.completed
        assert resumed.content == "parent result"
        result_message = next(message.content for message in resumed.messages if message.tool_call_id == "delegate-0")
        assert isinstance(result_message, str)
        if outcome == "revoke":
            assert "Cannot delegate" in result_message
        else:
            assert "child result" in result_message
        # Every delegation's requirement holds its child's answer as the external result the parent continued with.
        assert all(requirement.external_execution_result for requirement in resumed.requirements or ())
        assert all(not tool.is_paused for tool in resumed.tools or ())


@pytest.mark.asyncio
async def test_a_child_resumed_after_its_approval_reports_its_activity_as_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The approved call a resumed child runs, and what it says next, are its caller's progress, as a fresh child's are."""
    paths = _runtime_paths(tmp_path)
    config = _config(paths)
    identity = _leader_identity()
    leader = ScriptedModel(
        [
            calls(tool_call("run_subagent", "delegate-0", agent_name="code", task="Write report")),
            answer("parent result"),
        ],
    )
    code = ScriptedModel([calls(tool_call("write_report", "same-call")), answer("child result")])
    ScriptedAgents({"leader": leader, "code": code}).install(monkeypatch)
    reports: list[str] = []

    with registered_report_tool() as report:
        paused = await respond(
            "leader",
            "delegate",
            config=config,
            runtime_paths=paths,
            identity=identity,
            run_id="parent-run",
        )
        assert isinstance(paused, PausedAttempt)
        state = DelegationState.from_metadata(_parent_record(config, paths, identity, "parent-run").metadata)
        call_id = str(state.pending_tools[0]["tool_call_id"])
        with tool_progress_reporter(reports.append):
            result = await resume(
                approval_executor(config, paths),
                paused,
                _saved_approval_calls(state),
                agent_name="leader",
                identity=identity,
                config=config,
                runtime_paths=paths,
                decisions={call_id: True},
                denial_reasons={call_id: None},
                approval_id="card-0",
            )

    assert isinstance(result, CompletedApprovalRun)
    assert report.written == ["written"]
    assert reports[-1] == "\nCalling write_report\nchild result"


@pytest.mark.asyncio
async def test_a_reply_whose_continuation_fails_records_every_delegation_it_answered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply that fails after two delegation continuations records both finished calls, from the failed run's record."""
    paths = _runtime_paths(tmp_path)
    config = _config(paths)
    identity = _leader_identity()
    leader = ScriptedModel(
        [
            calls(tool_call("run_subagent", "delegate-0", agent_name="code", task="Summarize")),
            calls(tool_call("run_subagent", "delegate-1", agent_name="code", task="Review")),
            [_provider_failure("provider connection lost")],
        ],
    )
    code = ScriptedModel([answer("summary"), answer("review")])
    ScriptedAgents({"leader": leader, "code": code}).install(monkeypatch)

    text = await respond(
        "leader",
        "delegate",
        config=config,
        runtime_paths=paths,
        identity=identity,
        run_id="parent-run",
    )

    assert isinstance(text, str)
    assert "provider connection lost" in text
    stored = _parent_record(config, paths, identity, "parent-run")
    replay = next(message.content for message in stored.messages if message.role == "assistant")
    assert isinstance(replay, str)
    assert replay.startswith("(turn failed before completion; 2 tool call(s) had finished)")


@pytest.mark.asyncio
async def test_an_approved_call_reads_the_run_metadata_of_the_run_it_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The call an approval resumes reads the paused run's metadata, as the call would have in the turn it paused."""
    paths = _runtime_paths(tmp_path)
    config = _config(paths)
    identity = replace(_leader_identity(), agent_name="code")
    ScriptedAgents(
        {"code": ScriptedModel([calls(tool_call("write_report", "write-call")), answer("Written.")])},
    ).install(monkeypatch)
    with registered_metadata_probe_tool() as probe:
        paused = await respond("code", "Write", config=config, runtime_paths=paths, identity=identity, run_id="run")
        assert isinstance(paused, PausedAttempt)
        result = await resume(
            approval_executor(config, paths),
            paused,
            approval_calls_for(paused, default_agent="code", toolkit_names={"write_report": "report"}),
            agent_name="code",
            identity=identity,
            config=config,
            runtime_paths=paths,
            decisions={"write-call": True},
        )

    assert isinstance(result, CompletedApprovalRun)
    [metadata] = probe.seen
    assert metadata["room_id"] == identity.room_id


@pytest.mark.asyncio
async def test_an_approval_resume_that_pauses_on_a_call_nothing_answers_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed run that pauses on an externally executed call no approval asks about fails the resume.

    Only a delegation's call is answered outside the loop, so a plugin function marked for external execution
    leaves the resumed run paused with nothing to resume it, as it fails a live turn.
    """
    paths = _runtime_paths(tmp_path)
    config = _config(paths)
    identity = replace(_leader_identity(), agent_name="code")
    ScriptedAgents(
        {"code": ScriptedModel([calls(tool_call("write_report", "write-call")), calls(tool_call("hand_off", "hand"))])},
    ).install(monkeypatch)

    with registered_hand_off_tool() as tools:
        paused = await respond("code", "Write", config=config, runtime_paths=paths, identity=identity, run_id="run")
        assert isinstance(paused, PausedAttempt)
        with pytest.raises(RuntimeError) as raised:
            await resume(
                approval_executor(config, paths),
                paused,
                approval_calls_for(paused, default_agent="code", toolkit_names={"write_report": "report"}),
                agent_name="code",
                identity=identity,
                config=config,
                runtime_paths=paths,
                decisions={"write-call": True},
            )

    assert tools.handed_off == []
    stored = create_session_storage("code", config, paths, identity).get_run("run")
    assert stored is not None
    assert stored.status is RunStatus.paused
    # The resume fails naming the waiting call before the reply's continuation runs, not with the run's stale
    # pause notice ("I have tools to execute, but I need confirmation.").
    assert str(raised.value) == "agent run 'run' paused on ['hand_off'] without an approval to ask for"


@pytest.mark.asyncio
async def test_a_blocking_pause_shows_the_delegation_answered_before_it_as_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply that pauses on its second child's approval shows its first delegation done, and the rest waiting."""
    paths = _runtime_paths(tmp_path)
    config = _config(paths)
    leader = ScriptedModel(
        [
            calls(
                tool_call("run_subagent", "delegate-0", agent_name="code", task="Summarize"),
                tool_call("run_subagent", "delegate-1", agent_name="code", task="Write report"),
            ),
        ],
    )
    code = ScriptedModel([answer("summary"), calls(tool_call("write_report", "same-call"))])
    ScriptedAgents({"leader": leader, "code": code}).install(monkeypatch)

    with registered_report_tool() as report:
        paused = await respond(
            "leader",
            "delegate",
            config=config,
            runtime_paths=paths,
            identity=_leader_identity(),
            run_id="parent-run",
        )

    assert isinstance(paused, PausedAttempt)
    assert report.written == []
    [card] = paused.tools
    assert [(entry.tool_call_id, entry.type) for entry in paused.tool_trace] == [
        ("delegate-0", "tool_call_completed"),
        ("delegate-1", "tool_call_started"),
        (card.tool_call_id, "tool_call_started"),
    ]


@pytest.mark.asyncio
async def test_a_call_gated_two_delegations_down_keeps_the_toolkit_of_the_agent_that_made_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leader's card for a grandchild's gated call names the grandchild's toolkit, so the approved call runs."""
    paths = _runtime_paths(tmp_path)
    config = _config(paths, child_tools=())
    config.agents["code"].delegate_to = ["writer"]
    config.agents["writer"] = AgentConfig(display_name="Writer", tools=["report"])
    with_responder_access(config, "writer", users=[REQUESTER])
    entity_ids(config, paths)
    identity = _leader_identity()
    leader = ScriptedModel(
        [
            calls(tool_call("run_subagent", "delegate-0", agent_name="code", task="Write report")),
            answer("parent result"),
        ],
    )
    code = ScriptedModel(
        [calls(tool_call("run_subagent", "delegate-1", agent_name="writer", task="Write it")), answer("code result")],
    )
    writer = ScriptedModel([calls(tool_call("write_report", "write-call")), answer("writer result")])
    ScriptedAgents({"leader": leader, "code": code, "writer": writer}).install(monkeypatch)

    with registered_report_tool() as report:
        paused = await respond(
            "leader",
            "delegate",
            config=config,
            runtime_paths=paths,
            identity=identity,
            run_id="parent-run",
        )
        assert isinstance(paused, PausedAttempt)
        assert paused.toolkit_owners == {("writer", "write_report"): "report"}
        state = DelegationState.from_metadata(_parent_record(config, paths, identity, "parent-run").metadata)
        call_id = str(state.pending_tools[0]["tool_call_id"])
        result = await resume(
            approval_executor(config, paths),
            paused,
            _saved_approval_calls(state),
            agent_name="leader",
            identity=identity,
            config=config,
            runtime_paths=paths,
            decisions={call_id: True},
            denial_reasons={call_id: None},
        )

    assert isinstance(result, CompletedApprovalRun)
    assert result.response_text.endswith("parent result")
    assert report.written == ["written"]


@pytest.mark.asyncio
async def test_a_completed_sibling_keeps_its_result_when_the_policy_changes_before_the_next_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sibling answered on an earlier entry is never authorized again; only the call still waiting meets the change."""
    paths = _runtime_paths(tmp_path)
    config = _config(paths)
    identity = _leader_identity()
    leader = ScriptedModel(
        [
            calls(
                *(
                    tool_call("run_subagent", f"delegate-{index}", agent_name="code", task="Write report")
                    for index in range(2)
                ),
            ),
            answer("parent result"),
        ],
    )
    code = ScriptedModel(_child_script(nested=False, retry=False, siblings=2))
    ScriptedAgents({"leader": leader, "code": code}).install(monkeypatch)
    executor = approval_executor(config, paths)

    with registered_report_tool() as report:
        result = await respond(
            "leader",
            "delegate",
            config=config,
            runtime_paths=paths,
            identity=identity,
            run_id="parent-run",
        )
        for index, revoke in enumerate((False, True)):
            assert isinstance(result, PausedAttempt)
            if revoke:
                config.agents["leader"].delegate_to = []
            state = DelegationState.from_metadata(_parent_record(config, paths, identity, "parent-run").metadata)
            call_id = str(state.pending_tools[0]["tool_call_id"])
            result = await resume(
                executor,
                result,
                _saved_approval_calls(state),
                agent_name="leader",
                identity=identity,
                config=config,
                runtime_paths=paths,
                decisions={call_id: True},
                denial_reasons={call_id: None},
                approval_id=f"card-{index}",
            )

    assert isinstance(result, CompletedApprovalRun)
    assert report.written == ["written"]
    resumed = _parent_record(config, paths, identity, "parent-run")
    answers = {message.tool_call_id: str(message.content) for message in resumed.messages if message.role == "tool"}
    assert "child result" in answers["delegate-0"]
    assert "Cannot delegate" in answers["delegate-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_entity", ["parent", "child"])
async def test_delegated_approval_preserves_redacted_provider_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_entity: str,
) -> None:
    """A provider failure after the approved write keeps its cause, credentials redacted, without replaying the write."""
    paths = _runtime_paths(tmp_path)
    config = _config(paths)
    identity = _leader_identity()
    failure = "provider connection lost: api_key=example-secret"
    leader = ScriptedModel(
        [
            calls(tool_call("run_subagent", "delegate-0", agent_name="code", task="Write report")),
            [_provider_failure(failure)] if failed_entity == "parent" else answer("parent result"),
        ],
    )
    code = ScriptedModel(
        [
            calls(tool_call("write_report", "same-call")),
            [_provider_failure(failure)] if failed_entity == "child" else answer("child result"),
        ],
    )
    ScriptedAgents({"leader": leader, "code": code}).install(monkeypatch)
    executor = approval_executor(config, paths)

    with registered_report_tool() as report:
        paused = await respond(
            "leader",
            "delegate",
            config=config,
            runtime_paths=paths,
            identity=identity,
            run_id="parent-run",
        )
        assert isinstance(paused, PausedAttempt)
        state = DelegationState.from_metadata(_parent_record(config, paths, identity, "parent-run").metadata)
        call_id = str(state.pending_tools[0]["tool_call_id"])
        continuation = resume(
            executor,
            paused,
            _saved_approval_calls(state),
            agent_name="leader",
            identity=identity,
            config=config,
            runtime_paths=paths,
            decisions={call_id: True},
        )
        if failed_entity == "parent":
            with pytest.raises(ProviderError, match="provider connection lost"):
                await continuation
            assert report.written == ["written"]
            failed = _parent_record(config, paths, identity, "parent-run")
            assert failed.status is RunStatus.error
            return
        result = await continuation

    assert isinstance(result, CompletedApprovalRun)
    assert report.written == ["written"]
    resumed = _parent_record(config, paths, identity, "parent-run")
    assert resumed.status is RunStatus.completed
    result_message = next(message.content for message in resumed.messages if message.tool_call_id == "delegate-0")
    assert isinstance(result_message, str)
    assert "provider connection lost" in result_message
    assert "example-secret" not in result_message
    [record] = _record_files(tmp_path)
    assert record["status"] == "failed"
    assert "example-secret" not in json.dumps(record)


@pytest.mark.asyncio
@pytest.mark.parametrize("child_wrote_text", [False, True])
async def test_a_fresh_child_s_provider_failure_reaches_its_parent_as_the_redacted_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    child_wrote_text: bool,
) -> None:
    """The parent and the child's audit record read why the child failed, credentials redacted, even when it wrote text."""
    paths = _runtime_paths(tmp_path)
    config = _config(paths, child_tools=())
    identity = _leader_identity()
    failure = "provider connection lost: api_key=example-secret"
    leader = ScriptedModel(
        [
            calls(tool_call("run_subagent", "delegate-0", agent_name="code", task="Write report")),
            answer("parent result"),
        ],
    )
    earlier = (
        [calls(tool_call("no_such_tool", "child-call"), content="I will look into it.")] if child_wrote_text else []
    )
    code = ScriptedModel([*earlier, [_provider_failure(failure)]])
    ScriptedAgents({"leader": leader, "code": code}).install(monkeypatch)

    await respond("leader", "delegate", config=config, runtime_paths=paths, identity=identity, run_id="parent-run")

    parent = _parent_record(config, paths, identity, "parent-run")
    result = next(message.content for message in parent.messages if message.tool_call_id == "delegate-0")
    assert isinstance(result, str)
    assert result.startswith("Delegation to 'code' failed: provider connection lost: api_key=")
    assert "example-secret" not in result
    assert "I will look into it." not in result
    [record] = _record_files(tmp_path)
    assert record["status"] == "failed"
    assert str(record["error"]).startswith("provider connection lost: api_key=")
    assert "example-secret" not in json.dumps(record)
    events = _record_events(tmp_path)
    assert "example-secret" not in json.dumps(events)
    *_, output = (event for event in events if event["kind"] == "output")
    assert output["data"] == {"content": record["error"]}


@pytest.mark.asyncio
async def test_failed_nested_resume_settles_descendants_and_visible_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed reconstruction leaves no descendant approval or visible tool pending."""
    paths = _runtime_paths(tmp_path)
    config = _config(paths)
    identity = _leader_identity()
    leader = ScriptedModel(
        [
            calls(tool_call("run_subagent", "delegate-0", agent_name="code", task="Write report")),
            answer("parent result"),
        ],
    )
    code = ScriptedModel(_child_script(nested=True, retry=False, siblings=1))
    ScriptedAgents({"leader": leader, "code": code}).install(monkeypatch)

    async def failed_resume(*_args: object, **_kwargs: object) -> RunRecord:
        message = "resume failed"
        raise RuntimeError(message)

    with registered_report_tool() as report:
        paused = await respond(
            "leader",
            "delegate",
            config=config,
            runtime_paths=paths,
            identity=identity,
            run_id="parent-run",
        )
        assert isinstance(paused, PausedAttempt)
        state = DelegationState.from_metadata(_parent_record(config, paths, identity, "parent-run").metadata)
        call_id = str(state.pending_tools[0]["tool_call_id"])
        monkeypatch.setattr(execution, "_continue_child", failed_resume)
        result = await resume(
            approval_executor(config, paths),
            paused,
            _saved_approval_calls(state),
            agent_name="leader",
            identity=identity,
            config=config,
            runtime_paths=paths,
            decisions={call_id: True},
        )

    assert isinstance(result, CompletedApprovalRun)
    assert report.written == []
    resumed = _parent_record(config, paths, identity, "parent-run")
    result_message = next(message.content for message in resumed.messages if message.tool_call_id == "delegate-0")
    assert isinstance(result_message, str)
    assert "resume failed" in result_message
    assert sorted(record["status"] for record in _record_files(tmp_path)) == ["cancelled", "failed"]
    [child] = DelegationState.from_metadata(resumed.metadata).children
    stored = _child_record(child, config, paths)
    assert stored.status is RunStatus.error
    assert not stored.requirements
    [grandchild] = DelegationState.from_metadata(stored.metadata).children
    nested = _child_record(grandchild, config, paths)
    assert nested.status is RunStatus.cancelled
    assert not nested.requirements


def test_native_delegate_uses_external_execution() -> None:
    """A native parent must pause before entering the child call stack."""
    from agno.tools import Toolkit  # noqa: PLC0415 - the Agno toolkit is the authoring API the flags live on

    async def run_subagent(agent_name: str, task: str) -> str:
        return f"{agent_name}: {task}"

    toolkit = Toolkit(name="delegate", tools=[run_subagent])
    functions = gated_functions(toolkit, Config(), registered_tool_name="delegate")
    assert functions["run_subagent"].external_execution is True


def test_detached_delegate_retains_inline_execution() -> None:
    """Detached callers cannot take ownership of Matrix approvals."""
    from agno.tools import Toolkit  # noqa: PLC0415 - the Agno toolkit is the authoring API the flags live on

    async def run_subagent(agent_name: str, task: str) -> str:
        return f"{agent_name}: {task}"

    toolkit = Toolkit(name="delegate", tools=[run_subagent])
    functions = gated_functions(toolkit, Config(), registered_tool_name="delegate", resumable=False)
    assert not functions["run_subagent"].external_execution


@pytest.mark.asyncio
async def test_delegate_policy_denial_never_starts_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """External execution must retain the policy gate on run_subagent itself."""
    paths = _runtime_paths(tmp_path)
    config = _config(paths, child_tools=(), gate_delegation=True)
    identity = _leader_identity()
    leader = ScriptedModel(
        [
            calls(tool_call("run_subagent", "policy-gated", agent_name="code", task="Write report")),
            answer("Denied safely"),
        ],
    )
    ScriptedAgents({"leader": leader, "code": ScriptedModel([])}).install(monkeypatch)

    paused = await respond(
        "leader",
        "delegate",
        config=config,
        runtime_paths=paths,
        identity=identity,
        run_id="parent-run",
    )

    assert isinstance(paused, PausedAttempt)
    stored = _parent_record(config, paths, identity, "parent-run")
    state = DelegationState.from_metadata(stored.metadata)
    assert state.children == []
    assert state.pending_tools[0]["tool_call_id"] == "policy-gated"
    assert [tool.tool_call_id for tool in paused.tools] == ["policy-gated"]
    assert paused.toolkit_owners[("leader", "run_subagent")] == "delegate"

    result = await resume(
        approval_executor(config, paths),
        paused,
        _saved_approval_calls(state),
        agent_name="leader",
        identity=identity,
        config=config,
        runtime_paths=paths,
        decisions={"policy-gated": False},
        denial_reasons={"policy-gated": "No"},
    )

    assert isinstance(result, CompletedApprovalRun)
    resumed = _parent_record(config, paths, identity, "parent-run")
    assert resumed.status is RunStatus.completed
    assert DelegationState.from_metadata(resumed.metadata).children == []
    assert "child was not executed" in str(
        next(message.content for message in resumed.messages if message.tool_call_id == "policy-gated"),
    )


@pytest.mark.asyncio
async def test_member_delegation_gate_and_card_keep_the_member_call_id(tmp_path: Path) -> None:
    """A team member's gated delegation is carded and gated as ``member_id:call_id``, never as the bare call id.

    The member's id is its config name, so the card is asked for the member and its gate is keyed by it.
    """
    paths = _runtime_paths(tmp_path)
    config = _config(paths, child_tools=(), gate_delegation=True)
    identity = _leader_identity()
    requirement = RunRequirement(
        ToolExecution(
            tool_call_id="call",
            tool_name="run_subagent",
            tool_args={"agent_name": "code", "task": "Write report"},
            external_execution_required=True,
        ),
        member_agent_id="code",
    )
    response = RunRecord(
        run_id="parent-run",
        session_id="parent",
        status=RunStatus.paused,
        tools=[requirement.tool_execution],
        requirements=[requirement],
    )
    persisted: list[DelegationState] = []

    async def persist(state: DelegationState) -> None:
        persisted.append(deepcopy(state))

    async def unused_child(**_kwargs: object) -> str:
        pytest.fail("A gated delegation must not execute a child before its gate is approved")

    events: list[object] = []
    options = {
        "response": response,
        "persist": persist,
        "agent_name": "leader",
        "run_child": unused_child,
        "config": config,
        "runtime_paths": paths,
        "execution_identity": identity,
        "delegation_depth": 0,
        "refresh_scheduler": None,
        "approval_calls": (),
        "on_event": events.append,
    }
    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
        state, previous, _own = execution.prepare_delegation_state(
            response,
            agent_name="leader",
            config=config,
            decisions=None,
            denial_reasons=None,
        )
        asked = await execution.advance_delegation_call(
            requirement,
            state=state,
            previous_state=previous,
            decisions=None,
            denial_reasons=None,
            **options,
        )
        assert asked is None
        assert state.pending_agent_name == "code"
        assert state.pending_tools[0]["tool_call_id"] == "code:call"
        assert RunRequirement.from_dict(state.pending_requirements[0]).tool_execution.tool_call_id == "code:call"
        assert persisted[-1].pending_tools == state.pending_tools

        # The answer to that card is kept as the gate of the same key, which settles the call without a child.
        response.metadata = {DELEGATION_STATE_KEY: state.to_dict()}
        decisions = {"code:call": False}
        denial_reasons = {"code:call": "No"}
        state, previous, _own = execution.prepare_delegation_state(
            response,
            agent_name="leader",
            config=config,
            decisions=decisions,
            denial_reasons=denial_reasons,
        )
        assert state.gates == {"code:call": False}
        denied = await execution.advance_delegation_call(
            requirement,
            state=state,
            previous_state=previous,
            decisions=decisions,
            denial_reasons=denial_reasons,
            **options,
        )

    assert denied == "Delegation denied by requester; child was not executed."
    assert state.children == []
    assert [event.call.result for event in events if isinstance(event, ToolCompleted)] == [denied]


@pytest.mark.asyncio
async def test_copied_delegation_call_never_starts_twice_from_one_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker code that copies the pending call in the stored run cannot make one approval start two children."""
    paths = _runtime_paths(tmp_path)
    config = _config(paths, child_tools=(), gate_delegation=True)
    identity = _leader_identity()
    leader = ScriptedModel(
        [
            calls(tool_call("run_subagent", "gated", agent_name="code", task="Write report")),
            answer("Parent done"),
        ],
    )
    ScriptedAgents({"leader": leader, "code": ScriptedModel([answer("Report written")] * 2)}).install(monkeypatch)

    paused = await respond(
        "leader",
        "delegate",
        config=config,
        runtime_paths=paths,
        identity=identity,
        run_id="parent-run",
    )
    assert isinstance(paused, PausedAttempt)
    storage = create_session_storage("leader", config, paths, identity)
    stored = storage.get_run("parent-run")
    assert isinstance(stored, RunRecord)
    state = DelegationState.from_metadata(stored.metadata)
    session = storage.load_session("parent", "agent")
    assert session is not None
    planted = deepcopy(stored)
    assert planted.requirements is not None
    copy = RunRequirement.from_dict(planted.requirements[0].to_dict())
    copy.id = "copied-requirement"
    planted.requirements.append(copy)
    storage.save_run(session, planted, record_usage=False)

    with pytest.raises(RuntimeError, match="same delegation call more than once"):
        await resume(
            approval_executor(config, paths),
            paused,
            _saved_approval_calls(state),
            agent_name="leader",
            identity=identity,
            config=config,
            runtime_paths=paths,
            decisions={"gated": True},
        )

    assert DelegationState.from_metadata(storage.get_run("parent-run").metadata).children == []  # type: ignore[union-attr]
    assert _record_files(tmp_path) == []


@pytest.mark.asyncio
async def test_a_blocking_turn_continues_after_its_delegation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocking turn's parent continues with its delegation's result after the request it paused on, unchanged.

    The paused parent stored the memory results its request sent, so the continuation sends them again where they
    were, then the delegation call and its result.
    """
    paths = _runtime_paths(tmp_path)
    config = _config(paths, child_tools=())
    recall_memories(monkeypatch)
    leader = ScriptedModel(
        [calls(tool_call("run_subagent", "fresh", agent_name="code", task="Write report")), answer("Parent done")],
    )
    ScriptedAgents({"leader": leader, "code": ScriptedModel([answer("Report written")])}).install(monkeypatch)

    text = await respond(
        "leader",
        "delegate",
        config=config,
        runtime_paths=paths,
        identity=_leader_identity(),
        run_id="parent-run",
    )

    assert "Parent done" in text
    paused, continued = leader.requests
    assert RECALLED_MEMORY in str(paused.messages[-1].content)
    assert repeats_each_request(leader.requests)
    assert [message.role for message in continued.messages[len(paused.messages) :]] == ["assistant", "tool"]


@pytest.mark.asyncio
async def test_the_driver_saves_the_parent_as_it_was_handed_over(tmp_path: Path) -> None:
    """The driver saves the parent as it was handed over; a change made while the lane is busy never lands in that write."""
    paths = _runtime_paths(tmp_path)
    store = create_session_storage("leader", _config(paths), paths, _leader_identity())
    session = new_scope_session(session_id="parent", scope_id="leader", is_team=False)
    parent = RunRecord(run_id="parent-run", session_id="parent", status=RunStatus.paused, content="as handed over")
    lane_held = threading.Event()
    release = threading.Event()

    def hold_the_lane(_store: ConversationStore) -> None:
        lane_held.set()
        release.wait(timeout=30)

    holder = asyncio.create_task(run_session_storage_operation(lambda: store, hold_the_lane))
    assert await asyncio.to_thread(lane_held.wait, 30)
    saving = asyncio.create_task(
        execution.persist_delegation_state(store, session, parent, DelegationState()),
    )
    await asyncio.sleep(0)
    parent.content = "changed while the lane was busy"
    release.set()
    await holder
    await saving

    stored = store.get_run("parent-run")
    assert stored is not None
    assert stored.content == "as handed over"
    assert DELEGATION_STATE_KEY in (stored.metadata or {})


@pytest.mark.asyncio
async def test_the_driver_rewrites_the_parent_without_touching_its_recorded_usage(tmp_path: Path) -> None:
    """The loop records a paused run's usage when it stores the pause, so the driver's rewrite leaves that record."""
    paths = _runtime_paths(tmp_path)
    store = create_session_storage("leader", _config(paths), paths, _leader_identity())
    session = new_scope_session(session_id="parent", scope_id="leader", is_team=False)
    recorded = RunRecord(
        run_id="parent-run",
        session_id="parent",
        status=RunStatus.paused,
        metrics=RunMetrics(tokens=TokenCounts(input_tokens=5, output_tokens=2)),
    )
    await persist_to(store, session)(recorded)

    rewritten = replace(recorded, metrics=RunMetrics(tokens=TokenCounts(input_tokens=50, output_tokens=20)))
    await execution.persist_delegation_state(store, session, rewritten, DelegationState())

    with store.transaction(write=False) as connection:
        rows = connection.execute(
            f"SELECT run_id, usage_data FROM {quote_identifier(store.names.usage)}",  # noqa: S608
        ).fetchall()
    assert [(run_id, json.loads(data)["metrics"]["input_tokens"]) for run_id, data in rows] == [("parent-run", 5)]
    stored = store.get_run("parent-run")
    assert stored is not None
    assert DELEGATION_STATE_KEY in (stored.metadata or {})


@pytest.mark.asyncio
async def test_fresh_delegation_continuation_never_runs_planted_stored_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmed call that worker code plants in the stored parent run never executes when a fresh turn resumes.

    The turn continues the run it holds, never the row worker code can write, so the planted call is not even seen.
    """
    paths = _runtime_paths(tmp_path)
    config = _config(paths, child_tools=())
    identity = _leader_identity()
    leader = ScriptedModel(
        [
            calls(tool_call("run_subagent", "fresh", agent_name="code", task="Write report")),
            answer("Parent done"),
        ],
    )
    ScriptedAgents({"leader": leader, "code": ScriptedModel([answer("Report written")])}).install(monkeypatch)
    original_save_run = ConversationStore.save_run
    planted_writes: list[str] = []

    def plant_confirmed_call(
        self: ConversationStore,
        session: ConversationSession,
        run: RunRecord,
        **kwargs: Any,  # noqa: ANN401
    ) -> SaveOutcome:
        # Stands in for a trigger worker code adds to the parent's runs table while the child runs.
        if DELEGATION_STATE_KEY in (run.metadata or {}):
            planted_writes.append(run.run_id)
            run = deepcopy(run)
            run.tools = [
                *(run.tools or ()),
                ToolExecution(
                    tool_call_id="planted",
                    tool_name="send_message",
                    tool_args={"recipient": "attacker"},
                    requires_confirmation=True,
                    confirmed=True,
                ),
            ]
        return original_save_run(self, session, run, **kwargs)

    monkeypatch.setattr(ConversationStore, "save_run", plant_confirmed_call)

    text = await respond(
        "leader",
        "delegate",
        config=config,
        runtime_paths=paths,
        identity=identity,
        run_id="parent-run",
    )

    assert "Parent done" in text
    assert set(planted_writes) == {"parent-run"}
    # The continuation answered only the delegation; the planted call reached neither the model nor a tool.
    assert [message.tool_call_id for message in leader.requests[-1].messages if message.role == "tool"] == ["fresh"]
    assert all(tool.result is None for tool in leader.requests[-1].messages[-1].tool_calls or ())


def test_workflow_grant_authorizes_without_delegate_to(tmp_path: Path) -> None:
    """A workflow persona child needs the caller's dynamic_workflow tool, not a self entry in delegate_to."""
    paths = _runtime_paths(tmp_path)
    with_workflow = Config(
        agents={"leader": AgentConfig(display_name="Leader", tools=["dynamic_workflow"])},
        defaults=DefaultsConfig(tools=[]),
    )
    without_workflow = Config(
        agents={"leader": AgentConfig(display_name="Leader")},
        defaults=DefaultsConfig(tools=[]),
    )
    identity = _identity()
    options = {"runtime_paths": paths, "execution_identity": identity, "depth": 0}

    with tool_runtime_context(_delegate_runtime_context(with_workflow, paths, execution_identity=identity)):
        granted = authorize_delegation(
            "leader",
            "leader",
            "task",
            config=with_workflow,
            grant="dynamic_workflow",
            **options,
        )
        other_agent = authorize_delegation(
            "leader",
            "child",
            "task",
            config=with_workflow,
            grant="dynamic_workflow",
            **options,
        )
        delegate_rule = authorize_delegation("leader", "leader", "task", config=with_workflow, **options)
    with tool_runtime_context(_delegate_runtime_context(without_workflow, paths, execution_identity=identity)):
        revoked = authorize_delegation(
            "leader",
            "leader",
            "task",
            config=without_workflow,
            grant="dynamic_workflow",
            **options,
        )

    assert isinstance(granted, Config)
    assert isinstance(other_agent, str)
    assert isinstance(delegate_rule, str)
    assert isinstance(revoked, str)


@pytest.mark.usefixtures("enforce_turn_authorization")
def test_workflow_grant_runs_for_requester_the_caller_already_serves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow participant is the caller's own copy, so the caller's individual access is not rechecked.

    A requester may reach a team member only through the team's access, which this check would refuse.
    """
    monkeypatch.setattr("mindroom.delegation.lifecycle.is_sender_allowed_for_responder", lambda *_args: False)
    paths = _runtime_paths(tmp_path)
    config = Config(
        agents={"leader": AgentConfig(display_name="Leader", tools=["dynamic_workflow"], delegate_to=["leader"])},
        defaults=DefaultsConfig(tools=[]),
    )
    identity = _identity()
    options = {"config": config, "runtime_paths": paths, "execution_identity": identity, "depth": 0}

    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
        workflow = authorize_delegation("leader", "leader", "task", grant="dynamic_workflow", **options)
        delegate = authorize_delegation("leader", "leader", "task", **options)

    assert isinstance(workflow, Config)
    assert delegate == "Cannot delegate to 'leader': that agent is not allowed to reply to you."


def test_adapted_delegate_functions_carry_the_external_execution_flags(tmp_path: Path) -> None:
    """The adapted delegate functions carry the external-execution flags the approval capability sets."""
    paths = _runtime_paths(tmp_path)
    config = _config(paths, child_tools=())
    toolkit = DelegateTools(
        "leader",
        ["code"],
        paths,
        config,
        agent_workspace_root_path(paths.storage_root, "leader"),
        execution_identity=_leader_identity(),
    )

    flags = {
        function.name: (function.external_execution, function.external_execution_silent)
        for function in gated_functions(toolkit, config, registered_tool_name="delegate").values()
    }
    assert flags == {"run_subagent": (True, True), "continue_subagent": (True, True)}


def _cut_off_turn(call_id: str) -> list[ScriptStep]:
    """Return a response the output limit cut off while it requested ``run_subagent``."""
    *steps, completed = calls(tool_call("run_subagent", call_id, agent_name="code", task="Cut off"))
    assert isinstance(completed, TurnCompleted)
    return [*steps, replace(completed, stop_reason="output_limit")]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["turn", "resume"])
async def test_a_run_keeps_its_hooks_across_its_delegation_continuations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    """Per-run hook state lasts the whole run: the output-limit count reaches its cap across two continuations.

    The leader's run is cut off twice after its first delegation and once more after its second; the third cut-off
    of one run ends it, whether the run is a live turn or an approval resume the delegation driver continues.
    """
    paths = _runtime_paths(tmp_path)
    config = _config(paths, child_tools=())
    leader = ScriptedModel(
        [
            calls(tool_call("run_subagent", "delegate-1", agent_name="code", task="First")),
            _cut_off_turn("cut-1"),
            _cut_off_turn("cut-2"),
            calls(tool_call("run_subagent", "delegate-2", agent_name="code", task="Second")),
            _cut_off_turn("cut-3"),
            answer("The hooks were rebuilt."),
        ],
    )
    code = ScriptedModel([answer("first result"), answer("second result")])
    ScriptedAgents({"leader": leader, "code": code}).install(monkeypatch)
    identity = _leader_identity()

    if path == "turn":
        reply = await respond("leader", "delegate", config=config, runtime_paths=paths, identity=identity)
        assert isinstance(reply, str)
        assert "output limit" in reply.lower()
    else:
        parent = await pause_parent("leader", "delegate", config=config, runtime_paths=paths, identity=identity)
        with (
            tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)),
            pytest.raises(ModelOutputLimitError),
        ):
            await execution._drive_delegations(
                parent.record,
                continue_parent=parent.runner,
                agent_name="leader",
                run_child=run_delegated_child_response,
                config=config,
                runtime_paths=paths,
                execution_identity=identity,
            )
    assert len(leader.requests) == 5
