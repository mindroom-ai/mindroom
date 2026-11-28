"""Continue the child turn a restart cut short in a subagent job, as the follow-up its caller would send."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from mindroom.ai import run_delegated_child_response
from mindroom.ai_run_metadata import prior_usage_by_run
from mindroom.delegation.background import carried_job_receipts, delegation_child
from mindroom.delegation.cost import SubagentCost, merge_by_key
from mindroom.delegation.execution import delegation_call_job
from mindroom.delegation.lifecycle import (
    authorize_delegation,
    child_own_usage,
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
    SPENT_RUNS_KEY,
    interrupted_by_restart,
)
from mindroom.delegation.workers import validate_sandbox_child
from mindroom.history.interrupted_replay import render_stopped_attempt
from mindroom.history.run_checkpoint import INTERRUPTED_CALL_RESULT, is_recovered_run
from mindroom.tool_jobs.runtime import waited_for_approval, waited_for_user
from mindroom.tool_system.events import format_tool_completed_event
from mindroom.tool_system.output_files import OUTPUT_PATH_ARGUMENT
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import require_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild
    from mindroom.llm.runs import RunRecord
    from mindroom.tool_jobs.runtime import BackgroundJob, JobSpec, ToolJobRuntime
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

# How many of a job's continuations its saved adapter counts, so the count survives further restarts.
_CONTINUATIONS_KEY = "restart_continuations"
# What the job's latest continuation was sent in place of its task, saved with the job and nowhere a room reads.
_PROMPT_KEY = "continuation_prompt"
# What stands in a saved continuation prompt for the prompt of the workflow run's call, which the run keeps.
_CALL_PROMPT_MARK = "<<mindroom:workflow-call-prompt>>"
# What the job's latest continuation started from having received, in case a restart stops it before its run's first row.
_RECEIPTS_KEY = "continuation_receipts"
# How many restarts may cut one subagent job short before the next one ends it, in case continuing is what stops it.
_RESTART_CONTINUATION_LIMIT = 2
# How much of what the cut-short turn was sent the continuation prompt repeats.
_MAX_TASK_CHARACTERS = 8_000


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
    its calls on the cards they have, with the decisions those hold.
    """
    context = require_tool_runtime_context()
    config, runtime_paths, owner = context.current_config, context.runtime_paths, job.owner
    continuations = job.adapter.get(_CONTINUATIONS_KEY, 0)
    # A job waiting on an approval or its own ask_user was not cut short by work retries.
    for_user = waited_for_user(job)
    if continuations >= _RESTART_CONTINUATION_LIMIT and not (for_user or waited_for_approval(job)):
        return f"restarts already cut it short {continuations + 1} times"
    child = await _stopped_turn(job, config, runtime_paths)
    if isinstance(child, str):
        return child
    sent = await _sent_or_resumed(job, child, config, runtime_paths)
    if isinstance(sent, str):
        return sent
    prompt, paused, run = sent
    authorized = _authorize_continuation(
        child,
        prompt,
        owner=owner,
        config=config,
        runtime_paths=runtime_paths,
        depth=job.depth,
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
    receipts = await _continued_receipts(job, child, run, config, runtime)
    if receipts is not None:
        follow_up.cost_usd = follow_up.subagents_cost_usd = SubagentCost(receipts).usd
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
        run_child=run_delegated_child_response,
        prompt=prompt,
        config=authorized,
        runtime_paths=runtime_paths,
        refresh_scheduler=None if orchestrator is None else orchestrator.knowledge_refresh_scheduler,
        paused=paused,
        receipts=receipts,
    )
    if not isinstance(spec, str):
        _record_continuation_adapter(
            spec,
            job,
            child,
            prompt=prompt,
            continuations=continuations + (not for_user),
            paused=paused is not None,
            receipts=receipts,
        )
    return spec


def _authorize_continuation(
    child: DelegationChild,
    prompt: str,
    *,
    owner: ToolExecutionIdentity,
    config: Config,
    runtime_paths: RuntimePaths,
    depth: int,
) -> Config | str:
    tools = config_plugins(config, runtime_paths).tools
    refusal = follow_up_refusal(child.persona, owner.agent_name, config, tools, delegation_depth=depth)
    if refusal is not None:
        return refusal
    authorized = authorize_delegation(
        owner.agent_name,
        child.child_agent_name,
        prompt,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=owner,
        depth=depth,
        grant=delegation_grant(child),
    )
    if isinstance(authorized, str):
        return authorized
    sandbox_refusal = validate_sandbox_child(child, authorized, runtime_paths, caller_name=owner.agent_name)
    return sandbox_refusal if sandbox_refusal is not None else authorized


