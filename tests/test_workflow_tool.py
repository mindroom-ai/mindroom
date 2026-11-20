"""Tests for the `workflow` toolkit: the run's job, its admission, the inline path, ownership and cancellation.

Level: engine harness (`WorkflowStack`): the real stores, broker, engine and job runtime; the test plays the script.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agents import apply_tool_approval_capability
from mindroom.config.models import BackgroundToolJobsConfig, ModelConfig
from mindroom.custom_tools.dynamic_workflow import DynamicWorkflowTools
from mindroom.custom_tools.workflow import WorkflowTools
from mindroom.llm.runs import RunStatus
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.script_runs.manager import ScriptRunManagerError
from mindroom.script_runs.models import ScriptRunState
from mindroom.tool_jobs.execution import assembled_job_tools
from mindroom.tool_jobs.publication import _job_state
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import BackgroundJob
from mindroom.tool_system.agno_toolkits import adapt_toolkit, toolkit_function_names
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.tool_function import ToolGate
from mindroom.workflow_runs.models import WorkflowRunState
from tests.access_schema_support import with_responder_access
from tests.conftest import SETTLE_TIMEOUT_SECONDS
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.tool_job_helpers import (
    job_actor,
    resume_job_turn,
    run_job_turn,
    tool_job_journal,
    wait_for_status,
)
from tests.workflow_helpers import CallFailed, WorkflowStack, tool_args, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.tool_system.runtime_context import ToolRuntimeContext

SCRIPT = "def main(args):\n    return 'counted'\n"
_UNKNOWN = "No workflow run {} in your runs."


@pytest_asyncio.fixture
async def stack(tmp_path: Path) -> AsyncIterator[WorkflowStack]:
    """The engine harness with the `workflow` toolkit for its owner and the real job runtime."""
    with workflow_stack(tmp_path, extra_tools=("workflow",)) as stack:
        await stack.open_jobs()
        yield stack
        await stack.close()


def _other_requester(stack: WorkflowStack) -> ToolRuntimeContext:
    return replace(stack.context, requester_id="@bob:example.test")


async def _started(stack: WorkflowStack, **arguments: object) -> dict[str, str]:
    """Start a run as its job and return the handle, once its first attempt runs."""
    handle = json.loads(await stack.call("start_workflow", source=SCRIPT, **arguments))
    stack.run_id = handle["run_id"]
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING)
    return handle


async def _inline(stack: WorkflowStack, runs: int = 1, **options: object) -> asyncio.Task[str]:
    """Start a run as a delegated child's call, which waits for it; return the waiting call once the run runs."""
    call = asyncio.create_task(stack.call("start_workflow", depth=1, source=SCRIPT, **options))
    await stack.until(
        lambda: [run.state for run in stack.store.list_runs()].count(WorkflowRunState.RUNNING) == runs,
    )
    return call


async def _refused(stack: WorkflowStack, name: str, **options: object) -> str:
    """Return the sentence a refused call fails with."""
    with pytest.raises(CallFailed) as refusal:
        await stack.call(name, **options)
    return str(refusal.value)


async def _ended_job(stack: WorkflowStack, job_id: str) -> BackgroundJob:
    owner = stack.context.execution_identity
    async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
        return (await stack.jobs.wait(job_id, owner=owner, depth=0)).job


@pytest.mark.asyncio
async def test_start_returns_a_handle_and_its_job_delivers_the_whole_report(stack: WorkflowStack) -> None:
    """The call returns at once with the run's ID, and the job ends with the report in full, not a summary."""
    handle = await _started(stack, title="Count things")

    assert handle.keys() == {"job_id", "run_id", "status", "title"}
    assert handle["run_id"].startswith("wf-")
    assert (handle["status"], handle["title"]) == ("running", "Count things")
    await stack.finish_attempt(result="x" * 20_000)
    job = await _ended_job(stack, handle["job_id"])

    assert (job.kind, job.tool_name, job.status) == ("workflow", "workflow", "completed")
    assert job.adapter == {"workflow_job": handle["job_id"], "workflow": handle["run_id"]}
    assert len(str(await stack.collect(job))) == 20_000
    assert stack.store.get_run(handle["run_id"]).job_id == handle["job_id"]


@pytest.mark.asyncio
async def test_a_report_that_is_not_text_is_delivered_as_indented_json(stack: WorkflowStack) -> None:
    """A JSON report reaches the agent as JSON it can read."""
    handle = await _started(stack)
    await stack.finish_attempt(result={"counted": 3})

    assert await stack.collect(await _ended_job(stack, handle["job_id"])) == '{\n  "counted": 3\n}'


