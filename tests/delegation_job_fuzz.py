"""Generated background subagent lifecycles checked against invariants that must hold on every path.

Real Agno parents and children run scripted models through MindRoom's native delegation, background jobs, the
approval cards each job posts for its child, parent waits, cancellation, Stop, orderly restart, and crash recovery.
The child's approval-gated tool records every execution, so the oracle compares effects with the decisions given.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Literal

from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.tools.function import Function

from mindroom import agno_compat_session_persistence as session_persistence
from mindroom import approval_manager
from mindroom.agent_storage import create_session_storage
from mindroom.agents import apply_tool_approval_capability
from mindroom.approval_tools import toolkit_owners_for_agents
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.custom_tools.job import JobTools
from mindroom.delegation.background import delegation_child, reconcile_delegation
from mindroom.delegation.execution import drive_delegations
from mindroom.delegation.job_approvals import _approval_run_id, settle_child_approvals
from mindroom.delegation.recovery import interrupt_stopped_child, read_child_run
from mindroom.event_journal import BackgroundApprovalDecision
from mindroom.response_turn import ResponsePausedForApproval, paused_attempt_from_response
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.authorization import bind_toolkit_authority
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import TERMINAL_STATUSES, register_background_runtime
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.access_schema_support import with_responder_access
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context, _runtime_paths
from tests.tool_job_helpers import lookup, run_journal_statements_inline, tool_job_runtime

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from pathlib import Path

    import pytest
    from agno.db.base import BaseDb

    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild
    from mindroom.tool_approval import BackgroundScriptToolOrigin
    from mindroom.tool_jobs.runtime import BackgroundJob, BackgroundOutcome
    from tests.tool_job_helpers import ProcessRuntime

_REQUESTER = "@alice:example.org"
_WRITTEN = "Report written"
# Loop iterations, and timer waits shorter than this many seconds, one step may take before it counts as stuck.
_IDLE_ROUNDS = 20_000
_TIMER_HORIZON = 1.0
# More cards than any generated child can request.
_MAX_CARDS = 20


@dataclass(frozen=True)
class ChildScript:
    """What one delegated child does: approval-gated tool calls, one per model turn, then its answer."""

    approvals: int = 0
    # The child waits for an explicit release before its first model call.
    hold_start: bool = False
    # Each approved tool call waits for an explicit release before it finishes.
    hold_tool: bool = False


@dataclass(frozen=True)
class Step:
    """A printable, shrinkable step; it names a child or card by index, never by identity."""

    kind: Literal["delegate", "wait", "approve", "cancel", "stop", "release", "restart", "crash"]
    index: int = 0
    script: ChildScript = ChildScript()
    # A parent wait budget: None waits until the job finishes, 0 returns at once.
    budget: float | None = None
    # The requester's answer to a card.
    approve: bool = True


@dataclass
class _Card:
    run_id: str
    call_id: str
    decision: asyncio.Future[BackgroundApprovalDecision]


@dataclass
class _Cards:
    """The approval store a job posts its child's cards to: each card stays answerable until decided or settled."""

    cards: list[_Card] = field(default_factory=list)
    send_delivery: object = field(default_factory=object)

    async def request_background_approval(
        self,
        *,
        origin: BackgroundScriptToolOrigin,
        **_kwargs: object,
    ) -> BackgroundApprovalDecision:
        card = _Card(origin.run_id, origin.call_id, asyncio.get_running_loop().create_future())
        self.cards.append(card)
        # A cancelled job leaves its card answerable; only a decision or settlement retires it.
        return await asyncio.shield(card.decision)

    async def settle_pending_background_approvals(self, run_id: str, *, reason: str) -> int:
        settled = 0
        for card in self.pending():
            if card.run_id == run_id:
                card.decision.set_result(BackgroundApprovalDecision("denied", reason))
                settled += 1
        return settled

    def pending(self) -> list[_Card]:
        return [card for card in self.cards if not card.decision.done()]


@dataclass
class _Child:
    script: ChildScript
    responses: list[ModelResponse]
    job_id: str | None = None
    session_id: str | None = None
    runs: int = 0


@dataclass
class _Turn:
    name: str
    task: asyncio.Task[RunOutput | str]


