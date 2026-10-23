"""Team turns on the owned runtime, through ``team_response_stream``, ``team_response``, and the approval resume.

The seam is ``run_team`` fed by scripted providers behind the production member and leader builders: what a team
reply streams and renders, what it stores, how a member's approval pauses and resumes it, which scope its members'
tools address, and how it ends when a member is stopped. The last tests drive turns through ``ResponseRunner``, the
lifecycle owner: it writes the interrupted-turn record after the loop stored its own terminal record, and it posts the
approval cards of a member's gated delegations and resumes the turn they approve.
"""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from agno.tools import Toolkit

from mindroom import ai_runtime, loop_hooks, response_runner, response_turn
from mindroom.agent_loop.model_loop import LoopHooks
from mindroom.approval.manager import get_approval_store, initialize_approval_store
from mindroom.approval.receipt import build_approval_receipt
from mindroom.approval.receipt_readers import approval_receipt_context
from mindroom.approval.tool_approval import shutdown_approval_runtime
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.constants import AI_RUN_METADATA_KEY
from mindroom.delegation.lifecycle import child_execution_identity
from mindroom.delegation.state import DelegationState
from mindroom.event_journal import ApprovalDecision
from mindroom.history.storage import read_scope_seen_event_ids
from mindroom.hooks import EnrichmentItem, MessageEnvelope
from mindroom.llm.errors import ProviderError
from mindroom.llm.messages import Message
from mindroom.llm.provider import TextDelta, TurnCompleted, UsageReported
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.response_runner import ResponseRequest
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import CompletedApprovalRun, ResponsePausedForApproval
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.teams import envelope
from mindroom.teams.spec import TeamMode
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import register_background_runtime
from mindroom.tool_system.call_record import recording_tool_calls
from mindroom.tool_system.events import StructuredStreamChunk
from mindroom.usage.storage import quote_identifier
from tests.ai_user_id_helpers import _install_inert_post_response_effects
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
from tests.scripted_agent_helpers import registered_toolkit
from tests.team_turn_helpers import (
    EVENT_ID,
    PROBE_TOOL,
    REQUESTER_ID,
    ROOM_ID,
    SESSION_ID,
    TEAM_SCOPE,
    THREAD_ID,
    ProbeToolkit,
    TeamTurn,
    delegate,
    member_approval,
    squad_turn,
)
from tests.tool_job_helpers import saved_jobs, tool_job_runtime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

    from mindroom.bot import TeamBot
    from mindroom.event_journal import MatrixDelivery
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

    chunks = await team.stream(show_tool_calls=False)

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
    record, members = team.stored_runs()
    assert (record.status, record.content, record.team_id) == (RunStatus.completed, "Consensus answer.", "squad")
    [member] = members
    assert (member.agent_id, member.parent_run_id, member.status) == ("writer", record.run_id, RunStatus.completed)
    assert [message.role for message in member.messages] == ["user", "assistant", "tool", "assistant"]
    assert record.tools is not None
    assert [(tool.tool_name, tool.result) for tool in record.tools] == [("delegate_task_to_member", "Member answer.")]
    # The leader's prompt names both members and the mode; the member got the task alone.
    leader_system = team.models["default"].requests[0].messages[0].content
    assert isinstance(leader_system, str)
    assert '<member id="writer" name="Writer">' in leader_system
    assert "coordinate mode" in leader_system
    assert [message.content for message in team.models["writer-model"].requests[0].messages[1:]] == ["Report"]
    assert team.models["checker-model"].requests == []
    session = team.storage.load_session(SESSION_ID, "team")
    assert session is not None
    assert EVENT_ID in read_scope_seen_event_ids(team.storage, session, TEAM_SCOPE)


@pytest.mark.asyncio
async def test_a_blocking_team_turn_whose_attempts_all_came_back_empty_still_marks_its_trigger_seen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both empty runs are discarded, so the session row alone keeps the events the turn read."""
    team = squad_turn(tmp_path, monkeypatch, leader=[answer(""), answer("")])

    assert await team.respond() == ai_runtime.EMPTY_RESPONSE_NOTICE

    session = team.storage.load_session(SESSION_ID, "team")
    assert session is not None
    assert session.runs == []
    assert read_scope_seen_event_ids(team.storage, session, TEAM_SCOPE) == {EVENT_ID}


@pytest.mark.asyncio
async def test_only_a_team_turn_whose_attempt_started_hands_its_session_to_queued_notice_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn that fails while preparing its first attempt ran nothing in the team session, so it registers none."""
    team = squad_turn(tmp_path, monkeypatch, leader=[answer("Done.", usage(10, 1))])

    with ai_runtime.queued_message_signal_context(None) as notices:
        assert await team.respond() == f"{HEADER}\n**Team Consensus**:\n\nDone."
    assert list(notices.storage_targets) == [("squad", SESSION_ID, "team")]

    async def history_unavailable(*_args: object, **_kwargs: object) -> None:
        msg = "history unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(envelope, "prepare_materialized_team_execution", history_unavailable)
    with ai_runtime.queued_message_signal_context(None) as notices:
        assert "history unavailable" in await team.respond()
    assert notices.storage_targets == {}


