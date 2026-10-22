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
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from mindroom import response_runner
from mindroom.agent_loop.model_loop import LoopHooks
from mindroom.approval.receipt import approval_receipt_context, build_approval_receipt
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.delegation.lifecycle import child_execution_identity
from mindroom.delegation.state import DelegationState
from mindroom.event_journal import ApprovalDecision
from mindroom.history.storage import read_scope_seen_event_ids
from mindroom.hooks import EnrichmentItem, MessageEnvelope
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
from mindroom.teams.spec import TeamMode
from mindroom.tool_system.events import StructuredStreamChunk
from mindroom.usage.storage import quote_identifier
from tests.bot_helpers import make_test_team_bot, unique_room_send_responses
from tests.conftest import (
    TEST_PASSWORD,
    install_runtime_journal_support,
    make_matrix_client_mock,
    message_origin,
    patch_response_runner_module,
)
from tests.delegation_helpers import _saved_approval_calls
from tests.fakes.scripted_model import Await, ScriptedModel, ScriptStep, answer, calls, tool_call, usage
from tests.response_attempt_helpers import install_direct_response_admission
from tests.team_turn_helpers import (  # noqa: F401 - squad_probe is a pytest fixture
    EVENT_ID,
    PROBE_TOOL,
    REQUESTER_ID,
    ROOM_ID,
    SESSION_ID,
    TEAM_SCOPE,
    THREAD_ID,
    ProbeToolkit,
    SquadTurn,
    blocking_reply,
    delegate,
    member_approval,
    resume_approval,
    squad_probe,
    squad_turn,
    stored_runs,
    stream_reply,
    turn_context,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.session_storage.store import ConversationStore

HEADER = "🤝 **Team Response** (Writer, Checker):\n\n"

# Every test registers the members' probe toolkit.
pytestmark = pytest.mark.usefixtures("squad_probe")


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
) -> None:
    """The document grows member by member and ends with the consensus; the team and member rows are stored once."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Report"), answer("Consensus answer.", usage(50, 5))],
        writer=[calls(tool_call("touch", "call-touch", label="a"), metrics=usage(10, 1)), answer("Member answer.")],
    )

    chunks = await stream_reply(team, show_tool_calls=False)

    assert ProbeToolkit.calls == ["a"]
    assert all(isinstance(chunk, StructuredStreamChunk) for chunk in chunks)
    assert chunks[-1].content == f"{HEADER}**Writer**: Member answer.\n\n\n**Team Consensus**:\n\nConsensus answer."
    # Hidden tool calls still reach the reply's tool trace: the member's in its slot, the delegation in the team's.
    assert [(entry.tool_name, entry.scope_key) for entry in chunks[-1].tool_trace or []] == [
        ("touch", "agent:writer"),
        ("delegate_task_to_member", "team"),
    ]
    assert team.recorder.outcome == "completed"
    assert team.recorder.assistant_text == chunks[-1].content
    record, members = stored_runs(team)
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
    show_tool_calls: bool,
) -> None:
    """The final document is the member slots then the consensus; shown tool calls mark each slot, the record never."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Report"), answer("Consensus answer.", usage(50, 5))],
        writer=[calls(tool_call("touch", "call-touch", label="a"), metrics=usage(10, 1)), answer("Member answer.")],
    )

    chunks = await stream_reply(team, show_tool_calls=show_tool_calls)

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
) -> None:
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "checker", "Check"), answer("All good.", usage(50, 5))],
        checker=[answer("Checked.")],
    )

    text = await blocking_reply(team, show_tool_calls=False)

    assert text == f"{HEADER}**Checker**: Checked.\n\n\n**Team Consensus**:\n\nAll good."
    assert all(not request.stream for request in team.leader.requests)
    record, members = stored_runs(team)
    assert record.status is RunStatus.completed
    assert [member.agent_id for member in members] == ["checker"]