class DelegationFuzzRunner:
    """Drive one parent conversation's background children through generated steps over durable storage."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._root = root
        self.paths = _runtime_paths(root)
        self.config = with_responder_access(
            Config(
                agents={
                    "leader": AgentConfig(display_name="Leader", delegate_to=["code"]),
                    "code": AgentConfig(display_name="Code", tools=["file"]),
                },
                defaults=DefaultsConfig(tools=[]),
                memory={"backend": "none"},
            ),
            "code",
            users=[_REQUESTER],
        )
        self.identity = ToolExecutionIdentity("matrix", "leader", _REQUESTER, "!room:example.org", None, None, "parent")
        self._baseline = asyncio.all_tasks()
        self._crashing = False
        self._turn_count = 0
        self._order = 0
        self.gate = asyncio.Event()
        self.children: list[_Child] = []
        self.approvals = _Cards()
        self.turns: list[_Turn] = []
        self.closed_turns: list[_Turn] = []
        self.executed: Counter[str] = Counter()
        # Notes of calls some resolution approved; only those may ever run.
        self.approved: set[str] = set()
        self.violations: list[str] = []
        # What first stopped each job's work: a cancellation or Stop, or a restart; the first cause stands.
        self.causes: dict[str, Literal["cancel", "restart"]] = {}
        self._sessions: dict[str, int] = {}
        self._storages: list[BaseDb] = []
        pin_background_tool_jobs(self.config, self.paths)
        self.runtime: ProcessRuntime
        self.toolkit = DelegateTools("leader", ["code"], self.paths, self.config, execution_identity=self.identity)
        apply_tool_approval_capability(
            self.toolkit,
            self.config,
            supports_native_tool_approval=True,
            registered_tool_name="delegate",
        )
        bind_toolkit_authority(self.toolkit, authored_name="delegate")
        self.storage = create_session_storage("leader", self.config, self.paths, self.identity)
        monkeypatch.setattr("mindroom.agents.create_agent", self._build_child)
        monkeypatch.setattr(approval_manager, "get_approval_store", lambda: self.approvals)
        # Blocking work runs on the event loop, so an idle loop marks the end of each step and a crash lands between two
        # awaits; synchronous session saves skip their thread lane for the same reason.
        monkeypatch.setattr(asyncio, "to_thread", _inline)
        monkeypatch.setattr(session_persistence, "_registered_lane", lambda _database: None)
        run_journal_statements_inline(monkeypatch)

    async def open(self) -> None:
        """Start the first process."""
        self.runtime = await self._open()

    async def _open(self) -> ProcessRuntime:
        runtime = await tool_job_runtime(self.paths.storage_root, cancel=self._interrupt_child)
        register_background_runtime(self.paths, runtime)
        return runtime

    async def _interrupt_child(self, job: BackgroundJob) -> BackgroundOutcome | None:
        """Settle a recovered child as the job coordinator does: a restart interrupts it and denies its cards."""
        outcome = await reconcile_delegation(
            job,
            cleanup=partial(interrupt_stopped_child, config=self.config, runtime_paths=self.paths),
            runtime_paths=self.paths,
        )
        await settle_child_approvals(self.runtime, job.job_id)
        assert not self.runtime.unsettled_approvals
        return outcome

    async def run(self, steps: list[Step]) -> None:
        """Apply each step, then release everything and check the settled state."""
        for step in steps:
            await self.step(step)
        await self.finish()

    async def step(self, step: Step) -> None:
        """Apply one step, run every task it woke to a standstill, then check every invariant."""
        await getattr(self, f"_{step.kind}")(step)
        await self._idle()
        await self._collect()

    async def finish(self) -> None:
        """Release all held work and approve every card; every turn must finish and every child settle consistently."""
        self.gate.set()
        await self._idle()
        for _ in range(_MAX_CARDS):
            if not self.approvals.pending():
                break
            await self._approve(Step("approve"))
            await self._idle()
        await self._collect()
        assert not self.turns, [turn.name for turn in self.turns]
        await self._check_settled()

    async def close(self) -> None:
        """End every task this example started and release storage, even after a failed check."""
        self._crashing = True
        self.gate.set()
        await self._end_tasks()
        await self.runtime.close_journal()
        self.storage.close()
        for storage in self._storages:
            storage.close()

    def _build_child(self, *args: object, **kwargs: object) -> Agent:
        child_identity = args[3]
        assert isinstance(child_identity, ToolExecutionIdentity)
        storage = kwargs.get("history_storage") or create_session_storage(
            "code",
            self.config,
            self.paths,
            child_identity,
        )
        self._storages.append(storage)  # type: ignore[arg-type]
        record = self.children[self._sessions[child_identity.session_id]]

        async def write_report(note: str) -> str:
            self.executed[note] += 1
            if record.script.hold_tool:
                await self.gate.wait()
            return _WRITTEN

        function = Function.from_callable(write_report)
        function.requires_confirmation = True
        function.owning_toolkit = "file"
        return Agent(
            name="code",
            id="code",
            db=storage,  # type: ignore[arg-type]
            tools=[function],
            model=DelegationModel(id="test", responses=record.responses),
        )

    async def _run_child(
        self,
        child: DelegationChild,
        *,
        prompt: str,
        config: Config,
        runtime_paths: RuntimePaths,
        **_kwargs: object,
    ) -> str:
        index = int(child.task.removeprefix("script"))
        record = self.children[index]
        record.job_id, record.session_id = child.delegation_id, child.session_id
        record.runs += 1
        self._sessions[child.session_id] = index
        if record.script.hold_start:
            await self.gate.wait()
        child_identity = replace(self.identity, agent_name="code", session_id=child.session_id)
        agent = self._build_child("code", config, runtime_paths, child_identity)
        response = await agent.arun(prompt, session_id=child.session_id, run_id=child.run_id, user_id=_REQUESTER)
        paused = paused_attempt_from_response(
            response,
            fallback_session_id=child.session_id,
            fallback_run_id=child.run_id,
            toolkit_owners=toolkit_owners_for_agents([agent]),
        )
        if paused is not None:
            raise ResponsePausedForApproval(paused)
        return str(response.content)

    def _parent(self, responses: list[ModelResponse]) -> Agent:
        model = DelegationModel(id="test", responses=responses)
        install_tool_job_execution(model)
        return Agent(
            name="leader",
            db=self.storage,
            tools=[self.toolkit, JobTools(self.paths, self.identity)],
            model=model,
        )

    async def _drive(self, agent: Agent) -> RunOutput:
        response = await agent.arun("Delegate", session_id="parent", user_id=_REQUESTER)
        return await drive_delegations(
            agent,
            response,
            run_child=self._run_child,
            agent_name="leader",
            config=self.config,
            runtime_paths=self.paths,
            execution_identity=self.identity,
        )

    def _start_turn(self, name: str, coroutine: Coroutine[object, object, RunOutput | str]) -> None:
        self._turn_count += 1
        context = replace(
            _delegate_runtime_context(self.config, self.paths, execution_identity=self.identity),
            membership_turn_id=f"$turn{self._turn_count}",
        )

        async def run() -> RunOutput | str:
            with tool_runtime_context(context):
                return await coroutine

        self.turns.append(_Turn(name, asyncio.create_task(run())))

    async def _delegate(self, step: Step) -> None:
        index = len(self.children)
        responses = [
            ModelResponse(tool_calls=[_call("write_report", f"w{index}-{turn}", note=f"w{index}-{turn}")])
            for turn in range(step.script.approvals)
        ]
        self.children.append(_Child(step.script, [*responses, ModelResponse(content=f"Child {index} result")]))
        budget = {} if step.budget is None else {"wait_timeout": step.budget}
        call = _call("run_subagent", f"delegate{index}", agent_name="code", task=f"script{index}", **budget)
        agent = self._parent([ModelResponse(tool_calls=[call]), ModelResponse(content="Parent done")])
        self._start_turn(f"delegate{index}", self._drive(agent))

    def _job(self, index: int) -> str | None:
        known = [child.job_id for child in self.children if child.job_id is not None]
        return known[index % len(known)] if known else None

    async def _wait(self, step: Step) -> None:
        if (job_id := self._job(step.index)) is None:
            return
        budget = {} if step.budget is None else {"wait_timeout": step.budget}
        call = _call("job", f"wait{self._turn_count}", action="wait", job_id=job_id, **budget)
        agent = self._parent([ModelResponse(tool_calls=[call]), ModelResponse(content="Parent read")])
        self._start_turn(f"wait:{job_id}", self._drive(agent))

    async def _approve(self, step: Step) -> None:
        """Answer one card a job is still waiting on."""
        if not (pending := self.approvals.pending()):
            return
        card = pending[step.index % len(pending)]
        if step.approve:
            self.approved.add(card.call_id)
        status = "approved" if step.approve else "denied"
        card.decision.set_result(BackgroundApprovalDecision(status, None if step.approve else "Not now"))

    def _cause(self, job_id: str, cause: Literal["cancel", "restart"]) -> None:
        entry = self.runtime._entries.get(job_id)
        if entry is not None and entry.job.status not in TERMINAL_STATUSES:
            self.causes.setdefault(job_id, cause)

    async def _cancel(self, step: Step) -> None:
        if (job_id := self._job(step.index)) is not None:
            self._cause(job_id, "cancel")
            self._start_turn(f"cancel:{job_id}", JobTools(self.paths, self.identity).job("cancel", job_id))

    async def _stop(self, step: Step) -> None:
        if (job_id := self._job(step.index)) is None:
            return
        self._cause(job_id, "cancel")
        self._order += 1

        async def matches(job: BackgroundJob) -> bool:
            return job.job_id == job_id

        await self.runtime.stop_jobs(receipt_order=self._order, matches=matches)

    async def _release(self, _step: Step) -> None:
        self.gate.set()
        await self._idle()
        self.gate = asyncio.Event()

    async def _restart(self, _step: Step) -> None:
        """Stop the process in order: replies are cancelled, then the job runtime drains and closes."""
        for child in self.children:
            if child.job_id is not None:
                self._cause(child.job_id, "restart")
        await self._end_turns()
        shutdown = asyncio.create_task(self.runtime.shutdown())
        await self._idle()
        self.gate.set()
        await self._idle()
        assert shutdown.done()
        shutdown.result()
        self.gate = asyncio.Event()
        await self._reopen()

    async def _crash(self, _step: Step) -> None:
        """Tear down the event loop between two awaits: work unwinds unasked, and only saved state survives."""
        for child in self.children:
            if child.job_id is not None:
                self._cause(child.job_id, "restart")
        self._crashing = True
        await self._end_tasks()
        self._crashing = False
        self.turns.clear()
        await self._reopen()

    async def _reopen(self) -> None:
        runs = [child.runs for child in self.children]
        # The previous process is gone, and so is its connection.
        await self.runtime.close_journal()
        self.runtime = await self._open()
        await self.runtime.recover()
        # Recovery never runs a child again, and settles every job a restart cut short.
        assert [child.runs for child in self.children] == runs
        for job in await self._jobs():
            assert job.status in TERMINAL_STATUSES, (job.job_id, job.status)

    async def _end_turns(self) -> None:
        for turn in self.turns:
            turn.task.cancel()
        await asyncio.gather(*(turn.task for turn in self.turns), return_exceptions=True)
        self.turns.clear()

    async def _end_tasks(self) -> None:
        current = asyncio.current_task()
        while pending := [
            task
            for task in asyncio.all_tasks()
            if task not in self._baseline and task is not current and not task.done()
        ]:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def _idle(self) -> None:
        """Yield until no task is runnable and no near timer remains; every waiter then awaits a test gate."""
        loop = asyncio.get_running_loop()
        for _ in range(_IDLE_ROUNDS):
            await asyncio.sleep(0)
            if loop._ready:  # type: ignore[attr-defined]
                continue
            timers = [
                handle.when()
                for handle in loop._scheduled  # type: ignore[attr-defined]
                if not handle.cancelled() and handle.when() - loop.time() < _TIMER_HORIZON
            ]
            if not timers:
                return
            await asyncio.sleep(max(0.0, min(timers) - loop.time()))
        msg = "Delegation fuzz step never became idle"
        raise AssertionError(msg)

    async def _collect(self) -> None:
        """Check effects, finished turns, and that every answerable card belongs to a job still waiting on it."""
        assert not self.violations, self.violations
        for name, count in self.executed.items():
            assert count <= 1, (name, count)
            assert name in self.approved, f"{name} ran without approval"
        for turn in [turn for turn in self.turns if turn.task.done()]:
            self.turns.remove(turn)
            self.closed_turns.append(turn)
            result = turn.task.result()
            # A child's approvals never pause its parent; the job asks for them.
            assert not (isinstance(result, RunOutput) and result.status == RunStatus.paused), turn.name
        waiting = {_approval_run_id(job.job_id) for job in await self._jobs() if job.status == "awaiting_approval"}
        answerable = {card.run_id for card in self.approvals.pending()}
        assert answerable <= waiting, (answerable, waiting)

    async def _jobs(self) -> list[BackgroundJob]:
        return [
            await lookup(self.runtime, child.job_id, owner=self.identity, depth=0)
            for child in self.children
            if child.job_id is not None and self.runtime.has_job(child.job_id)
        ]

    async def _check_settled(self) -> None:
        """Compare each job with its child's saved run, and each approved effect with that run's tools."""
        assert not self.approvals.pending()
        for job in await self._jobs():
            assert job.status in TERMINAL_STATUSES, (job.job_id, job.status)
            run = await read_child_run(delegation_child(job), self.config, self.paths)
            if job.status == "completed":
                assert run is not None
                assert run.status == RunStatus.completed, (job.job_id, run.status)
            elif job.status in TERMINAL_STATUSES and run is not None:
                assert run.status not in {RunStatus.running, RunStatus.paused}, (job.job_id, job.status, run.status)
            if job.status in {"completed", "cancelled"}:
                assert job.adapter["child"]["status"] == job.status, (job.job_id, job.adapter["child"]["status"])
            cause = self.causes.get(job.job_id)
            if cause == "cancel":
                # A cancellation or Stop ends the child as cancelled, even when a restart finishes settling it.
                assert job.status == "cancelled", (job.job_id, job.status, job.result)
            elif cause == "restart":
                assert job.status in {"failed", "interrupted"}, (job.job_id, job.status, job.result)
                assert "restart" in (job.result or "") or "shutdown" in (job.result or ""), (job.job_id, job.result)
            for tool in (run.tools or ()) if run is not None else ():
                if tool.tool_name == "write_report" and tool.result == _WRITTEN:
                    assert self.executed[str(tool.tool_args["note"])] == 1, tool.tool_call_id


async def _inline[Result](function: Callable[..., Result], /, *args: object, **kwargs: object) -> Result:
    return function(*args, **kwargs)
