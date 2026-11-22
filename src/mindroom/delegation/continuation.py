"""Continue the child turn a restart cut short in a subagent job, as the follow-up its caller would send."""

from __future__ import annotations

import re
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any

from mindroom.ai import run_delegated_child_response
from mindroom.delegation.background import delegation_child
from mindroom.delegation.execution import delegation_call_job
from mindroom.delegation.lifecycle import (
    authorize_delegation,
    delegation_grant,
    original_prompt,
    prepare_child_turn,
    workflow_call_prompt,
)
from mindroom.delegation.personas import follow_up_refusal
from mindroom.delegation.recovery import paused_for_approval, read_child_run, settled_subagent
from mindroom.delegation.sessions import SubagentSessionError
from mindroom.delegation.state import (
    APPROVAL_TOOLKITS_KEY,
    CONTINUE_SUBAGENT,
    RESTART_INTERRUPTION_REASON,
    SPENT_RUNS_KEY,
    interrupted_by_restart,
)
from mindroom.history.interrupted_replay import render_stopped_attempt
from mindroom.history.run_checkpoint import INTERRUPTED_CALL_RESULT, is_recovered_run
from mindroom.tool_jobs.approvals import supersede_job_approvals
from mindroom.tool_jobs.runtime import waited_for_approval
from mindroom.tool_system.events import format_tool_completed_event
from mindroom.tool_system.output_files import OUTPUT_PATH_ARGUMENT
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import require_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import ChildResponseRunner, DelegationChild
    from mindroom.llm.runs import RunRecord
    from mindroom.tool_jobs.runtime import BackgroundJob, JobSpec, ToolJobRuntime
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

# How many of a job's continuations its saved adapter counts, so the count survives further restarts.
_CONTINUATIONS_KEY = "restart_continuations"
# What the job's latest continuation was sent in place of its task, saved with the job and nowhere a room reads.
_PROMPT_KEY = "continuation_prompt"
# What stands in a saved continuation prompt for the prompt of the workflow run's call, which the run keeps.
_CALL_PROMPT_MARK = "<<mindroom:workflow-call-prompt>>"
# How many restarts may cut one subagent job short before the next one ends it, in case continuing is what stops it.
_RESTART_CONTINUATION_LIMIT = 2
# How much of what the cut-short turn was sent the continuation prompt repeats.
_MAX_TASK_CHARACTERS = 8_000
# The call of a workflow run's agent that starts a run inside that run, which is never the model's to make again.
_START_WORKFLOW = "start_workflow"
# What stands in a continuation prompt for the report of such a run, until the job has waited for the run.
_RUN_REPORT_MARK = "<<mindroom:workflow-run-report:{}>>"
_RUN_REPORT_MARKS = re.compile(r"<<mindroom:workflow-run-report:(wf-[0-9a-f]{32})>>")
# What the job's saved adapter says under `wait` while it waits for such runs, before its turn begins.
_WAITS_FOR_RUN = "run"
_NESTED_RESULT = (
    "Your `start_workflow` call was not lost in the restart: the workflow run it started went on and has ended. "
    "Do not call start_workflow for it again. This is the result of that call:\n"
    "<start_workflow_result>\n{}\n</start_workflow_result>"
)


