"""What a requester and the leader's model see of a subagent that runs as a background job.

A leader reply delegates to ``code`` with background tool jobs on, and each test reads only what reaches a person or a
model: the call's answer, the job a handle or listing names, its approval cards, and how it ends.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

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
from mindroom.event_journal import BackgroundApprovalDecision, tool_job_run_id
from mindroom.history.run_checkpoint import INTERRUPTED_CALL_RESULT
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.orchestration import tool_job_runtime as tool_job_coordinator
from mindroom.prompts import BACKGROUND_JOBS_CONTINUED_NOTICE, SUBAGENT_RESTART_CONTINUATION_PROMPT
from mindroom.response_turn import pending_approval_requirements
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_jobs.conversation import ConversationKey
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.notices import JobNotices, append_turn_start_job_note, job_notice_context
from mindroom.tool_jobs.runtime import describe_job, register_background_runtime
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.tool_function import POLICY_CONFIRMATION_APPROVAL_TYPE
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.access_schema_support import with_responder_access
from tests.delegation_helpers import ScriptedAgents, _delegate_runtime_context, _runtime_paths, registered_report_tool
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call
from tests.scripted_agent_helpers import PROBE_TOOL, registered_probe_tools
from tests.tool_job_helpers import (
    DELEGATING_REQUESTER,
    JOB_TEST_TIMEOUT,
    DelegatingLeader,
    JobApprovalCards,
    answer_of,
    continue_stopped_jobs,
    delegating_config,
    lookup,
    saved_job_card_decision,
    saved_jobs,
    settle_stopped_subagent,
    tool_job_runtime,
    wait_for_status,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
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
        )
        register_background_runtime(self.paths, self.runtime)

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
        grant = "allowed" if self.allowed else "denied"
        await continue_stopped_jobs(self.runtime, self.config, self.paths, context, grant=grant)

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


@pytest.mark.asyncio
async def test_a_stopped_replys_subagent_is_not_continued_after_a_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Stop of the reply that started a working subagent is saved with its job, so no restart continues it."""
    started, held = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(held, answer("Never"))
    try:
        record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
        job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
        await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
        conversation.runtime._entries[job_id].reply_id = "reply"
        await conversation.runtime.cancel_stopped_reply("reply")
        assert (await conversation.job(job_id)).status == "running"
        await conversation.restart()
        job = await conversation.job(job_id)
        assert (job.status, job.result) == ("interrupted", RESTART_INTERRUPTION_REASON)
        assert len(conversation.agents.models["code"].requests) == 1
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
        assert _declined(logs) == [(job_id, "its turn's run was left paused, which history never replays")]
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
        finally:
            await conversation.close()


@pytest.mark.asyncio
async def test_a_stopped_subagent_waits_for_its_requesters_access_and_its_bot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before the first sync reply grants are unresolved and a bot may have no client: the job waits, still running."""
    started, held = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(held, answer("Later"))
    try:
        record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
        job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
        await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
        await conversation.runtime.shutdown()
        await conversation._start_runtime()
        await conversation.runtime.recover()
        context = _delegate_runtime_context(conversation.config, conversation.paths, execution_identity=_IDENTITY)
        runtime, config, paths = conversation.runtime, conversation.config, conversation.paths
        await continue_stopped_jobs(runtime, config, paths, context, grant="pending")
        assert [job.job_id for job in runtime.awaiting_continuation()] == [job_id]
        await continue_stopped_jobs(runtime, config, paths, None)
        assert [job.job_id for job in runtime.awaiting_continuation()] == [job_id]
        assert (await conversation.job(job_id)).status == "running"
        assert len(conversation.agents.models["code"].requests) == 1
        await conversation.continue_stopped()
        await wait_for_status(conversation.runtime, job_id, "completed")
        assert str((await conversation.job(job_id)).result).startswith("Later")
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