@pytest.mark.asyncio
async def test_a_streamed_team_turn_whose_attempts_all_came_back_empty_leaves_its_trigger_unseen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream that showed nothing marks no event seen, so the next turn still reads the message."""
    team = squad_turn(tmp_path, monkeypatch, leader=[answer(""), answer("")])

    chunks = await team.stream()

    assert chunks == [ai_runtime.EMPTY_RESPONSE_NOTICE]
    session = team.storage.load_session(SESSION_ID, "team")
    assert session is not None
    assert session.runs == []
    assert read_scope_seen_event_ids(team.storage, session, TEAM_SCOPE) == set()


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

    chunks = await team.stream(show_tool_calls=show_tool_calls)

    canonical = f"{HEADER}**Writer**: Member answer.\n\n\n**Team Consensus**:\n\nConsensus answer."
    shown = (
        f"{HEADER}**Writer**:\n\n🔧 `touch` [1]\n\nMember answer.\n\n\n"
        "**Team Consensus**:\n\n🔧 `delegate_task_to_member` [2]\n\nConsensus answer."
    )
    assert chunks[-1].content == (shown if show_tool_calls else canonical)
    assert team.recorder.assistant_text == canonical


@pytest.mark.asyncio
@pytest.mark.parametrize("show_tool_calls", [True, False])
async def test_a_blocking_team_reply_is_the_marker_free_member_and_consensus_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    show_tool_calls: bool,
) -> None:
    """A blocking reply carries no tool trace, so shown tool calls add no marker to the document it delivers."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Report"), answer("Consensus answer.", usage(50, 5))],
        writer=[calls(tool_call("touch", "call-touch", label="a"), metrics=usage(10, 1)), answer("Member answer.")],
    )

    text = await team.respond(show_tool_calls=show_tool_calls)

    assert ProbeToolkit.calls == ["a"]
    assert text == f"{HEADER}**Writer**: Member answer.\n\n\n**Team Consensus**:\n\nConsensus answer."
    assert team.recorder.assistant_text == text


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False])
async def test_a_blocking_team_reply_lists_the_members_as_they_finished_and_a_streamed_one_as_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    """The checker finishes before the writer; only the blocking reply puts it first, and it records what it sent."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[
            delegate("call-check", "checker", "Check"),
            delegate("call-write", "writer", "Write"),
            answer("Both done.", usage(50, 5)),
        ],
        writer=[answer("Written.")],
        checker=[answer("Checked.")],
    )

    text = await team.reply(stream=stream, show_tool_calls=False)

    slots = (
        "**Writer**: Written.\n\n**Checker**: Checked." if stream else "**Checker**: Checked.\n\n**Writer**: Written."
    )
    assert text == f"{HEADER}{slots}\n\n\n**Team Consensus**:\n\nBoth done."
    assert team.recorder.assistant_text == text


async def _checker_overloaded() -> None:
    msg = "Checker overloaded"
    raise ProviderError(msg, provider="Synthetic", status_code=503)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("leader", "writer", "checker", "body"),
    [
        pytest.param(
            [delegate("call-check", "checker", "Check"), answer("The checker is unavailable.")],
            [],
            [[Await(_checker_overloaded)]],
            "**Checker**: Checker overloaded\n\n\n**Team Consensus**:\n\nThe checker is unavailable.",
            id="member-failed-before-text",
        ),
        pytest.param(
            [delegate("call-write", "writer", "Touch it"), answer("Done.")],
            [calls(tool_call("touch", "call-touch", label="a")), answer("")],
            [],
            "\n**Team Consensus**:\n\nDone.",
            id="tool-only-member",
        ),
        pytest.param(
            [delegate("call-check", "checker", "Check"), answer("Done.")],
            [],
            [answer("")],
            "\n**Team Consensus**:\n\nDone.",
            id="empty-member",
        ),
        pytest.param(
            [
                delegate("call-draft", "writer", "Draft"),
                delegate("call-revise", "writer", "Revise"),
                answer("Both drafts done."),
            ],
            [answer("Draft one."), answer("Draft two.")],
            [],
            "**Writer**: Draft one.\n\n**Writer**: Draft two.\n\n\n**Team Consensus**:\n\nBoth drafts done.",
            id="member-delegated-twice",
        ),
        pytest.param(
            [delegate("call-write", "writer", "Write"), answer("")],
            [answer("Written.\n")],
            [],
            "**Writer**: Written.\n\n\n\n*No team consensus - showing individual responses only*",
            id="no-consensus",
        ),
    ],
)
async def test_a_blocking_team_reply_renders_each_finished_member_run_then_the_consensus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    leader: list[list[ScriptStep]],
    writer: list[list[ScriptStep]],
    checker: list[list[ScriptStep]],
    body: str,
) -> None:
    """Each member run that ended shows its own text, then the leader's text, and the turn records the reply.

    The bodies are what the Agno-backed blocking reply rendered for the same runs: a member that failed before any
    text shows its error, one without text shows nothing, a member delegated twice shows twice, and text keeps its
    whitespace.
    """
    team = squad_turn(tmp_path, monkeypatch, leader=leader, writer=writer, checker=checker)

    text = await team.respond(show_tool_calls=False)

    assert text == HEADER + body
    assert team.recorder.assistant_text == text


@pytest.mark.asyncio
async def test_a_blocking_team_reply_sends_non_streaming_requests_and_renders_the_member_and_consensus_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "checker", "Check"), answer("All good.", usage(50, 5))],
        checker=[answer("Checked.")],
    )

    text = await team.respond(show_tool_calls=False)

    assert text == f"{HEADER}**Checker**: Checked.\n\n\n**Team Consensus**:\n\nAll good."
    assert all(not request.stream for request in team.models["default"].requests)
    record, members = team.stored_runs()
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

    chunks = await team.stream(mode=TeamMode.COLLABORATE, show_tool_calls=False)

    assert chunks[-1].content == (
        f"{HEADER}**Writer**: Writer fact.\n\n**Checker**: Checker fact.\n\n\n**Team Consensus**:\n\nTwo facts."
    )
    record, members = team.stored_runs()
    assert record.tools is not None
    assert record.tools[0].result == "Writer fact.Checker fact."
    assert sorted(member.agent_id or "" for member in members) == ["checker", "writer"]
    assert "broadcast mode" in str(team.models["default"].requests[0].messages[0].content)


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

    await team.stream()

    assert ProbeToolkit.scopes == ["team:squad"]
    _, [member] = team.stored_runs()
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
    ctx = team.ctx(transient_enrichment_items=(EnrichmentItem(key="sample", text=transient),))

    await team.stream(ctx=ctx)

    # The transient block is the user message right after the prompt, apart from it.
    assert [(message.role, message.content) for message in team.models["default"].requests[0].messages[1:]] == [
        ("user", f'Current message:\n<msg event_id="{EVENT_ID}" from="{REQUESTER_ID}"><![CDATA[Hello]]></msg>'),
        (
            "user",
            "<mindroom_transient_context>\n<mindroom_message_context>\n"
            f'<item key="sample" cache_policy="volatile">\n{transient}\n</item>\n'
            "</mindroom_message_context>\n</mindroom_transient_context>",
        ),
    ]
    record, members = team.stored_runs()
    assert len(members) == 1
    assert all(transient not in json.dumps(run.to_dict()) for run in [record, *members])


@pytest.mark.asyncio
async def test_a_member_s_todo_work_defaults_to_the_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member's ``add_todo`` without an assignee assigns the work to that member, not to the team."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Plan it"), answer("Planned.", usage(50, 5))],
        writer=[
            calls(tool_call("add_todo", "call-todo", title="Draft the report"), metrics=usage(10, 1)),
            answer("Done."),
        ],
    )
    team.config.agents["writer"].tools = [PROBE_TOOL, "todo"]

    await team.stream()

    _, [member] = team.stored_runs()
    assert member.tools is not None
    assert member.tools[0].result is not None
    assert member.tools[0].result.endswith("assigned to writer")


@pytest.mark.asyncio
async def test_a_member_s_claude_sessions_are_the_member_s_own(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member's Claude session is keyed by the member, so members of one team never share a session."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Check Claude"), answer("Checked.", usage(50, 5))],
        writer=[calls(tool_call("claude_session_status", "call-claude"), metrics=usage(10, 1)), answer("None yet.")],
    )
    team.config.agents["writer"].tools = [PROBE_TOOL, "claude_agent"]

    await team.stream()

    _, [member] = team.stored_runs()
    assert member.tools is not None
    assert member.tools[0].result == f"No active Claude session for `writer:{SESSION_ID}`."


@pytest.mark.asyncio
async def test_a_member_s_calls_run_through_the_member_s_tool_call_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The policy the member's agent build runs its calls under, such as call recording, sees each call it makes."""
    recorded: list[tuple[str, dict[str, object]]] = []

    class _Recorder:
        async def started(self, tool_name: str, args: Mapping[str, object]) -> str:
            recorded.append((tool_name, dict(args)))
            return tool_name

        async def finished(self, call_id: str, tool_name: str, args: Mapping[str, object], result: object) -> None:
            del call_id, tool_name, args, result

    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Touch it"), answer("Touched.", usage(50, 5))],
        writer=[calls(tool_call("touch", "call-touch", label="a"), metrics=usage(10, 1)), answer("Done.")],
    )

    with recording_tool_calls(_Recorder()):
        await team.stream()

    assert recorded == [("touch", {"label": "a"})]


