"""Team turns on the owned runtime, through ``team_response_stream``, streamed or collected, and the approval resume.

The seam is ``run_team`` fed by scripted providers behind the production member and leader builders: what a team
reply streams and renders, what it stores, how a member's approval pauses and resumes it, which scope its members'
tools address, and how it ends when a member is stopped. The last tests drive turns through ``ResponseRunner``, the
lifecycle owner: it dates a turn in the zone of the entity whose bot runs it, it writes the interrupted-turn record
after the loop stored its own terminal record, and it posts the approval cards of a member's gated delegations and
resumes the turn they approve.
"""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import nio
import pytest
from agno.tools import Toolkit
from structlog.testing import capture_logs

from mindroom import agents, ai_runtime, loop_hooks, response_turn
from mindroom.agent_loop.model_loop import LoopHooks
from mindroom.approval.manager import get_approval_store
from mindroom.approval.receipt import build_approval_receipt
from mindroom.approval.receipt_readers import approval_receipt_context
from mindroom.approval.tool_approval import shutdown_approval_runtime
from mindroom.coalescing.batch import CoalescingKey, RequesterCoalescingOwner, build_prepared_turn
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.constants import AI_RUN_METADATA_KEY, MINDROOM_HISTORY_METADATA_KEY
from mindroom.delegation.state import DelegationState, child_execution_identity
from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.event_journal import ApprovalDecision
from mindroom.history.interrupted_replay import _render_interrupted_replay_content
from mindroom.history.storage import read_scope_seen_event_ids
from mindroom.hooks import EVENT_TOOL_BEFORE_CALL, EnrichmentItem, MessageEnvelope, ToolBeforeCallContext, hook
from mindroom.llm.errors import ProviderError
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, NativeCompactionReplay, TextDelta, TurnCompleted, UsageReported
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.response_runner import ResponseRequest
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt, ResponsePausedForApproval
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.schema import quote_identifier
from mindroom.teams import envelope
from mindroom.teams import leader as leader_module
from mindroom.teams.members import _as_member
from mindroom.teams.presentation import NO_TEAM_RESPONSE
from mindroom.teams.spec import TeamMode
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import register_background_runtime
from mindroom.tool_system.call_record import recording_tool_calls
from mindroom.tool_system.events import StructuredStreamChunk
from mindroom.tool_system.invocation import ToolCallContext, run_tool_call
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.tool_system.tool_function import ToolFunction
from tests.ai_user_id_helpers import _install_inert_post_response_effects
from tests.approval_continuation_helpers import (
    carries_approval_receipt,
    receipt_call_ids,
    receipts,
    repeats_each_request,
    request_call_ids,
)
from tests.bot_helpers import make_test_agent_bot, unique_room_send_responses
from tests.conftest import (
    TEST_PASSWORD,
    handle_card_response_for_test,
    initialize_approval_store_for_test,
    install_runtime_journal_support,
    make_matrix_client_mock,
    make_pending_event,
    make_visible_message,
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
    rendered,
    squad_turn,
)
from tests.teams.squad import member as squad_member
from tests.test_tool_hooks import _plugin
from tests.tool_job_helpers import saved_jobs, tool_job_runtime
from tests.turn_dispatch_helpers import nothing_else_queued

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from contextlib import AbstractContextManager
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.event_journal import ApprovalCall, MatrixDelivery
    from mindroom.session_storage.store import ConversationStore
    from mindroom.teams.envelope import TeamStreamChunk
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

HEADER = "🤝 **Team Response** (Writer, Checker):\n\n"
_TOUCH_NEEDS_APPROVAL = [ApprovalRuleConfig(match="touch", action="require_approval")]

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
    assert [(entry.tool_name, entry.member_id) for entry in chunks[-1].tool_trace or []] == [
        ("touch", "writer"),
        ("delegate_task_to_member", None),
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
    member_request = team.models["writer-model"].requests[0].messages
    assert [message.content for message in member_request if message.role != "system"] == ["Report"]
    assert team.models["checker-model"].requests == []
    session = team.storage.load_session(SESSION_ID)
    assert session is not None
    assert EVENT_ID in read_scope_seen_event_ids(team.storage, session)


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
async def test_a_team_turn_whose_attempts_all_came_back_empty_leaves_its_trigger_unseen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream that showed nothing marks no event seen, so the next turn still reads the message."""
    team = squad_turn(tmp_path, monkeypatch, leader=[answer(""), answer("")])

    chunks = await team.stream()

    assert chunks == [ai_runtime.EMPTY_RESPONSE_NOTICE]
    session = team.storage.load_session(SESSION_ID)
    assert session is not None
    assert session.runs == []
    assert read_scope_seen_event_ids(team.storage, session) == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False])
async def test_a_team_turn_that_only_ran_tools_marks_its_trigger_seen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    """A turn whose runs called tools but wrote nothing keeps its run and marks the events it read seen, on either path.

    The stored session row keeps them as well as the run, as it does for every turn that produced anything.
    """
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Report"), answer("")],
        writer=[calls(tool_call("touch", "call-touch", label="a")), answer("")],
    )

    await team.reply(stream=stream, show_tool_calls=False)

    assert ProbeToolkit.calls == ["a"]
    session = team.storage.load_session(SESSION_ID)
    assert session is not None
    assert read_scope_seen_event_ids(team.storage, session) == {EVENT_ID}
    assert session.metadata == {MINDROOM_HISTORY_METADATA_KEY: {"seen_event_ids": [EVENT_ID]}}


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
async def test_a_collected_team_reply_is_the_streamed_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    show_tool_calls: bool,
) -> None:
    """The collected reply is the last document the stream showed, shown tool calls marked; the record never is."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Report"), answer("Consensus answer.", usage(50, 5))],
        writer=[calls(tool_call("touch", "call-touch", label="a"), metrics=usage(10, 1)), answer("Member answer.")],
    )

    tool_trace: list[ToolTraceEntry] = []

    text = await team.respond(show_tool_calls=show_tool_calls, tool_trace=tool_trace)

    assert ProbeToolkit.calls == ["a"]
    canonical = f"{HEADER}**Writer**: Member answer.\n\n\n**Team Consensus**:\n\nConsensus answer."
    shown = (
        f"{HEADER}**Writer**:\n\n🔧 `touch` [1]\n\nMember answer.\n\n\n"
        "**Team Consensus**:\n\n🔧 `delegate_task_to_member` [2]\n\nConsensus answer."
    )
    assert text == (shown if show_tool_calls else canonical)
    assert [entry.tool_name for entry in tool_trace] == ["touch", "delegate_task_to_member"]
    assert team.recorder.assistant_text == canonical


