"""What a requester and the leader's model see of a subagent that runs as a background job.

A leader reply delegates to ``code`` with background tool jobs on, and each test reads only what reaches a person or a
model: the call's answer, the job a handle or listing names, its approval cards, and how it ends.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any

import pytest
from structlog.testing import capture_logs

from mindroom.agent_loop import run as agent_loop_run
from mindroom.approval import manager as approval_manager
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.delegation import execution as delegation_execution
from mindroom.delegation.background import delegation_child
from mindroom.delegation.recovery import read_child_run
from mindroom.delegation.sessions import SUBAGENT_BUSY
from mindroom.delegation.state import RESTART_INTERRUPTION_REASON
from mindroom.event_journal import ApprovalDecisionMetadata, BackgroundApprovalDecision, CallKey, tool_job_run_id
from mindroom.event_journal.replies import ReplyStopped
from mindroom.history.run_checkpoint import INTERRUPTED_CALL_RESULT
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.orchestration import tool_job_runtime as tool_job_coordinator
from mindroom.prompts import BACKGROUND_JOBS_CONTINUED_NOTICE, SUBAGENT_RESTART_CONTINUATION_PROMPT
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import pending_approval_requirements
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_jobs.client_cancel import cancel_stopped_reply_jobs
from mindroom.tool_jobs.control import current_job_id
from mindroom.tool_jobs.conversation import ConversationKey
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.notices import JobNotices, append_turn_start_job_note, job_notice_context
from mindroom.tool_jobs.runtime import _REPLY_STOPPED_REASON, describe_job, register_background_runtime
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.tool_function import POLICY_CONFIRMATION_APPROVAL_TYPE
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.access_schema_support import with_responder_access
from tests.conftest import FakeApprovalTransport, approval_manager_for_test
from tests.delegation_helpers import ScriptedAgents, _delegate_runtime_context, _runtime_paths, registered_report_tool
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call
from tests.reply_span_helpers import reply_span, seed_finished_reply
from tests.scripted_agent_helpers import PROBE_TOOL, registered_probe_tools
from tests.tool_job_helpers import (
    DELEGATING_REQUESTER,
    JOB_BOT_PRINCIPAL,
    JOB_TEST_TIMEOUT,
    DelegatingLeader,
    JobApprovalCards,
    answer_of,
    continue_stopped_jobs,
    delegating_config,
    intercept_job_saves,
    lookup,
    saved_job_card_decision,
    saved_jobs,
    settle_stopped_subagent,
    stopped_job_coordinator,
    tool_job_runtime,
    wait_for_status,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.approval.tool_approval import BackgroundScriptToolOrigin
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.reply_scope import SpanHandle
    from mindroom.tool_jobs.runtime import BackgroundJob
    from tests.tool_job_helpers import ProcessRuntime

_IDENTITY = ToolExecutionIdentity("matrix", "leader", DELEGATING_REQUESTER, "!room:example.org", None, None, "parent")
_HANDLE_KEYS = {"job_id", "subagent_id", "status", "tool", "summary", "summary_truncated"}


def _config() -> Config:
    """The leader delegates to ``code``, whose ``write_report`` asks for approval, with background tool jobs on."""
    return delegating_config(
        child_tools=["report"],
        rules=[ApprovalRuleConfig(match="write_report", action="require_approval")],
    )


@dataclass
class _Conversation:
    """One requester's conversation with the leader, over one process's job runtime."""

    tmp_path: Path
    monkeypatch: pytest.MonkeyPatch
    config: Config = field(default_factory=_config)
    allowed: bool = True
    crashed: list[ProcessRuntime] = field(default_factory=list)
    runtime: ProcessRuntime = field(init=False)
    paths: RuntimePaths = field(init=False)
    agents: ScriptedAgents = field(init=False)
    turns: int = 0

    async def open(self, *code_turns: list) -> None:
        self.paths = _runtime_paths(self.tmp_path)
        pin_background_tool_jobs(self.config, self.paths)
        await self._start_runtime()
        self.agents = ScriptedAgents({"code": ScriptedModel(list(code_turns))})
        self.agents.install(self.monkeypatch)

    async def _start_runtime(self) -> None:
        self.runtime = await tool_job_runtime(
            self.tmp_path,
            grant=lambda _job: "allowed" if self.allowed else "denied",
            cancel=settle_stopped_subagent(self.config, self.paths),
            continues=tool_job_coordinator._is_subagent_job,
            reply_stopped=self._reply_stopped,
        )
        register_background_runtime(self.paths, self.runtime)

    async def _reply_stopped(self, job: BackgroundJob) -> bool:
        """Read a job's reply as the coordinator's runtime does when it recovers."""
        return await stopped_job_coordinator(self.runtime, self.config, self.paths, None)._reply_stopped(job)

    async def restart(self, *, crash: bool = False) -> None:
        """Stop this process, in order or by a crash that runs none of its code, and recover in the next one.

        A crashed process's work stays where it stopped until ``close``; the next process takes its jobs over, and its
        worker's first pass continues or settles the subagent jobs the restart stopped.
        """
        if crash:
            self.crashed.append(self.runtime)
        else:
            await self.runtime.shutdown()
        await self._start_runtime()
        await self.runtime.recover()
        await self.continue_stopped()

    async def continue_stopped(self) -> None:
        """Run the worker's pass over the subagent jobs a restart stopped, in the conversation its bot rebuilds."""
        context = _delegate_runtime_context(self.config, self.paths, execution_identity=_IDENTITY)
        await continue_stopped_jobs(self.runtime, self.config, self.paths, context)

    async def kill(self, *, settle: bool) -> None:
        """End this process's tasks without an orderly shutdown, then recover and continue in the next one.

        With ``settle``, the tasks unwind as an event-loop teardown lets them, which settles each running child; without,
        the process dies before any of that code runs, and each child's turn stays as its last save left it.
        """
        tasks = [entry.task for entry in self.runtime._entries.values() if entry.task is not None]
        with self.monkeypatch.context() as patch:
            if not settle:
                patch.setattr(delegation_execution, "interrupt_stopped_child", _leave_child)
                patch.setattr(agent_loop_run, "_store_interrupted", _leave_run)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.runtime.close_journal()
        await self._start_runtime()
        await self.runtime.recover()
        await self.continue_stopped()

    async def close(self) -> None:
        """Stop the current process and end the work any crashed one left behind."""
        await self.runtime.shutdown()
        for crashed in self.crashed:
            tasks = [entry.task for entry in crashed._entries.values() if entry.task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await crashed.close_journal()

    async def reply(self, *steps: list) -> RunRecord:
        """Run one leader reply whose model takes ``steps``; its subagents run as the leader's real children."""
        self.turns += 1
        context = replace(
            _delegate_runtime_context(self.config, self.paths, execution_identity=_IDENTITY),
            membership_turn_id=f"$turn{self.turns}",
        )
        leader = DelegatingLeader(self.config, self.paths, _IDENTITY, self.agents)
        with tool_runtime_context(context):
            return await asyncio.wait_for(
                leader.reply(*steps, run_id=f"reply{self.turns}"),
                JOB_TEST_TIMEOUT,
            )

    async def job(self, job_id: str) -> BackgroundJob:
        return await lookup(self.runtime, job_id, owner=_IDENTITY, depth=0)


@dataclass
class _Reply:
    """A real reply of the leader's bot, in which the conversation's replies ran, and its Stop."""

    conversation: _Conversation
    span: SpanHandle
    stops: int = 0

    async def stop(self, *, applied: bool = True) -> None:
        """Record a Stop of the reply in its journal; without ``applied``, the process dies before the Stop's effects."""
        self.stops += 1
        principal = self.conversation.runtime.journal.principal(JOB_BOT_PRINCIPAL)
        stopped = await principal.replies.stop(self.span.reply_id, receipt_order=self.stops, now_ns=time.time_ns())
        assert stopped is not None
        assert ReplyStopped(self.span.reply_id) in stopped.post_commit
        if applied:
            await cancel_stopped_reply_jobs(self.conversation.paths, self.span.reply_id)


@asynccontextmanager
async def _replying(conversation: _Conversation, *, regenerates: str | None = None) -> AsyncIterator[_Reply]:
    """Run the block's leader replies in a real reply of its bot, so the jobs they start name it.

    With ``regenerates``, the reply is the finished one shown as that event, which an edit regenerates in place.
    """
    principal = conversation.runtime.journal.principal(JOB_BOT_PRINCIPAL)
    async with reply_span(
        principal,
        source_event_id="$source",
        room_id="!room:example.org",
        placeholder_event_id=None if regenerates else "$reply",
        regenerated_event_id=regenerates,
    ) as span:
        yield _Reply(conversation, span)


async def _leave_child(_child: object, **_kwargs: object) -> None:
    """Stand in for the settlement a process that dies never runs."""


async def _leave_run(*_args: object, **_kwargs: object) -> None:
    """Stand in for the terminal store a process that dies never runs: the run's row stays as its last save."""


def _held_child() -> tuple[asyncio.Event, list]:
    """Return a child turn that starts and then never finishes, with the event its start sets."""
    started = asyncio.Event()

    async def hold() -> None:
        started.set()
        await asyncio.Event().wait()

    return started, [Await(hold), *answer("Never")]


def _delegate(call_id: str, **budget: float) -> list:
    return calls(tool_call("run_subagent", call_id, task="Report", agent_name="code", **budget))


@pytest.mark.asyncio
async def test_a_released_wait_answers_with_a_handle_that_names_the_subagent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handle names the child agent and its reusable subagent ID; a later wait reads the child's answer."""
    release = asyncio.Event()
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open([Await(release.wait), *answer("Exact child result")])
    try:
        record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
        assert record.status is RunStatus.completed
        handle = json.loads(str(answer_of(record, "first").content))
        assert set(handle) == _HANDLE_KEYS
        assert (handle["tool"], handle["status"]) == ("code", "running")
        job = await conversation.job(handle["job_id"])
        assert job.kind == "delegation"
        assert describe_job(job, short=True) == f"`{handle['job_id'][:8]}` (code subagent)"
        release.set()
        await wait_for_status(conversation.runtime, handle["job_id"], "completed")
        read = await conversation.reply(
            calls(tool_call("job", "wait", action="wait", job_id=handle["job_id"])),
            answer("Read"),
        )
        result = str(answer_of(read, "wait").content)
        assert "Exact child result" in result
        assert f"Subagent ID: {handle['subagent_id']}" in result
    finally:
        release.set()
        await conversation.runtime.shutdown()


@pytest.mark.asyncio
async def test_a_waiting_parent_reads_the_childs_answer_and_a_follow_up_continues_the_same_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parent that waits gets the answer as its call's result; ``continue_subagent`` reuses the child's session."""
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(answer("First answer"), answer("Second answer"))
    try:
        first = str(answer_of(await conversation.reply(_delegate("first"), answer("Done")), "first").content)
        assert first.startswith("First answer")
        subagent_id = first.rsplit("Subagent ID: ", 1)[1].strip()
        follow_up = calls(tool_call("continue_subagent", "again", subagent_id=subagent_id, message="And now?"))
        second = str(answer_of(await conversation.reply(follow_up, answer("Done")), "again").content)
        assert second.startswith("Second answer")
        assert second.endswith(f"Subagent ID: {subagent_id}")
        (_, resumed) = conversation.agents.models["code"].requests
        assert [message.content for message in resumed.messages if message.role == "assistant"][-1] == "First answer"
        jobs = conversation.runtime.retained_jobs()
        assert [(job.kind, job.tool_name, job.status) for job in jobs] == [("delegation", "code", "completed")] * 2
    finally:
        await conversation.runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approved", "denied", "cancelled"])
async def test_a_childs_gated_call_asks_as_a_phase_of_its_subagents_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
) -> None:
    """The job posts the card and waits for it; the parent's reply is never paused by the child's approval."""
    conversation = _Conversation(tmp_path, monkeypatch)
    with registered_report_tool() as reports:
        await conversation.open(calls(tool_call("write_report", "write-once")), answer("Child done"))
        cards = JobApprovalCards(asyncio.get_running_loop().create_future(), conversation.runtime.journal)
        monkeypatch.setattr(approval_manager, "get_approval_store", lambda: cards)
        try:
            record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
            assert record.status is RunStatus.completed
            job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
            await asyncio.wait_for(cards.posted.wait(), JOB_TEST_TIMEOUT)
            assert (await conversation.job(job_id)).status == "awaiting_approval"
            assert [run_id for run_id, _call_id, _tool in cards.requested] == [tool_job_run_id(job_id)]
            assert reports.written == []
            if decision == "cancelled":
                await conversation.runtime.request_cancel(job_id)
            else:
                cards.decision.set_result(BackgroundApprovalDecision(decision, "Not now"))
            status = {"approved": "completed", "denied": "completed", "cancelled": "cancelled"}[decision]
            await wait_for_status(conversation.runtime, job_id, status)
            assert reports.written == (["written"] if decision == "approved" else [])
            child_run = await read_child_run(
                delegation_child(await conversation.job(job_id)),
                conversation.config,
                conversation.paths,
            )
            assert child_run is not None
            assert child_run.status is (RunStatus.cancelled if decision == "cancelled" else RunStatus.completed)
        finally:
            if not cards.decision.done():
                cards.decision.cancel()
            await conversation.runtime.shutdown()


_KEY = ConversationKey("leader", "!room:example.org", None, DELEGATING_REQUESTER, silent=False)
# How a process stops: in order, by an event-loop teardown that unwinds its tasks, or dead before any cleanup runs.
_STOPS = pytest.mark.parametrize("stop", ["restart", "teardown", "crash"])


async def _stop(conversation: _Conversation, stop: str) -> None:
    if stop == "restart":
        await conversation.restart()
    else:
        await conversation.kill(settle=stop == "teardown")


def _hang(call_id: str) -> list:
    """A child turn whose shell call keeps running until its turn is stopped."""
    return calls(tool_call("run_shell_command", call_id, cmd="sleep 60"))


def _declined(logs: list) -> list[tuple[str, str]]:
    """Return the job and reason of each log line that says a stopped subagent job ends instead of continuing."""
    event = "Subagent job ends instead of continuing after a restart"
    return [(log["job_id"], log["reason"]) for log in logs if log["event"] == event]


def _sent(request: object) -> list[tuple[str, object, str | None]]:
    return [(m.role, m.content, m.tool_call_id) for m in request.messages if m.role != "system"]  # type: ignore[attr-defined]


@pytest.mark.asyncio
@_STOPS
async def test_a_subagent_a_restart_stopped_continues_from_its_own_calls_and_its_job_gives_its_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop: str,
) -> None:
    """A restart mid-call never ends the job: its child goes on after its own calls, and the job gives the answer."""
    with registered_probe_tools() as probe:
        conversation = _Conversation(tmp_path, monkeypatch, config=delegating_config(child_tools=[PROBE_TOOL]))
        await conversation.open(calls(tool_call("read_file", "read", path="a")), _hang("shell"), answer("Continued"))
        try:
            record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
            handle = json.loads(str(answer_of(record, "first").content))
            await asyncio.wait_for(probe.running.wait(), JOB_TEST_TIMEOUT)
            await _stop(conversation, stop)
            await wait_for_status(conversation.runtime, handle["job_id"], "completed")
            job = await conversation.job(handle["job_id"])
            assert job.result is not None
            assert job.result.startswith("Continued")
            assert job.adapter["child"]["subagent_id"] == handle["subagent_id"]
            assert job.adapter["restart_continuations"] == 1
            _first, cut_short, continued = conversation.agents.models["code"].requests
            # The continuation's request extends the cut-short run's last request unchanged, so a prompt cache holds.
            sent, replayed = _sent(cut_short), _sent(continued)
            assert replayed[: len(sent)] == sent
            assert [(role, call_id) for role, _content, call_id in replayed[len(sent) :]] == [
                ("assistant", None),
                ("tool", "shell"),
                ("user", None),
            ]
            assert replayed[-2][1] == INTERRUPTED_CALL_RESULT
            assert SUBAGENT_RESTART_CONTINUATION_PROMPT in str(replayed[-1][1])
            read = await conversation.reply(
                calls(tool_call("job", "wait", action="wait", job_id=handle["job_id"])),
                answer("Read"),
            )
            result = str(answer_of(read, "wait").content)
            assert result.startswith("Continued")
            assert f"Subagent ID: {handle['subagent_id']}" in result
        finally:
            await conversation.close()


@pytest.mark.asyncio
@_STOPS
@pytest.mark.parametrize("gated", ["run_shell_command", "dangerous"])
async def test_a_subagent_stopped_after_an_approval_resumed_it_continues_from_its_own_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop: str,
    gated: str,
) -> None:
    """A child an approval resumed saves its run as a fresh one does, so a crash keeps its work.

    The crash lands in the shell call: the call the approval let run, or one the child made after its approved call.
    """
    rule = ApprovalRuleConfig(match=gated, action="require_approval")
    config = delegating_config(child_tools=[PROBE_TOOL], rules=[rule])
    before = (
        tool_call("dangerous", "danger", value=1) if gated == "dangerous" else tool_call("read_file", "read", path="a")
    )
    with registered_probe_tools() as probe:
        conversation = _Conversation(tmp_path, monkeypatch, config=config)
        await conversation.open(calls(before), _hang("shell"), answer("Continued"))
        cards = JobApprovalCards(asyncio.get_running_loop().create_future(), conversation.runtime.journal)
        cards.decision.set_result(BackgroundApprovalDecision("approved", None))
        monkeypatch.setattr(approval_manager, "get_approval_store", lambda: cards)
        try:
            record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
            job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
            # The shell call runs after the approval: the job works again, and the pause stored its child's run last.
            await asyncio.wait_for(probe.running.wait(), JOB_TEST_TIMEOUT)
            assert [tool for _run_id, _call_id, tool in cards.requested] == [gated]
            assert (await conversation.job(job_id)).status == "running"
            await _stop(conversation, stop)
            await wait_for_status(conversation.runtime, job_id, "completed")
            job = await conversation.job(job_id)
            assert str(job.result).startswith("Continued")
            assert job.adapter["restart_continuations"] == 1
            _first, cut_short, continued = conversation.agents.models["code"].requests
            sent, replayed = _sent(cut_short), _sent(continued)
            assert replayed[: len(sent)] == sent
            assert [(role, call_id) for role, _content, call_id in replayed[len(sent) :]] == [
                ("assistant", None),
                ("tool", "shell"),
                ("user", None),
            ]
            assert replayed[-2][1] == INTERRUPTED_CALL_RESULT
            assert SUBAGENT_RESTART_CONTINUATION_PROMPT in str(replayed[-1][1])
        finally:
            await conversation.close()


@pytest.mark.asyncio
async def test_a_continued_subagent_is_told_its_calls_when_its_history_limits_leave_them_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """History limits apply to a recovered run as to any turn, so the prompt names the cut-short run's calls itself."""
    config = delegating_config(child_tools=[PROBE_TOOL])
    config.agents["code"].max_tool_calls_from_history = 0
    hooked: list[dict] = []
    before_delegation = delegation_execution.before_delegation

    async def hook(**call: Any) -> object:  # noqa: ANN401
        hooked.append(call["arguments"])
        return await before_delegation(**call)

    monkeypatch.setattr(delegation_execution, "before_delegation", hook)
    with registered_probe_tools() as probe:
        conversation = _Conversation(tmp_path, monkeypatch, config=config)
        await conversation.open(calls(tool_call("read_file", "read", path="a")), _hang("shell"), answer("Continued"))
        try:
            record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
            job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
            await asyncio.wait_for(probe.running.wait(), JOB_TEST_TIMEOUT)
            await conversation.kill(settle=False)
            await wait_for_status(conversation.runtime, job_id, "completed")
            _first, _cut_short, continued = conversation.agents.models["code"].requests
            sent = _sent(continued)
            assert [call_id for role, _content, call_id in sent if role == "tool"] == []
            prompt = str(sent[-1][1])
            assert SUBAGENT_RESTART_CONTINUATION_PROMPT in prompt
            assert "The `read_file` tool finished with input preview" in prompt
            assert "The `run_shell_command` tool was still running with input preview" in prompt
            assert "check before repeating it" in prompt
            # The account is the child's input only: the turn's label, which the room shows, stays its task, and the
            # job keeps the prompt in its adapter, which no room or model reads.
            assert "contents of a" in prompt
            job = await conversation.job(job_id)
            assert delegation_child(job).task == hooked[-1]["message"] == "Report"
            assert job.adapter["continuation_prompt"] in prompt
            assert "contents of a" not in json.dumps(asdict(replace(job, adapter={"child": job.adapter["child"]})))
        finally:
            await conversation.close()


@pytest.mark.asyncio
async def test_a_continued_subagent_is_told_its_task_when_its_history_keeps_one_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A history limit can leave the cut-short turn's task out, so each continuation's prompt states that task."""
    config = delegating_config(child_tools=[PROBE_TOOL])
    config.agents["code"].num_history_messages = 1
    with registered_probe_tools() as probe:
        conversation = _Conversation(tmp_path, monkeypatch, config=config)
        await conversation.open(
            calls(tool_call("read_file", "read", path="a")),
            _hang("one"),
            _hang("two"),
            answer("Continued"),
        )
        try:
            record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
            job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
            for _restart in range(2):
                await asyncio.wait_for(probe.running.wait(), JOB_TEST_TIMEOUT)
                probe.running.clear()
                await conversation.kill(settle=False)
            await wait_for_status(conversation.runtime, job_id, "completed")
            requests = conversation.agents.models["code"].requests
            for continued in (requests[2], requests[3]):
                prompt = str(_sent(continued)[-1][1])
                assert SUBAGENT_RESTART_CONTINUATION_PROMPT in prompt
                assert "<interrupted_task>\nReport\n</interrupted_task>" in prompt
            assert delegation_child(await conversation.job(job_id)).task == "Report"
        finally:
            await conversation.close()


@pytest.mark.asyncio
async def test_a_continuation_stopped_before_its_first_call_is_sent_the_same_prompt_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continuation that called no tool left nothing in history, so the next one is sent what it was sent.

    The prompt was built once, from the run the first restart cut short, and saved with the job: the account of that
    run's calls is still in it, which a history limit may leave out of the child's history.
    """
    config = delegating_config(child_tools=[PROBE_TOOL])
    config.agents["code"].num_history_messages = 1
    started, held = _held_child()
    with registered_probe_tools() as probe:
        conversation = _Conversation(tmp_path, monkeypatch, config=config)
        await conversation.open(
            calls(tool_call("read_file", "read", path="a")),
            _hang("one"),
            held,
            answer("Continued"),
        )
        try:
            record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
            job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
            await asyncio.wait_for(probe.running.wait(), JOB_TEST_TIMEOUT)
            await conversation.kill(settle=False)
            await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
            await conversation.kill(settle=False)
            await wait_for_status(conversation.runtime, job_id, "completed")
            *_cut_short, first, second = conversation.agents.models["code"].requests
            prompt = str(_sent(second)[-1][1])
            assert prompt == str(_sent(first)[-1][1])
            assert (
                f"{SUBAGENT_RESTART_CONTINUATION_PROMPT}\n\n<interrupted_task>\nReport\n</interrupted_task>" in prompt
            )
            assert "The `read_file` tool finished with input preview" in prompt
            assert "The `run_shell_command` tool was still running with input preview" in prompt
            job = await conversation.job(job_id)
            assert delegation_child(job).task == "Report"
            assert job.adapter["restart_continuations"] == 2
        finally:
            await conversation.close()


@pytest.mark.asyncio
async def test_a_continued_subagents_own_subagent_is_sent_its_own_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The continuation prompt starts only the continued turn: a subagent that turn starts later is sent its task."""
    rule = ApprovalRuleConfig(match="dangerous", action="require_approval")
    config = delegating_config(child_tools=[PROBE_TOOL], rules=[rule])
    config.agents["code"].delegate_to = ["helper"]
    config.agents["helper"] = AgentConfig(display_name="Helper")
    with_responder_access(config, "helper", users=[DELEGATING_REQUESTER])
    with registered_probe_tools() as probe:
        conversation = _Conversation(tmp_path, monkeypatch, config=config)
        await conversation.open(
            calls(tool_call("read_file", "read", path="a")),
            _hang("shell"),
            calls(tool_call("dangerous", "danger", value=1)),
            # The approval resumes the continued turn, which then starts a subagent of its own.
            calls(tool_call("run_subagent", "nested", task="Give a weather summary.", agent_name="helper")),
            answer("Continued"),
        )
        helper = conversation.agents.models["helper"] = ScriptedModel([answer("Sunny")])
        # The card is asked for after the restart, over no journal: the first process's is closed by then.
        cards = JobApprovalCards(asyncio.get_running_loop().create_future())
        cards.decision.set_result(BackgroundApprovalDecision("approved", None))
        monkeypatch.setattr(approval_manager, "get_approval_store", lambda: cards)
        try:
            record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
            job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
            await asyncio.wait_for(probe.running.wait(), JOB_TEST_TIMEOUT)
            await conversation.restart()
            await wait_for_status(conversation.runtime, job_id, "completed")
            assert "contents of a" in str(_sent(conversation.agents.models["code"].requests[2])[-1][1])
            [request] = helper.requests
            [sent] = [str(content) for role, content, _call_id in _sent(request) if role == "user"]
            assert "Give a weather summary." in sent
            assert SUBAGENT_RESTART_CONTINUATION_PROMPT not in sent
            assert "contents of a" not in sent
        finally:
            await conversation.close()


@pytest.mark.asyncio
async def test_a_continued_subagents_call_waits_for_its_own_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An approval is of one child turn's call, so a continued turn that reuses the call's ID asks again.

    The job keeps its ID over the continuation, so its cards name the child turn as well as the call.
    """
    rule = ApprovalRuleConfig(match="dangerous", action="require_approval")
    config = delegating_config(child_tools=[PROBE_TOOL], rules=[rule])
    asked: list[CallKey] = []
    did: list[int] = []
    asked_again = asyncio.Event()
    request_approval = approval_manager.ApprovalManager.request_background_approval

    async def request(self: object, *, origin: BackgroundScriptToolOrigin, **kwargs: object) -> object:
        asked.append(CallKey.detached(origin.run_id, origin.call_id))
        return await request_approval(self, origin=origin, **kwargs)

    async def decide(manager: approval_manager.ApprovalManager, status: str) -> None:
        metadata = ApprovalDecisionMetadata(resolved_by=DELEGATING_REQUESTER)
        await manager.cards.approvals.resolve_approval_call(
            asked[-1],
            requested_status=status,
            reason=None,
            metadata=metadata,
        )

    async def deliver(manager: approval_manager.ApprovalManager, _delivery_ids: object) -> None:
        # The requester approves the first card only.
        if len(asked) == 1:
            await decide(manager, "approved")
        else:
            asked_again.set()

    async def prepare(_room_id: str, _thread_id: str | None, payload: object) -> object:
        return payload

    with registered_probe_tools() as probe:
        ran = probe.dangerous

        async def dangerous(self: object, value: int) -> str:
            did.append(value)
            return await ran(self, value)

        dangerous.__doc__ = ran.__doc__
        monkeypatch.setattr(probe, "dangerous", dangerous)
        conversation = _Conversation(tmp_path, monkeypatch, config=config)
        await conversation.open(
            calls(tool_call("dangerous", "reused", value=1)),
            _hang("shell"),
            calls(tool_call("dangerous", "reused", value=2)),
            answer("Continued"),
        )

        def store() -> approval_manager.ApprovalManager:
            # Over the current process's journal, which a crash replaces.
            cards = conversation.runtime.journal.principal("router@test")
            return approval_manager_for_test(conversation.paths, cards=cards, prepare_approval_event=prepare)

        monkeypatch.setattr(approval_manager.ApprovalManager, "request_background_approval", request)
        monkeypatch.setattr(approval_manager.ApprovalManager, "_publish_reserved_cards", deliver)
        monkeypatch.setattr(approval_manager, "get_approval_store", store)
        try:
            reply = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
            job_id = json.loads(str(answer_of(reply, "first").content))["job_id"]
            await asyncio.wait_for(probe.running.wait(), JOB_TEST_TIMEOUT)
            await conversation.kill(settle=False)
            await asyncio.wait_for(asked_again.wait(), JOB_TEST_TIMEOUT)
            assert (await conversation.job(job_id)).status == "awaiting_approval"
            first, second = asked
            assert first != second
            assert did == [1]
            await decide(store(), "denied")
            await asyncio.wait_for(wait_for_status(conversation.runtime, job_id, "completed"), JOB_TEST_TIMEOUT)
            assert did == [1]
        finally:
            await conversation.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["approved", "denied"])