@pytest.mark.asyncio
async def test_a_collaborate_team_puts_the_task_to_every_member_and_reads_them_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team = squad_turn(
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

    chunks = await stream_reply(team, mode=TeamMode.COLLABORATE, show_tool_calls=False)

    assert chunks[-1].content == (
        f"{HEADER}**Writer**: Writer fact.\n\n**Checker**: Checker fact.\n\n\n**Team Consensus**:\n\nTwo facts."
    )
    record, members = stored_runs(team)
    assert record.tools is not None
    assert record.tools[0].result == "Writer fact.Checker fact."
    assert sorted(member.agent_id or "" for member in members) == ["checker", "writer"]
    assert "broadcast mode" in str(team.leader.requests[0].messages[0].content)


@pytest.mark.asyncio
async def test_a_member_tool_addresses_the_team_history_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member's tools read the team scope as the scope the running entity stores to."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Which scope?"), answer("Done.", usage(50, 5))],
        writer=[calls(tool_call("scope", "call-scope"), metrics=usage(10, 1)), answer("The team's.")],
    )

    await stream_reply(team)

    assert ProbeToolkit.scopes == ["team:squad"]
    _, [member] = stored_runs(team)
    assert member.tools is not None
    assert member.tools[0].result == "team:squad"


@pytest.mark.asyncio
async def test_the_turn_s_transient_context_reaches_the_leader_and_is_stored_in_no_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient turn context is sent with the leader's request, and neither the team run nor a member run stores it."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Draft it"), answer("Done.", usage(50, 5))],
        writer=[answer("Drafted.")],
    )
    transient = "Sample transient turn context."
    ctx = replace(turn_context(), transient_enrichment_items=(EnrichmentItem(key="sample", text=transient),))

    await stream_reply(team, ctx=ctx)

    assert [message.role for message in team.leader.requests[0].messages if transient in str(message.content)] == [
        "user",
    ]
    record, members = stored_runs(team)
    assert len(members) == 1
    assert all(transient not in json.dumps(run.to_dict()) for run in [record, *members])


@pytest.mark.asyncio
async def test_the_leader_runs_with_compaction_and_receipt_hooks_bound_to_the_team_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leader loop gets mid-turn compaction then the receipt before each request, and the notices around tools."""
    team = squad_turn(tmp_path, monkeypatch, leader=[answer("Alone.", usage(50, 5))])
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

    await stream_reply(team)

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


async def _pause_on_the_checker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[SquadTurn, ResponsePausedForApproval]:
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "checker", "Touch it")],
        checker=[calls(tool_call("touch", "call-touch", label="dangerous"), metrics=usage(10, 1))],
        approval_rules=[ApprovalRuleConfig(match="touch", action="require_approval")],
    )
    with pytest.raises(ResponsePausedForApproval) as raised:
        await stream_reply(team, show_tool_calls=False)
    return team, raised.value


@pytest.mark.asyncio
async def test_a_member_approval_pauses_the_team_with_the_member_s_identity_and_toolkit_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)

    paused = raised.paused
    assert ProbeToolkit.calls == []
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
    record, [member] = stored_runs(team)
    assert (record.status, member.status) == (RunStatus.paused, RunStatus.paused)
    assert record.member_responses[0].run_id == member.run_id
    assert record.content == "Team run paused. The following require input:\n- Checker: touch requires confirmation"
    assert team.recorder.outcome == "suspended"


@pytest.mark.asyncio
async def test_an_approved_member_call_resumes_the_stored_team_run_and_renders_the_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel([answer("The checker touched it.", usage(50, 5))])
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])

    result = await resume_approval(
        team,
        raised.paused,
        approved=True,
        calls_=(member_approval("call-touch", "dangerous"),),
    )

    assert isinstance(result, CompletedApprovalRun)
    assert ProbeToolkit.calls == ["dangerous"]
    assert result.response_text == f"{HEADER}**Checker**: Touched.\n\n\n**Team Consensus**:\n\nThe checker touched it."
    assert result.metadata_content["io.mindroom.ai_run"]["status"] == "completed"
    record, [member] = stored_runs(team)
    assert (record.status, member.status) == (RunStatus.completed, RunStatus.completed)
    assert record.requirements == []
    assert member.tools is not None
    assert (member.tools[0].confirmed, member.tools[0].result) == (True, "touched dangerous")
    # The leader reads the member's result where it read the pause notice.
    assert team.leader.requests[-1].messages[-1].content == (
        "Member results after human-in-the-loop resolution:\n[Checker]: Touched."
    )


@pytest.mark.asyncio
async def test_an_approval_resume_sends_the_receipt_to_the_member_and_the_leader_and_stores_it_in_neither(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The member whose call was approved and the leader each read the trusted receipt in their first resumed request.

    The response runner binds the receipt around the resume; it is projected into each loop's request only, so no
    stored run holds it.
    """
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel([answer("The checker touched it.", usage(50, 5))])
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])
    call = member_approval("call-touch", "dangerous")
    receipt = build_approval_receipt(
        (replace(call, decision=ApprovalDecision.APPROVED, human_approval_required=True),),
    )

    with approval_receipt_context(receipt):
        await resume_approval(team, raised.paused, approved=True, calls_=(call,))

    for model in (team.checker, team.leader):
        [request] = model.requests
        assert [message.role for message in request.messages if receipt in str(message.content)] == ["system"]
    record, members = stored_runs(team)
    assert all(receipt not in json.dumps(run.to_dict()) for run in [record, *members])