@pytest.mark.asyncio
async def test_a_team_reply_lists_the_members_as_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The checker finishes before the writer; the reply lists the writer first, and the turn records what it sent."""
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

    text = await team.respond(show_tool_calls=False)

    assert text == f"{HEADER}**Writer**: Written.\n\n**Checker**: Checked.\n\n\n**Team Consensus**:\n\nBoth done."
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
            "**Writer**: Draft one.\n\nDraft two.\n\n\n**Team Consensus**:\n\nBoth drafts done.",
            id="member-delegated-twice",
        ),
        pytest.param(
            [delegate("call-write", "writer", "Write"), answer("")],
            [answer("Written.\n")],
            [],
            "**Writer**: Written.\n\n\n*No team consensus - showing individual responses only*",
            id="no-consensus",
        ),
    ],
)
async def test_a_team_reply_renders_each_finished_member_run_then_the_consensus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    leader: list[list[ScriptStep]],
    writer: list[list[ScriptStep]],
    checker: list[list[ScriptStep]],
    body: str,
) -> None:
    """Each member's slot shows the text of its runs that ended, then the leader's text, and the turn records the reply.

    A member that failed before any text shows its error, one without text shows nothing, and a member delegated
    twice shows both runs' text in its one slot, a paragraph apart.
    """
    team = squad_turn(tmp_path, monkeypatch, leader=leader, writer=writer, checker=checker)

    text = await team.respond(show_tool_calls=False)

    assert text == HEADER + body
    assert team.recorder.assistant_text == text


@pytest.mark.asyncio
async def test_a_team_reply_renders_the_member_and_consensus_document_and_stores_the_member_run(
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
    assert record.tools[0].result == "Agent Writer: Writer fact.\n\nAgent Checker: Checker fact."
    assert sorted(member.agent_id or "" for member in members) == ["checker", "writer"]
    assert "collaborate mode" in str(team.models["default"].requests[0].messages[0].content)


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
    leader_request = team.models["default"].requests[0].messages
    assert [(message.role, message.content) for message in leader_request if message.role != "system"] == [
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
@pytest.mark.parametrize("as_job", [False, True], ids=["inline", "tool-job"])
async def test_a_member_s_tool_hooks_run_as_the_member_with_the_turn_s_live_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    as_job: bool,
) -> None:
    """A plugin's tool hook on a member's call sees the member's tool runtime context and the turn's Matrix bindings.

    The hook runs inside the member's tool-call policy, inline or in the managed job a call becomes, so the whole
    policy runs as the member, as the tool itself does.
    """
    seen: list[tuple[str, str | None, bool]] = []

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(context: ToolBeforeCallContext) -> None:
        runtime = get_tool_runtime_context()
        seen.append(
            (
                context.agent_name,
                runtime.agent_name if runtime is not None else None,
                context.room_state_querier is not None,
            ),
        )

    monkeypatch.setattr(agents, "_load_agent_plugins", lambda *_args, **_kwargs: [_plugin("member-policy", [before])])
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Touch it"), answer("Touched.", usage(50, 5))],
        writer=[calls(tool_call("touch", "call-touch", label="a"), metrics=usage(10, 1)), answer("Done.")],
    )
    if not as_job:
        await team.stream()
    else:
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
        assert (job.tool_name, job.status, job.owner.agent_name) == ("touch", "completed", "writer")

    assert ProbeToolkit.calls == ["a"]
    assert seen == [("writer", "writer", True)]


@pytest.mark.asyncio
async def test_the_member_step_runs_a_call_as_it_is_outside_a_matrix_turn() -> None:
    """Without a tool runtime context, as in an OpenAI-compatible API turn, a member's call runs without one."""
    seen: list[ToolRuntimeContext | None] = []

    async def probe() -> str:
        seen.append(get_tool_runtime_context())
        return "ran"

    function = ToolFunction(name="probe", description=None, parameters={"type": "object"}, entrypoint=probe)

    result = await run_tool_call(
        function,
        {},
        ToolCallContext(run_id="run-1", session_id=SESSION_ID, user_id=None),
        middleware=(_as_member("writer", "writer-model"),),
    )

    assert (result.value, seen) == ("ran", [None])


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
    real_run_team = leader_module.run_team
    real_compaction_hook = loop_hooks.mid_turn_compaction_hook

    def capture_run_team(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        captured["hooks"] = kwargs["hooks"]
        return real_run_team(*args, **kwargs)

    def capture_compaction_hook(**kwargs: Any) -> Any:  # noqa: ANN401
        compactions.append(kwargs)
        return real_compaction_hook(**kwargs)

    monkeypatch.setattr(leader_module, "run_team", capture_run_team)
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
    [compaction] = [kwargs for kwargs in compactions if kwargs["scope"].kind == "team"]
    assert (compaction["scope"], compaction["entity_name"]) == (TEAM_SCOPE, "squad")
    assert compaction["storage"].scope == TEAM_SCOPE
    assert compaction["model"] is team.models["default"]
    assert compaction["prompt_id"] is not None


async def _pause_on_the_checker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    show_tool_calls: bool = False,
) -> tuple[TeamTurn, ResponsePausedForApproval]:
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "checker", "Touch it")],
        checker=[calls(tool_call("touch", "call-touch", label="dangerous"), metrics=usage(10, 1))],
        approval_rules=_TOUCH_NEEDS_APPROVAL,
    )
    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.stream(show_tool_calls=show_tool_calls)
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
    assert (requirement.member_agent_id, requirement.member_display_name) == ("checker", "Checker")
    assert requirement.tool_execution.tool_call_id == "call-touch"
    assert paused.toolkit_owners[("checker", "touch")] == PROBE_TOOL
    # The writer never ran, so it was never built and owns none of the team's calls.
    assert not any(member == "writer" for member, _function in paused.toolkit_owners)
    assert paused.approval_agent_name is None
    assert paused.runtime_model_name == "default"
    assert paused.team_member_model_names == (("checker", "checker-model"), ("writer", "writer-model"))
    assert [member["id"] for member in paused.response_presentation_state["members"]] == [
        "writer",
        "checker",
    ]
    assert [(entry.tool_name, entry.member_id) for entry in paused.tool_trace] == [
        ("touch", "checker"),
        ("delegate_task_to_member", None),
    ]
    # With tool calls hidden and no member text yet, the pause shows no document of its own.
    assert paused.response_text == ""
    record, [member] = team.stored_runs()
    assert (record.status, member.status) == (RunStatus.paused, RunStatus.paused)
    assert record.member_responses[0].run_id == member.run_id
    assert record.content is None
    assert team.recorder.outcome == "suspended"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("show_tool_calls", "text", "checker_slot", "consensus_slot"),
    [
        pytest.param(
            True,
            f"{HEADER}**Checker**:\n\n🔧 `touch` [1] ⏳\n\n\n**Team Consensus**:\n\n🔧 `delegate_task_to_member` [2]",
            "\n\n🔧 `touch` [1] ⏳\n\n",
            "\n\n🔧 `delegate_task_to_member` [2]\n\n",
            id="tool-calls-shown",
        ),
        # With tool calls hidden and no text yet, the pause shows no document of its own.
        pytest.param(False, "", "", "", id="tool-calls-hidden"),
    ],
)
async def test_a_team_pause_shows_the_document_its_events_built_without_paused_notices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    show_tool_calls: bool,
    text: str,
    checker_slot: str,
    consensus_slot: str,
) -> None:
    """A pause shows the document its run's events built.

    The member and the leader paused without text of their own, so their runs hold no content and the pause shows no
    notice. The snapshot the resume continues holds the same slots.
    """
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch, show_tool_calls=show_tool_calls)

    paused = raised.paused
    assert paused.response_text == text
    assert [(entry.tool_name, entry.member_id, entry.type) for entry in paused.tool_trace] == [
        ("touch", "checker", "tool_call_started"),
        ("delegate_task_to_member", None, "tool_call_completed"),
    ]
    state = paused.response_presentation_state
    assert [member["content"] for member in state["members"]] == ["", checker_slot]
    assert state["consensus"] == consensus_slot
    record, [member] = team.stored_runs()
    assert (record.content, member.content) == (None, None)
    assert team.recorder.outcome == "suspended"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("show_tool_calls", "body"),
    [
        pytest.param(
            True,
            "**Checker**:\n\n🔧 `touch` [1]\n\nTouched.\n\n\n"
            "**Team Consensus**:\n\n🔧 `delegate_task_to_member` [2]\n\nThe checker touched it.",
            id="tool-calls-shown",
        ),
        pytest.param(
            False,
            "**Checker**: Touched.\n\n\n**Team Consensus**:\n\nThe checker touched it.",
            id="tool-calls-hidden",
        ),
    ],
)
async def test_an_approved_member_call_resumes_the_stored_team_run_and_renders_the_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    show_tool_calls: bool,
    body: str,
) -> None:
    """The reply ends with each run's text after its approval, and no paused notice.

    The member and the leader paused without text of their own, so the reply shows only what they wrote after the
    approved call ran.
    """
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch, show_tool_calls=show_tool_calls)
    team.models["default"] = ScriptedModel([answer("The checker touched it.", usage(50, 5))])
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])

    result = await team.resume(
        raised.paused,
        approved=True,
        approvals=(member_approval("call-touch", "dangerous"),),
        show_tool_calls=show_tool_calls,
    )

    assert isinstance(result, CompletedApprovalRun)
    assert ProbeToolkit.calls == ["dangerous"]
    assert result.response_text == HEADER + body
    assert result.metadata_content[AI_RUN_METADATA_KEY]["status"] == "completed"
    record, [member] = team.stored_runs()
    assert (record.status, member.status) == (RunStatus.completed, RunStatus.completed)
    assert record.requirements == []
    assert member.tools is not None
    assert (member.tools[0].confirmed, member.tools[0].result) == (True, "touched dangerous")
    # The leader reads the member's result where it read the pause notice, as it reads a member that never paused.
    assert team.models["default"].requests[-1].messages[-1].content == "Touched."


class _HandOffProbe(ProbeToolkit):
    """The squad's probe toolkit plus ``hand_off``, a call executed outside the agent loop, which nothing answers."""

    def __init__(self) -> None:
        Toolkit.__init__(
            self,
            name="probe",
            tools=[self.touch, self.scope, self.dig, self.model, self.hand_off],
            external_execution_required_tools=["hand_off"],
        )

    async def hand_off(self) -> str:
        """Hand the work to an external executor."""
        return "handed off"


@pytest.mark.asyncio
async def test_a_team_approval_resume_that_pauses_on_a_call_nothing_answers_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member that pauses again on an externally executed call no approval asks about fails the team's resume."""
    with registered_toolkit(PROBE_TOOL, _HandOffProbe):
        team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
        team.models["checker-model"] = ScriptedModel([calls(tool_call("hand_off", "call-hand"))])

        # The failure names the call the team waits for, not the generic note it gave before.
        with pytest.raises(RuntimeError, match=r"team run 'run-1' paused on \['hand_off'\] without an approval"):
            await team.resume(
                raised.paused,
                approved=True,
                approvals=(member_approval("call-touch", "dangerous"),),
            )

    assert ProbeToolkit.calls == ["dangerous"]
    record, _members = team.stored_runs()
    assert record.status is RunStatus.paused


@pytest.mark.asyncio
async def test_a_member_resumed_by_an_approval_stores_to_the_team_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member's calls after its approved call address the team's history, as its calls before the pause did."""
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel([answer("Done.")])
    team.models["checker-model"] = ScriptedModel([calls(tool_call("scope", "call-scope")), answer("Touched.")])

    result = await team.resume(
        raised.paused,
        approved=True,
        approvals=(member_approval("call-touch", "dangerous"),),
    )

    assert isinstance(result, CompletedApprovalRun)
    assert ProbeToolkit.scopes == ["team:squad"]


@pytest.mark.asyncio
async def test_a_saved_team_approval_resumes_the_run_in_the_scope_its_approval_retained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A team no longer configured resumes from the storage its approval retained, not the scope it resolves to now."""
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel([answer("The checker touched it.")])
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])
    del team.config.teams["squad"]

    result = await team.resume(
        raised.paused,
        approved=True,
        approvals=(member_approval("call-touch", "dangerous"),),
    )

    assert isinstance(result, CompletedApprovalRun)
    assert ProbeToolkit.calls == ["dangerous"]


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


class _LeaderRoute:
    """A native-history route that replays its own checkpoints whatever the request asks for."""

    replay = NativeCompactionReplay(route="leader-route", threshold=None)

    def native_compaction_settings(
        self,
        *,
        threshold: int | None,
        history_generation: str,
        allow_authored: bool,
    ) -> NativeCompactionReplay:
        del threshold, history_generation, allow_authored
        return self.replay