async def test_a_crash_in_a_resumed_batch_keeps_what_its_earlier_calls_did(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
) -> None:
    """A crash in a later approved call of one batch keeps the earlier call's result, or its denial, as recorded.

    Only the call that was running is answered as interrupted, so the child neither repeats what took effect nor
    takes a call its requester refused for one that may have run.
    """
    rules = [ApprovalRuleConfig(match=name, action="require_approval") for name in ("dangerous", "run_shell_command")]
    config = delegating_config(child_tools=[PROBE_TOOL], rules=rules)
    batch = calls(tool_call("dangerous", "first-call", value=1), tool_call("run_shell_command", "second-call", cmd="x"))

    class Cards:
        transport = FakeApprovalTransport()

        async def request_background_approval(self, *, tool_name: str, **_kwargs: object) -> BackgroundApprovalDecision:
            if tool_name == "dangerous" and first == "denied":
                return BackgroundApprovalDecision("denied", "Requester said do not run.")
            return BackgroundApprovalDecision("approved", None)

    with registered_probe_tools() as probe:
        conversation = _Conversation(tmp_path, monkeypatch, config=config)
        await conversation.open(batch, answer("Continued"))
        monkeypatch.setattr(approval_manager, "get_approval_store", Cards)
        try:
            record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
            job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
            await asyncio.wait_for(probe.running.wait(), JOB_TEST_TIMEOUT)
            await conversation.kill(settle=False)
            await wait_for_status(conversation.runtime, job_id, "completed")
            _cut_short, continued = conversation.agents.models["code"].requests
            answers = {call_id: content for role, content, call_id in _sent(continued) if role == "tool"}
            assert answers == {
                "first-call": "did 1" if first == "approved" else "Requester said do not run.",
                "second-call": INTERRUPTED_CALL_RESULT,
            }
        finally:
            await conversation.close()


