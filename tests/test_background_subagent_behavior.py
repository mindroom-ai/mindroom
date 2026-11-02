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

from mindroom.approval import manager as approval_manager
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.delegation import execution as delegation_execution
from mindroom.delegation.background import delegation_child
from mindroom.delegation.recovery import read_child_run
from mindroom.delegation.sessions import SUBAGENT_BUSY
from mindroom.event_journal import BackgroundApprovalDecision, tool_job_run_id
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.response_turn import pending_approval_requirements
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import describe_job, register_background_runtime
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.tool_function import POLICY_CONFIRMATION_APPROVAL_TYPE
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.delegation_helpers import ScriptedAgents, _delegate_runtime_context, _runtime_paths, registered_report_tool
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call
from tests.tool_job_helpers import (
    DELEGATING_REQUESTER,
    JOB_TEST_TIMEOUT,
    DelegatingLeader,
    JobApprovalCards,
    answer_of,
    delegating_config,
    lookup,
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
        )
        register_background_runtime(self.paths, self.runtime)

    async def restart(self, *, crash: bool = False) -> None:
        """Stop this process, in order or by a crash that runs none of its code, and recover in the next one.

        A crashed process's work stays where it stopped until ``close``; the next process takes its jobs over.
        """
        if crash:
            self.crashed.append(self.runtime)
        else:
            await self.runtime.shutdown()
        await self._start_runtime()
        await self.runtime.recover()

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


@pytest.mark.asyncio
async def test_a_restart_interrupts_a_running_subagent_and_says_so(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart never runs the child again; its job ends with the interruption the child records."""
    started, held = _held_child()
    conversation = _Conversation(tmp_path, monkeypatch)
    await conversation.open(held)
    try:
        record = await conversation.reply(_delegate("first", background_after=0), answer("Parent free"))
        job_id = json.loads(str(answer_of(record, "first").content))["job_id"]
        await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
        await conversation.restart()
        job = await conversation.job(job_id)
        assert (job.status, job.result) == (
            "failed",
            "Subagent turn was interrupted by a restart. Send a follow-up to continue its history.",
        )
        child_run = await read_child_run(delegation_child(job), conversation.config, conversation.paths)
        assert child_run is None or child_run.status not in {RunStatus.running, RunStatus.paused}
    finally:
        await conversation.runtime.shutdown()


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
        assert (job.status, job.result) == (
            "failed",
            "Subagent turn was interrupted by a restart. Send a follow-up to continue its history.",
        )
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