@pytest.mark.asyncio
async def test_an_approval_resume_restores_the_leader_s_native_history_from_the_leader_s_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resumed leader requests carry the native history the leader's route restores, not a member's."""
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel(
        [answer("The checker touched it.", usage(50, 5))],
        native_history_route=_LeaderRoute(),
    )
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])

    result = await team.resume(
        raised.paused,
        approved=True,
        approvals=(member_approval("call-touch", "dangerous"),),
    )

    assert isinstance(result, CompletedApprovalRun)
    [leader_request] = team.models["default"].requests
    assert leader_request.native_compaction == _LeaderRoute.replay
    [member_request] = team.models["checker-model"].requests
    assert member_request.native_compaction is None


@pytest.mark.asyncio
async def test_an_approval_resume_sends_the_receipt_to_the_member_and_the_leader_and_stores_it_in_neither(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The member whose call was approved and the leader each read the trusted receipt after their decided results.

    The response runner binds the receipt around the resume; each run keeps it, as a user message, for the rest of
    the run, and no finished run stores it. A member the resumed leader then hands a new task runs fresh and never
    reads it.
    """
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel(
        [delegate("call-polish", "writer", "Polish it"), answer("The checker touched it.", usage(50, 5))],
    )
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])
    team.models["writer-model"] = ScriptedModel([answer("Polished.")])
    call = member_approval("call-touch", "dangerous")
    decided = (replace(call, decision=ApprovalDecision.APPROVED, human_approval_required=True),)
    receipt = build_approval_receipt(decided)

    with approval_receipt_context(decided, run_id=raised.paused.run_id):
        await team.resume(raised.paused, approved=True, approvals=(call,))

    def receipt_roles(model: str) -> list[list[str]]:
        return [
            [message.role for message in request.messages if receipt in str(message.content)]
            for request in team.models[model].requests
        ]

    assert receipt_roles("checker-model") == [["user"]]
    assert receipt_roles("default") == [["user"], ["user"]]
    assert receipt_roles("writer-model") == [[]]
    record, members = team.stored_runs()
    assert all(receipt not in json.dumps(run.to_dict()) for run in [record, *members])


@pytest.mark.asyncio
async def test_an_approval_resume_repeats_the_request_the_leader_and_the_member_each_paused_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leader and the member resume with the request each paused on, then the paused turn, its result, the receipt.

    The leader's turn context, which no history keeps, comes back where the paused request sent it; each run's
    receipt follows its decided result and stays there for the run's later requests.
    """
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "checker", "Touch it")],
        checker=[calls(tool_call("touch", "call-touch", label="dangerous"), metrics=usage(10, 1))],
        approval_rules=_TOUCH_NEEDS_APPROVAL,
    )
    location = EnrichmentItem(key="location", text="Alice is in Lisbon.", persist=False)
    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.stream(ctx=team.ctx(transient_enrichment_items=(location,)), show_tool_calls=False)
    [paused_leader] = team.models["default"].requests
    [paused_checker] = team.models["checker-model"].requests
    team.models["default"] = ScriptedModel(
        [delegate("call-polish", "writer", "Polish it"), answer("The checker touched it.", usage(50, 5))],
    )
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])
    team.models["writer-model"] = ScriptedModel([answer("Polished.")])
    call = member_approval("call-touch", "dangerous")
    decided = (replace(call, decision=ApprovalDecision.APPROVED, human_approval_required=True),)

    with approval_receipt_context(decided, run_id=raised.value.paused.run_id):
        result = await team.resume(raised.value.paused, approved=True, approvals=(call,))

    assert isinstance(result, CompletedApprovalRun)
    assert "Alice is in Lisbon." in str(paused_leader.messages[-1].content)
    for paused_request, model in ((paused_leader, "default"), (paused_checker, "checker-model")):
        requests = [paused_request, *team.models[model].requests]
        assert repeats_each_request(requests), model
        resumed = requests[1]
        assert [message.role for message in resumed.messages[len(paused_request.messages) :]] == [
            "assistant",
            "tool",
            "user",
        ], model
        assert receipts(resumed) == [build_approval_receipt(decided)], model
    assert [len(receipts(request)) for request in team.models["default"].requests] == [1, 1]


@pytest.mark.asyncio
async def test_a_member_continued_after_its_own_delegation_in_an_approval_resume_never_reads_the_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the runs the approval resumes read its receipt, each once, and keep it for the rest of the run.

    The resumed leader hands the writer a fresh task; the writer delegates to the helper and continues with the
    helper's answer. No approval resumed the writer, so neither of its requests carries the receipt.
    """
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "checker", "Touch it")],
        checker=[calls(tool_call("touch", "call-touch", label="dangerous"), metrics=usage(10, 1))],
        helper=[],
        approval_rules=_TOUCH_NEEDS_APPROVAL,
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
    decided = (replace(call, decision=ApprovalDecision.APPROVED, human_approval_required=True),)
    receipt = build_approval_receipt(decided)

    with approval_receipt_context(decided, run_id=paused.run_id):
        result = await team.resume(paused, approved=True, approvals=(call,))

    def carries(model: str) -> list[bool]:
        return [any(receipt in str(m.content) for m in r.messages) for r in team.models[model].requests]

    assert isinstance(result, CompletedApprovalRun)
    assert carries("checker-model") == [True]
    assert carries("default") == [True, True]
    assert carries("writer-model") == [False, False]
    assert carries("helper-model") == [False]


@pytest.mark.asyncio
async def test_each_approval_of_a_chained_team_approval_reaches_the_runs_it_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The checker reads each approval's receipt on its resume, and the leader reads the second approval's.

    The checker's first approved call leads only to a second gated call, so its second resume keeps the first
    receipt where it was and adds the second after the newly decided result; the leader's first request comes after
    the second approval. What a receipt covers is the response runner's to compose, so each approval here binds a
    receipt of its own call alone.
    """
    team, paused = await _pause_after_the_checker_wrote(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel([answer("Both touches were approved.")])
    team.models["checker-model"] = ScriptedModel(
        [calls(tool_call("touch", "call-touch-2", label="again")), answer("Touched twice.")],
    )
    approvals = (member_approval("call-touch", "dangerous"), member_approval("call-touch-2", "again"))
    receipts = tuple(
        build_approval_receipt((replace(call, decision=ApprovalDecision.APPROVED, human_approval_required=True),))
        for call in approvals
    )

    async def approve(attempt: PausedAttempt, call: ApprovalCall) -> CompletedApprovalRun | PausedAttempt:
        decided = (replace(call, decision=ApprovalDecision.APPROVED, human_approval_required=True),)
        with approval_receipt_context(decided, run_id=attempt.run_id):
            return await team.resume(attempt, approved=True, approvals=(call,))

    repaused = await approve(paused, approvals[0])
    assert isinstance(repaused, PausedAttempt)
    result = await approve(repaused, approvals[1])

    def receipts_read(model: str) -> list[list[str]]:
        """Return, per request ``model`` received, the receipts it carries."""
        return [
            [receipt for receipt in receipts if any(receipt in str(message.content) for message in request.messages)]
            for request in team.models[model].requests
        ]

    assert isinstance(result, CompletedApprovalRun)
    assert ProbeToolkit.calls == ["dangerous", "again"]
    assert receipts_read("checker-model") == [[receipts[0]], [receipts[0], receipts[1]]]
    first, second = team.models["checker-model"].requests
    assert second.messages[: len(first.messages)] == first.messages
    assert receipts_read("default") == [[receipts[1]]]


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
    assert (member.tools[0].confirmed, member.tools[0].is_error) == (False, True)


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
        pytest.param([answer("\n")], [], NO_TEAM_RESPONSE, id="blank-leader-text"),
    ],
)
async def test_an_approved_resume_that_shows_no_text_renders_its_finished_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    leader: list[list[ScriptStep]],
    writer: list[list[ScriptStep]],
    body: str,
) -> None:
    """A resume whose runs write no text shows what they hand over, else the no-reply note, as a new team turn does.

    A member the resumed leader hands a task and that fails before any text shows its error; a leader that writes only
    blank text leaves the no-reply note. The approved checker says nothing after its call.
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
async def test_an_approved_resume_shows_each_run_s_content_in_a_slot_that_streamed_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resume shows each run's content in a slot that streamed no text of its own, as the Agno-backed resume did.

    The approved checker's streamed answer stands as it streamed. The writer the resumed leader then hands a task
    streams nothing, and its error fills its slot.
    """
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel([delegate("call-write", "writer", "Write it up"), answer("Done.")])
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])
    team.models["writer-model"] = ScriptedModel([[Await(_writer_unavailable)]])

    result = await team.resume(
        raised.paused,
        approved=True,
        approvals=(member_approval("call-touch", "dangerous"),),
    )

    assert ProbeToolkit.calls == ["dangerous"]
    assert result.response_text == (
        HEADER + "**Writer**: Writer unavailable\n\n**Checker**: Touched.\n\n\n**Team Consensus**:\n\nDone."
    )


@pytest.mark.asyncio
async def test_a_resume_that_pauses_on_another_member_s_call_shows_no_paused_notice_through_the_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resume that pauses again on a call of the writer shows no paused notice, nor does the reply it completes.

    The writer the resumed leader hands a task pauses on its own gated call without text.
    """
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel([delegate("call-write", "writer", "Touch it too"), answer("Both touched.")])
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])
    team.models["writer-model"] = ScriptedModel([calls(tool_call("touch", "call-touch-2", label="risky")), answer("")])

    repaused = await team.resume(raised.paused, approved=True, approvals=(member_approval("call-touch", "dangerous"),))
    assert isinstance(repaused, PausedAttempt)
    assert repaused.response_text == f"{HEADER}**Checker**: Touched."
    writer_call = replace(member_approval("call-touch-2", "risky"), invoking_agent="writer")
    result = await team.resume(repaused, approved=True, approvals=(writer_call,))

    assert isinstance(result, CompletedApprovalRun)
    assert ProbeToolkit.calls == ["dangerous", "risky"]
    assert result.response_text == f"{HEADER}**Checker**: Touched.\n\n\n**Team Consensus**:\n\nBoth touched."
    record, _members = team.stored_runs()
    assert record.status is RunStatus.completed


_KEY = "api_key=unit-test-secret"


async def _writer_leaks_its_key() -> None:
    msg = f"Writer unavailable: {_KEY}"
    raise ProviderError(msg, provider="Synthetic", status_code=503)