@pytest.mark.asyncio
@_STOPS
async def test_a_subagent_stopped_before_its_first_call_is_sent_its_task_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop: str,
) -> None:
    """A turn that called no tool left nothing in the child's history, so its continuation starts from its task."""
    started, held = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(held, answer("Fresh answer"))
    try:
        record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
        job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
        await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
        await _stop(conversation, stop)
        await wait_for_status(conversation.runtime, job_id, "completed")
        assert str((await conversation.job(job_id)).result).startswith("Fresh answer")
        _first, again = conversation.agents.models["code"].requests
        [(role, prompt, _call_id)] = _sent(again)
        assert role == "user"
        assert "Report" in str(prompt)
        assert SUBAGENT_RESTART_CONTINUATION_PROMPT not in str(prompt)
    finally:
        await conversation.close()


@pytest.mark.asyncio
async def test_a_continued_subagent_writes_its_answer_where_its_call_asked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The output file a subagent call named is saved with its job, so its continuation's answer lands there."""
    started, held = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(held, answer("Continued report"))
    delegate = tool_call(
        "run_subagent",
        "first",
        task="Report",
        agent_name="code",
        background_after=0,
        mindroom_output_path="report.txt",
    )
    try:
        record = await conversation.reply(calls(delegate), answer("Parent free"))
        job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
        await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
        await conversation.restart()
        await wait_for_status(conversation.runtime, job_id, "completed")
        receipt = json.loads(str((await conversation.job(job_id)).result))["mindroom_tool_output"]
        assert receipt["status"] == "saved_to_file"
        workspace = resolve_agent_runtime("leader", conversation.config, conversation.paths, _IDENTITY).workspace.root
        assert "Continued report" in (workspace / receipt["path"]).read_text()
    finally:
        await conversation.close()


