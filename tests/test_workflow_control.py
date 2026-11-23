"""What a client's control events do to a workflow run, and what room state lists of one (engine harness level)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest
import pytest_asyncio

from mindroom.custom_tools.workflow import handle_workflow_control, handle_workflow_event
from mindroom.event_journal import EventKind
from mindroom.journal_dispatch import _BINDINGS
from mindroom.matrix.event_types import WORKFLOW_ANSWER_EVENT_TYPE, WORKFLOW_CONTROL_EVENT_TYPE
from mindroom.script_runs import manager as manager_module
from mindroom.script_runs.models import ScriptRunState
from mindroom.tool_jobs import runtime as tool_job_runtime
from mindroom.tool_jobs.client_cancel import handle_job_cancel_event
from mindroom.tool_jobs.publication import _jobs_state_content
from mindroom.workflow_runs import agent_nodes
from mindroom.workflow_runs import engine as engine_module
from mindroom.workflow_runs.models import WorkflowNodeState, WorkflowRunState
from tests.conftest import SETTLE_TIMEOUT_SECONDS
from tests.fakes.scripted_model import Await, answer
from tests.workflow_helpers import WorkflowStack, tool_args, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

_SCRIPT = "def main(args):\n    return 'counted'\n"
_ROOM = "!room:example.test"
_THREAD = "$thread:example.test"
_ALICE = "@alice:example.test"


@pytest.fixture(autouse=True)
def _scripts_can_run_here(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the placement check pass on every platform; the stack starts no process."""
    monkeypatch.setattr(manager_module, "background_script_supervision_supported", lambda: True)


@pytest_asyncio.fixture
async def stack(tmp_path: Path) -> AsyncIterator[WorkflowStack]:
    """The harness with the `workflow` toolkit, the real job runtime, and one run started as a job."""
    with workflow_stack(tmp_path, extra_tools=("workflow",), require_approval=()) as stack:
        await stack.open_jobs()
        handle = json.loads(await stack.call("start_workflow", source=_SCRIPT, title="Count the invented things"))
        stack.run_id = handle["run_id"]
        await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING)
        yield stack
        await stack.close()


async def _fail(stack: WorkflowStack) -> str:
    """Let one call of the run finish and its script fail, and return the run's job once that ended."""
    job_id = stack.store.get_run(stack.run_id).job_id
    assert job_id is not None
    assert await stack.result("@0", "tool", tool_args("counter", "append", line="a")) == "appended"
    await stack.end_attempt(ScriptRunState.FAILED, output="KeyError: 'tide'")
    async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
        ended = await stack.jobs.wait(job_id, owner=stack.context.execution_identity, depth=0)
    assert ended.job.status == "failed"
    return job_id


def _event(event_type: str, content: dict[str, object], *, sender: str, thread: str | None) -> nio.UnknownEvent:
    relation = {} if thread is None else {"m.relates_to": {"rel_type": "m.thread", "event_id": thread}}
    source = {"type": event_type, "sender": sender, "event_id": "$control", "origin_server_ts": 1, "content": {}}
    event = nio.UnknownEvent.from_dict(source)
    assert isinstance(event, nio.UnknownEvent)
    event.source["content"] = content | relation
    return event


async def _control(
    stack: WorkflowStack,
    action: str = "resume",
    *,
    sender: str = _ALICE,
    room_id: str = _ROOM,
    thread: str | None = _THREAD,
    agent_name: str | None = None,
    max_cost_usd: float | None = None,
) -> bool:
    """Hand one control event that names the stack's run to an agent's bot, as its journal callback does."""
    payload: dict[str, object] = {"run": stack.run_id, "action": action}
    if max_cost_usd is not None:
        payload["max_cost_usd"] = max_cost_usd
    event = _event(WORKFLOW_CONTROL_EVENT_TYPE, payload, sender=sender, thread=thread)
    room = nio.MatrixRoom(room_id, "@mindroom_watcher:example.test")
    return await handle_workflow_control(
        room,
        event,
        agent_name=agent_name or stack.context.agent_name,
        engine=lambda: stack.engine,
    )