async def _checker_leaks_its_key() -> None:
    msg = f"Checker unavailable: {_KEY}"
    raise ProviderError(msg, provider="Synthetic", status_code=503)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("leader", "checker", "writer", "body"),
    [
        pytest.param(
            [delegate("call-write", "writer", "Write it up"), answer("Done.")],
            [answer("Touched.")],
            [[Await(_writer_leaks_its_key)]],
            "**Writer**: Writer unavailable: api_key=***redacted***\n\n**Checker**: Touched.\n\n\n"
            "**Team Consensus**:\n\nDone.",
            id="started-in-the-resume",
        ),
        pytest.param(
            [answer("Done.")],
            [[Await(_checker_leaks_its_key)]],
            [],
            "**Checker**: Checker unavailable: api_key=***redacted***\n\n\n**Team Consensus**:\n\nDone.",
            id="resumed-after-approval",
        ),
    ],
)
async def test_an_approved_resume_shows_a_failed_member_s_redacted_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    leader: list[list[ScriptStep]],
    checker: list[list[ScriptStep]],
    writer: list[list[ScriptStep]],
    body: str,
) -> None:
    """A member whose run fails in an approval resume shows why in the resumed reply, credentials redacted.

    The writer the resumed leader hands a task fails before it writes anything. The approved checker fails after its
    approved call ran.
    """
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel(leader)
    team.models["checker-model"] = ScriptedModel(checker)
    team.models["writer-model"] = ScriptedModel(writer)

    result = await team.resume(
        raised.paused,
        approved=True,
        approvals=(member_approval("call-touch", "dangerous"),),
    )

    assert ProbeToolkit.calls == ["dangerous"]
    assert result.response_text == HEADER + body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("leader", "checker", "writer", "body"),
    [
        pytest.param(
            [answer("Done.")],
            [[TextDelta("Touched it, now checking"), Await(_checker_leaks_its_key)]],
            [],
            "**Checker**: Touched it, now checking\n\nChecker unavailable: api_key=***redacted***\n\n\n"
            "**Team Consensus**:\n\nDone.",
            id="failed-after-writing",
        ),
        pytest.param(
            [
                delegate("call-draft", "writer", "Draft it"),
                delegate("call-revise", "writer", "Revise it"),
                answer("Done."),
            ],
            [answer("Touched.")],
            [answer("Draft."), [Await(_writer_leaks_its_key)]],
            "**Writer**: Draft.\n\nWriter unavailable: api_key=***redacted***\n\n**Checker**: Touched.\n\n\n"
            "**Team Consensus**:\n\nDone.",
            id="failed-after-an-earlier-run-wrote",
        ),
    ],
)
async def test_an_approved_resume_shows_a_failed_run_s_reason_after_the_text_its_member_wrote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    leader: list[list[ScriptStep]],
    checker: list[list[ScriptStep]],
    writer: list[list[ScriptStep]],
    body: str,
) -> None:
    """A member run that fails in an approval resume shows why in its own paragraph, after the text its slot holds.

    The approved checker streams part of an answer before its request fails. The writer the resumed leader hands two
    tasks answers the first and fails the second before writing anything; the first run's answer stays.
    """
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel(leader)
    team.models["checker-model"] = ScriptedModel(checker)
    team.models["writer-model"] = ScriptedModel(writer)

    result = await team.resume(
        raised.paused,
        approved=True,
        approvals=(member_approval("call-touch", "dangerous"),),
    )

    assert ProbeToolkit.calls == ["dangerous"]
    assert result.response_text == HEADER + body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("checker", "slot"),
    [
        pytest.param(
            [[TextDelta("Checking it"), Await(_checker_leaks_its_key)]],
            "Checking it\n\nChecker unavailable: api_key=***redacted***",
            id="failed-after-writing",
        ),
        pytest.param(
            [[Await(_checker_leaks_its_key)]],
            "Checker unavailable: api_key=***redacted***",
            id="failed-before-writing",
        ),
    ],
)
async def test_a_streamed_team_reply_shows_a_failed_member_run_s_reason_after_the_text_it_streamed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checker: list[list[ScriptStep]],
    slot: str,
) -> None:
    """A member run that fails in a live streamed turn shows why in its own paragraph, as a resumed reply shows it.

    The checker's deltas show only what it wrote before its request failed. The reply delivers and records the
    reason after that text, credentials redacted.
    """
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-check", "checker", "Check"), answer("Done.")],
        checker=checker,
    )

    chunks = await team.stream(show_tool_calls=False)

    text = f"{HEADER}**Checker**: {slot}\n\n\n**Team Consensus**:\n\nDone."
    assert rendered(chunks[-1]) == text
    assert team.recorder.assistant_text == text


@pytest.mark.asyncio
async def test_a_team_pause_shows_a_failed_member_run_s_reason_after_the_text_it_streamed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The document a live pause shows, and its resume continues, holds a failed member run's reason.

    The checker fails after writing; the writer the leader then hands a task pauses on its gated call.
    """
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-check", "checker", "Check"), delegate("call-write", "writer", "Touch it")],
        checker=[[TextDelta("Checking it"), Await(_checker_leaks_its_key)]],
        writer=[calls(tool_call("touch", "call-touch", label="dangerous"))],
        approval_rules=_TOUCH_NEEDS_APPROVAL,
    )

    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.respond(show_tool_calls=False)

    paused = raised.value.paused
    slot = "Checking it\n\nChecker unavailable: api_key=***redacted***"
    # The leader has not answered yet, so the pause shows no no-consensus note.
    assert paused.response_text == f"{HEADER}**Checker**: {slot}"
    assert [member["content"] for member in paused.response_presentation_state["members"]] == ["", slot]


_NO_CONSENSUS = "*No team consensus - showing individual responses only*"


@pytest.mark.asyncio
async def test_a_team_reply_shows_the_no_consensus_note_only_once_it_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live chunks, the pause, and the resume's progress show no no-consensus note; the finished reply ends with it.

    The checker writes before its gated call and the leader has not answered when the team pauses, so every
    unfinished view shows member text without a consensus. After the approval the leader says nothing, so the reply
    finishes without one.
    """
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "checker", "Touch it")],
        checker=[calls(tool_call("touch", "call-touch", label="dangerous"), content="Let me touch it.")],
        approval_rules=_TOUCH_NEEDS_APPROVAL,
    )
    live: list[TeamStreamChunk] = []
    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.stream(show_tool_calls=False, chunks=live)
    paused = raised.value.paused
    team.models["default"] = ScriptedModel([answer("")])
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])
    progress: list[StructuredStreamChunk] = []

    async def publish(chunk: StructuredStreamChunk) -> None:
        progress.append(chunk)

    result = await team.resume(
        paused,
        approved=True,
        approvals=(member_approval("call-touch", "dangerous"),),
        progress=publish,
    )

    assert live
    assert progress
    unfinished = [*(rendered(chunk) for chunk in live), paused.response_text, *(chunk.content for chunk in progress)]
    assert all("Let me touch it." in text and _NO_CONSENSUS not in text for text in unfinished)
    assert isinstance(result, CompletedApprovalRun)
    assert result.response_text == f"{HEADER}**Checker**: Let me touch it.\n\nTouched.\n\n\n{_NO_CONSENSUS}"


@pytest.mark.asyncio
async def test_a_completed_team_reply_in_which_only_members_spoke_ends_with_the_no_consensus_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reply's live chunks show no note and its last chunk, the finished reply, adds it.

    The turn records the reply it ends with, note included.
    """
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-write", "writer", "Write it"), answer("")],
        writer=[answer("Written.")],
    )

    chunks = await team.stream(show_tool_calls=False)
    *live, text = [rendered(chunk) for chunk in chunks]
    assert live
    assert all(_NO_CONSENSUS not in shown for shown in live)

    assert text == f"{HEADER}**Writer**: Written.\n\n\n{_NO_CONSENSUS}"
    assert team.recorder.assistant_text == text


async def _pause_after_the_checker_wrote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TeamTurn, PausedAttempt]:
    """Pause the team on the checker's gated call after the leader and the checker each wrote a sentence."""
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
        approval_rules=_TOUCH_NEEDS_APPROVAL,
    )
    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.stream(show_tool_calls=False)
    return team, raised.value.paused


_WROTE_BEFORE_THE_PAUSE = f"{HEADER}**Checker**: Let me touch it.\n\n\n**Team Consensus**:\n\nAsking the checker."


@pytest.mark.asyncio
async def test_an_approved_resume_shows_once_the_text_its_runs_wrote_before_the_pause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text the leader and the checker wrote before the pause stands once when neither writes more after approval.

    The Agno-backed resume added a content-less run's earlier messages to its slot again; the restored document
    already holds them.
    """
    team, paused = await _pause_after_the_checker_wrote(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel([answer("")])
    team.models["checker-model"] = ScriptedModel([answer("")])

    result = await team.resume(paused, approved=True, approvals=(member_approval("call-touch", "dangerous"),))

    assert ProbeToolkit.calls == ["dangerous"]
    assert result.response_text == _WROTE_BEFORE_THE_PAUSE


@pytest.mark.asyncio
async def test_a_member_asked_again_after_an_approval_resume_starts_a_new_paragraph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The writer's draft from before another member's pause stays a paragraph apart from its revision after approval."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-draft", "writer", "Draft"), delegate("call-check", "checker", "Touch it")],
        writer=[answer("Draft one.")],
        checker=[calls(tool_call("touch", "call-touch", label="dangerous"))],
        approval_rules=_TOUCH_NEEDS_APPROVAL,
    )
    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.stream(show_tool_calls=False)
    team.models["default"] = ScriptedModel([delegate("call-revise", "writer", "Revise"), answer("Both done.")])
    team.models["checker-model"] = ScriptedModel([answer("Checked.")])
    team.models["writer-model"] = ScriptedModel([answer("Draft two.")])

    result = await team.resume(
        raised.value.paused,
        approved=True,
        approvals=(member_approval("call-touch", "dangerous"),),
    )

    assert result.response_text == (
        f"{HEADER}**Writer**: Draft one.\n\nDraft two.\n\n**Checker**: Checked.\n\n\n**Team Consensus**:\n\nBoth done."
    )


@pytest.mark.asyncio
async def test_an_approved_member_that_answers_without_text_shows_no_paused_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member that paused without text and answers after its approved call without text adds nothing to its slot."""
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel([answer("Done.")])
    team.models["checker-model"] = ScriptedModel([answer("")])

    result = await team.resume(raised.paused, approved=True, approvals=(member_approval("call-touch", "dangerous"),))

    assert ProbeToolkit.calls == ["dangerous"]
    assert result.response_text == f"{HEADER}\n**Team Consensus**:\n\nDone."