async def _only_job(conversation: _Conversation) -> BackgroundJob:
    [job] = conversation.runtime.retained_jobs()
    return job


async def _working_job_of(conversation: _Conversation, reply: _Reply, started: asyncio.Event) -> str:
    """Start a subagent job from ``reply`` that works on after the reply's call returned; return the job's ID."""
    record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
    job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
    await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
    assert (await conversation.job(job_id)).reply_id == reply.span.reply_id
    return job_id


@pytest.mark.asyncio
@pytest.mark.parametrize("applied", [True, False])
@pytest.mark.parametrize("stop", ["restart", "crash"])
async def test_a_stopped_replys_subagent_is_not_continued_after_a_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    applied: bool,
    stop: str,
) -> None:
    """A Stop of the reply that started a working subagent keeps it working, and no restart continues it.

    The reply's record decides: it holds the Stop even when the process died before it applied the Stop to the job.
    The job's result says its user stopped it, whether the shutdown or the next start's recovery ends it, so the
    parent that reads it does not continue the child itself.
    """
    started, held = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(held, answer("Never"))
    try:
        async with _replying(conversation) as reply:
            job_id = await _working_job_of(conversation, reply, started)
            await reply.stop(applied=applied)
            assert (await conversation.job(job_id)).status == "running"
        await _stop(conversation, stop)
        job = await conversation.job(job_id)
        assert (job.status, job.result) == ("interrupted", _REPLY_STOPPED_REASON)
        assert len(conversation.agents.models["code"].requests) == 1
    finally:
        await conversation.close()