@pytest.mark.asyncio
async def test_a_denied_member_call_never_runs_and_the_member_answers_the_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel([answer("The checker declined.", usage(50, 5))])
    team.models["checker-model"] = ScriptedModel([answer("I did not touch it.")])

    result = await resume_approval(
        team,
        raised.paused,
        approved=False,
        calls_=(member_approval("call-touch", "dangerous"),),
    )

    assert isinstance(result, CompletedApprovalRun)
    assert ProbeToolkit.calls == []
    assert team.checker.requests[-1].messages[-1].content == "Declined by requester"
    _, [member] = stored_runs(team)
    assert member.tools is not None
    assert (member.tools[0].confirmed, member.tools[0].tool_call_error) == (False, True)


@pytest.mark.asyncio
async def test_a_failed_resume_stores_the_error_over_the_paused_row_and_ran_the_approved_tool_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leader request that fails after the approved call ran reaches the caller; the rows record the outcome."""
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)

    async def lose_the_provider() -> None:
        msg = "provider connection lost"
        raise ProviderError(msg, provider="Synthetic")

    team.models["default"] = ScriptedModel([[Await(lose_the_provider)]])
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])

    with pytest.raises(ProviderError, match="provider connection lost"):
        await resume_approval(team, raised.paused, approved=True, calls_=(member_approval("call-touch", "dangerous"),))

    assert ProbeToolkit.calls == ["dangerous"]
    record, [member] = stored_runs(team)
    assert (record.status, member.status) == (RunStatus.error, RunStatus.completed)
    assert "provider connection lost" in str(record.content)
    assert member.tools is not None
    assert (member.tools[0].confirmed, member.tools[0].result) == (True, "touched dangerous")


@pytest.mark.asyncio
async def test_a_resume_is_refused_before_anything_runs_when_the_approval_no_longer_fits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member that lost the approved toolkit, or an approval naming another call, leaves the paused row untouched."""
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel([])
    team.models["checker-model"] = ScriptedModel([])

    with pytest.raises(RuntimeError, match="no longer match the pending requirements"):
        await resume_approval(team, raised.paused, approved=True, calls_=(member_approval("call-other", "dangerous"),))
    team.config.agents["checker"].tools = []
    with pytest.raises(ExceptionGroup) as refused:
        await resume_approval(team, raised.paused, approved=True, calls_=(member_approval("call-touch", "dangerous"),))

    [error] = refused.value.exceptions
    assert str(error) == f"Saved approval toolkit {PROBE_TOOL!r} is no longer permitted; retry the request"
    assert ProbeToolkit.calls == []
    assert (team.leader.requests, team.checker.requests) == ([], [])
    record, _ = stored_runs(team)
    assert record.status is RunStatus.paused