@pytest.mark.asyncio
async def test_a_member_that_pauses_again_after_approval_shows_its_earlier_text_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member whose approved call leads only to another gated call keeps its earlier text once in the new pause.

    The leader sends no request while its member waits, so it adds nothing to the pause.
    """
    team, paused = await _pause_after_the_checker_wrote(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel([])
    team.models["checker-model"] = ScriptedModel([calls(tool_call("touch", "call-touch-2", label="again"))])

    repaused = await team.resume(paused, approved=True, approvals=(member_approval("call-touch", "dangerous"),))

    assert isinstance(repaused, PausedAttempt)
    assert ProbeToolkit.calls == ["dangerous"]
    assert repaused.response_text == _WROTE_BEFORE_THE_PAUSE
    state = repaused.response_presentation_state
    assert [member["content"] for member in state["members"]] == ["", "Let me touch it."]


@pytest.mark.asyncio
async def test_a_chained_approval_shows_the_text_each_run_wrote_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two approvals in a row show the text the checker wrote before the first pause once, and no paused notice.

    Its first approved call leads only to a second gated call, and after the second it answers without text, as
    the leader does.
    """
    team, paused = await _pause_after_the_checker_wrote(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel([answer("")])
    team.models["checker-model"] = ScriptedModel([calls(tool_call("touch", "call-touch-2", label="again")), answer("")])

    repaused = await team.resume(paused, approved=True, approvals=(member_approval("call-touch", "dangerous"),))
    assert isinstance(repaused, PausedAttempt)
    result = await team.resume(repaused, approved=True, approvals=(member_approval("call-touch-2", "again"),))

    assert isinstance(result, CompletedApprovalRun)
    assert ProbeToolkit.calls == ["dangerous", "again"]
    assert result.response_text == _WROTE_BEFORE_THE_PAUSE


@pytest.mark.asyncio
async def test_a_collaborate_resume_hands_the_leader_the_sibling_that_finished_before_the_pause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The writer finished before the checker paused; once the checker's call is approved, the leader reads both.

    The collaborate result keeps the writer's piece as the first run wrote it, and the checker's pause notice
    becomes the checker's result under the notice's label.
    """
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[calls(tool_call("delegate_task_to_members", "call-all", task="Touch it"), metrics=usage(40, 4))],
        writer=[answer("Writer drafted.")],
        checker=[calls(tool_call("touch", "call-touch", label="dangerous"), metrics=usage(10, 1))],
        mode="collaborate",
        approval_rules=_TOUCH_NEEDS_APPROVAL,
    )
    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.respond(show_tool_calls=False)
    team.models["default"] = ScriptedModel([answer("Both answered.")])
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])

    result = await team.resume(
        raised.value.paused,
        approved=True,
        approvals=(member_approval("call-touch", "dangerous"),),
    )

    assert isinstance(result, CompletedApprovalRun)
    assert ProbeToolkit.calls == ["dangerous"]
    answered = "Agent Writer: Writer drafted.\n\nAgent Checker: Touched."
    [request] = team.models["default"].requests
    assert request.messages[-1].content == answered
    record, _members = team.stored_runs()
    assert record.tools is not None
    assert [tool.result for tool in record.tools] == [answered]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["coordinate", "collaborate"])
async def test_a_chained_team_approval_hands_the_leader_each_member_s_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str,
) -> None:
    """Both members pause; the first approval finishes the writer and leads the checker to a second gated call.

    The leader's request after the second approval reads the writer's result beside the checker's, though no call of
    that approval was the writer's: a coordinate leader reads each member's result on the call that delegated to
    it, and a collaborate leader reads both pieces under their labels.
    """
    if mode == "coordinate":
        delegations = calls(
            tool_call("delegate_task_to_member", "call-w", member_id="writer", task="Touch a"),
            tool_call("delegate_task_to_member", "call-c", member_id="checker", task="Touch b"),
            metrics=usage(40, 4),
        )
    else:
        delegations = calls(tool_call("delegate_task_to_members", "call-all", task="Touch it"), metrics=usage(40, 4))
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegations],
        writer=[calls(tool_call("touch", "touch-w", label="a"), metrics=usage(10, 1))],
        checker=[calls(tool_call("touch", "touch-c", label="b"), metrics=usage(10, 1))],
        mode=mode,
        approval_rules=_TOUCH_NEEDS_APPROVAL,
    )
    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.stream(show_tool_calls=False)
    team.models["default"] = ScriptedModel([answer("Both touched.")])
    team.models["writer-model"] = ScriptedModel([answer("Writer touched.")])
    team.models["checker-model"] = ScriptedModel(
        [calls(tool_call("touch", "touch-c2", label="b again")), answer("Checker touched.")],
    )

    repaused = await team.resume(
        raised.value.paused,
        approved=True,
        approvals=(member_approval("touch-w", "a", agent="writer"), member_approval("touch-c", "b")),
    )
    assert isinstance(repaused, PausedAttempt)
    result = await team.resume(repaused, approved=True, approvals=(member_approval("touch-c2", "b again"),))

    assert isinstance(result, CompletedApprovalRun)
    assert sorted(ProbeToolkit.calls) == ["a", "b", "b again"]
    [request] = team.models["default"].requests
    answered = (
        ["Writer touched.", "Checker touched."]
        if mode == "coordinate"
        else ["Agent Writer: Writer touched.\n\nAgent Checker: Checker touched."]
    )
    assert [message.content for message in request.messages if message.role == "tool"] == answered


async def _lose_the_provider() -> None:
    msg = "provider connection lost"
    raise ProviderError(msg, provider="Synthetic")


@pytest.mark.asyncio
async def test_a_failed_resume_stores_the_error_over_the_paused_row_and_ran_the_approved_tool_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leader request that fails after the approved call ran reaches the caller; the rows record the outcome."""
    team, raised = await _pause_on_the_checker(tmp_path, monkeypatch)
    team.models["default"] = ScriptedModel([[Await(_lose_the_provider)]])
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])

    with pytest.raises(ProviderError, match="provider connection lost"):
        await team.resume(raised.paused, approved=True, approvals=(member_approval("call-touch", "dangerous"),))

    assert ProbeToolkit.calls == ["dangerous"]
    record, [member] = team.stored_runs()
    assert (record.status, member.status) == (RunStatus.error, RunStatus.completed)
    assert "provider connection lost" in str(record.error)
    assert member.tools is not None
    assert (member.tools[0].confirmed, member.tools[0].result) == (True, "touched dangerous")