async def continued_subagent_job(job: BackgroundJob, runtime: ToolJobRuntime) -> JobSpec | str:
    """Return what subagent job ``job`` runs on as after a restart cut its child's turn short, or why it ends instead.

    Called in the tool context of the job's conversation, as a reply's ``continue_subagent`` call is. The child's
    handle is settled first, so this writes state. The job continues only from a turn the restart settled: a turn
    that ended on its own stands, and a turn left paused, for an approval or a nested subagent, holds no run history
    replays. The continuation is a follow-up turn of the same subagent, which the caller's delegation and the
    requester's access must still allow. Its task, which the room shows as the turn's label, stays the cut-short
    turn's; the child is sent the continuation prompt instead (`_continuation_prompt`), after its history, which holds
    the cut-short run's own messages as far as its history limits replay them. The prompt is built here once and
    saved with the job: only this turn's first response starts on it, and a restart that stops the turn before its
    first call sends it again.

    Only the call of a workflow run is kept from a wait for approvals (`waited_for_approval`), and no restart cut
    that short. Its turn is still paused when the stop left it so: the job then resumes that same turn, asking about
    its calls on the cards they have, with the decisions those hold. A turn the stop settled instead goes on as a
    follow-up, as above, after the cards it left open are closed: it asks again for what it still needs.
    """
    context = require_tool_runtime_context()
    config, runtime_paths, owner = context.current_config, context.runtime_paths, job.owner
    continuations = job.adapter.get(_CONTINUATIONS_KEY, 0)
    # A job that waited for a run inside its agent's run had not begun its turn: no restart cut that turn short.
    for_run = job.adapter.get("wait") == _WAITS_FOR_RUN
    if continuations >= _RESTART_CONTINUATION_LIMIT and not (for_run or waited_for_approval(job)):
        return f"restarts already cut it short {continuations + 1} times"
    child = await _stopped_turn(job, config, runtime_paths)
    if isinstance(child, str):
        return child
    sent = await _sent_or_resumed(job, child, config, runtime_paths)
    if isinstance(sent, str):
        return sent
    prompt, paused = sent
    tools = config_plugins(config, runtime_paths).tools
    refusal = follow_up_refusal(child.persona, owner.agent_name, config, tools, delegation_depth=job.depth)
    if refusal is not None:
        return refusal
    authorized = authorize_delegation(
        owner.agent_name,
        child.child_agent_name,
        prompt,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=owner,
        depth=job.depth,
        grant=delegation_grant(child),
    )
    if isinstance(authorized, str):
        return authorized
    follow_up = (
        child
        if paused
        else prepare_child_turn(
            owner.agent_name,
            child.child_agent_name,
            child.task,
            owner=owner,
            config=authorized,
            runtime_paths=runtime_paths,
            depth=job.depth,
            budget_monitor=context.budget_monitor,
            model=None,
            agent_mode=child.agent_mode,
            previous=child,
            persona=child.persona,
            parent_tool_call_id=child.parent_tool_call_id,
        )
    )
    # The continuation is a turn of the same call of a workflow run, which no other follow-up of its agent is.
    follow_up.workflow_run_id, follow_up.workflow_node_key, follow_up.workflow_generation = (
        child.workflow_run_id,
        child.workflow_node_key,
        child.workflow_generation,
    )
    arguments = {"subagent_id": child.subagent_id, "message": child.task}
    if OUTPUT_PATH_ARGUMENT in job.adapter:
        arguments[OUTPUT_PATH_ARGUMENT] = job.adapter[OUTPUT_PATH_ARGUMENT]
    orchestrator = context.orchestrator
    spec = delegation_call_job(
        follow_up,
        job_id=job.job_id,
        runtime=runtime,
        owner=owner,
        parent_run_id=None,
        tool_name=CONTINUE_SUBAGENT,
        arguments=arguments,
        run_child=run_delegated_child_response
        if child.workflow_run_id is None
        else _after_nested_runs(child, context, partial(runtime.save_adapter, job.job_id)),
        prompt=prompt,
        config=authorized,
        runtime_paths=runtime_paths,
        refresh_scheduler=None if orchestrator is None else orchestrator.knowledge_refresh_scheduler,
        paused=paused,
    )
    if not isinstance(spec, str) and paused:
        # The same turn goes on, so the job keeps what it had saved beside its child.
        spec.adapter.update({key: value for key, value in job.adapter.items() if key != "child"})
    elif not isinstance(spec, str):
        spec.adapter.update({_CONTINUATIONS_KEY: continuations + (not for_run), _PROMPT_KEY: prompt})
        if "workflow" in job.adapter:
            # The job stays the call of its workflow run, which reads what each run of the call used and did.
            spec.adapter.update({key: job.adapter[key] for key in ("workflow", "node", "generation")})
            spec.adapter[SPENT_RUNS_KEY] = [*dict.fromkeys([*job.adapter.get(SPENT_RUNS_KEY, ()), child.run_id])]
            # The run keeps the call's prompt, so the job, whose every snapshot repeats its adapter, keeps a mark
            # in its place.
            asked = workflow_call_prompt(child) or _CALL_PROMPT_MARK
            spec.adapter[_PROMPT_KEY] = prompt.replace(asked, _CALL_PROMPT_MARK)
    return spec