@pytest.mark.asyncio
async def test_a_stop_that_arrives_while_the_process_shuts_down_ends_the_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Stop the runtime receives after its shutdown began still wins over keeping the job for the next start."""
    started, held = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(held, answer("Never"))
    try:
        async with _replying(conversation) as reply:
            job_id = await _working_job_of(conversation, reply, started)
            runtime = conversation.runtime
            quiescing = asyncio.ensure_future(runtime.quiesce())
            await asyncio.sleep(0)
            assert runtime._shutdown_task is not None
            await runtime.cancel_stopped_reply(reply.span.reply_id)
            await quiescing
        saved = (await saved_jobs(tmp_path))[job_id]
        assert (saved.status, saved.result) == ("interrupted", _REPLY_STOPPED_REASON)
    finally:
        await conversation.close()


@pytest.mark.asyncio
async def test_a_stop_after_a_restart_reaches_the_continued_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job a restart continued still belongs to its reply: a Stop of it ends the wait, or the next continuation."""
    started, held = _held_child()
    continued, held_again = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(held, held_again, answer("Never"))
    try:
        async with _replying(conversation) as reply:
            job_id = await _working_job_of(conversation, reply, started)
        await conversation.restart()
        await asyncio.wait_for(continued.wait(), JOB_TEST_TIMEOUT)
        await reply.stop()
        assert (await conversation.job(job_id)).status == "running"
        assert conversation.runtime._entries[job_id].control.reply_stopped
        await conversation.kill(settle=False)
        job = await conversation.job(job_id)
        assert (job.status, job.result, job.adapter["restart_continuations"]) == (
            "interrupted",
            _REPLY_STOPPED_REASON,
            1,
        )
        assert len(conversation.agents.models["code"].requests) == 2
    finally:
        await conversation.close()


@pytest.mark.asyncio
async def test_a_stop_ends_a_job_that_waits_to_continue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Stop marks a job that a restart left waiting to continue, which then ends when its turn to continue comes."""
    started, held = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(held, answer("Never"))
    try:
        async with _replying(conversation) as reply:
            job_id = await _working_job_of(conversation, reply, started)
        await conversation.runtime.shutdown()
        await conversation._start_runtime()
        await conversation.runtime.recover()
        assert [job.job_id for job in conversation.runtime.awaiting_continuation()] == [job_id]
        await reply.stop()
        await conversation.continue_stopped()
        job = await conversation.job(job_id)
        assert (job.status, job.result) == ("interrupted", _REPLY_STOPPED_REASON)
        assert len(conversation.agents.models["code"].requests) == 1
    finally:
        await conversation.close()


@pytest.mark.asyncio
async def test_a_stop_while_a_continuation_launches_has_the_effect_it_has_on_a_working_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Stop that lands once the launch began leaves the job working, refused its next approval, as before a restart.

    The job carries the Stop in memory and its reply's record keeps it, so the next restart does not continue the job.
    """
    started, held = _held_child()
    continued, held_again = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(held, held_again, answer("Never"))
    saving, saved = asyncio.Event(), asyncio.Event()

    async def hold(row: dict, _payload: object) -> None:
        if not saving.is_set() and row["adapter"].get("restart_continuations") == 1:
            saving.set()
            await saved.wait()

    try:
        async with _replying(conversation) as reply:
            job_id = await _working_job_of(conversation, reply, started)
        await conversation.runtime.shutdown()
        await conversation._start_runtime()
        await conversation.runtime.recover()
        with monkeypatch.context() as patch:
            intercept_job_saves(patch, before=hold)
            launching = asyncio.create_task(conversation.continue_stopped())
            await asyncio.wait_for(saving.wait(), JOB_TEST_TIMEOUT)
            stopping = asyncio.create_task(reply.stop())
            entry = conversation.runtime._entries[job_id]
            while not entry.control.reply_stopped:  # noqa: ASYNC110 - the Stop marks the job without an event.
                await asyncio.sleep(0)
            assert entry.task is None
            saved.set()
            await asyncio.gather(launching, stopping)
        await asyncio.wait_for(continued.wait(), JOB_TEST_TIMEOUT)
        assert (await conversation.job(job_id)).status == "running"
        assert conversation.runtime._entries[job_id].control.reply_stopped
        await conversation.kill(settle=False)
        job = await conversation.job(job_id)
        assert (job.status, job.result, job.adapter["restart_continuations"]) == (
            "interrupted",
            _REPLY_STOPPED_REASON,
            1,
        )
        assert len(conversation.agents.models["code"].requests) == 2
    finally:
        await conversation.close()


@pytest.mark.asyncio
async def test_a_stop_before_the_job_began_does_not_end_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reply stopped and then regenerated keeps its earlier Stop on record, which its later jobs are not judged by."""
    started, held = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(held, answer("Regenerated answer"))
    principal = conversation.runtime.journal.principal(JOB_BOT_PRINCIPAL)
    sources = ResponseSources(pending_event_ids=("$source",), logical_source_event_ids=("$source",))
    finished = await seed_finished_reply(
        principal,
        "$answer",
        sources=sources,
        room_id="!room:example.org",
        thread_id=None,
    )
    assert await principal.replies.stop(finished.reply_id, receipt_order=1, now_ns=time.time_ns()) is not None
    try:
        async with _replying(conversation, regenerates="$answer") as reply:
            assert reply.span.reply_id == finished.reply_id
            job_id = await _working_job_of(conversation, reply, started)
            assert (await conversation.job(job_id)).reply_stop_order == 1
        await conversation.restart()
        await wait_for_status(conversation.runtime, job_id, "completed")
        assert str((await conversation.job(job_id)).result).startswith("Regenerated answer")
    finally:
        await conversation.close()


@pytest.mark.asyncio
@_STOPS
async def test_a_subagent_awaiting_approval_is_not_continued_after_a_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop: str,
) -> None:
    """A restart denies the card a child waited on, so its job ends instead of asking again or going on without it."""
    conversation = _Conversation(tmp_path, monkeypatch)
    with registered_report_tool() as reports:
        await conversation.open(calls(tool_call("write_report", "write-once")), answer("Never"))
        cards = JobApprovalCards(asyncio.get_running_loop().create_future(), conversation.runtime.journal)
        monkeypatch.setattr(approval_manager, "get_approval_store", lambda: cards)
        try:
            record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
            job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
            await asyncio.wait_for(cards.posted.wait(), JOB_TEST_TIMEOUT)
            await _stop(conversation, stop)
            job = await conversation.job(job_id)
            assert (job.status, job.result) == ("interrupted", RESTART_INTERRUPTION_REASON)
            [(run_id, call_id, _tool)] = cards.requested
            assert await saved_job_card_decision(tmp_path, run_id, call_id) is not None
            assert reports.written == []
            assert len(conversation.agents.models["code"].requests) == 1
        finally:
            cards.decision.cancel()
            await conversation.close()


@pytest.mark.asyncio
async def test_a_stopped_replys_subagent_that_awaited_approval_says_its_user_stopped_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash between a Stop's record and its effects leaves a waiting child's job: it ends as its user's Stop does."""
    conversation = _Conversation(tmp_path, monkeypatch)
    with registered_report_tool() as reports:
        await conversation.open(calls(tool_call("write_report", "write-once")), answer("Never"))
        cards = JobApprovalCards(asyncio.get_running_loop().create_future(), conversation.runtime.journal)
        monkeypatch.setattr(approval_manager, "get_approval_store", lambda: cards)
        try:
            async with _replying(conversation) as reply:
                record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
                job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
                await asyncio.wait_for(cards.posted.wait(), JOB_TEST_TIMEOUT)
                await reply.stop(applied=False)
                assert (await conversation.job(job_id)).status == "awaiting_approval"
            await conversation.kill(settle=False)
            job = await conversation.job(job_id)
            assert (job.status, job.result) == ("interrupted", _REPLY_STOPPED_REASON)
            assert reports.written == []
        finally:
            cards.decision.cancel()
            await conversation.close()