@pytest.mark.asyncio
async def test_a_team_reply_that_fails_mid_turn_reports_its_stored_run_in_the_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run whose leader request fails after a member answered reports that run, ended in error, as the reply's metadata.

    The turn driver stores the failed turn as its interrupted record, in the failed run's place.
    """
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Draft it"), [Await(_lose_the_provider)]],
        writer=[answer("Draft.", usage(20, 2))],
    )
    metadata: dict[str, Any] = {}

    await team.stream(run_metadata_collector=metadata)

    record, _ = team.stored_runs()
    payload = metadata[AI_RUN_METADATA_KEY]
    assert (record.status, payload["run_id"], payload["status"]) == (RunStatus.completed, record.run_id, "error")
    assert record.metadata is not None
    assert record.metadata["mindroom_original_status"] == "error"
    assert payload["tools"] == {"count": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False])
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
            "\n**Team Consensus**:\n\nprovider connection lost",
            id="on-the-first-request",
        ),
        pytest.param(
            [[TextDelta("Checked once."), Await(_lose_the_provider)]],
            "\n**Team Consensus**:\n\nChecked once.\n\nprovider connection lost",
            id="after-streaming-text",
        ),
    ],
)
async def test_a_failed_team_turn_records_why_its_runs_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    leader: list[list[ScriptStep]],
    recorded: str,
    stream: bool,
) -> None:
    """A turn whose leader request fails records its document with each failed run's reason, on either path.

    A member that failed before any text shows its error; the leader's failure stands in its consensus, after any
    text the failed request had streamed. The turn's interrupted record takes the failed team run's place.
    """
    team = squad_turn(tmp_path, monkeypatch, leader=leader, checker=[[Await(_checker_overloaded)]])

    await team.reply(stream=stream, show_tool_calls=False)

    record, _ = team.stored_runs()
    assert team.recorder.outcome == "interrupted"
    assert team.recorder.assistant_text == recorded
    assert (record.status, record.content) == (
        RunStatus.completed,
        _render_interrupted_replay_content(team.recorder.interrupted_snapshot()),
    )
    assert record.metadata is not None
    assert record.metadata["mindroom_original_status"] == "error"


async def _lose_the_provider_and_leak_its_key() -> None:
    msg = f"provider connection lost: {_KEY}"
    raise ProviderError(msg, provider="Synthetic")


@pytest.mark.asyncio
async def test_a_failed_team_turn_records_its_leader_s_redacted_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The text a turn records when its leader fails before writing is why it failed, credentials redacted."""
    team = squad_turn(tmp_path, monkeypatch, leader=[[Await(_lose_the_provider_and_leak_its_key)]])

    await team.respond(show_tool_calls=False)

    assert team.recorder.outcome == "interrupted"
    assert team.recorder.assistant_text == "\n**Team Consensus**:\n\nprovider connection lost: api_key=***redacted***"


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
    """A saved approval from a session that holds no paused run of the team is refused before anything runs, as an agent's is."""
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
    assert [(entry.tool_name, entry.member_id) for entry in chunks[-1].tool_trace or []] == [
        ("run_subagent", "writer"),
        ("delegate_task_to_member", None),
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
    assert team.models["default"].requests[-1].messages[-1].content == "Writer: the helper found it."


def _cut_off(call_id: str) -> list[ScriptStep]:
    """Return a response the output limit cut off while it requested ``touch``."""
    *steps, completed = calls(tool_call("touch", call_id, label="cut off"))
    assert isinstance(completed, TurnCompleted)
    return [*steps, replace(completed, stop_reason="output_limit")]


@pytest.mark.asyncio
async def test_a_member_keeps_its_hooks_across_its_delegation_continuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member run's hook state lasts the whole run: its third cut-off ends it, though a delegation came between.

    The writer is cut off twice, delegates to the helper, and is cut off once more when the team continues it, so
    the output-limit policy stops its run and the leader reads why.
    """
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Research it"), answer("Consensus.")],
        helper=[answer("Helper found it.")],
    )
    team.models["writer-model"] = ScriptedModel(
        [_cut_off("cut-1"), _cut_off("cut-2"), _delegate_to_helper("Dig"), _cut_off("cut-3"), answer("Rebuilt.")],
    )

    await team.stream()

    assert ProbeToolkit.calls == []
    assert len(team.models["writer-model"].requests) == 4
    _, member, _ = _delegation_state(team)
    assert member.status is RunStatus.error
    assert "The output limit cut off 3 of this model's responses" in str(
        team.models["default"].requests[-1].messages[-1].content,
    )


def test_a_copy_of_a_member_build_keeps_the_hooks_of_its_runs() -> None:
    """A member's build is a frozen spec that holds its runs' hooks, so ``replace`` must carry them over.

    A copy continues a run with the hooks the run started with, and a new run gets one set whichever copy runs it.
    """
    made: list[LoopHooks] = []

    def new_hooks() -> LoopHooks:
        made.append(LoopHooks())
        return made[-1]

    writer = replace(squad_member("writer", "Writer", ScriptedModel([])).build(), new_hooks=new_hooks)
    first = writer.hooks("run-1")
    copy = replace(writer, request_template=ModelRequest(messages=()))

    assert copy.hooks("run-1") is first
    assert copy.hooks("run-2") is writer.hooks("run-2")
    assert len(made) == 2


@pytest.mark.asyncio
async def test_two_members_delegations_under_one_call_id_each_run_once_and_the_team_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Members number their calls independently, so two can delegate under one call id; each child runs once.

    The members run concurrently, so either child may ask the shared helper first; each member's call finishes with
    the answer of the child its delegation state records for it.
    """
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

    chunks = await team.stream()

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
    answers: dict[str, str] = {}
    for child in state.children:
        identity = child_execution_identity(child)
        helper_run = create_session_storage("helper", team.config, team.runtime_paths, identity).get_run(child.run_id)
        assert helper_run is not None
        answers[child.caller_agent_name] = str(helper_run.content)
    assert sorted(answers.values()) == ["Helper one.", "Helper two."]
    # The driver's events about each member's call land in that member's slot, each call finished with its child.
    assert sorted(
        (entry.member_id or "", entry.type, (entry.result_preview or "").partition("\n")[0])
        for entry in chunks[-1].tool_trace or []
        if entry.tool_name == "run_subagent"
    ) == [
        ("checker", "tool_call_completed", answers["checker"]),
        ("writer", "tool_call_completed", answers["writer"]),
    ]


@pytest.mark.asyncio
async def test_two_members_approvals_under_one_call_id_are_refused_and_neither_call_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An approval names its call by call id, so two members' calls under one id are refused rather than guessed at.

    The members' rows stay paused; the failed turn's interrupted record takes the team run's place.
    """
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[
            calls(
                tool_call("delegate_task_to_member", "call-w", member_id="writer", task="Touch A"),
                tool_call("delegate_task_to_member", "call-c", member_id="checker", task="Touch B"),
                metrics=usage(40, 4),
            ),
        ],
        writer=[calls(tool_call("touch", "call-touch", label="a"), metrics=usage(10, 1))],
        checker=[calls(tool_call("touch", "call-touch", label="b"), metrics=usage(10, 1))],
        approval_rules=_TOUCH_NEEDS_APPROVAL,
    )

    chunks = await team.stream(show_tool_calls=False)

    assert chunks[-1].endswith("Paused approval requirements contain duplicate tool-call IDs")
    assert ProbeToolkit.calls == []
    record, members = team.stored_runs()
    assert record.status is RunStatus.completed
    assert record.metadata is not None
    assert record.metadata["mindroom_original_status"] == "error"
    assert sorted((member.agent_id or "", member.status) for member in members) == [
        ("checker", RunStatus.paused),
        ("writer", RunStatus.paused),
    ]


@pytest.mark.asyncio
async def test_a_team_continues_after_a_member_s_delegation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A team turn continues its leader once a member's own delegation finished."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Research it"), answer("Consensus.", usage(50, 5))],
        writer=[_delegate_to_helper("Dig"), answer("Writer: the helper found it.")],
        helper=[answer("Helper found it.")],
    )

    text = await team.respond(show_tool_calls=False)

    assert text.endswith("**Team Consensus**:\n\nConsensus.")
    assert len(team.models["default"].requests) == 2


@pytest.mark.asyncio
async def test_a_team_reply_shows_what_a_member_wrote_before_and_after_its_delegation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member that paused on its delegation and resumed within the turn shows once, both its texts a paragraph apart.

    The member's slot holds what it wrote before it paused, then what its resumed run wrote; both stay in its stored
    messages.
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

    assert text == (
        f"{HEADER}**Writer**: Asking the helper.\n\nWriter: the helper found it.\n\n\n**Team Consensus**:\n\nConsensus."
    )
    assert team.recorder.assistant_text == text
    _, member, _ = _delegation_state(team)
    assert [message.content for message in member.messages if message.role == "assistant"] == [
        "Asking the helper.",
        "Writer: the helper found it.",
    ]


@pytest.mark.asyncio
async def test_a_blocking_team_reply_whose_leader_says_nothing_more_shows_what_it_wrote_before_its_member_paused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leader silent after its member's delegation keeps what it wrote before the pause, which the reply shows.

    Nobody wrote anything after the delegation, so the reply's consensus is the text the leader streamed before it
    delegated, and the leader's content is that text.
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

    assert text == f"{HEADER}\n**Team Consensus**:\n\nAsking the writer."
    assert team.recorder.assistant_text == text
    record, _, _ = _delegation_state(team)
    assert record.content == "Asking the writer."


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
        approval_rules=_TOUCH_NEEDS_APPROVAL,
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
async def test_a_child_that_pauses_again_after_its_approval_shows_the_leader_s_earlier_text_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A team paused on its member's child shows what the leader wrote before the pause once when the child asks again.

    The delegation driver hands back the stored team run unresumed, whose content is that text, which the restored
    document already shows.
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
            answer("Consensus.", usage(50, 5)),
        ],
        writer=[_delegate_to_helper("Dig"), answer("Writer: dug.")],
        helper=[
            calls(tool_call("touch", "call-helper", label="deep"), metrics=usage(5, 1)),
            calls(tool_call("touch", "call-helper-again", label="deeper"), metrics=usage(5, 1)),
            answer("Dug deeper."),
        ],
        approval_rules=_TOUCH_NEEDS_APPROVAL,
    )
    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.stream(show_tool_calls=False)
    first = raised.value.paused
    assert first.response_text.count("Asking the writer.") == 1
    _, _, state = _delegation_state(team)

    again = await team.resume(first, approved=True, approvals=_saved_approval_calls(state))

    assert isinstance(again, PausedAttempt)
    assert ProbeToolkit.calls == ["deep"]
    assert again.response_text.count("Asking the writer.") == 1


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
    assert [(entry.tool_name, entry.member_id) for entry in paused.tool_trace] == [
        ("run_subagent", "writer"),
        ("delegate_task_to_member", None),
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
    ("approval_rule", "helper", "helper_flags", "caller"),
    [
        pytest.param(
            "touch",
            [calls(tool_call("touch", "call-helper", label="deep"), metrics=usage(5, 1)), answer("Dug deep.")],
            [False, True],
            "helper-model",
            id="child-call",
        ),
        pytest.param("run_subagent", [answer("Dug.")], [False], "writer-model", id="delegation-card"),
    ],
)
async def test_a_member_continued_with_the_result_of_a_delegation_an_approval_decided_reads_the_receipt_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    approval_rule: str,
    helper: list[list[ScriptStep]],
    helper_flags: list[bool],
    caller: str,
) -> None:
    """The member whose delegation the approval answers reads its receipt once, as the leader the card belongs to does.

    The approval decides the helper's call the writer's delegation waits on, or the card of the writer's delegation
    itself; the writer then continues with the helper's answer. A helper the approved card starts runs fresh and
    never reads the receipt. The run that made the decided call reads it under the id that run gave it, though its
    card names it ``<delegation-id>:call-helper`` or ``writer:call-sub``; every other reader holds no such call and
    reads the card's id.
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
    decided = tuple(
        replace(call, decision=ApprovalDecision.APPROVED, human_approval_required=True) for call in approvals
    )

    with approval_receipt_context(decided, run_id=paused.run_id):
        result = await team.resume(paused, approved=True, approvals=approvals)

    def carries(model: str) -> list[bool]:
        return [carries_approval_receipt(request) for request in team.models[model].requests]

    assert isinstance(result, CompletedApprovalRun)
    # Each model's fresh request, then its first request after the approval.
    assert carries("helper-model") == helper_flags
    assert carries("writer-model") == [False, True]
    assert carries("default") == [False, True]
    [card_id] = [call.tool_call_id for call in approvals]
    own_id = card_id.rpartition(":")[2]
    for model in dict.fromkeys((caller, "writer-model", "default")):
        [resumed] = [request for request in team.models[model].requests if carries_approval_receipt(request)]
        assert receipt_call_ids(resumed) == [own_id if model == caller else card_id], model
        assert (own_id in request_call_ids(resumed)) is (model == caller), model


@pytest.mark.asyncio
async def test_a_member_reads_its_earlier_delegation_card_under_its_own_call_id_after_a_chained_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The writer's second receipt records its delegation card from the first approval under the writer's own id.

    The first approval decides the card of the writer's ``run_subagent`` (``writer:call-sub``); the writer then makes a
    gated call of its own, which the second approval decides. The second receipt records both calls, as the response
    runner composes it, and the writer reads both under the ids its own request holds.
    """
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Research it"), answer("Consensus.", usage(50, 5))],
        writer=[
            _delegate_to_helper("Dig"),
            calls(tool_call("touch", "call-touch", label="after"), metrics=usage(10, 1)),
            answer("Writer: done."),
        ],
        helper=[answer("Dug.")],
        approval_rules=[
            ApprovalRuleConfig(match="run_subagent", action="require_approval"),
            ApprovalRuleConfig(match="touch", action="require_approval"),
        ],
    )
    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.stream(show_tool_calls=False)
    _, _, state = _delegation_state(team)
    [card] = _saved_approval_calls(state)
    touch = replace(member_approval("call-touch", "after"), invoking_agent="writer")
    card_decided, touch_decided = (
        replace(call, decision=ApprovalDecision.APPROVED, human_approval_required=True) for call in (card, touch)
    )

    with approval_receipt_context((card_decided,), run_id=raised.value.paused.run_id):
        repaused = await team.resume(raised.value.paused, approved=True, approvals=(card,))
    assert isinstance(repaused, PausedAttempt)
    with approval_receipt_context((card_decided, touch_decided), run_id=repaused.run_id):
        result = await team.resume(repaused, approved=True, approvals=(touch,))

    assert isinstance(result, CompletedApprovalRun)
    assert card.tool_call_id == "writer:call-sub"
    first, second = [request for request in team.models["writer-model"].requests if carries_approval_receipt(request)]
    assert receipt_call_ids(first) == ["call-sub"]
    assert receipt_call_ids(second) == ["call-sub", "call-touch"]
    assert request_call_ids(second) == ["call-sub", "call-touch"]


@pytest.mark.asyncio
async def test_a_stopped_member_keeps_its_usage_and_its_row_ends_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Stop while a member works stores the member's cut-off request and both runs' usage before the stop lands.

    The member's row ends cancelled; the turn's interrupted record takes the team run's place.
    """
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
    assert (record.status, member.status) == (RunStatus.completed, RunStatus.cancelled)
    assert record.metadata is not None
    assert record.metadata["mindroom_original_status"] == "cancelled"
    assert member.parent_run_id == record.run_id
    [member_usage] = _usage_rows(team.storage, member.run_id)
    assert member_usage["metrics"]["input_tokens"] == 30
    [leader_usage] = _usage_rows(team.storage, record.run_id)
    assert leader_usage["metrics"]["input_tokens"] == 40
    assert team.recorder.outcome == "interrupted"
    assert team.recorder.assistant_text == "**Writer**: Dra"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False])