def _record_continuation_adapter(
    spec: JobSpec,
    job: BackgroundJob,
    child: DelegationChild,
    *,
    prompt: str,
    continuations: int,
    paused: bool,
    receipts: dict[str, float] | None,
) -> None:
    """Populate ``spec.adapter`` with the state a continuation job carries across further restarts."""
    if paused:
        # The same turn goes on, so the job keeps what it had saved beside its child.
        spec.adapter.update({key: value for key, value in job.adapter.items() if key != "child"})
        return
    spec.adapter.update({_CONTINUATIONS_KEY: continuations, _PROMPT_KEY: prompt})
    if child.workflow_run_id is not None:
        # The job stays the call of its workflow run, which reads what each run of the call used and did.
        spec.adapter[SPENT_RUNS_KEY] = [*dict.fromkeys([*job.adapter.get(SPENT_RUNS_KEY, ()), child.run_id])]
        # The run keeps the call's prompt, so the job, whose every snapshot repeats its adapter, keeps a mark
        # in its place.
        asked = workflow_call_prompt(child) or _CALL_PROMPT_MARK
        spec.adapter[_PROMPT_KEY] = prompt.replace(asked, _CALL_PROMPT_MARK)
    elif receipts is not None:
        spec.adapter[_RECEIPTS_KEY] = receipts


async def _continued_receipts(
    job: BackgroundJob,
    child: DelegationChild,
    run: RunRecord | None,
    config: Config,
    runtime: ToolJobRuntime,
) -> dict[str, float] | None:
    """Return what a non-workflow continuation starts from having received; workflow calls track spent runs by attempt."""
    if child.workflow_run_id is not None:
        return None
    receipts: dict[str, float] = job.adapter.get(_RECEIPTS_KEY) or {}
    if run is None:
        return receipts
    own_usd = child_own_usage(run, config)[1]
    ran = {**dict.fromkeys(prior_usage_by_run(run.metadata)), run.run_id: run.metadata}
    received = await carried_job_receipts(ran, runtime.delegation_jobs)
    return merge_by_key((receipts, received, {} if own_usd is None else {child.delegation_id: own_usd}), float)


async def _sent_or_resumed(
    job: BackgroundJob,
    child: DelegationChild,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[str, tuple[RunRecord, Sequence[Sequence[str]]] | None, RunRecord | None] | str:
    """Return what the job's next turn is sent, or the paused turn it resumes and its task, or why it does neither.

    A paused turn comes with the owners of its calls, which the job saved when it asked about them.
    """
    if child.status == "paused":
        owners, run = job.adapter.get(APPROVAL_TOOLKITS_KEY), await paused_for_approval(child, config, runtime_paths)
        return (
            "its paused turn cannot be resumed" if owners is None or run is None else (child.task, (run, owners), run)
        )
    run = await read_child_run(child, config, runtime_paths)
    prompt = _continuation_prompt(job, child, run, config)
    if prompt is None:
        return "its turn's run holds calls recovery could not finish, as a run paused for a decision or a subagent does"
    return prompt, None, run


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


def _continuation_prompt(
    job: BackgroundJob,
    child: DelegationChild,
    run: RunRecord | None,
    config: Config,
) -> str | None:
    """Return what the child is sent to continue cut-short turn ``child``, or None for a run that cannot continue.

    A continuation that was prepared and never reserved its turn is sent what was prepared. A run with calls that
    recovery did not turn into history cannot continue. A turn that called no tool left nothing in history and is sent
    again what it was sent: its task, or the prompt of the continuation it was. Any other turn is sent the
    continuation note, what it was sent, and an account of its run's calls: the child's history limits apply to the
    recovered run as to any turn, and may leave out both.
    """
    sent = job.adapter.get(_PROMPT_KEY) or original_prompt(child)
    if child.workflow_run_id is not None:
        sent = sent.replace(_CALL_PROMPT_MARK, original_prompt(child))
    if child.delegation_id != delegation_child(job).delegation_id:
        return sent
    if run is None:
        # The call of a workflow run goes on only on proof that its turn called no tool, which no run is not.
        return sent if child.workflow_run_id is None else None
    if not is_recovered_run(run):
        return None if run.tools else sent
    # The prompt of a workflow run's call is sent whole: what it asks for may stand at its end.
    clip = len(sent) > _MAX_TASK_CHARACTERS and child.workflow_run_id is None
    task = f"{sent[:_MAX_TASK_CHARACTERS]}…" if clip else sent
    note = config.get_prompt("SUBAGENT_RESTART_CONTINUATION_PROMPT")
    account = _calls_account(run) if run.tools else ""
    parts = (note, f"<interrupted_task>\n{task}\n</interrupted_task>", account)
    return "\n\n".join(part for part in parts if part)


def _calls_account(run: RunRecord) -> str:
    """Render the cut-short run's calls as the bounded account a replayed reply's prompt gets of its stopped attempt.

    The child's history replays the recovered run like any other turn, so its limits may leave the run's calls out;
    the account names each call from its preview either way, and which ones were cut short.
    """
    finished, cut_short = [], []
    for tool in run.tools or ():
        entry = replace(format_tool_completed_event(tool.call), call=None)
        (cut_short if tool.result == INTERRUPTED_CALL_RESULT else finished).append(entry)
    return render_stopped_attempt(partial_text="", completed_tools=finished, interrupted_tools=cut_short)