@pytest.mark.asyncio
@_STOPS
async def test_a_subagent_waiting_for_a_person_is_not_continued_after_a_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop: str,
) -> None:
    """A child whose call waits for a human decision, as a secret request does, ends however the process stops.

    Unwinding the wait must not leave the job saved as working, which the next start would continue.
    """
    with registered_probe_tools() as probe:
        conversation = _Conversation(tmp_path, monkeypatch, config=delegating_config(child_tools=[PROBE_TOOL]))
        waiting = asyncio.Event()

        async def run_shell_command(_self: object, cmd: str) -> str:
            """Wait for a person.

            Args:
                cmd: The command.

            """
            del cmd
            job_id = current_job_id()
            assert job_id is not None
            async with conversation.runtime.human_decision_wait(job_id):
                waiting.set()
                await asyncio.Event().wait()
            return "never"

        monkeypatch.setattr(probe, "run_shell_command", run_shell_command)
        await conversation.open(_hang("ask"), answer("Never"))
        try:
            record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
            job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
            await asyncio.wait_for(waiting.wait(), JOB_TEST_TIMEOUT)
            assert (await conversation.job(job_id)).status == "awaiting_approval"
            await _stop(conversation, stop)
            job = await conversation.job(job_id)
            assert (job.status, job.result) == ("interrupted", RESTART_INTERRUPTION_REASON)
            assert len(conversation.agents.models["code"].requests) == 1
        finally:
            await conversation.close()


@pytest.mark.asyncio
async def test_a_manual_follow_up_stopped_before_it_reserved_its_turn_is_not_continued_as_the_turn_before_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a continuation the job itself saved may start from the turn before its child's: a follow-up's may not."""
    started, held = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(held, answer("Wrong task ran"))
    reserving = asyncio.Event()

    async def never_reserve(*_args: object, **_kwargs: object) -> None:
        reserving.set()
        await asyncio.Event().wait()

    try:
        async with _replying(conversation) as reply:
            await _working_job_of(conversation, reply, started)
            await reply.stop()
        await conversation.restart()
        with monkeypatch.context() as patched:
            patched.setattr(delegation_execution, "reserve_child_turn", never_reserve)
            follow_up = tool_call(
                "continue_subagent",
                "next",
                subagent_id=(await _only_job(conversation)).adapter["child"]["subagent_id"],
                message="DIFFERENT TASK",
                background_after=0,
            )
            record = await conversation.reply(calls(follow_up), answer("Parent free"))
            # A follow-up's handle is followed by the note that names its subagent.
            job_id = json.JSONDecoder().raw_decode(str(answer_of(record, "next").content))[0]["job_id"]
            await asyncio.wait_for(reserving.wait(), JOB_TEST_TIMEOUT)
            with capture_logs() as logs:
                await conversation.restart()
        job = await conversation.job(job_id)
        assert (job.status, job.adapter["child"]["task"]) == ("interrupted", "DIFFERENT TASK")
        assert _declined(logs) == [(job_id, "its subagent is on another turn than the one the job ran")]
        assert len(conversation.agents.models["code"].requests) == 1
    finally:
        await conversation.close()


@pytest.mark.asyncio
async def test_a_subagent_is_busy_while_its_job_has_yet_to_reserve_the_continuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job that continues holds its subagent before the continuation's turn is on the handle: a follow-up waits."""
    started, held = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(held, answer("Continued"), answer("Manual turn ran"))
    reserve, reserving, release = delegation_execution.reserve_child_turn, asyncio.Event(), asyncio.Event()

    async def reserve_late(*args: object, **kwargs: object) -> None:
        reserving.set()
        await release.wait()
        await reserve(*args, **kwargs)

    try:
        record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
        handle = json.loads(str(answer_of(record, "first").content))
        await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
        monkeypatch.setattr(delegation_execution, "reserve_child_turn", reserve_late)
        await conversation.restart()
        await asyncio.wait_for(reserving.wait(), JOB_TEST_TIMEOUT)
        follow_up = tool_call("continue_subagent", "manual", subagent_id=handle["subagent_id"], message="Manual")
        manual = await conversation.reply(calls(follow_up), answer("Parent done"))
        assert SUBAGENT_BUSY in str(answer_of(manual, "manual").content)
        release.set()
        await wait_for_status(conversation.runtime, handle["job_id"], "completed")
        assert str((await conversation.job(handle["job_id"])).result).startswith("Continued")
    finally:
        release.set()
        await conversation.close()


@pytest.mark.asyncio
async def test_a_subagent_that_waited_for_its_own_subagent_is_not_continued_after_a_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child waiting on a nested subagent holds a paused run history never replays, so its job ends interrupted."""
    config = delegating_config()
    config.agents["code"].delegate_to = ["helper"]
    config.agents["helper"] = AgentConfig(display_name="Helper")
    with_responder_access(config, "helper", users=[DELEGATING_REQUESTER])
    started, held = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch, config=config)
    await conversation.open(
        calls(tool_call("run_subagent", "nested", task="Help", agent_name="helper")),
        answer("Never"),
    )
    conversation.agents.models["helper"] = ScriptedModel([held, answer("Never")])
    try:
        record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
        job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
        await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
        with capture_logs() as logs:
            await conversation.restart()
        job = await conversation.job(job_id)
        assert (job.status, job.result) == ("interrupted", RESTART_INTERRUPTION_REASON)
        assert [len(conversation.agents.models[name].requests) for name in ("code", "helper")] == [1, 1]
        [(declined, reason)] = _declined(logs)
        assert declined == job_id
        assert "a run paused for a decision or a subagent" in reason
    finally:
        await conversation.close()


@pytest.mark.asyncio
async def test_a_subagent_that_restarts_keep_cutting_short_stops_continuing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third restart of one job ends it interrupted, in case continuing it is what stops the process."""
    with registered_probe_tools() as probe:
        conversation = _Conversation(tmp_path, monkeypatch, config=delegating_config(child_tools=[PROBE_TOOL]))
        await conversation.open(_hang("one"), _hang("two"), _hang("three"), answer("Never"))
        try:
            record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
            job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
            for continuations in (1, 2):
                await asyncio.wait_for(probe.running.wait(), JOB_TEST_TIMEOUT)
                probe.running.clear()
                await conversation.restart()
                job = await conversation.job(job_id)
                assert (job.status, job.adapter["restart_continuations"]) == ("running", continuations)
            await asyncio.wait_for(probe.running.wait(), JOB_TEST_TIMEOUT)
            with capture_logs() as logs:
                await conversation.restart()
            job = await conversation.job(job_id)
            assert (job.status, job.result) == ("interrupted", RESTART_INTERRUPTION_REASON)
            assert _declined(logs) == [(job_id, "restarts already cut it short 3 times")]
            assert len(conversation.agents.models["code"].requests) == 3
        finally:
            await conversation.close()