@pytest.mark.asyncio
async def test_a_member_s_call_that_runs_as_a_tool_job_keeps_the_member_s_claude_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With background tool jobs on, a member's call runs as a managed job and still reaches the member's session."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Check Claude"), answer("Checked.", usage(50, 5))],
        writer=[calls(tool_call("claude_session_status", "call-claude"), metrics=usage(10, 1)), answer("None yet.")],
    )
    team.config.agents["writer"].tools = [PROBE_TOOL, "claude_agent"]
    team.config.background_tool_jobs.enabled = True
    pin_background_tool_jobs(team.config, team.runtime_paths)
    runtime = await tool_job_runtime(team.runtime_paths.storage_root)
    register_background_runtime(team.runtime_paths, runtime)
    try:
        async with execution_resources():
            await team.stream()
    finally:
        await runtime.shutdown()

    [job] = (await saved_jobs(team.runtime_paths.storage_root)).values()
    assert (job.tool_name, job.status, job.owner.agent_name) == ("claude_session_status", "completed", "writer")
    _, [member] = team.stored_runs()
    assert member.tools is not None
    assert member.tools[0].result == f"No active Claude session for `writer:{SESSION_ID}`."


@pytest.mark.asyncio
async def test_the_leader_runs_with_compaction_bound_to_the_team_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leader loop gets mid-turn compaction and the approval receipt hook before each request, and the notices.

    No approval receipt is bound around a fresh turn, so none of its requests carries one.
    """
    team = squad_turn(tmp_path, monkeypatch, leader=[answer("Alone.", usage(50, 5))])
    captured: dict[str, Any] = {}
    # Every member's hooks come from the same builder; the leader's compaction is the one built for the team.
    compactions: list[dict[str, Any]] = []
    real_run_team = envelope.run_team
    real_compaction_hook = loop_hooks.mid_turn_compaction_hook

    def capture_run_team(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        captured["hooks"] = kwargs["hooks"]
        return real_run_team(*args, **kwargs)

    def capture_compaction_hook(**kwargs: Any) -> Any:  # noqa: ANN401
        compactions.append(kwargs)
        return real_compaction_hook(**kwargs)

    monkeypatch.setattr(envelope, "run_team", capture_run_team)
    monkeypatch.setattr(loop_hooks, "mid_turn_compaction_hook", capture_compaction_hook)

    await team.stream()

    hooks = captured["hooks"]
    assert isinstance(hooks, LoopHooks)
    assert [hook.__qualname__.split(".")[0] for hook in hooks.before_request] == [
        "mid_turn_compaction_hook",
        "approval_receipt_hook",
    ]
    assert (len(hooks.after_tool_batch), len(hooks.on_resume_entry)) == (1, 1)
    assert hooks.tool_middleware == ()
    [compaction] = [kwargs for kwargs in compactions if kwargs["is_team"]]
    assert (compaction["scope_id"], compaction["entity_name"]) == ("squad", "squad")
    assert compaction["scope"] == TEAM_SCOPE
    assert compaction["model"] is team.models["default"]
    assert compaction["prompt_id"] is not None


async def _pause_on_the_checker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TeamTurn, ResponsePausedForApproval]:
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "checker", "Touch it")],
        checker=[calls(tool_call("touch", "call-touch", label="dangerous"), metrics=usage(10, 1))],
        approval_rules=[ApprovalRuleConfig(match="touch", action="require_approval")],
    )
    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.stream(show_tool_calls=False)
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
    record, [member] = team.stored_runs()
    assert (record.status, member.status) == (RunStatus.paused, RunStatus.paused)
    assert record.member_responses[0].run_id == member.run_id
    assert record.content == "Team run paused. The following require input:\n- Checker: touch requires confirmation"
    assert team.recorder.outcome == "suspended"


_MEMBER_PAUSED = "I have tools to execute, but I need confirmation."
_TEAM_PAUSED = "Team run paused. The following require input:\n- Checker: touch requires confirmation"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("show_tool_calls", "text", "checker_slot", "consensus_slot", "separators"),
    [
        pytest.param(
            True,
            f"{HEADER}**Checker**: {_MEMBER_PAUSED}\n\n🔧 `touch` [1] ⏳\n\n\n"
            f"**Team Consensus**:\n\n{_TEAM_PAUSED}\n\n🔧 `delegate_task_to_member` [2]",
            f"{_MEMBER_PAUSED}\n\n🔧 `touch` [1] ⏳\n\n",
            f"{_TEAM_PAUSED}\n\n🔧 `delegate_task_to_member` [2]\n\n",
            None,
            id="tool-calls-shown",
        ),
        pytest.param(
            False,
            f"{HEADER}**Checker**: {_MEMBER_PAUSED}\n\n\n**Team Consensus**:\n\n{_TEAM_PAUSED}",
            _MEMBER_PAUSED,
            _TEAM_PAUSED,
            ["agent:checker", "team"],
            id="tool-calls-hidden",
        ),
    ],
)
async def test_a_blocking_team_pause_shows_its_runs_records_with_agno_s_paused_notices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    show_tool_calls: bool,
    text: str,
    checker_slot: str,
    consensus_slot: str,
    separators: list[str] | None,
) -> None:
    """A blocking pause renders its runs' records, as the Agno-backed blocking pause did, not its events' document.

    Each slot holds its run's text first, the member's and the leader's paused notices included, and then the run's
    tool calls, the member's gated call still pending. The snapshot the resume continues holds the same slots.
    """
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "checker", "Touch it")],
        checker=[calls(tool_call("touch", "call-touch", label="dangerous"), metrics=usage(10, 1))],
        approval_rules=[ApprovalRuleConfig(match="touch", action="require_approval")],
    )

    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.respond(show_tool_calls=show_tool_calls)

    paused = raised.value.paused
    assert paused.response_text == text
    assert [(entry.tool_name, entry.scope_key, entry.type) for entry in paused.tool_trace] == [
        ("touch", "agent:checker", "tool_call_started"),
        ("delegate_task_to_member", "team", "tool_call_completed"),
    ]
    state = paused.response_presentation_state
    assert [member["content"] for member in state["members"]] == ["", checker_slot]
    assert state["consensus"] == consensus_slot
    assert state.get("separate_next_scopes") == separators
    assert team.recorder.outcome == "suspended"


@pytest.mark.asyncio
async def test_an_approved_member_call_resumes_the_stored_team_run_and_renders_the_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel([answer("The checker touched it.", usage(50, 5))])
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])

    result = await team.resume(
        raised.paused,
        approved=True,
        approvals=(member_approval("call-touch", "dangerous"),),
    )

    assert isinstance(result, CompletedApprovalRun)
    assert ProbeToolkit.calls == ["dangerous"]
    assert result.response_text == f"{HEADER}**Checker**: Touched.\n\n\n**Team Consensus**:\n\nThe checker touched it."
    assert result.metadata_content["io.mindroom.ai_run"]["status"] == "completed"
    record, [member] = team.stored_runs()
    assert (record.status, member.status) == (RunStatus.completed, RunStatus.completed)
    assert record.requirements == []
    assert member.tools is not None
    assert (member.tools[0].confirmed, member.tools[0].result) == (True, "touched dangerous")
    # The leader reads the member's result where it read the pause notice.
    assert team.models["default"].requests[-1].messages[-1].content == (
        "Member results after human-in-the-loop resolution:\n[Checker]: Touched."
    )


@pytest.mark.asyncio
async def test_a_resume_runs_each_member_on_the_model_its_pause_pinned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member whose configured model changed while the approval waited finishes on the model it paused on."""
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.config.agents["checker"].model = "writer-model"
    team.models["default"] = ScriptedModel([answer("The checker touched it.", usage(50, 5))])
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])

    result = await team.resume(
        raised.paused,
        approved=True,
        approvals=(member_approval("call-touch", "dangerous"),),
    )

    assert isinstance(result, CompletedApprovalRun)
    assert ProbeToolkit.calls == ["dangerous"]
    assert len(team.models["checker-model"].requests) == 1
    assert team.models["writer-model"].requests == []