def _delegate_to_helper(task: str) -> list[ScriptStep]:
    return calls(tool_call("run_subagent", "call-sub", agent_name="helper", task=task), metrics=usage(10, 1))


def _delegation_state(team: SquadTurn) -> tuple[TeamRunRecord, RunRecord, DelegationState]:
    record, [member] = stored_runs(team)
    return record, member, DelegationState.from_metadata(record.metadata)


@pytest.mark.asyncio
async def test_a_member_s_delegation_runs_its_child_and_the_team_continues_with_the_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member's ``run_subagent`` pauses the team run; the driver runs the child, then the member and the leader continue."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Research it"), answer("Consensus.", usage(50, 5))],
        writer=[_delegate_to_helper("Dig"), answer("Writer: the helper found it.")],
        helper=[calls(tool_call("touch", "call-helper", label="dug"), metrics=usage(5, 1)), answer("Helper found it.")],
    )

    chunks = await stream_reply(team)

    assert ProbeToolkit.calls == ["dug"]
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
) -> None:
    """The approval a member's child needs is asked through the team's pause; approving it resumes child, member, and leader."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Research it"), answer("Consensus.", usage(50, 5))],
        writer=[_delegate_to_helper("Dig"), answer("Writer: dug.")],
        helper=[calls(tool_call("touch", "call-helper", label="deep"), metrics=usage(5, 1)), answer("Dug deep.")],
        approval_rules=[ApprovalRuleConfig(match="touch", action="require_approval")],
    )
    with pytest.raises(ResponsePausedForApproval) as raised:
        await stream_reply(team, show_tool_calls=False)

    paused = raised.value.paused
    assert ProbeToolkit.calls == []
    record, member, state = _delegation_state(team)
    assert (record.status, member.status) == (RunStatus.paused, RunStatus.paused)
    [child] = state.children
    assert (child.caller_agent_name, child.child_agent_name, child.status) == ("writer", "helper", "paused")
    assert state.pending_child_id == child.delegation_id
    [tool] = paused.tools
    assert (tool.tool_name, tool.tool_call_id) == ("touch", f"{child.delegation_id}:call-helper")
    assert paused.toolkit_owners == {("helper", "touch"): PROBE_TOOL}
    assert team.recorder.outcome == "suspended"

    result = await resume_approval(team, paused, approved=True, calls_=_saved_approval_calls(state))

    assert isinstance(result, CompletedApprovalRun)
    assert ProbeToolkit.calls == ["deep"]
    assert result.response_text == f"{HEADER}**Writer**: Writer: dug.\n\n\n**Team Consensus**:\n\nConsensus."
    record, member, state = _delegation_state(team)
    assert (record.status, member.status) == (RunStatus.completed, RunStatus.completed)
    assert [child.status for child in state.children] == ["completed"]
    assert "Dug deep." in str(team.writer.requests[-1].messages[-1].content)


@pytest.mark.asyncio
async def test_a_stopped_member_keeps_its_usage_and_both_rows_end_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Stop while a member works stores the member's cut-off request and the team's row before the stop lands."""
    streaming = asyncio.Event()

    async def wait_to_be_cancelled() -> None:
        streaming.set()
        await asyncio.Event().wait()

    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Write")],
        writer=[[TextDelta("Dra"), UsageReported(usage(30, 2), final=False), Await(wait_to_be_cancelled)]],
    )
    task = asyncio.create_task(stream_reply(team))
    await asyncio.wait_for(streaming.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record, [member] = stored_runs(team)
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


def _runner(team: SquadTurn, tmp_path: Path) -> Any:  # noqa: ANN401
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
    streaming: bool,
) -> None:
    """Through ``ResponseRunner``, a stop mid-reply stores the ``CANCELLED`` team row before the interrupted record.

    The record the lifecycle owner writes replaces the cancelled row, and no later save of the run overwrites it.
    """
    working = asyncio.Event()

    async def wait_to_be_cancelled() -> None:
        working.set()
        await asyncio.Event().wait()

    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Write")],
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
