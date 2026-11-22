"""What a client's control events do to a workflow run, and what room state lists of one (engine harness level)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest
import pytest_asyncio

from mindroom.custom_tools.workflow import handle_workflow_control
from mindroom.event_journal import EventKind
from mindroom.journal_dispatch import _BINDINGS
from mindroom.matrix.event_types import WORKFLOW_CONTROL_EVENT_TYPE
from mindroom.script_runs import manager as manager_module
from mindroom.script_runs.models import ScriptRunState
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
) -> bool:
    """Hand one control event that names the stack's run to an agent's bot, as its journal callback does."""
    event = _event(WORKFLOW_CONTROL_EVENT_TYPE, {"run": stack.run_id, "action": action}, sender=sender, thread=thread)
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