@pytest.mark.asyncio
async def test_journal_hands_a_workflow_event_to_the_bots_workflows_and_settles_it() -> None:
    """The journal runs the bot's workflow callback for a control event, and settles the event without one too."""
    event = _event(WORKFLOW_CONTROL_EVENT_TYPE, {"run": "wf-1", "action": "resume"}, sender=_ALICE, thread=_THREAD)
    room, callback = nio.MatrixRoom(_ROOM, "@mindroom_watcher:example.test"), AsyncMock(return_value=False)
    run = _BINDINGS[EventKind.WORKFLOW_CONTROL].run

    assert await run(SimpleNamespace(callbacks=SimpleNamespace(on_workflow_event=callback)), room, event) is True
    assert await run(SimpleNamespace(callbacks=SimpleNamespace(on_workflow_event=None)), room, event) is True
    callback.assert_awaited_once_with(room, event)
    assert _BINDINGS[EventKind.WORKFLOW_ANSWER].run is run


@pytest.mark.asyncio
async def test_bots_workflow_callback_takes_an_answer_and_a_resume(stack: WorkflowStack) -> None:
    """The one callback of a bot hands an answer event to the questions and any other to the run's controls."""
    job_id = await _fail(stack)
    stack.show()
    ours = {"agent_name": stack.context.agent_name, "engine": lambda: stack.engine}
    room, names = nio.MatrixRoom(_ROOM, "@mindroom_watcher:example.test"), {"run": stack.run_id}
    answer = _event(WORKFLOW_ANSWER_EVENT_TYPE, {**names, "node": "n9"}, sender=_ALICE, thread=_THREAD)
    resume = _event(WORKFLOW_CONTROL_EVENT_TYPE, {**names, "action": "resume"}, sender=_ALICE, thread=_THREAD)

    await handle_workflow_event(room, answer, **ours)
    assert stack.room is not None
    assert [(kind, content["reason"]) for _, kind, content in stack.room.events] == [
        ("io.mindroom.workflow_answer_rejected", "no_longer_open"),
    ]
    assert stack.store.get_run(stack.run_id).attempt == 1
    await handle_workflow_event(room, resume, **ours)
    await stack.until(lambda: stack.store.get_run(stack.run_id).attempt == 2)

    assert stack.store.get_run(stack.run_id).job_id != job_id


def _standing(stack: WorkflowStack) -> tuple[str, int, int, int]:
    run = stack.store.get_run(stack.run_id)
    return run.state.value, run.attempt, run.epoch, len(stack.jobs.retained_jobs())


@pytest.mark.asyncio
async def test_resume_control_from_the_requester_in_the_runs_thread_resumes(stack: WorkflowStack) -> None:
    """The requester's Resume takes the run as `resume_workflow` does: a new job, and saved calls are reused."""
    first = await _fail(stack)

    assert await _control(stack) is True
    await stack.until(lambda: stack.store.get_run(stack.run_id).attempt == 2)

    run = stack.store.get_run(stack.run_id)
    [job] = [job for job in stack.jobs.retained_jobs() if job.adapter.get("workflow_job") == run.owner]
    assert job.job_id != first
    assert (job.kind, job.status, job.adapter["workflow"]) == ("workflow", "running", stack.run_id)
    assert (run.state, run.job_id) == (WorkflowRunState.RUNNING, job.job_id)
    assert (await stack.submit("@0", "tool", tool_args("counter", "append", line="a"))).cached
    assert stack.counting_tool.lines == ["a"]
    # The same event delivered again starts nothing more.
    assert await _control(stack) is False
    assert stack.store.get_run(stack.run_id).attempt == 2


@pytest.mark.asyncio
async def test_report_of_a_run_that_a_resume_control_resumed_is_delivered(stack: WorkflowStack) -> None:
    """Nobody waits for the job a Resume event starts, so nobody holds its outcome: the report is a follow-up."""
    await _fail(stack)
    assert await _control(stack) is True
    await stack.until(lambda: stack.store.get_run(stack.run_id).attempt == 2)
    job_id = stack.store.get_run(stack.run_id).job_id
    assert job_id not in stack.jobs.awaited_job_ids()

    await stack.finish_attempt(result="the resumed report")
    done = lambda: any(job.job_id == job_id and job.status == "completed" for job in stack.jobs.retained_jobs())  # noqa: E731
    await stack.until(done)

    assert len(await stack.follow_ups()) == 1
    [job] = [job for job in stack.jobs.retained_jobs() if job.job_id == job_id]
    assert await stack.collect(job) == "the resumed report"