@pytest.mark.asyncio
async def test_a_restart_before_the_continuation_reserved_its_turn_continues_the_job_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second restart finds a saved continuation that never ran and prepares another from the same settled turn."""
    reserving = asyncio.Event()

    async def never_reserve(*_args: object, **_kwargs: object) -> None:
        reserving.set()
        await asyncio.Event().wait()

    with registered_probe_tools() as probe:
        conversation = _Conversation(tmp_path, monkeypatch, config=delegating_config(child_tools=[PROBE_TOOL]))
        await conversation.open(_hang("shell"), answer("Continued once"))
        try:
            record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
            job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
            await asyncio.wait_for(probe.running.wait(), JOB_TEST_TIMEOUT)
            cut_short = delegation_child(await conversation.job(job_id))
            with monkeypatch.context() as patch:
                patch.setattr(delegation_execution, "reserve_child_turn", never_reserve)
                await conversation.restart()
                await asyncio.wait_for(reserving.wait(), JOB_TEST_TIMEOUT)
            prepared = delegation_child((await saved_jobs(tmp_path))[job_id])
            assert prepared.previous_delegation_id == cut_short.delegation_id
            await conversation.restart()
            await wait_for_status(conversation.runtime, job_id, "completed")
            job = await conversation.job(job_id)
            assert str(job.result).startswith("Continued once")
            assert job.adapter["restart_continuations"] == 2
            continued = delegation_child(job)
            assert continued.delegation_id != prepared.delegation_id
            assert continued.previous_delegation_id == cut_short.delegation_id
            assert len(conversation.agents.models["code"].requests) == 2
        finally:
            await conversation.close()


@pytest.mark.asyncio
async def test_the_turn_that_started_a_continued_subagent_is_told_it_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn the restart cut short while it waited holds no handle of its job, so its job note names the running job.

    The job never ended, so no reply hears of an ended job, and a turn that did not start it is told nothing.
    """
    with registered_probe_tools() as probe:
        conversation = _Conversation(tmp_path, monkeypatch, config=delegating_config(child_tools=[PROBE_TOOL]))
        await conversation.open(_hang("one"), _hang("two"), answer("Never"))
        try:
            waiting = asyncio.create_task(conversation.reply(_delegate("first"), answer("Never")))
            await asyncio.wait_for(probe.running.wait(), JOB_TEST_TIMEOUT)
            probe.running.clear()
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
            await conversation.restart()
            await asyncio.wait_for(probe.running.wait(), JOB_TEST_TIMEOUT)
            [job] = conversation.runtime.retained_jobs()
            assert job.status == "running"
            assert await conversation.runtime.ended_jobs(_KEY) == ()
            context = _delegate_runtime_context(conversation.config, conversation.paths, execution_identity=_IDENTITY)
            with tool_runtime_context(replace(context, membership_turn_id="$turn1")), job_notice_context(JobNotices()):
                [note] = await append_turn_start_job_note(())
            assert note.text == f"`{job.job_id}` (code subagent) running\n{BACKGROUND_JOBS_CONTINUED_NOTICE}"
            with tool_runtime_context(replace(context, membership_turn_id="$turn2")), job_notice_context(JobNotices()):
                assert await append_turn_start_job_note(()) == ()
            # A job whose requester's access is unresolved or gone is named to nobody.
            conversation.allowed = False
            with tool_runtime_context(replace(context, membership_turn_id="$turn1")), job_notice_context(JobNotices()):
                assert await append_turn_start_job_note(()) == ()
        finally:
            await conversation.close()


@pytest.mark.asyncio
async def test_a_job_that_ends_while_the_turn_note_is_read_is_still_named(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The note reads the continuing jobs and the ended ones apart; a job that ends in between is in one of them."""
    started, held = _held_child()
    continued, finish = asyncio.Event(), asyncio.Event()

    async def hold_again() -> None:
        continued.set()
        await finish.wait()

    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(held, [Await(hold_again), *answer("Real answer")])
    try:
        record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
        job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
        await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
        await conversation.restart()
        await asyncio.wait_for(continued.wait(), JOB_TEST_TIMEOUT)
        runtime = conversation.runtime
        ended_jobs = runtime.ended_jobs

        async def ended_then_the_job_ends(key: ConversationKey) -> tuple[BackgroundJob, ...]:
            ended = await ended_jobs(key)
            finish.set()
            await wait_for_status(runtime, job_id, "completed")
            return ended

        monkeypatch.setattr(runtime, "ended_jobs", ended_then_the_job_ends)
        context = _delegate_runtime_context(conversation.config, conversation.paths, execution_identity=_IDENTITY)
        with tool_runtime_context(replace(context, membership_turn_id="$turn1")), job_notice_context(JobNotices()):
            [note] = await append_turn_start_job_note(())
        assert note.text.startswith(f"`{job_id}` (code subagent) ")
    finally:
        finish.set()
        await conversation.close()


@pytest.mark.asyncio
async def test_a_stopped_subagent_waits_for_its_bots_first_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job waits, still running, while its bot has no client or has not completed its first sync.

    Until its first sync a bot has not seen the rooms it left while it was down, and room membership is unknown.
    """
    started, held = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch)
    config = conversation.config
    await conversation.open(held, answer("Later"))
    try:
        record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
        job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
        await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
        await conversation.runtime.shutdown()
        await conversation._start_runtime()
        await conversation.runtime.recover()
        context = _delegate_runtime_context(conversation.config, conversation.paths, execution_identity=_IDENTITY)
        runtime, paths = conversation.runtime, conversation.paths
        for bot in ({"context": None}, {"context": context, "synced": False}):
            await continue_stopped_jobs(runtime, config, paths, **bot)
            assert [job.job_id for job in runtime.awaiting_continuation()] == [job_id]
            assert (await conversation.job(job_id)).status == "running"
        assert len(conversation.agents.models["code"].requests) == 1
        await continue_stopped_jobs(runtime, config, paths, context)
        await wait_for_status(conversation.runtime, job_id, "completed")
        assert str((await conversation.job(job_id)).result).startswith("Later")
    finally:
        await conversation.close()


@pytest.mark.asyncio
async def test_a_stopped_subagent_whose_bot_cannot_start_ends_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bot whose start failed for good never continues its job, so the job ends as a restart ended it before."""
    started, held = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(held, answer("Never"))
    try:
        record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
        job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
        await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
        await conversation.runtime.shutdown()
        await conversation._start_runtime()
        await conversation.runtime.recover()
        with capture_logs() as logs:
            await continue_stopped_jobs(
                conversation.runtime,
                conversation.config,
                conversation.paths,
                None,
                unavailable=True,
            )
        job = await conversation.job(job_id)
        assert (job.status, job.result) == ("interrupted", RESTART_INTERRUPTION_REASON)
        assert _declined(logs) == [(job_id, "the bot of 'leader' could not start")]
        assert len(conversation.agents.models["code"].requests) == 1
    finally:
        await conversation.close()


@pytest.mark.asyncio
async def test_a_stopped_subagent_whose_bot_left_the_room_ends_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bot that completed its first sync and is not in the job's room left it, so the job ends and never runs."""
    started, held = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(held, answer("Never"))
    try:
        record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
        job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
        await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
        await conversation.runtime.shutdown()
        await conversation._start_runtime()
        await conversation.runtime.recover()
        context = _delegate_runtime_context(conversation.config, conversation.paths, execution_identity=_IDENTITY)
        with capture_logs() as logs:
            await continue_stopped_jobs(
                conversation.runtime,
                conversation.config,
                conversation.paths,
                context,
                joined=False,
            )
        job = await conversation.job(job_id)
        assert (job.status, job.result) == ("interrupted", RESTART_INTERRUPTION_REASON)
        assert _declined(logs) == [(job_id, "the bot of 'leader' is no longer in the job's room")]
        assert len(conversation.agents.models["code"].requests) == 1
    finally:
        await conversation.close()


@pytest.mark.asyncio
async def test_one_stopped_job_that_fails_does_not_hold_up_the_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job whose access check raises is retried next pass, and the jobs after it are still continued."""
    _started, held = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(held, held, answer("First done"), answer("Second done"))
    try:
        record = await conversation.reply(
            calls(
                tool_call("run_subagent", "first", task="Report", agent_name="code", background_after=0),
                tool_call("run_subagent", "second", task="Report", agent_name="code", background_after=0),
            ),
            answer("Parent free"),
        )
        first, second = (json.loads(str(answer_of(record, call).content))["job_id"] for call in ("first", "second"))
        while len(conversation.agents.models["code"].requests) < 2:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        await conversation.runtime.shutdown()
        await conversation._start_runtime()
        await conversation.runtime.recover()
        context = _delegate_runtime_context(conversation.config, conversation.paths, execution_identity=_IDENTITY)
        coordinator = stopped_job_coordinator(conversation.runtime, conversation.config, conversation.paths, context)
        grant = coordinator._grant
        failing, other = (job.job_id for job in conversation.runtime.awaiting_continuation())
        assert {failing, other} == {first, second}

        def failing_grant(job: BackgroundJob) -> str:
            if job.job_id == failing:
                msg = "membership store is down"
                raise RuntimeError(msg)
            return grant(job)

        monkeypatch.setattr(coordinator, "_grant", failing_grant)
        await coordinator._continue_stopped_jobs(conversation.runtime)
        await wait_for_status(conversation.runtime, other, "completed")
        assert [job.job_id for job in conversation.runtime.awaiting_continuation()] == [failing]
    finally:
        await conversation.close()


@pytest.mark.asyncio
async def test_a_job_whose_replys_record_cannot_be_read_ends_as_stopped_and_the_others_recover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply record that cannot be read ends its job as a stopped reply's, and recovery goes on to the other jobs."""
    _started, held = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(held, held, answer("Never"))
    try:
        record = await conversation.reply(
            calls(
                tool_call("run_subagent", "first", task="Report", agent_name="code", background_after=0),
                tool_call("run_subagent", "second", task="Report", agent_name="code", background_after=0),
            ),
            answer("Parent free"),
        )
        first, second = (json.loads(str(answer_of(record, call).content))["job_id"] for call in ("first", "second"))
        while len(conversation.agents.models["code"].requests) < 2:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        await conversation.runtime.shutdown()

        async def unreadable(job: BackgroundJob) -> bool:
            if job.job_id == first:
                msg = "journal is down"
                raise RuntimeError(msg)
            return False

        monkeypatch.setattr(conversation, "_reply_stopped", unreadable)
        await conversation._start_runtime()
        with capture_logs() as logs:
            await conversation.runtime.recover()
        job = await conversation.job(first)
        assert (job.status, job.result) == ("interrupted", _REPLY_STOPPED_REASON)
        assert [job.job_id for job in conversation.runtime.awaiting_continuation()] == [second]
        assert [log["job_id"] for log in logs if log["log_level"] == "warning"] == [first]
    finally:
        await conversation.close()