@pytest.mark.asyncio
async def test_an_approval_resume_sends_the_receipt_to_the_member_and_the_leader_and_stores_it_in_neither(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The member whose call was approved and the leader each read the trusted receipt in their first resumed request.

    The response runner binds the receipt around the resume; it is projected into each loop's request only, so no
    stored run holds it. A member the resumed leader then hands a new task runs fresh and never reads it.
    """
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel(
        [delegate("call-polish", "writer", "Polish it"), answer("The checker touched it.", usage(50, 5))],
    )
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])
    team.models["writer-model"] = ScriptedModel([answer("Polished.")])
    call = member_approval("call-touch", "dangerous")
    receipt = build_approval_receipt(
        (replace(call, decision=ApprovalDecision.APPROVED, human_approval_required=True),),
    )

    with approval_receipt_context(receipt, run_id=raised.paused.run_id):
        await team.resume(raised.paused, approved=True, approvals=(call,))

    def receipt_roles(model: str) -> list[list[str]]:
        return [
            [message.role for message in request.messages if receipt in str(message.content)]
            for request in team.models[model].requests
        ]

    assert receipt_roles("checker-model") == [["system"]]
    assert receipt_roles("default") == [["system"], []]
    assert receipt_roles("writer-model") == [[]]
    record, members = team.stored_runs()
    assert all(receipt not in json.dumps(run.to_dict()) for run in [record, *members])


@pytest.mark.asyncio
async def test_a_member_continued_after_its_own_delegation_in_an_approval_resume_never_reads_the_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the runs the approval resumes read its receipt, each once.

    The resumed leader hands the writer a fresh task; the writer delegates to the helper and continues with the
    helper's answer. No approval resumed the writer, so neither of its requests carries the receipt.
    """
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "checker", "Touch it")],
        checker=[calls(tool_call("touch", "call-touch", label="dangerous"), metrics=usage(10, 1))],
        helper=[],
        approval_rules=[ApprovalRuleConfig(match="touch", action="require_approval")],
    )
    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.stream(show_tool_calls=False)
    paused = raised.value.paused
    team.models["default"] = ScriptedModel(
        [delegate("call-polish", "writer", "Polish it"), answer("The writer polished it.", usage(50, 5))],
    )
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])
    team.models["writer-model"] = ScriptedModel(
        [
            calls(tool_call("run_subagent", "call-sub", agent_name="helper", task="Dig"), metrics=usage(10, 1)),
            answer("Polished with the helper's find."),
        ],
    )
    team.models["helper-model"] = ScriptedModel([answer("Helper found it.")])
    call = member_approval("call-touch", "dangerous")
    receipt = build_approval_receipt(
        (replace(call, decision=ApprovalDecision.APPROVED, human_approval_required=True),),
    )

    with approval_receipt_context(receipt, run_id=paused.run_id):
        result = await team.resume(paused, approved=True, approvals=(call,))

    def carries(model: str) -> list[bool]:
        return [any(receipt in str(m.content) for m in r.messages) for r in team.models[model].requests]

    assert isinstance(result, CompletedApprovalRun)
    assert carries("checker-model") == [True]
    assert carries("default") == [True, False]
    assert carries("writer-model") == [False, False]
    assert carries("helper-model") == [False]


@pytest.mark.asyncio
async def test_a_denied_member_call_never_runs_and_the_member_answers_the_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel([answer("The checker declined.", usage(50, 5))])
    team.models["checker-model"] = ScriptedModel([answer("I did not touch it.")])

    result = await team.resume(
        raised.paused,
        approved=False,
        approvals=(member_approval("call-touch", "dangerous"),),
    )

    assert isinstance(result, CompletedApprovalRun)
    assert ProbeToolkit.calls == []
    assert team.models["checker-model"].requests[-1].messages[-1].content == "Declined by requester"
    _, [member] = team.stored_runs()
    assert member.tools is not None
    assert (member.tools[0].confirmed, member.tools[0].tool_call_error) == (False, True)


async def _writer_unavailable() -> None:
    msg = "Writer unavailable"
    raise ProviderError(msg, provider="Synthetic", status_code=503)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("leader", "writer", "body"),
    [
        pytest.param(
            [delegate("call-write", "writer", "Write it up"), answer("")],
            [[Await(_writer_unavailable)]],
            "**Writer**: Writer unavailable\n\n\n*No team consensus - showing individual responses only*",
            id="member-failed-before-text",
        ),
        pytest.param([answer("\n")], [], "\n", id="blank-leader-text"),
    ],
)
async def test_an_approved_resume_that_shows_no_text_renders_its_finished_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    leader: list[list[ScriptStep]],
    writer: list[list[ScriptStep]],
    body: str,
) -> None:
    """A resume whose document stays empty renders its runs' records, as the Agno-backed resume did.

    The approved checker says nothing. A member the resumed leader hands a task and that fails before any text shows
    its error; without member text the leader's own text stands.
    """
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel(leader)
    team.models["checker-model"] = ScriptedModel([answer("")])
    team.models["writer-model"] = ScriptedModel(writer)

    result = await team.resume(
        raised.paused,
        approved=True,
        approvals=(member_approval("call-touch", "dangerous"),),
    )

    assert isinstance(result, CompletedApprovalRun)
    assert ProbeToolkit.calls == ["dangerous"]
    assert result.response_text == HEADER + body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("leader", "writer", "body"),
    [
        pytest.param(
            [delegate("call-write", "writer", "Write it up"), answer("Done.")],
            [[Await(_writer_unavailable)]],
            "**Writer**: Writer unavailable\n\n**Checker**: Touched.\n\n\n**Team Consensus**:\n\nDone.",
            id="completed",
        ),
        pytest.param(
            [delegate("call-write", "writer", "Touch it too")],
            [calls(tool_call("touch", "call-touch-2", label="risky"))],
            f"**Writer**: {_MEMBER_PAUSED}\n\n**Checker**: Touched.\n\n\n**Team Consensus**:\n\n"
            "Team run paused. The following require input:\n- Writer: touch requires confirmation",
            id="paused-again",
        ),
    ],
)
async def test_an_approved_resume_shows_each_run_s_content_in_a_slot_that_streamed_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    leader: list[list[ScriptStep]],
    writer: list[list[ScriptStep]],
    body: str,
) -> None:
    """A resume shows each run's content in a slot that streamed no text of its own, as the Agno-backed resume did.

    The approved checker's streamed answer stands as it streamed. The writer the resumed leader then hands a task
    streams nothing: its error, or its paused notice when its own call pauses the team again, fills its slot, and a
    leader that streamed nothing before that pause shows its paused notice as the consensus.
    """
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel(leader)
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])
    team.models["writer-model"] = ScriptedModel(writer)

    result = await team.resume(
        raised.paused,
        approved=True,
        approvals=(member_approval("call-touch", "dangerous"),),
    )

    assert ProbeToolkit.calls == ["dangerous"]
    assert result.response_text == HEADER + body