@pytest.mark.asyncio
async def test_resume_control_delivered_again_after_its_job_is_gone_starts_nothing(stack: WorkflowStack) -> None:
    """The run remembers the event that took it: the same event resumes it once, whatever is kept of its job."""
    await _fail(stack)
    assert await _control(stack) is True
    await stack.until(lambda: stack.store.get_run(stack.run_id).attempt == 2)
    job_id = stack.store.get_run(stack.run_id).job_id
    await stack.end_attempt(ScriptRunState.FAILED, output="KeyError: 'tide'")
    kept = lambda: [job for job in stack.jobs.retained_jobs() if job.job_id == job_id]  # noqa: E731
    await stack.until(lambda: [job.status for job in kept()] == ["failed"])

    async def finished(_job: object) -> bool:
        return True

    await stack.jobs.expire_ended(now=datetime.now(UTC) + timedelta(days=365), source_finished=finished)
    assert kept() == []

    assert await _control(stack) is False
    assert stack.store.get_run(stack.run_id).attempt == 2


@pytest.mark.asyncio
async def test_resume_control_job_expires_through_coordinator(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ended job started by a Resume control event has no chat turn and expires after its retention period."""
    monkeypatch.setattr(tool_job_runtime, "_ENDED_JOB_RETENTION", timedelta(0))
    await _fail(stack)
    assert await _control(stack) is True
    await stack.until(lambda: stack.store.get_run(stack.run_id).attempt == 2)
    job_id = stack.store.get_run(stack.run_id).job_id
    await stack.finish_attempt(result="the resumed report")
    kept = lambda: [job for job in stack.jobs.retained_jobs() if job.job_id == job_id]  # noqa: E731
    await stack.until(lambda: [job.status for job in kept()] == ["completed"])

    await stack.coordinator._expire_ended_jobs()
    assert kept() == []
    assert await _control(stack) is False


@pytest.mark.asyncio
async def test_events_of_a_run_are_for_the_bot_that_serves_it(stack: WorkflowStack) -> None:
    """A run of a team's member is served by the team's bot, which gets its events: the member's bot need not be there."""
    await _fail(stack)
    stack.engine.served_by = lambda _run: "crew"

    assert await _control(stack) is False
    assert stack.store.get_run(stack.run_id).attempt == 1
    assert await _control(stack, agent_name="crew") is True
    await stack.until(lambda: stack.store.get_run(stack.run_id).attempt == 2)


@pytest.mark.parametrize(
    "event",
    [
        {"sender": "@bob:example.test"},
        {"room_id": "!elsewhere:example.test"},
        {"thread": "$other:example.test"},
        {"thread": None},
        {"agent_name": "helper"},
        {"action": "page"},
        {"action": "cancel"},
    ],
    ids=["another user", "another room", "another thread", "no thread", "another bot", "unknown action", "cancel"],
)
@pytest.mark.asyncio
async def test_any_other_control_changes_nothing(stack: WorkflowStack, event: dict[str, str]) -> None:
    """Only the requester's Resume, in the run's room and thread, to the run's agent does anything."""
    await _fail(stack)
    before = _standing(stack)

    assert await _control(stack, **event) is False  # type: ignore[arg-type]

    assert _standing(stack) == before


@pytest.mark.asyncio
async def test_resume_control_of_a_run_that_still_runs_or_is_unknown_changes_nothing(stack: WorkflowStack) -> None:
    """A Resume of a run that has not ended is refused as the tool's is, and one of no run names nothing."""
    before = _standing(stack)

    assert await _control(stack) is False
    assert _standing(stack) == before
    known, stack.run_id = stack.run_id, "wf-" + "0" * 32
    assert await _control(stack) is False
    stack.run_id = known


@pytest.mark.asyncio
async def test_stop_of_a_node_job_reads_the_job_runtime_and_cancels_that_call(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client's Stop that names the job of one agent of a run, which room state does not list, ends that agent."""
    monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(agent_nodes, "RECHECK_SECONDS", 0.05)
    stack.script_agents(monkeypatch)
    stack.child_script([Await(asyncio.Event().wait), *answer("never")])
    await stack.submit("@0", "agent", {"prompt": "Wait."})
    await stack.until(lambda: stack.store.get_node(stack.run_id, "@0").state is WorkflowNodeState.RUNNING)
    job = stack.node_job("@0")
    assert _jobs_state_content(stack.jobs.retained_jobs())["total"] == 1
    cancel = _event("io.mindroom.background_job_cancel", {"job_id": job.job_id}, sender=_ALICE, thread=None)

    await handle_job_cancel_event(
        nio.MatrixRoom(_ROOM, "@mindroom_watcher:example.test"),
        cancel,
        agent_name=stack.context.agent_name,
        runtime=SimpleNamespace(config=stack.context.config),
        runtime_paths=stack.context.runtime_paths,
        may_talk=lambda _sender, _room: True,
    )
    await stack.until(lambda: stack.node_job("@0").status == "cancelled")
    await stack.until(lambda: stack.store.get_node(stack.run_id, "@0").state is not WorkflowNodeState.RUNNING)

    node = stack.store.get_node(stack.run_id, "@0")
    assert (node.state, node.error["kind"]) == (WorkflowNodeState.CANCELLED, "cancelled")
    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING


@pytest.mark.asyncio
async def test_room_state_has_one_job_for_a_run_of_200_agents(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Room state, which is not encrypted, lists the run's job only, with no title, label or phase in it."""
    stack.script_agents(monkeypatch)
    gate = asyncio.Event()
    stack.child_script(*([Await(gate.wait), *answer("done")] for _ in range(200)))
    for index in range(200):
        meta = {"label": f"Secret label {index}", "phase": "Secret phase"}
        await stack.submit(f"@{index}", "agent", {"prompt": f"Look into part {index}."}, meta)
    await stack.until(lambda: len(stack.jobs.retained_jobs()) > 1)
    content = _jobs_state_content(stack.jobs.retained_jobs())
    run = stack.store.get_run(stack.run_id)
    [job] = [job for job in stack.jobs.retained_jobs() if job.job_id == run.job_id]

    assert content == {
        "jobs": [
            {
                "job_id": run.job_id,
                "thread_id": _THREAD,
                "kind": "workflow",
                "tool": "workflow",
                "status": "running",
                "started_at": job.created_at,
                "workflow": run.run_id,
            },
        ],
        "total": 1,
    }
    assert not any(word in json.dumps(content) for word in ("Secret", "invented", "Look into"))
    gate.set()
    await stack.engine.cancel(stack.run_id, reason="Done.")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected_status"),
    [
        (WorkflowRunState.CANCELLED, "cancelled"),
        (WorkflowRunState.COMPLETED, "completed"),
        (WorkflowRunState.FAILED, "failed"),
        (WorkflowRunState.INTERRUPTED, "interrupted"),
    ],
)
async def test_ended_workflow_run_settles_its_job_on_orderly_shutdown(
    stack: WorkflowStack,
    state: WorkflowRunState,
    expected_status: str,
) -> None:
    """A workflow run that already ended before a stop settles its job on shutdown instead of staying running."""
    stack.continues = lambda job: job.kind == "workflow"
    run = stack.store.get_run(stack.run_id)
    error = None if state is WorkflowRunState.COMPLETED else "ended"
    stack.store.update_run(run.run_id, run.epoch, state=state, error=error)

    await stack.restart(kill=False)

    assert stack.jobs.awaiting_continuation() == ()
    assert [job.status for job in stack.jobs.retained_jobs()] == [expected_status]
    assert _jobs_state_content(stack.jobs.retained_jobs()) == {"jobs": [], "total": 0}


@pytest.mark.asyncio
async def test_workflow_job_whose_run_ended_while_waiting_to_continue_clears_room_state_at_once(
    stack: WorkflowStack,
) -> None:
    """When a run ends while its job waits to continue, continuation settles the job before room state publishes."""
    stack.continues = lambda job: job.kind == "workflow"
    await stack.restart(kill=True)
    assert len(stack.jobs.awaiting_continuation()) == 1

    await stack.engine.cancel(stack.run_id, reason="The run reached its cost limit.")
    await stack.continue_jobs()

    # No event-loop yield: `_reconcile` publishes room state on the line right after `_continue_stopped_jobs`.
    assert [job.status for job in stack.jobs.retained_jobs()] == ["cancelled"]
    assert _jobs_state_content(stack.jobs.retained_jobs()) == {"jobs": [], "total": 0}


@pytest.mark.asyncio
async def test_resume_control_at_cost_limit_requires_higher_max_cost_usd(stack: WorkflowStack) -> None:
    """A Resume control on a cost-stopped run refuses without a higher limit and resumes when given one."""
    await _fail(stack)
    run = stack.store.get_run(stack.run_id)
    stack.store.update_run(run.run_id, run.epoch, when=frozenset({run.state}), max_cost_usd=1.0)
    stack.store.add_usage(run.run_id, "@0", 1, "turn-1", run.attempt, {"cost_usd": 1.2})

    assert await _control(stack) is False
    assert await _control(stack, max_cost_usd=1.2) is False
    assert stack.store.get_run(stack.run_id).attempt == 1

    assert await _control(stack, max_cost_usd=2.0) is True
    await stack.until(lambda: stack.store.get_run(stack.run_id).attempt == 2)
    assert stack.store.get_run(stack.run_id).max_cost_usd == 2.0