@pytest.mark.asyncio
async def test_a_continuation_that_fails_to_start_ends_its_job_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure while preparing a continuation ends that job as before and leaves the worker's pass running."""
    started, held = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(held, answer("Never"))

    async def fail(*_args: object, **_kwargs: object) -> None:
        msg = "broken handle"
        raise RuntimeError(msg)

    try:
        record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
        job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
        await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
        monkeypatch.setattr(tool_job_coordinator, "continued_subagent_job", fail)
        await conversation.restart()
        job = await conversation.job(job_id)
        assert (job.status, job.result) == ("interrupted", RESTART_INTERRUPTION_REASON)
    finally:
        await conversation.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["restart", "crash"])
async def test_a_stopped_subagent_cancelled_before_it_continues_ends_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop: str,
) -> None:
    """Losing access, or a cancel, before the continuation starts ends the job cancelled, and its child never runs."""
    started, held = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(held, answer("Never"))
    try:
        record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
        job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
        await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
        conversation.allowed = False
        await _stop(conversation, stop)
        assert conversation.runtime._entries[job_id].job.status == "running"
        await conversation.runtime.cancel_revoked()
        await wait_for_status(conversation.runtime, job_id, "cancelled")
        conversation.allowed = True
        await conversation.continue_stopped()
        assert (await conversation.job(job_id)).status == "cancelled"
        assert len(conversation.agents.models["code"].requests) == 1
    finally:
        await conversation.close()


@pytest.mark.asyncio
async def test_losing_access_while_the_parent_waits_fails_its_call_not_its_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revoked access ends the parent's wait with a failed call; the reply itself completes."""
    started, held = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(held)
    try:
        replying = asyncio.create_task(conversation.reply(_delegate("waiting"), answer("Parent done")))
        await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
        conversation.allowed = False
        await conversation.runtime.cancel_revoked()
        record = await replying
        assert record.status is RunStatus.completed
        answered = answer_of(record, "waiting")
        assert (answered.content, answered.tool_call_error) == ("Tool job is not available in this conversation.", True)
    finally:
        await conversation.runtime.shutdown()


@pytest.mark.asyncio
async def test_a_subagent_call_an_approval_rule_gates_pauses_the_reply_for_its_own_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reply pauses on a card for the call itself, before any job or child starts."""
    conversation = _Conversation(tmp_path, monkeypatch)
    conversation.config.tool_approval.rules.append(ApprovalRuleConfig(match="run_subagent", action="require_approval"))
    await conversation.open(answer("Never"))
    try:
        record = await conversation.reply(_delegate("gated"), answer("Never"))
        assert record.status is RunStatus.paused
        [asked] = [requirement.tool_execution for requirement in pending_approval_requirements(record)]
        assert (asked.tool_name, asked.tool_args, asked.requires_confirmation, asked.approval_type) == (
            "run_subagent",
            {"task": "Report", "agent_name": "code"},
            True,
            POLICY_CONFIRMATION_APPROVAL_TYPE,
        )
        assert conversation.runtime.retained_jobs() == ()
        assert conversation.agents.models["code"].requests == []
    finally:
        await conversation.runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("crash", [False, True], ids=["restart", "crash"])
async def test_a_job_stopped_before_its_child_started_ends_interrupted_without_running_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    crash: bool,
) -> None:
    """A subagent job a restart or crash stops before it starts its child never runs it; it reports the interruption."""
    held = asyncio.Event()

    async def hold_before(**_kwargs: object) -> object:
        held.set()
        await asyncio.Event().wait()
        raise AssertionError

    monkeypatch.setattr(delegation_execution, "before_delegation", hold_before)
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(answer("Never"))
    try:
        record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
        job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
        await asyncio.wait_for(held.wait(), JOB_TEST_TIMEOUT)
        await conversation.restart(crash=crash)
        job = await conversation.job(job_id)
        assert (job.status, job.result) == ("interrupted", RESTART_INTERRUPTION_REASON)
        assert conversation.agents.models["code"].requests == []
    finally:
        await conversation.close()


@pytest.mark.asyncio
async def test_subagent_calls_of_one_step_run_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two subagent calls in one model step run as two jobs at once, as any managed calls do."""
    both = asyncio.Barrier(2)

    async def meet() -> None:
        await asyncio.wait_for(both.wait(), JOB_TEST_TIMEOUT)

    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open([Await(meet), *answer("One")], [Await(meet), *answer("Two")])
    try:
        record = await conversation.reply(
            calls(
                tool_call("run_subagent", "first", task="Report", agent_name="code"),
                tool_call("run_subagent", "second", task="Review", agent_name="code"),
            ),
            answer("Both done"),
        )
        assert {str(answer_of(record, call_id).content).split("\n")[0] for call_id in ("first", "second")} == {
            "One",
            "Two",
        }
        assert sorted(job.status for job in conversation.runtime.retained_jobs()) == ["completed", "completed"]
    finally:
        await conversation.runtime.shutdown()


@pytest.mark.asyncio
async def test_two_follow_ups_to_one_subagent_in_one_step_run_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Of two follow-ups to one subagent in one step, the one whose job finds the other's turn running fails as busy."""
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(answer("First answer"), answer("Follow-up answer"), answer("Never"))
    try:
        first = str(answer_of(await conversation.reply(_delegate("first"), answer("Done")), "first").content)
        subagent_id = first.rsplit("Subagent ID: ", 1)[1].strip()
        record = await conversation.reply(
            calls(
                tool_call("continue_subagent", "again", subagent_id=subagent_id, message="And now?"),
                tool_call("continue_subagent", "twice", subagent_id=subagent_id, message="And now?"),
            ),
            answer("Done"),
        )
        answers = [str(answer_of(record, "again").content), str(answer_of(record, "twice").content)]
        assert sum(answer.startswith("Follow-up answer") for answer in answers) == 1
        assert sum(answer.startswith(f"Delegation to 'code' failed: {SUBAGENT_BUSY}") for answer in answers) == 1
        assert len(conversation.agents.models["code"].requests) == 2
    finally:
        await conversation.runtime.shutdown()


@pytest.mark.asyncio
async def test_a_copied_subagent_call_never_starts_a_second_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call dispatched again under the same run and call ID meets its job and is refused; its child ran once."""
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(answer("Only answer"), answer("Never"))
    try:
        record = await conversation.reply(_delegate("same"), _delegate("same"), answer("Done"))
        answered = [str(message.content) for message in record.messages or () if message.tool_call_id == "same"]
        assert answered[0].startswith("Only answer")
        assert answered[1] == "Tool job is not available in this conversation."
        assert len(conversation.agents.models["code"].requests) == 1
        assert len(conversation.runtime.retained_jobs()) == 1
    finally:
        await conversation.runtime.shutdown()


@pytest.mark.asyncio
async def test_a_running_subagent_job_saves_the_child_it_started_with_its_audit_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once its child started, the saved job names the child's audit record, so recovery after a crash can finish it."""
    started, held = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(held)
    try:
        record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
        job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
        await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
        saved = delegation_child((await saved_jobs(tmp_path))[job_id])
        assert saved.status == "running"
        assert saved.record_locator is not None
        assert saved.record_locator["delegation_id"] == saved.delegation_id
    finally:
        await conversation.close()