@pytest.mark.asyncio
async def test_an_approved_resume_shows_once_the_text_its_runs_wrote_before_the_pause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text the leader and the checker wrote before the pause stands once when neither writes more after approval.

    The Agno-backed resume added a content-less run's earlier messages to its slot again; the restored document
    already holds them.
    """
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[
            calls(
                tool_call("delegate_task_to_member", "call-team", member_id="checker", task="Touch it"),
                content="Asking the checker.",
            ),
        ],
        checker=[calls(tool_call("touch", "call-touch", label="dangerous"), content="Let me touch it.")],
        approval_rules=[ApprovalRuleConfig(match="touch", action="require_approval")],
    )
    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.stream(show_tool_calls=False)
    team.models["default"] = ScriptedModel([answer("")])
    team.models["checker-model"] = ScriptedModel([answer("")])

    result = await team.resume(
        raised.value.paused,
        approved=True,
        approvals=(member_approval("call-touch", "dangerous"),),
    )

    assert ProbeToolkit.calls == ["dangerous"]
    assert (
        result.response_text == f"{HEADER}**Checker**: Let me touch it.\n\n\n**Team Consensus**:\n\nAsking the checker."
    )


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
        await team.resume(raised.paused, approved=True, approvals=(member_approval("call-touch", "dangerous"),))

    assert ProbeToolkit.calls == ["dangerous"]
    record, [member] = team.stored_runs()
    assert (record.status, member.status) == (RunStatus.error, RunStatus.completed)
    assert "provider connection lost" in str(record.content)
    assert member.tools is not None
    assert (member.tools[0].confirmed, member.tools[0].result) == (True, "touched dangerous")


@pytest.mark.asyncio
async def test_a_team_reply_that_fails_mid_turn_reports_its_stored_run_in_the_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run whose leader request fails after a member answered reports that run, ended in error, as the reply's metadata."""

    async def lose_the_provider() -> None:
        msg = "provider connection lost"
        raise ProviderError(msg, provider="Synthetic")

    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Draft it"), [Await(lose_the_provider)]],
        writer=[answer("Draft.", usage(20, 2))],
    )
    metadata: dict[str, Any] = {}

    await team.stream(run_metadata_collector=metadata)

    record, _ = team.stored_runs()
    payload = metadata[AI_RUN_METADATA_KEY]
    assert (record.status, payload["run_id"], payload["status"]) == (RunStatus.error, record.run_id, "error")
    assert payload["tools"] == {"count": 1}


async def _lose_the_provider() -> None:
    msg = "provider connection lost"
    raise ProviderError(msg, provider="Synthetic")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("leader", "recorded"),
    [
        pytest.param(
            [delegate("call-check", "checker", "Check"), [Await(_lose_the_provider)]],
            "**Checker**: Checker overloaded\n\n\n**Team Consensus**:\n\nprovider connection lost",
            id="after-a-member-failed",
        ),
        pytest.param(
            [[Await(_lose_the_provider)]],
            "**Team Consensus**:\n\nprovider connection lost",
            id="on-the-first-request",
        ),
    ],
)
async def test_a_failed_blocking_team_turn_records_the_failed_run_s_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    leader: list[list[ScriptStep]],
    recorded: str,
) -> None:
    """A blocking turn whose leader request fails records the stored team run's records, as the Agno-backed turn did.

    A member that failed before any text shows its error; the leader said nothing, so the failed run's error text
    stands as its consensus.
    """
    team = squad_turn(tmp_path, monkeypatch, leader=leader, checker=[[Await(_checker_overloaded)]])

    await team.respond(show_tool_calls=False)

    record, _ = team.stored_runs()
    assert (record.status, record.content) == (RunStatus.error, "provider connection lost")
    assert team.recorder.outcome == "interrupted"
    assert team.recorder.assistant_text == recorded


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False], ids=["approved", "denied"])
async def test_a_resume_is_refused_before_anything_runs_when_the_approval_no_longer_fits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    approved: bool,
) -> None:
    """A member that lost the approved toolkit, or a decision naming another call, leaves the paused row untouched."""
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel([])
    team.models["checker-model"] = ScriptedModel([])

    with pytest.raises(RuntimeError, match="no longer match the pending requirements"):
        await team.resume(raised.paused, approved=approved, approvals=(member_approval("call-other", "dangerous"),))
    team.config.agents["checker"].tools = []
    with pytest.raises(ExceptionGroup) as refused:
        await team.resume(raised.paused, approved=True, approvals=(member_approval("call-touch", "dangerous"),))

    [error] = refused.value.exceptions
    assert str(error) == f"Saved approval toolkit {PROBE_TOOL!r} is no longer permitted; retry the request"
    assert ProbeToolkit.calls == []
    assert (team.models["default"].requests, team.models["checker-model"].requests) == ([], [])
    record, _ = team.stored_runs()
    assert record.status is RunStatus.paused


@pytest.mark.asyncio
async def test_a_saved_team_approval_that_names_no_members_is_refused_before_anything_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No member could run the approved calls, so the resume refuses at once and leaves the paused row untouched."""
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel([])
    members, team.member_names = team.member_names, ()

    with pytest.raises(RuntimeError, match="The paused team approval names no members"):
        await team.resume(raised.paused, approved=True, approvals=(member_approval("call-touch", "dangerous"),))

    team.member_names = members
    assert ProbeToolkit.calls == []
    assert team.models["default"].requests == []
    record, _ = team.stored_runs()
    assert record.status is RunStatus.paused


@pytest.mark.asyncio
async def test_an_approval_from_another_session_never_resumes_the_paused_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run the team store holds for another conversation is refused before anything runs, as an agent's is."""
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel([])
    team.models["checker-model"] = ScriptedModel([])

    with pytest.raises(RuntimeError, match="is no longer available"):
        await team.resume(
            raised.paused,
            approved=True,
            approvals=(member_approval("call-touch", "dangerous"),),
            session_id=f"{ROOM_ID}:$other-thread:localhost",
        )

    assert ProbeToolkit.calls == []
    assert (team.models["default"].requests, team.models["checker-model"].requests) == ([], [])
    record, _ = team.stored_runs()
    assert record.status is RunStatus.paused


def _delegate_to_helper(task: str) -> list[ScriptStep]:
    return calls(tool_call("run_subagent", "call-sub", agent_name="helper", task=task), metrics=usage(10, 1))


def _delegation_state(team: TeamTurn) -> tuple[TeamRunRecord, RunRecord, DelegationState]:
    record, [member] = team.stored_runs()
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

    metadata: dict[str, Any] = {}
    chunks = await team.stream(run_metadata_collector=metadata)

    assert ProbeToolkit.calls == ["dug"]
    # The writer's run ended twice, paused and then completed; the turn counts its record once.
    assert metadata[AI_RUN_METADATA_KEY]["tools"] == {"count": 2}
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
    assert "Helper found it." in str(team.models["writer-model"].requests[-1].messages[-1].content)
    assert team.models["default"].requests[-1].messages[-1].content == (
        "Member results after human-in-the-loop resolution:\n[Writer]: Writer: the helper found it."
    )