async def _sent_or_resumed(
    job: BackgroundJob,
    child: DelegationChild,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[str, tuple[RunRecord, Sequence[Sequence[str]]] | None] | str:
    """Return what the job's next turn is sent, or the paused turn it resumes and its task, or why it does neither.

    A paused turn comes with the owners of its calls, which the job saved when it asked about them. A job that
    waited for approvals and whose turn was settled has cards open that no turn waits for any more: they are closed.
    """
    if child.status == "paused":
        owners, run = job.adapter.get(APPROVAL_TOOLKITS_KEY), await paused_for_approval(child, config, runtime_paths)
        return "its paused turn cannot be resumed" if owners is None or run is None else (child.task, (run, owners))
    if waited_for_approval(job):
        await supersede_job_approvals(job)
    prompt = await _continuation_prompt(job, child, config, runtime_paths)
    if prompt is None:
        return "its turn's run holds calls recovery could not finish, as a run paused for a decision or a subagent does"
    return prompt, None


def _after_nested_runs(
    child: DelegationChild,
    context: ToolRuntimeContext,
    save: Callable[..., Awaitable[None]],
) -> ChildResponseRunner:
    """Return what runs the continued turn of a workflow run's agent: it waits for the runs its prompt names first.

    Each mark of a run inside the agent's run is replaced by what that run delivers, as the call that started it
    would have returned it. The saved prompt keeps the marks, and the job says that it waits (*save*), so a restart
    during the wait finds a turn that had not begun and sends it the same prompt, to wait again.
    """
    outer = (child.workflow_run_id, child.workflow_node_key, child.workflow_generation)

    async def run(turn: DelegationChild, *, prompt: str, **options: Any) -> str:  # noqa: ANN401 - the runner's own.
        engine = context.orchestrator.workflow_engine if context.orchestrator is not None else None
        waited_for = dict.fromkeys(_RUN_REPORT_MARKS.findall(prompt) if engine is not None else ())
        if waited_for:
            await save(wait=_WAITS_FOR_RUN)
            for run_id in waited_for:
                # Text of the task that only looks like a mark names no run inside this call, and stays.
                if (report := await engine.nested_report(outer, run_id)) is not None:
                    prompt = prompt.replace(_RUN_REPORT_MARK.format(run_id), report)
            await save(wait=None)
        return await run_delegated_child_response(turn, prompt=prompt, **options)

    return run


async def _nested_runs(child: DelegationChild, run: RunRecord) -> dict[str, str] | None:
    """Return the run each cut-short `start_workflow` call of a workflow agent's turn provably started, by call ID.

    Empty for a turn in which no such call was cut short, which goes on as any turn does. None when one of them
    has no proof (`WorkflowEngine.nested_call`), or when a call of another tool was cut short beside them: then
    nobody can say what the turn did, and it does not go on.
    """
    cut_short = [tool for tool in run.tools or () if tool.result == INTERRUPTED_CALL_RESULT]
    if child.workflow_run_id is None or all(tool.tool_name != _START_WORKFLOW for tool in cut_short):
        return {}
    context = require_tool_runtime_context()
    if context.orchestrator is None:
        return None
    outer = (child.workflow_run_id, child.workflow_node_key, child.workflow_generation)
    prove = partial(context.orchestrator.workflow_engine.nested_call, context, outer)
    found: dict[str, str] = {}
    for tool in cut_short:
        proven = await prove(tool.tool_call_id, tool.tool_args or {}) if tool.tool_name == _START_WORKFLOW else None
        if proven is None:
            return None
        found[tool.tool_call_id] = proven
    return found


async def _stopped_turn(job: BackgroundJob, config: Config, runtime_paths: RuntimePaths) -> DelegationChild | str:
    """Return the settled child turn a restart cut short under ``job``, or why no such turn is its current one."""
    saved = delegation_child(job)
    try:
        child = await settled_subagent(
            saved.subagent_id,
            owner=job.owner,
            config=config,
            runtime_paths=runtime_paths,
            depth=job.depth,
        )
    except SubagentSessionError as error:
        return str(error)
    # The handle names the turn the job ran. A job that saved a continuation may find the turn before it instead: a
    # second restart stopped it after that save and before the continuation reserved its turn. A manual follow-up's
    # job names a previous turn too, which is not its own to continue.
    continued = _CONTINUATIONS_KEY in job.adapter
    if child.delegation_id not in (saved.delegation_id, saved.previous_delegation_id if continued else None):
        return "its subagent is on another turn than the one the job ran"
    # Only the call of a workflow run waits here from a wait for approvals, with the turn that asked still paused.
    if not interrupted_by_restart(child) and not (child.status == "paused" and waited_for_approval(job)):
        return f"its turn ended {child.status} before the restart settled it"
    if child.workflow_run_id is not None and workflow_call_prompt(child) is None:
        return "the call of its workflow run has an outcome already, or its run is gone"
    return child


async def _continuation_prompt(
    job: BackgroundJob,
    child: DelegationChild,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Return what the child is sent to continue cut-short turn ``child``, or None for a run that cannot continue.

    A continuation that was prepared and never reserved its turn is sent what was prepared. A run with calls that
    recovery did not turn into history cannot continue. A turn that called no tool left nothing in history and is sent
    again what it was sent: its task, or the prompt of the continuation it was. Any other turn is sent the
    continuation note, what it was sent, and an account of its run's calls: the child's history limits apply to the
    recovered run as to any turn, and may leave out both. A `start_workflow` call of a workflow run's agent that was
    cut short is not left to the model: the run it started is named by a mark that the job replaces with that run's
    report once it has waited for it (`_after_nested_runs`), and without proof of that run the turn does not go on.
    """
    sent = job.adapter.get(_PROMPT_KEY) or original_prompt(child)
    if child.workflow_run_id is not None:
        sent = sent.replace(_CALL_PROMPT_MARK, original_prompt(child))
    if child.delegation_id != delegation_child(job).delegation_id:
        return sent
    run = await read_child_run(child, config, runtime_paths)
    if run is None and child.workflow_run_id is not None:
        # The call of a workflow run goes on only on proof that its turn called no tool, which no run is not. A job
        # that still waited for the runs its prompt names is: its turn had not begun.
        return sent if job.adapter.get("wait") == _WAITS_FOR_RUN else None
    if run is None or not is_recovered_run(run):
        # The turn of a workflow run's agent that waited for approvals was settled as it stood: the calls it asked
        # about never ran, and it goes on while no other call of it is left without an end.
        results = [tool.result for tool in run.tools or ()] if run is not None else []
        waited = child.workflow_run_id is not None and RESTART_INTERRUPTION_REASON in results and None not in results
        if not waited:
            return None if run is not None and run.tools else sent
    nested = await _nested_runs(child, run)
    if nested is None:
        return None
    # The prompt of a workflow run's call is sent whole: what it asks for may stand at its end.
    clip = len(sent) > _MAX_TASK_CHARACTERS and child.workflow_run_id is None
    task = f"{sent[:_MAX_TASK_CHARACTERS]}…" if clip else sent
    note = config.get_prompt("SUBAGENT_RESTART_CONTINUATION_PROMPT")
    account = _calls_account(run, nested) if len(run.tools or ()) > len(nested) else ""
    results = [_NESTED_RESULT.format(_RUN_REPORT_MARK.format(run_id)) for run_id in dict.fromkeys(nested.values())]
    parts = (note, f"<interrupted_task>\n{task}\n</interrupted_task>", account, *results)
    return "\n\n".join(part for part in parts if part)


def _calls_account(run: RunRecord, nested: dict[str, str]) -> str:
    """Render the cut-short run's calls as the bounded account a replayed reply's prompt gets of its stopped attempt.

    The child's history replays the recovered run like any other turn, so its limits may leave the run's calls out;
    the account names each call from its preview either way, and which ones were cut short. The calls in *nested*
    started runs whose reports the prompt gives, so they are no calls to check.
    """
    finished, cut_short = [], []
    for tool in run.tools or ():
        if tool.result == RESTART_INTERRUPTION_REASON or tool.tool_call_id in nested:
            # A call that still waited for its approval never ran: the turn that goes on asks for it again.
            continue
        entry = replace(format_tool_completed_event(tool.call), call=None)
        (cut_short if tool.result == INTERRUPTED_CALL_RESULT else finished).append(entry)
    return render_stopped_attempt(partial_text="", completed_tools=finished, interrupted_tools=cut_short)