@pytest.mark.asyncio
async def test_refused_admission_starts_no_process_and_reserves_no_run(stack: WorkflowStack) -> None:
    """A job the runtime does not admit leaves nothing behind: the intent is deleted and no script starts."""
    stack.refuse_admission()

    with pytest.raises(CallFailed, match="not available"):
        await stack.call("start_workflow", source=SCRIPT)

    assert stack.scripts.launched == []
    assert stack.store.list_runs() == []


@pytest.mark.asyncio
async def test_a_start_cancelled_before_its_job_is_admitted_leaves_no_intent(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The intent is written before admission, so a call cancelled in between deletes it again."""

    async def cancelled(*_args: object, **_options: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(stack.jobs, "start", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await stack.call("start_workflow", source=SCRIPT)

    assert stack.store.list_runs() == []
    assert stack.scripts.launched == []


@pytest.mark.asyncio
async def test_zero_wait_handle_names_the_run_before_the_first_attempt_exists(stack: WorkflowStack) -> None:
    """The handle holds the run's ID while the script's process is still starting."""
    stack.scripts.hold_launch = asyncio.Event()

    handle = json.loads(await stack.call("start_workflow", source=SCRIPT))
    run = stack.store.get_run(handle["run_id"])

    assert (run.state, run.attempt, run.script_run_id) == (WorkflowRunState.STARTING, 0, None)
    assert run.origin_job_id == handle["job_id"]
    stack.scripts.hold_launch.set()
    await stack.until(lambda: stack.store.get_run(handle["run_id"]).state is WorkflowRunState.RUNNING)
    assert stack.store.get_run(handle["run_id"]).attempt == 1


@pytest.mark.asyncio
async def test_redelivered_call_reattaches_and_launches_once(stack: WorkflowStack) -> None:
    """The same call delivered twice is one job and one run with one process."""
    first = await _started(stack, call_id="call-1")
    second = json.loads(await stack.call("start_workflow", source=SCRIPT, call_id="call-1"))

    assert first["run_id"] == second["run_id"]
    assert first["job_id"] == second["job_id"]
    assert len(stack.scripts.launched) == 1
    assert len(stack.store.list_runs()) == 1


@pytest.mark.asyncio
async def test_require_approval_rule_launches_only_after_the_user_approves(tmp_path: Path) -> None:
    """An operator's approval rule gates the call like any other: no intent and no process before the approval."""
    with workflow_stack(tmp_path, extra_tools=("workflow",), require_approval=("start_workflow",)) as stack:
        await stack.open_jobs()
        context = stack.context
        toolkit = WorkflowTools(context.config)
        offered = apply_tool_approval_capability(
            adapt_toolkit(toolkit, key="workflow"),
            toolkit,
            context.config,
            tools=context.plugins.tools,
            registered_tool_name="workflow",
            resumable=True,
            approvals_as_jobs=True,
        )
        tools = assembled_job_tools(((toolkit, function) for function in offered.functions), {})
        actor = job_actor(*tools.values(), name=context.agent_name)
        session_id = context.execution_identity.session_id or "session"
        async with execution_resources():
            with tool_runtime_context(context):
                model = ScriptedModel([calls(tool_call("start_workflow", "call-1", source=SCRIPT))])
                paused = await run_job_turn(actor, model, session_id=session_id)

                # The reply pauses for the approval, so the toolkit's job hook has not been asked yet.
                assert tools["start_workflow"].function.gate is ToolGate.POLICY
                assert paused.status is RunStatus.paused
                assert stack.scripts.launched == []
                assert stack.store.list_runs() == []

                resumed = await resume_job_turn(actor, ScriptedModel([answer("Started.")]), paused)

        assert resumed.status is RunStatus.completed
        await stack.until(lambda: [run.state for run in stack.store.list_runs()] == [WorkflowRunState.RUNNING])
        assert len(stack.scripts.launched) == 1
        await stack.close()


@pytest.mark.asyncio
async def test_a_child_agent_gets_the_report_inline(stack: WorkflowStack) -> None:
    """A delegated child cannot start a job, so its call waits and returns the report itself."""
    call = await _inline(stack)
    [run] = stack.store.list_runs()
    stack.run_id = run.run_id
    await stack.finish_attempt(result="the report")

    assert await asyncio.wait_for(call, SETTLE_TIMEOUT_SECONDS) == "the report"
    assert await stack.jobs.list_jobs(owner=stack.context.execution_identity, depth=1) == []
    assert run.origin_job_id is not None
    assert run.origin_job_id.startswith("inline:")
    assert stack.store.get_run(run.run_id).job_id is None


@pytest.mark.asyncio
async def test_two_requesters_with_the_same_tool_call_id_get_their_own_runs_and_reports(stack: WorkflowStack) -> None:
    """A provider's call ID alone is no key: the same ID from two requesters reserves two runs."""
    alice = await _inline(stack, call_id="call-1")
    bob = await _inline(stack, runs=2, call_id="call-1", context=_other_requester(stack))
    runs = {run.owner_user_id: run for run in stack.store.list_runs()}

    assert len(runs) == 2
    for requester, report in (("@alice:example.test", "for Alice"), ("@bob:example.test", "for Bob")):
        stack.run_id = runs[requester].run_id
        await stack.finish_attempt(result=report)
    assert await asyncio.wait_for(alice, SETTLE_TIMEOUT_SECONDS) == "for Alice"
    assert await asyncio.wait_for(bob, SETTLE_TIMEOUT_SECONDS) == "for Bob"


@pytest.mark.asyncio
async def test_a_repeated_inline_call_with_another_script_is_refused_and_tells_nothing_of_the_run(
    stack: WorkflowStack,
) -> None:
    """A reservation hit must match the script and arguments; a mismatch returns nothing of the run it hit."""
    first = await _inline(stack, call_id="call-1")
    [run] = stack.store.list_runs()

    other = await _refused(stack, "start_workflow", depth=1, call_id="call-1", source=SCRIPT + "# changed\n")

    assert "another owner, script or arguments" in other
    assert len(stack.scripts.launched) == 1
    stack.run_id = run.run_id
    await stack.finish_attempt(result="the first report")
    assert "the first report" not in other
    assert await asyncio.wait_for(first, SETTLE_TIMEOUT_SECONDS) == "the first report"


@pytest.mark.asyncio
async def test_an_inline_call_that_is_cancelled_cancels_its_run(stack: WorkflowStack) -> None:
    """Nobody would receive the report of a run whose waiting call went away, so the run stops with it."""
    call = await _inline(stack)
    [run] = stack.store.list_runs()

    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    assert stack.store.get_run(run.run_id).state is WorkflowRunState.CANCELLED
    assert stack.script_store.get_run(stack.scripts.launched[0].run_id).state is ScriptRunState.CANCELLED


@pytest.mark.asyncio
async def test_background_jobs_off_is_refused_in_one_sentence(tmp_path: Path) -> None:
    """A top-level call needs the job runtime: with it off, or with `workflow` excluded, the call says what to set."""
    refusal = "Workflows need background tool jobs. Set background_tool_jobs.enabled: true and do not exclude workflow."
    with workflow_stack(tmp_path, extra_tools=("workflow",)) as stack:
        stack.context.config.background_tool_jobs = BackgroundToolJobsConfig(enabled=False)
        assert await _refused(stack, "start_workflow", source=SCRIPT) == refusal

        stack.context.config.background_tool_jobs = BackgroundToolJobsConfig(
            enabled=True,
            exclude_toolkits=["workflow"],
        )
        await stack.open_jobs()
        assert await _refused(stack, "start_workflow", source=SCRIPT) == refusal

        assert stack.store.list_runs() == []
        assert stack.scripts.launched == []
        await stack.close()


@pytest.mark.asyncio
async def test_another_requesters_run_id_reads_as_unknown(stack: WorkflowStack) -> None:
    """Another requester's run answers exactly like an ID that does not exist, and is not cancelled."""
    handle = await _started(stack)
    bob = _other_requester(stack)
    unknown = _UNKNOWN.format(handle["run_id"])

    assert await _refused(stack, "get_workflow", context=bob, run_id=handle["run_id"]) == unknown
    assert await _refused(stack, "cancel_workflow", context=bob, run_id=handle["run_id"]) == unknown
    missing = "wf-" + "0" * 32
    assert await _refused(stack, "get_workflow", run_id=missing) == _UNKNOWN.format(missing)
    assert stack.store.get_run(handle["run_id"]).state is WorkflowRunState.RUNNING


@pytest.mark.asyncio
async def test_another_agents_run_id_reads_as_unknown(stack: WorkflowStack) -> None:
    """A run belongs to the agent that started it, for the same requester too."""
    handle = await _started(stack)
    other_agent = replace(stack.context, agent_name="reviewer")
    unknown = _UNKNOWN.format(handle["run_id"])

    assert await _refused(stack, "get_workflow", context=other_agent, run_id=handle["run_id"]) == unknown
    assert await _refused(stack, "cancel_workflow", context=other_agent, run_id=handle["run_id"]) == unknown
    assert stack.store.get_run(handle["run_id"]).state is WorkflowRunState.RUNNING


def test_jobs_state_names_the_run_and_carries_no_title(stack: WorkflowStack) -> None:
    """Room state is not encrypted: a workflow job's entry holds IDs only."""
    run_id = "wf-" + "c" * 32
    job = BackgroundJob(
        job_id="job-1",
        owner=stack.context.execution_identity,
        tool_name="workflow",
        depth=0,
        kind="workflow",
        toolkit_name="workflow",
        adapter={"workflow_job": "job-1", "workflow": run_id},
    )

    assert _job_state(job) == {
        "job_id": "job-1",
        "thread_id": "$thread:example.test",
        "kind": "workflow",
        "tool": "workflow",
        "status": "running",
        "started_at": job.created_at,
        "workflow": run_id,
    }


@pytest.mark.asyncio
async def test_start_needs_exactly_one_source(stack: WorkflowStack) -> None:
    """Neither or both of `source` and `path` start nothing."""
    one = "Give exactly one of source or path."

    assert await _refused(stack, "start_workflow") == one
    assert await _refused(stack, "start_workflow", source=SCRIPT, path="count.py") == one
    assert await _refused(stack, "start_workflow", depth=1, source=SCRIPT, path="count.py") == one
    assert stack.store.list_runs() == []


@pytest.mark.asyncio
async def test_path_reads_a_workspace_file_and_nothing_outside_the_workspace(stack: WorkflowStack) -> None:
    """`path` names a file in the agent's workspace, read as script sources are."""
    workspace = stack.context.agent_workspace_root(create=True)
    (workspace / "count.py").write_text(SCRIPT, encoding="utf-8")

    handle = json.loads(await stack.call("start_workflow", path="count.py"))
    outside = await _refused(stack, "start_workflow", path="../count.py")

    assert stack.store.get_run(handle["run_id"]).source == SCRIPT
    assert "within the workspace" in outside
    assert len(stack.store.list_runs()) == 1


@pytest.mark.asyncio
async def test_start_is_refused_where_the_operator_lets_no_code_run(stack: WorkflowStack) -> None:
    """Without a worker and without the operator's switch for shell on this machine, no run is reserved."""
    stack.context.config.defaults.allow_shell_without_worker = False

    refusal = await _refused(stack, "start_workflow", source=SCRIPT)

    assert refusal.startswith("Workflows run code. Route this agent to a worker")
    assert stack.store.list_runs() == []


@pytest.mark.asyncio
async def test_cancelling_the_job_cancels_the_run(stack: WorkflowStack) -> None:
    """Every cancel path of a job reaches the run: its attempt is revoked and the run ends cancelled."""
    handle = await _started(stack)
    owner = stack.context.execution_identity

    await stack.jobs.cancel(handle["job_id"], owner=owner, depth=0)
    await wait_for_status(stack.jobs, handle["job_id"], "cancelled")

    assert stack.store.get_run(handle["run_id"]).state is WorkflowRunState.CANCELLED
    assert stack.script_store.get_run(stack.scripts.launched[0].run_id).state is ScriptRunState.CANCELLED


@pytest.mark.asyncio
async def test_a_stop_of_the_reply_that_started_the_run_does_not_reach_its_job(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run's job names no reply, so it outlives the one that started it and a later Stop of that reply."""
    span = SimpleNamespace(reply_id="reply-1", reply=SimpleNamespace(stop_receipt_order=3), stopped=lambda: False)
    monkeypatch.setattr("mindroom.tool_jobs.execution.current_span", lambda: span)

    handle = await _started(stack)
    await stack.jobs.cancel_stopped_reply("reply-1")
    [job] = await stack.jobs.list_jobs(owner=stack.context.execution_identity, depth=0)

    assert (job.job_id, job.reply_id, job.reply_stop_order) == (handle["job_id"], None, None)
    assert job.status == "running"
    assert stack.store.get_run(handle["run_id"]).state is WorkflowRunState.RUNNING


@pytest.mark.asyncio
async def test_a_job_cancelled_while_its_attempt_launches_leaves_no_running_attempt(stack: WorkflowStack) -> None:
    """A launch in progress is finished, not dropped, so the cancel finds the attempt and stops it."""
    stack.scripts.hold_launch = asyncio.Event()
    handle = json.loads(await stack.call("start_workflow", source=SCRIPT))
    await stack.until(lambda: len(stack.scripts.launched) == 1)
    owner = stack.context.execution_identity

    await stack.jobs.cancel(handle["job_id"], owner=owner, depth=0)
    stack.scripts.hold_launch.set()
    await wait_for_status(stack.jobs, handle["job_id"], "cancelled")

    assert stack.store.get_run(handle["run_id"]).state is WorkflowRunState.CANCELLED
    assert stack.script_store.get_run(stack.scripts.launched[0].run_id).state is ScriptRunState.CANCELLED


@pytest.mark.asyncio
async def test_cancel_workflow_ends_the_run_and_its_job(stack: WorkflowStack) -> None:
    """The agent's own cancel ends the run, and the run's job ends cancelled with it."""
    handle = await _started(stack)

    answer_text = json.loads(await stack.call("cancel_workflow", run_id=handle["run_id"]))
    job = await _ended_job(stack, handle["job_id"])

    assert answer_text == {"run_id": handle["run_id"], "status": "cancelled"}
    assert job.status == "cancelled"
    assert stack.script_store.get_run(stack.scripts.launched[0].run_id).state is ScriptRunState.CANCELLED


@pytest.mark.asyncio
async def test_a_shutdown_interrupts_the_job_and_does_not_cancel_the_run(stack: WorkflowStack) -> None:
    """A stopping primary is no cancel: the job ends interrupted and the run is left for the restart to judge."""
    handle = await _started(stack)

    await stack.jobs.quiesce()

    job = await _ended_job(stack, handle["job_id"])
    assert job.status == "interrupted"
    assert stack.store.get_run(handle["run_id"]).state is WorkflowRunState.RUNNING
    assert stack.attempt.cancel_requested_at is None


@pytest.mark.asyncio
async def test_failed_run_delivers_the_error_tail(stack: WorkflowStack) -> None:
    """A failed run's job fails with the end of the script's traceback, so the agent can fix the script."""
    handle = await _started(stack)
    traceback = "Traceback (most recent call last):\n" + "x" * 6000 + "\nKeyError: 'tide'"

    await stack.end_attempt(ScriptRunState.FAILED, output=traceback)
    job = await _ended_job(stack, handle["job_id"])

    delivered = str(await stack.collect(job))
    assert job.status == "failed"
    assert delivered.endswith("KeyError: 'tide'")
    assert len(delivered.encode()) == 4096


@pytest.mark.asyncio
async def test_a_launch_that_is_refused_fails_the_run_and_its_job_with_the_reason(stack: WorkflowStack) -> None:
    """An admitted job whose script cannot start ends failed with why, and its run is not left starting."""
    stack.scripts.refuse_launch = ScriptRunManagerError("The worker backend refused the script.")

    handle = json.loads(await stack.call("start_workflow", source=SCRIPT))
    job = await _ended_job(stack, handle["job_id"])
    run = stack.store.get_run(handle["run_id"])

    assert job.status == "failed"
    assert await stack.collect(job) == "The worker backend refused the script."
    assert (run.state, run.attempt, run.error) == (WorkflowRunState.FAILED, 0, "The worker backend refused the script.")


@pytest.mark.asyncio
async def test_ninth_top_level_run_is_refused_and_nested_runs_do_not_count(stack: WorkflowStack) -> None:
    """Eight live top-level runs of one requester and agent are the limit; a run inside another is not one of them."""
    for _ in range(7):
        await _started(stack)
    nested = await _inline(stack, runs=8)
    [inline] = [run for run in stack.store.list_runs() if (run.origin_job_id or "").startswith("inline:")]
    stack.store.update_run(inline.run_id, parent_run_id=stack.run_id, parent_node_key="@0")

    eighth = await _started(stack)
    ninth = await _refused(stack, "start_workflow", source=SCRIPT)

    assert ninth == "You have 8 workflow runs in progress, the limit. Wait for one to end or cancel one."
    assert len(stack.scripts.launched) == 9
    await stack.call("cancel_workflow", run_id=eighth["run_id"])
    assert json.loads(await stack.call("start_workflow", source=SCRIPT))["run_id"].startswith("wf-")
    nested.cancel()


@pytest.mark.asyncio
async def test_get_workflow_reports_the_state_counts_failed_calls_and_nodes(stack: WorkflowStack) -> None:
    """One read tells the agent where the run stands and which calls failed, and `nodes` picks which calls it lists."""
    handle = await _started(stack, title="Count things")
    await stack.result("@0", "tool", tool_args("counter", "text", size=3))
    with pytest.raises(Exception, match="The counter failed"):
        await stack.result("@1", "tool", tool_args("counter", "fail"))

    status = json.loads(await stack.call("get_workflow", run_id=handle["run_id"]))
    everything = json.loads(await stack.call("get_workflow", run_id=handle["run_id"], nodes="all"))
    failed = json.loads(await stack.call("get_workflow", run_id=handle["run_id"], nodes="failed"))

    assert status.keys() == {"run_id", "title", "status", "attempt", "counts", "usage", "waiting", "failed", "nodes"}
    assert (status["title"], status["status"], status["attempt"]) == ("Count things", "running", 1)
    assert status["counts"] == {"completed": 1, "failed": 1}
    assert status["waiting"] == []
    assert status["nodes"] == []
    [failure] = status["failed"]
    assert (failure["id"], failure["label"], failure["error"]["kind"]) == ("n2", "counter.fail", "failed")
    assert [(node["id"], node["kind"], node["status"]) for node in everything["nodes"]] == [
        ("n1", "tool", "completed"),
        ("n2", "tool", "failed"),
    ]
    assert [node["id"] for node in failed["nodes"]] == ["n2"]


@pytest.mark.usefixtures("enforce_turn_authorization")
def test_a_workflow_job_is_granted_while_the_agent_has_the_toolkit_and_the_requester_has_access(
    stack: WorkflowStack,
) -> None:
    """The job kind's own check: the owner agent still has `workflow`, and the requester may still talk to it."""
    config = with_responder_access(stack.context.config, "watcher", users=["@alice:example.test"])
    paths = stack.context.runtime_paths
    coordinator = ToolJobRuntimeCoordinator(
        runtime_paths=paths,
        config_provider=lambda: config,
        bot_provider=lambda _name: None,
        agent_reply_memberships=AgentReplyMembershipIndex(),
        journal_provider=partial(tool_job_journal, paths.storage_root),
    )
    job = BackgroundJob(
        job_id="job-1",
        owner=stack.context.execution_identity,
        tool_name="workflow",
        depth=0,
        kind="workflow",
        toolkit_name="workflow",
        adapter={"workflow_job": "job-1", "workflow": "wf-" + "c" * 32},
    )

    assert coordinator._grant(job) == "allowed"
    assert coordinator._grant(replace(job, owner=replace(job.owner, requester_id="@bob:example.test"))) == "denied"
    config.agents["watcher"].tools = ["counter", "shell"]
    assert coordinator._grant(job) == "denied"


def test_description_lists_the_configured_models(stack: WorkflowStack) -> None:
    """The description ends with the models this deployment has, as `run_subagent`'s does, and teaches the SDK."""
    config = stack.context.config
    levels = {"levels": ["low", "high"], "default": "low"}
    config.models = {
        "opus": ModelConfig(provider="anthropic", id="test-opus", reasoning=levels),
        "haiku": ModelConfig(provider="anthropic", id="test-haiku"),
    }
    toolkit = WorkflowTools(config)
    functions = {function.name: function for function in adapt_toolkit(toolkit, key="workflow").functions}
    description = functions["start_workflow"].description or ""

    assert description.startswith("Run a workflow: a Python script")
    assert "Models: haiku, opus (add an effort as in opus@high)." in description
    assert "shell() returns at most the last 50 KiB of output; redirect to a file and read it for more." in description
    assert "    from mindroom.workflow import shell, tool, phase, log" in description
    assert set(functions["start_workflow"].parameters["properties"]) == {
        "source",
        "path",
        "args",
        "title",
        "max_concurrent",
        "resource_profile",
    }
    assert "Stop a run" in (functions["cancel_workflow"].description or "")


def test_no_function_name_is_shared_with_dynamic_workflow(stack: WorkflowStack) -> None:
    """An agent can hold both toolkits while it migrates."""
    names = set(toolkit_function_names(WorkflowTools(stack.context.config)))

    assert names == {"start_workflow", "get_workflow", "cancel_workflow"}
    assert names.isdisjoint(toolkit_function_names(DynamicWorkflowTools()))