@pytest.mark.asyncio
async def test_two_members_delegations_under_one_call_id_each_run_once_and_the_team_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Members number their calls independently, so two can delegate under one call id; each child runs once."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[
            calls(
                tool_call("delegate_task_to_member", "call-w", member_id="writer", task="Dig A"),
                tool_call("delegate_task_to_member", "call-c", member_id="checker", task="Dig B"),
                metrics=usage(40, 4),
            ),
            answer("Consensus.", usage(50, 5)),
        ],
        writer=[_delegate_to_helper("A"), answer("Writer: done.")],
        checker=[_delegate_to_helper("B"), answer("Checker: done.")],
        helper=[answer("Helper one."), answer("Helper two.")],
    )
    team.config.agents["checker"].delegate_to = ["helper"]

    await team.stream()

    assert len(team.models["helper-model"].requests) == 2
    record, members = team.stored_runs()
    assert (record.status, record.content) == (RunStatus.completed, "Consensus.")
    assert sorted((member.agent_id or "", member.status) for member in members) == [
        ("checker", RunStatus.completed),
        ("writer", RunStatus.completed),
    ]
    state = DelegationState.from_metadata(record.metadata)
    assert sorted((child.caller_agent_name, child.status) for child in state.children) == [
        ("checker", "completed"),
        ("writer", "completed"),
    ]


@pytest.mark.asyncio
async def test_a_blocking_team_continues_after_a_member_s_delegation_with_streamed_leader_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The team's continuation after a member's delegation streams even in a blocking turn, as Agno's did."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Research it"), answer("Consensus.", usage(50, 5))],
        writer=[_delegate_to_helper("Dig"), answer("Writer: the helper found it.")],
        helper=[answer("Helper found it.")],
    )

    text = await team.respond(show_tool_calls=False)

    assert text.endswith("**Team Consensus**:\n\nConsensus.")
    assert [request.stream for request in team.models["default"].requests] == [False, True]


@pytest.mark.asyncio
async def test_a_blocking_team_reply_shows_a_member_paused_on_its_delegation_once_with_its_resumed_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member that paused on its delegation and resumed within the turn shows once, with what its resumed run wrote.

    Agno continued the paused member's output in place and started its content afresh, so Agno's blocking reply
    showed only the resumed text; what the member wrote before it paused stays in its stored messages.
    """
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Research it"), answer("Consensus.", usage(50, 5))],
        writer=[
            calls(
                tool_call("run_subagent", "call-sub", agent_name="helper", task="Dig"),
                content="Asking the helper.",
                metrics=usage(10, 1),
            ),
            answer("Writer: the helper found it."),
        ],
        helper=[answer("Helper found it.")],
    )

    text = await team.respond(show_tool_calls=False)

    assert text == f"{HEADER}**Writer**: Writer: the helper found it.\n\n\n**Team Consensus**:\n\nConsensus."
    assert team.recorder.assistant_text == text
    _, member, _ = _delegation_state(team)
    assert [message.content for message in member.messages if message.role == "assistant"] == [
        "Asking the helper.",
        "Writer: the helper found it.",
    ]


@pytest.mark.asyncio
async def test_a_blocking_team_reply_without_content_shows_what_the_leader_wrote_before_its_member_paused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leader silent after its member's delegation has no content, so the reply is what it wrote before the pause.

    The leader's continuation starts its content afresh and nobody wrote anything after the delegation, so the reply is
    the text of the leader's own messages, as Agno's blocking reply read a team run without content.
    """
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[
            calls(
                tool_call("delegate_task_to_member", "call-team", member_id="writer", task="Research it"),
                content="Asking the writer.",
                metrics=usage(40, 4),
            ),
            [TurnCompleted(message=Message(role="assistant", content=None), stop_reason="end")],
        ],
        writer=[_delegate_to_helper("Dig"), answer("")],
        helper=[answer("Helper found it.")],
    )

    text = await team.respond(show_tool_calls=False)

    assert text == f"{HEADER}Asking the writer."
    assert team.recorder.assistant_text == text
    record, _, _ = _delegation_state(team)
    assert record.content is None


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
        await team.stream(show_tool_calls=False)

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

    result = await team.resume(paused, approved=True, approvals=_saved_approval_calls(state))

    assert isinstance(result, CompletedApprovalRun)
    assert ProbeToolkit.calls == ["deep"]
    assert result.response_text == f"{HEADER}**Writer**: Writer: dug.\n\n\n**Team Consensus**:\n\nConsensus."
    record, member, state = _delegation_state(team)
    assert (record.status, member.status) == (RunStatus.completed, RunStatus.completed)
    assert [child.status for child in state.children] == ["completed"]
    assert "Dug deep." in str(team.models["writer-model"].requests[-1].messages[-1].content)