async def test_a_stopped_team_turn_records_its_member_runs_in_configured_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    """A Stop records the document its run's events built, each member in its configured slot, on either path.

    The writer, stopped while its call ran, shows what it wrote before; the checker failed before any text and shows
    its error.
    """
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-check", "checker", "Check"), delegate("call-write", "writer", "Write")],
        checker=[[Await(_checker_overloaded)]],
        writer=[calls(tool_call("touch", "call-touch", label="draft", wait=True), content="Drafting.")],
    )
    task = asyncio.create_task(team.reply(stream=stream, show_tool_calls=False))
    await asyncio.wait_for(ProbeToolkit.waiting.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record, members = team.stored_runs()
    assert [run.status for run in (record, *members)] == [RunStatus.completed, RunStatus.error, RunStatus.cancelled]
    assert record.metadata is not None
    assert record.metadata["mindroom_original_status"] == "cancelled"
    assert team.recorder.outcome == "interrupted"
    assert team.recorder.assistant_text == "**Writer**: Drafting.\n\n**Checker**: Checker overloaded"


@pytest.mark.asyncio
async def test_a_stopped_team_turn_records_what_its_leader_wrote_before_its_member_s_delegation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leader stopped after its member's delegation keeps the text it had written, which the turn records.

    Its member said nothing after the delegation, so the text the leader wrote before the delegation is the consensus
    the stopped reply showed, and the turn's interrupted record keeps it.
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
    assert team.recorder.outcome == "interrupted"
    assert team.recorder.assistant_text == "\n**Team Consensus**:\n\nAsking the writer."
    assert (record.status, record.content) == (
        RunStatus.completed,
        _render_interrupted_replay_content(team.recorder.interrupted_snapshot()),
    )
    assert record.metadata is not None
    assert record.metadata["mindroom_original_status"] == "cancelled"


@pytest.mark.asyncio
async def test_a_team_turn_stopped_after_its_run_ended_records_the_finished_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Stop that lands after the run ended, before the turn settles, records the finished reply's document.

    The checker failed before any text and shows its error, which the document added once the team run ended.
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
async def test_a_team_turn_that_fails_after_its_run_ended_records_the_document_it_built(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn whose run paused on a call nothing can resume fails, and records the document its run built.

    The members are in their configured slots: the writer paused on its call with the text it wrote before it, and the
    checker failed before any text and shows its error, which the document added once the team run ended. The members'
    rows stay as the run left them; the turn's interrupted record takes the team run's place.
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
    assert [run.status for run in (record, *members)] == [RunStatus.completed, RunStatus.error, RunStatus.paused]
    assert record.metadata is not None
    assert record.metadata["mindroom_original_status"] == "error"
    assert (team.recorder.outcome, team.recorder.original_status) == ("interrupted", RunStatus.error)
    assert team.recorder.assistant_text == "**Writer**: Let me touch it.\n\n**Checker**: Checker overloaded"


def _team_bot(team: TeamTurn, entity: str = "squad") -> AgentBot:
    """Build the squad's team bot on the team's config, with a mocked Matrix client and inert post-response effects.

    A completed reply would otherwise queue a thread summary on the config's real ``default`` model. An agent
    ``entity`` builds that agent's bot instead, which runs a team request as an ad hoc team.
    """
    bot = make_test_agent_bot(
        AgentMatrixUser(
            agent_name=entity,
            password=TEST_PASSWORD,
            display_name=entity.title(),
            user_id=f"@mindroom_{entity}:localhost",
        ),
        team.config,
        team.runtime_paths,
        rooms=[ROOM_ID],
    )
    install_direct_response_admission(bot)
    bot.client = make_matrix_client_mock(user_id=f"@mindroom_{entity}:localhost")
    unique_room_send_responses(bot.client)
    install_runtime_journal_support(bot)
    _install_inert_post_response_effects(bot._response_runner)
    return bot


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


# 2026-03-20 15:00 UTC, which is 08:00 in Los Angeles.
_OLDER_MESSAGE_MS = 1_774_018_800_000


def _older_thread_history(bot: AgentBot) -> AbstractContextManager[object]:
    """Serve the thread as one older message from the requester, which the turn reads once it owns the thread."""
    history = [make_visible_message(sender=REQUESTER_ID, body="older text", timestamp=_OLDER_MESSAGE_MS)]
    return patch.object(
        bot._conversation_resolver,
        "fetch_thread_history",
        new=AsyncMock(return_value=ThreadHistoryResult(history, is_full_history=True)),
    )


def _leader_request_text(team: TeamTurn, model: str) -> tuple[str, str]:
    """Return the leader's first request on ``model`` as the text of its system messages and of every other message."""
    messages = team.models[model].requests[0].messages
    system = [message for message in messages if message.role == "system"]
    rest = [message for message in messages if message.role != "system"]
    return "\n".join(str(message.content) for message in system), "\n".join(str(message.content) for message in rest)


@pytest.mark.asyncio
@pytest.mark.parametrize("coalesced", [True, False], ids=["coalesced", "single"])
async def test_an_ad_hoc_team_turn_is_dated_in_the_zone_of_the_agent_whose_bot_runs_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    coalesced: bool,
) -> None:
    """An ad hoc team turn on the writer's bot uses the writer's zone for every time the leader sees.

    The message tags of a coalesced batch or of a single message, the thread history, and the leader's date line all
    read Los Angeles time, though the global timezone is UTC. The leader of a team the writer's bot runs uses the
    writer's model.
    """
    team = squad_turn(tmp_path, monkeypatch, leader=[], writer=[answer("Done.")])
    team.config.agents["writer"].timezone = "America/Los_Angeles"
    bot = _team_bot(team, "writer")
    room = nio.MatrixRoom(ROOM_ID, "@mindroom_writer:localhost")
    messages = [
        ("$first:localhost", "first", _OLDER_MESSAGE_MS + 900_000),
        (EVENT_ID, "second", _OLDER_MESSAGE_MS + 960_000),
    ]
    batch = build_prepared_turn(
        CoalescingKey(ROOM_ID, THREAD_ID, RequesterCoalescingOwner(REQUESTER_ID)),
        [
            make_pending_event(
                nio.RoomMessageText.from_dict(
                    {
                        "content": {"body": body, "msgtype": "m.text"},
                        "event_id": event_id,
                        "sender": REQUESTER_ID,
                        "origin_server_ts": timestamp_ms,
                        "room_id": ROOM_ID,
                        "type": "m.room.message",
                    },
                ),
                room,
                source_kind=MESSAGE_SOURCE_KIND,
                requester_user_id=REQUESTER_ID,
            )
            for event_id, body, timestamp_ms in (messages if coalesced else messages[1:])
        ],
        timestamp_formatter=bot._coalescing_gate._timestamp_formatter,
        queues_other_run=nothing_else_queued,
    )
    assert batch.current_prompt_is_structured is coalesced
    request = _request(batch.event.body)
    request = replace(
        request,
        response_envelope=replace(request.response_envelope, agent_name="writer"),
        current_prompt_is_structured=batch.current_prompt_is_structured,
        current_timestamp_ms=_OLDER_MESSAGE_MS + 960_000,
    )

    with _older_thread_history(bot), patch_response_runner_module(should_use_streaming=AsyncMock(return_value=False)):
        await bot._response_runner.generate_team_response_helper(
            request,
            member_names=team.member_names,
            team_mode="coordinate",
        )

    system, turn = _leader_request_text(team, "writer-model")
    assert "Timezone: America/Los_Angeles (" in system
    assert ('ts="2026-03-20 08:15 PDT"' in turn) is coalesced
    assert 'ts="2026-03-20 08:16 PDT"' in turn
    assert "[2026-03-20 08:00 PDT] older text" in turn
    assert "UTC" not in turn


@pytest.mark.asyncio
async def test_a_configured_team_turn_is_dated_in_the_global_zone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured team has no zone of its own, so its turn uses the global one even when a member has another."""
    team = squad_turn(tmp_path, monkeypatch, leader=[answer("Done.")])
    team.config.agents["writer"].timezone = "America/Los_Angeles"
    request = replace(_request("What changed?"), current_timestamp_ms=_OLDER_MESSAGE_MS + 960_000)
    bot = _team_bot(team)

    with _older_thread_history(bot), patch_response_runner_module(should_use_streaming=AsyncMock(return_value=False)):
        await bot._response_runner.generate_team_response_helper(
            request,
            member_names=team.member_names,
            team_mode="coordinate",
        )

    system, turn = _leader_request_text(team, "default")
    assert "Timezone: UTC (UTC)" in system
    assert 'ts="2026-03-20 15:16 UTC"' in turn
    assert "[2026-03-20 15:00 UTC] older text" in turn
    assert "PDT" not in turn


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
async def test_the_driver_writes_the_interrupted_record_after_the_loop_stored_the_cancelled_team_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    """Through ``ResponseRunner``, a stop mid-reply stores the ``CANCELLED`` team row before the interrupted record.

    The record the turn driver writes replaces the cancelled row, and no later save of the run overwrites it.
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
    runner = _team_bot(team)._response_runner
    statuses_when_interrupted_record_written: list[RunStatus | None] = []
    original = response_turn.persist_interrupted_replay_snapshot

    def spy(**kwargs: Any) -> None:  # noqa: ANN401
        stored = team.storage.get_run(kwargs["run_id"])
        statuses_when_interrupted_record_written.append(stored.status if stored is not None else None)
        original(**kwargs)

    monkeypatch.setattr(response_turn, "persist_interrupted_replay_snapshot", spy)

    with patch_response_runner_module(should_use_streaming=AsyncMock(return_value=streaming)):
        task = asyncio.create_task(
            runner.generate_team_response_helper(_request(), member_names=team.member_names, team_mode="coordinate"),
        )
        await asyncio.wait_for(working.wait(), timeout=5)
        # A Stop cancels the reply span's attempt task, as the stop button does.
        spans = runner.deps.replies.spans
        assert [spans.cancel(span_id, cancel_source="user_stop") for span_id in spans.live_span_ids()] == [True]
        await asyncio.wait_for(task, timeout=5)

    assert statuses_when_interrupted_record_written == [RunStatus.cancelled]
    session = team.storage.load_session(SESSION_ID)
    assert session is not None
    [record] = [run for run in session.runs if isinstance(run, TeamRunRecord)]
    assert record.metadata is not None
    assert record.metadata["mindroom_original_status"] == "cancelled"
    [member] = [run for run in session.runs if isinstance(run, RunRecord)]
    assert member.status is RunStatus.cancelled
    [row] = _usage_rows(team.storage, member.run_id)
    assert row["metrics"]["input_tokens"] == 30


def _leader_writing_then_waiting(working: asyncio.Event) -> list[list[ScriptStep]]:
    """Return a leader turn that writes a little, sets ``working``, and then waits to be stopped."""

    async def wait_to_be_cancelled() -> None:
        working.set()
        await asyncio.Event().wait()

    return [[TextDelta("Dra"), Await(wait_to_be_cancelled)]]


@pytest.mark.asyncio
async def test_a_stopped_team_turn_whose_record_cannot_be_stored_still_ends_as_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store that refuses the interrupted record is logged; the stopped turn still ends with its cancellation."""
    working = asyncio.Event()
    team = squad_turn(tmp_path, monkeypatch, leader=_leader_writing_then_waiting(working))

    def refuse(**_kwargs: Any) -> None:  # noqa: ANN401
        msg = "database is locked"
        raise RuntimeError(msg)

    monkeypatch.setattr(response_turn, "persist_interrupted_replay_snapshot", refuse)
    task = asyncio.create_task(team.stream())
    await asyncio.wait_for(working.wait(), timeout=5)
    with capture_logs() as logs:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert team.recorder.outcome == "interrupted"
    assert "Failed to persist interrupted replay state" in [log["event"] for log in logs if log["log_level"] == "error"]


@pytest.mark.asyncio
async def test_a_stopped_team_turn_cancelled_again_while_its_record_is_stored_ends_once_the_record_lands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second cancellation while the record is stored off the event loop ends the turn once the store finishes."""
    working = asyncio.Event()
    team = squad_turn(tmp_path, monkeypatch, leader=_leader_writing_then_waiting(working))
    storing = threading.Event()
    release = threading.Event()
    original = response_turn.persist_interrupted_replay_snapshot

    def slow_store(**kwargs: Any) -> None:  # noqa: ANN401
        storing.set()
        assert release.wait(timeout=5)
        original(**kwargs)

    monkeypatch.setattr(response_turn, "persist_interrupted_replay_snapshot", slow_store)
    task = asyncio.create_task(team.stream())
    await asyncio.wait_for(working.wait(), timeout=5)
    task.cancel()
    assert await asyncio.to_thread(storing.wait, 5)
    task.cancel()
    done, _pending = await asyncio.wait({task}, timeout=0.2)
    assert not done

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    record, _members = team.stored_runs()
    assert record.metadata is not None
    assert record.metadata["mindroom_original_status"] == "cancelled"


@asynccontextmanager
async def _approving_team_bot(
    team: TeamTurn,
    *,
    streaming: bool = True,
) -> AsyncIterator[tuple[AgentBot, list[MatrixDelivery]]]:
    """Yield the squad's team bot with its approval cards published into the yielded list, the n-th as ``$card-n``."""
    bot = _team_bot(team)
    cards: list[MatrixDelivery] = []

    async def prepare_event(_room_id: str, _thread_id: str | None, content: dict[str, Any]) -> dict[str, Any]:
        return content

    async def send_card(delivery: MatrixDelivery) -> str:
        if delivery.edits_event_id is not None:
            # A decided card's resolution edit.
            return f"$edit-{delivery.edits_event_id}"
        cards.append(delivery)
        return f"$card-{len(cards)}"

    initialize_approval_store_for_test(
        bot.runtime_paths,
        prepare_approval_event=prepare_event,
        send_approval_delivery=send_card,
        resolve_approval_delivery=AsyncMock(return_value=None),
        cards=bot._journal_store.principal("router@shared"),
        transport_sender_id=lambda: "@router:localhost",
        transport_device_id=lambda: "DEVICE",
    )
    try:
        with patch_response_runner_module(should_use_streaming=AsyncMock(return_value=streaming)):
            yield bot, cards
    finally:
        await shutdown_approval_runtime()


def _card_calls(cards: list[MatrixDelivery]) -> list[tuple[object, object]]:
    return [(card.payload["tool_name"], card.payload["tool_call_id"]) for card in cards]


async def _approve(bot: AgentBot, card_event_id: str) -> None:
    """Approve one card as the requester does, then hand the request's source to the readied resume, as the journal does."""
    manager = get_approval_store()
    assert manager is not None
    result = await handle_card_response_for_test(
        manager,
        room_id=ROOM_ID,
        sender_id=REQUESTER_ID,
        card_event_id=card_event_id,
        status="approved",
        authorize_responder=lambda _entity: True,
    )
    assert result.consumed
    runner = bot._response_runner
    await runner.handoff_approval_source(EVENT_ID)
    await runner.inbox.wait_for_source_owned_inbox_responses()


def _shown(bot: AgentBot) -> str:
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
    async with _approving_team_bot(team, streaming=streaming) as (bot, cards):
        await bot._response_runner.generate_team_response_helper(
            _request(),
            member_names=team.member_names,
            team_mode="coordinate",
        )

        assert _card_calls(cards) == [("run_subagent", "writer:call-sub")]
        assert _shown(bot) == (
            f"{HEADER}**Writer**:\n\n🔧 `run_subagent` [1] ⏳\n\n\n**Team Consensus**:\n\n🔧 `delegate_task_to_member` [2]"
        )
        continuation = await bot.journal_principal().approvals.approval_continuation_for_source(EVENT_ID)
        assert continuation is not None
        assert (continuation.state, continuation.show_tool_calls) == ("waiting", True)

        await _approve(bot, "$card-1")

    assert _shown(bot) == (
        f"{HEADER}**Writer**:\n\n🔧 `run_subagent` [1]\n\nWriter: dug.\n\n\n"
        "**Team Consensus**:\n\n🔧 `delegate_task_to_member` [2]\n\nConsensus."
    )
    assert await bot.journal_principal().approvals.approval_continuation_for_source(EVENT_ID) is None
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
    async with _approving_team_bot(team) as (bot, cards):
        await bot._response_runner.generate_team_response_helper(
            _request(),
            member_names=team.member_names,
            team_mode="coordinate",
        )
        await _approve(bot, "$card-1")

        assert _card_calls(cards) == [("run_subagent", "writer:call-sub"), ("run_subagent", "writer:call-sub-2")]
        assert _shown(bot) == (
            f"{HEADER}**Writer**:\n\n🔧 `run_subagent` [1]\n\n\n\n🔧 `run_subagent` [2] ⏳\n\n\n"
            "**Team Consensus**:\n\n🔧 `delegate_task_to_member` [3]"
        )
        continuation = await bot.journal_principal().approvals.approval_continuation_for_source(EVENT_ID)
        assert continuation is not None
        assert continuation.state == "waiting"

        await _approve(bot, "$card-2")

    assert _shown(bot) == (
        f"{HEADER}**Writer**:\n\n🔧 `run_subagent` [1]\n\n\n\n🔧 `run_subagent` [2]\n\nWriter: dug twice.\n\n\n"
        "**Team Consensus**:\n\n🔧 `delegate_task_to_member` [3]\n\nConsensus."
    )
    assert await bot.journal_principal().approvals.approval_continuation_for_source(EVENT_ID) is None
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
    async with _approving_team_bot(team) as (bot, cards):
        await bot._response_runner.generate_team_response_helper(
            _request(),
            member_names=team.member_names,
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
    assert await bot.journal_principal().approvals.approval_continuation_for_source(EVENT_ID) is None
    assert len(team.models["checker-model"].requests) == 1
    *_, helper_result = [m for m in team.models["writer-model"].requests[-1].messages if m.role == "tool"]
    assert "Helper: dug deeper." in str(helper_result.content)
    record, member, state = _delegation_state(team)
    assert (record.status, member.status) == (RunStatus.completed, RunStatus.completed)
    assert [child.status for child in state.children] == ["completed"]