@pytest.mark.asyncio
async def test_a_member_s_delegation_card_adds_no_marker_and_the_approved_reply_shows_the_call_finished(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The card asking about a member's ``run_subagent`` shows that call once, pending; the resume finishes it."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Research it"), answer("Consensus.", usage(50, 5))],
        writer=[_delegate_to_helper("Dig"), answer("Writer: dug.")],
        helper=[answer("Dug.")],
        approval_rules=[ApprovalRuleConfig(match="run_subagent", action="require_approval")],
    )
    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.stream()

    paused = raised.value.paused
    [tool] = paused.tools
    assert (tool.tool_name, tool.tool_call_id) == ("run_subagent", "writer:call-sub")
    assert [(entry.tool_name, entry.scope_key) for entry in paused.tool_trace] == [
        ("run_subagent", "agent:writer"),
        ("delegate_task_to_member", "team"),
    ]
    # The leader's delegation was answered with the member's pause; only the member's call still waits.
    assert (paused.response_text.count("🔧"), paused.response_text.count("⏳")) == (2, 1)
    _, _, state = _delegation_state(team)

    result = await team.resume(paused, approved=True, approvals=_saved_approval_calls(state), show_tool_calls=True)

    assert isinstance(result, CompletedApprovalRun)
    assert result.response_text == (
        f"{HEADER}**Writer**:\n\n🔧 `run_subagent` [1]\n\nWriter: dug.\n\n\n"
        "**Team Consensus**:\n\n🔧 `delegate_task_to_member` [2]\n\nConsensus."
    )
    record, member, state = _delegation_state(team)
    assert (record.status, member.status) == (RunStatus.completed, RunStatus.completed)
    assert [child.status for child in state.children] == ["completed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("approval_rule", "helper", "helper_flags"),
    [
        pytest.param(
            "touch",
            [calls(tool_call("touch", "call-helper", label="deep"), metrics=usage(5, 1)), answer("Dug deep.")],
            [False, True],
            id="child-call",
        ),
        pytest.param("run_subagent", [answer("Dug.")], [False], id="delegation-card"),
    ],
)
async def test_a_member_continued_with_the_result_of_a_delegation_an_approval_decided_reads_the_receipt_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    approval_rule: str,
    helper: list[list[ScriptStep]],
    helper_flags: list[bool],
) -> None:
    """The member whose delegation the approval answers reads its receipt once, as the leader the card belongs to does.

    The approval decides the helper's call the writer's delegation waits on, or the card of the writer's delegation
    itself; the writer then continues with the helper's answer. A helper the approved card starts runs fresh and
    never reads the receipt.
    """
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Research it"), answer("Consensus.", usage(50, 5))],
        writer=[_delegate_to_helper("Dig"), answer("Writer: dug.")],
        helper=helper,
        approval_rules=[ApprovalRuleConfig(match=approval_rule, action="require_approval")],
    )
    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.stream(show_tool_calls=False)
    paused = raised.value.paused
    _, _, state = _delegation_state(team)
    approvals = _saved_approval_calls(state)
    receipt = build_approval_receipt(
        tuple(replace(call, decision=ApprovalDecision.APPROVED, human_approval_required=True) for call in approvals),
    )

    with approval_receipt_context(receipt, run_id=paused.run_id):
        result = await team.resume(paused, approved=True, approvals=approvals)

    def carries(model: str) -> list[bool]:
        return [any(receipt in str(m.content) for m in r.messages) for r in team.models[model].requests]

    assert isinstance(result, CompletedApprovalRun)
    # Each model's fresh request, then its first request after the approval.
    assert carries("helper-model") == helper_flags
    assert carries("writer-model") == [False, True]
    assert carries("default") == [False, True]


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
    task = asyncio.create_task(team.stream())
    await asyncio.wait_for(streaming.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record, [member] = team.stored_runs()
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


@pytest.mark.asyncio
async def test_a_stopped_blocking_team_turn_records_its_member_runs_in_the_order_they_ended(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Stop of a blocking turn records the stopped team run's records, as the Agno-backed blocking turn did.

    The members are listed as their runs ended: the checker failed before any text and shows its error, then the
    writer, stopped while its call ran, shows what it wrote before.
    """
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-check", "checker", "Check"), delegate("call-write", "writer", "Write")],
        checker=[[Await(_checker_overloaded)]],
        writer=[calls(tool_call("touch", "call-touch", label="draft", wait=True), content="Drafting.")],
    )
    task = asyncio.create_task(team.respond(show_tool_calls=False))
    await asyncio.wait_for(ProbeToolkit.waiting.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record, members = team.stored_runs()
    assert [run.status for run in (record, *members)] == [RunStatus.cancelled, RunStatus.error, RunStatus.cancelled]
    assert team.recorder.outcome == "interrupted"
    assert team.recorder.assistant_text == (
        "**Checker**: Checker overloaded\n\n**Writer**: Drafting.\n\n\n"
        "*No team consensus - showing individual responses only*"
    )


@pytest.mark.asyncio
async def test_a_stopped_blocking_team_turn_whose_leader_has_no_content_records_no_leader_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leader stopped after its member's delegation has no content, so the turn records none of the leader's text.

    The leader's continuation starts its content afresh and its member said nothing after the delegation. Agno's
    stopped run then held only its cancellation notice, which the Agno-backed turn dropped, so the text the leader
    wrote before the delegation is not recorded either.
    """
    working = asyncio.Event()

    async def wait_to_be_cancelled() -> None:
        working.set()
        await asyncio.Event().wait()

    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[
            calls(
                tool_call("delegate_task_to_member", "call-team", member_id="writer", task="Research it"),
                content="Asking the writer.",
                metrics=usage(40, 4),
            ),
            [Await(wait_to_be_cancelled)],
        ],
        writer=[_delegate_to_helper("Dig"), answer("")],
        helper=[answer("Helper found it.")],
    )
    task = asyncio.create_task(team.respond(show_tool_calls=False))
    await asyncio.wait_for(working.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record, _, _ = _delegation_state(team)
    assert (record.status, record.content) == (RunStatus.cancelled, None)
    assert team.recorder.outcome == "interrupted"
    assert team.recorder.assistant_text == ""


@pytest.mark.asyncio
async def test_a_blocking_team_turn_stopped_after_its_run_ended_records_the_finished_runs_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Stop that lands after the run ended, before the turn settles, records the finished runs' records.

    The checker failed before any text and shows its error, as the reply would have shown it; the text the run's
    events built held no checker text.
    """
    finishing = asyncio.Event()
    finalize_consumption = response_turn.finalize_consumption

    async def stop_once_the_run_ended() -> None:
        if not finishing.is_set():
            finishing.set()
            await asyncio.Event().wait()
        await finalize_consumption()

    monkeypatch.setattr(response_turn, "finalize_consumption", stop_once_the_run_ended)
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-check", "checker", "Check"), answer("Done.")],
        checker=[[Await(_checker_overloaded)]],
    )
    task = asyncio.create_task(team.respond(show_tool_calls=False))
    await asyncio.wait_for(finishing.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record, [member] = team.stored_runs()
    assert (record.status, member.status) == (RunStatus.completed, RunStatus.error)
    assert team.recorder.outcome == "interrupted"
    assert team.recorder.assistant_text == "**Checker**: Checker overloaded\n\n\n**Team Consensus**:\n\nDone."


class _ExternalProbeToolkit(ProbeToolkit):
    """The members' probe with ``touch`` executed outside the loop: a run paused on that call has nothing to resume it."""

    def __init__(self) -> None:
        Toolkit.__init__(self, name="probe", tools=[self.touch], external_execution_required_tools=["touch"])


@pytest.mark.asyncio
async def test_a_blocking_team_turn_that_fails_after_its_run_ended_records_the_run_s_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocking turn whose run paused on a call nothing can resume fails, and records the paused run's records.

    The members are listed as their runs ended: the checker failed before any text and shows its error, then the
    writer paused on its call with the text it wrote before it; the text the run's events built held no checker text.
    """
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-check", "checker", "Check"), delegate("call-write", "writer", "Write")],
        checker=[[Await(_checker_overloaded)]],
        writer=[calls(tool_call("touch", "call-touch", label="outside"), content="Let me touch it.")],
    )

    with registered_toolkit(PROBE_TOOL, _ExternalProbeToolkit):
        reply = await team.respond(show_tool_calls=False)

    assert "without an approval to ask for" in reply
    record, members = team.stored_runs()
    assert [run.status for run in (record, *members)] == [RunStatus.paused, RunStatus.error, RunStatus.paused]
    assert (team.recorder.outcome, team.recorder.original_status) == ("interrupted", RunStatus.error)
    assert team.recorder.assistant_text == (
        "**Checker**: Checker overloaded\n\n**Writer**: Let me touch it.\n\n\n"
        "*No team consensus - showing individual responses only*"
    )


def _team_bot(team: TeamTurn, tmp_path: Path) -> TeamBot:
    """Build the squad's team bot on the team's config, with a mocked Matrix client and inert post-response effects.

    A completed reply would otherwise queue a thread summary on the config's real ``default`` model.
    """
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
    _install_inert_post_response_effects(bot._response_runner)
    bot.orchestrator = team.orchestrator
    return bot


def _runner(team: TeamTurn, tmp_path: Path) -> Any:  # noqa: ANN401
    """Return the runner of the squad's team bot."""
    return _team_bot(team, tmp_path)._response_runner


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


@asynccontextmanager
async def _approving_team_bot(
    team: TeamTurn,
    tmp_path: Path,
    *,
    streaming: bool = True,
) -> AsyncIterator[tuple[TeamBot, list[MatrixDelivery]]]:
    """Yield the squad's team bot with its approval cards published into the yielded list, the n-th as ``$card-n``."""
    bot = _team_bot(team, tmp_path)
    cards: list[MatrixDelivery] = []

    async def prepare_event(_room_id: str, _thread_id: str | None, content: dict[str, Any]) -> dict[str, Any]:
        return content

    async def send_card(delivery: MatrixDelivery) -> str:
        if delivery.edits_event_id is not None:
            # A decided card's resolution edit.
            return f"$edit-{delivery.edits_event_id}"
        cards.append(delivery)
        return f"$card-{len(cards)}"

    initialize_approval_store(
        bot.runtime_paths,
        prepare_event=prepare_event,
        send_delivery=send_card,
        resolve_delivery=AsyncMock(return_value=None),
        cards=bot._journal_store.principal("router@shared"),
        transport_sender=lambda: "@router:localhost",
        sending_device=lambda: "DEVICE",
    )
    try:
        with patch_response_runner_module(should_use_streaming=AsyncMock(return_value=streaming)):
            yield bot, cards
    finally:
        await shutdown_approval_runtime()


def _card_calls(cards: list[MatrixDelivery]) -> list[tuple[object, object]]:
    return [(card.payload["tool_name"], card.payload["tool_call_id"]) for card in cards]


async def _approve(bot: TeamBot, card_event_id: str) -> None:
    """Approve one card as the requester does, then hand the request's source to the readied resume, as the journal does."""
    manager = get_approval_store()
    assert manager is not None
    result = await manager.handle_card_response(
        room_id=ROOM_ID,
        sender_id=REQUESTER_ID,
        card_event_id=card_event_id,
        status="approved",
        reason=None,
        authorize_responder=lambda _entity: True,
    )
    assert result.consumed
    runner = bot._response_runner
    await runner.handoff_approval_source(EVENT_ID)
    await runner.wait_for_source_owned_inbox_responses()


def _shown(bot: TeamBot) -> str:
    """Return what the reply shows now: the body of its last send or edit."""
    content = bot.client.room_send.await_args_list[-1].kwargs["content"]
    return str(content.get("m.new_content", content)["body"])


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
async def test_a_member_s_gated_delegation_posts_its_card_and_the_approved_reply_shows_the_call_finished(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    """Through ``ResponseRunner`` with tool calls shown, the card asks about the member's call, shown once in its slot.

    The approved resume finishes that call where the pause showed it pending.
    """
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Research it"), answer("Consensus.", usage(50, 5))],
        writer=[_delegate_to_helper("Dig"), answer("Writer: dug.")],
        helper=[answer("Dug.")],
        approval_rules=[ApprovalRuleConfig(match="run_subagent", action="require_approval")],
    )
    async with _approving_team_bot(team, tmp_path, streaming=streaming) as (bot, cards):
        await bot._response_runner.generate_team_response_helper(
            _request(),
            team_agents=team.member_ids(),
            team_mode="coordinate",
        )

        assert _card_calls(cards) == [("run_subagent", "writer:call-sub")]
        assert _shown(bot) == (
            f"{HEADER}**Writer**:\n\n🔧 `run_subagent` [1] ⏳\n\n\n**Team Consensus**:\n\n🔧 `delegate_task_to_member` [2]"
        )
        continuation = await bot.journal_principal().approval_continuation_for_source(EVENT_ID)
        assert continuation is not None
        assert (continuation.state, continuation.show_tool_calls) == ("waiting", True)

        await _approve(bot, "$card-1")

    assert _shown(bot) == (
        f"{HEADER}**Writer**:\n\n🔧 `run_subagent` [1]\n\nWriter: dug.\n\n\n"
        "**Team Consensus**:\n\n🔧 `delegate_task_to_member` [2]\n\nConsensus."
    )
    assert await bot.journal_principal().approval_continuation_for_source(EVENT_ID) is None
    record, member, state = _delegation_state(team)
    assert (record.status, member.status) == (RunStatus.completed, RunStatus.completed)
    assert [child.status for child in state.children] == ["completed"]


@pytest.mark.asyncio
async def test_a_member_s_next_gated_delegation_during_the_resume_posts_the_next_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resume's next pause, on the writer's second gated ``run_subagent``, is presented and approved as the first."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Research it"), answer("Consensus.", usage(50, 5))],
        writer=[
            _delegate_to_helper("Dig"),
            calls(tool_call("run_subagent", "call-sub-2", agent_name="helper", task="Dig more"), metrics=usage(10, 1)),
            answer("Writer: dug twice."),
        ],
        helper=[answer("Dug."), answer("Dug more.")],
        approval_rules=[ApprovalRuleConfig(match="run_subagent", action="require_approval")],
    )
    async with _approving_team_bot(team, tmp_path) as (bot, cards):
        await bot._response_runner.generate_team_response_helper(
            _request(),
            team_agents=team.member_ids(),
            team_mode="coordinate",
        )
        await _approve(bot, "$card-1")

        assert _card_calls(cards) == [("run_subagent", "writer:call-sub"), ("run_subagent", "writer:call-sub-2")]
        assert _shown(bot) == (
            f"{HEADER}**Writer**:\n\n🔧 `run_subagent` [1]\n\n\n\n🔧 `run_subagent` [2] ⏳\n\n\n"
            "**Team Consensus**:\n\n🔧 `delegate_task_to_member` [3]"
        )
        continuation = await bot.journal_principal().approval_continuation_for_source(EVENT_ID)
        assert continuation is not None
        assert continuation.state == "waiting"

        await _approve(bot, "$card-2")

    assert _shown(bot) == (
        f"{HEADER}**Writer**:\n\n🔧 `run_subagent` [1]\n\n\n\n🔧 `run_subagent` [2]\n\nWriter: dug twice.\n\n\n"
        "**Team Consensus**:\n\n🔧 `delegate_task_to_member` [3]\n\nConsensus."
    )
    assert await bot.journal_principal().approval_continuation_for_source(EVENT_ID) is None
    record, member, state = _delegation_state(team)
    assert (record.status, member.status) == (RunStatus.completed, RunStatus.completed)
    assert [child.status for child in state.children] == ["completed", "completed"]


@pytest.mark.asyncio
async def test_a_member_s_child_s_gated_delegation_posts_its_card_in_the_team_slot_and_finishes_there(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gated ``run_subagent`` of the member's child is asked about under the child's id, in the team's own slot."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Research it"), answer("Consensus.", usage(50, 5))],
        writer=[_delegate_to_helper("Dig"), answer("Writer: dug.")],
        helper=[
            calls(tool_call("run_subagent", "call-deep", agent_name="checker", task="Dig deeper"), metrics=usage(5, 1)),
            answer("Helper: dug deeper."),
        ],
        checker=[answer("Dug deeper.")],
        approval_rules=[ApprovalRuleConfig(match="run_subagent", action="require_approval")],
    )
    team.config.agents["helper"].delegate_to = ["checker"]
    async with _approving_team_bot(team, tmp_path) as (bot, cards):
        await bot._response_runner.generate_team_response_helper(
            _request(),
            team_agents=team.member_ids(),
            team_mode="coordinate",
        )
        await _approve(bot, "$card-1")

        _, _, state = _delegation_state(team)
        [child] = state.children
        assert _card_calls(cards) == [
            ("run_subagent", "writer:call-sub"),
            ("run_subagent", f"{child.delegation_id}:call-deep"),
        ]
        # The writer's call still waits on its child; the child's call shows in the team's slot.
        assert _shown(bot) == (
            f"{HEADER}**Writer**:\n\n🔧 `run_subagent` [1] ⏳\n\n\n"
            "**Team Consensus**:\n\n🔧 `delegate_task_to_member` [2]\n\n\n\n🔧 `run_subagent` [3] ⏳"
        )
        assert team.models["checker-model"].requests == []

        await _approve(bot, "$card-2")

    assert _shown(bot) == (
        f"{HEADER}**Writer**:\n\n🔧 `run_subagent` [1]\n\nWriter: dug.\n\n\n"
        "**Team Consensus**:\n\n🔧 `delegate_task_to_member` [2]\n\n\n\n🔧 `run_subagent` [3]\n\nConsensus."
    )
    assert await bot.journal_principal().approval_continuation_for_source(EVENT_ID) is None
    assert len(team.models["checker-model"].requests) == 1
    assert "Helper: dug deeper." in str(team.models["writer-model"].requests[-1].messages[-1].content)
    record, member, state = _delegation_state(team)
    assert (record.status, member.status) == (RunStatus.completed, RunStatus.completed)
    assert [child.status for child in state.children] == ["completed"]
