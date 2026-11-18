"""Continue the child turn a restart cut short in a subagent job, as the follow-up its caller would send."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from mindroom.ai import run_delegated_child_response
from mindroom.delegation.background import delegation_child
from mindroom.delegation.execution import delegation_call_job
from mindroom.delegation.lifecycle import authorize_delegation, delegation_grant, prepare_child_turn
from mindroom.delegation.personas import follow_up_refusal
from mindroom.delegation.recovery import read_child_run, settled_subagent
from mindroom.delegation.sessions import SubagentSessionError
from mindroom.delegation.state import CONTINUE_SUBAGENT, interrupted_by_restart
from mindroom.history.interrupted_replay import render_stopped_attempt
from mindroom.history.run_checkpoint import INTERRUPTED_CALL_RESULT, is_recovered_run
from mindroom.tool_system.events import format_tool_completed_event
from mindroom.tool_system.output_files import OUTPUT_PATH_ARGUMENT
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import require_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import ChildResponseRunner, DelegationChild
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.llm.runs import RunRecord
    from mindroom.llm.usage import RunMetrics
    from mindroom.tool_jobs.runtime import BackgroundJob, JobSpec, ToolJobRuntime

# How many of a job's continuations its saved adapter counts, so the count survives further restarts.
_CONTINUATIONS_KEY = "restart_continuations"
# How many restarts may cut one subagent job short before the next one ends it, in case continuing is what stops it.
_RESTART_CONTINUATION_LIMIT = 2
# How much of the cut-short turn's task the continuation prompt repeats.
_MAX_TASK_CHARACTERS = 8_000


async def continued_subagent_job(job: BackgroundJob, runtime: ToolJobRuntime) -> JobSpec | str:
    """Return what subagent job ``job`` runs on as after a restart cut its child's turn short, or why it ends instead.

    Called in the tool context of the job's conversation, as a reply's ``continue_subagent`` call is. The child's
    handle is settled first, so this writes state. The job continues only from a turn the restart settled: a turn
    that ended on its own stands, and a turn left paused, for an approval or a nested subagent, holds no run history
    replays. The continuation is a follow-up turn of the same subagent, which the caller's delegation and the
    requester's access must still allow. Its task, which the room shows as the turn's label, stays the cut-short
    turn's; the child is sent the continuation prompt instead (`_continuation_prompt`), after its history, which holds
    the cut-short run's own messages as far as its history limits replay them.
    """
    context = require_tool_runtime_context()
    config, runtime_paths, owner = context.current_config, context.runtime_paths, job.owner
    continuations = job.adapter.get(_CONTINUATIONS_KEY, 0)
    if continuations >= _RESTART_CONTINUATION_LIMIT:
        return f"restarts already cut it short {continuations + 1} times"
    child = await _stopped_turn(job, config, runtime_paths)
    if isinstance(child, str):
        return child
    prompt = await _continuation_prompt(child, config, runtime_paths)
    if prompt is None:
        return "its turn's run holds calls recovery could not finish, as a run paused for a decision or a subagent does"
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
    follow_up = prepare_child_turn(
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
        run_child=_sending(prompt),
        config=authorized,
        runtime_paths=runtime_paths,
        refresh_scheduler=None if orchestrator is None else orchestrator.knowledge_refresh_scheduler,
    )
    if not isinstance(spec, str):
        spec.adapter[_CONTINUATIONS_KEY] = continuations + 1
    return spec


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
    return child if interrupted_by_restart(child) else f"its turn ended {child.status} before the restart settled it"


def _sending(sent: str) -> ChildResponseRunner:
    """Return the response envelope that starts a continued turn on ``sent`` in place of its task.

    ``sent`` holds results of the cut-short run's calls, which are the child's input only: the turn's task is its
    label in the job and in the subagent's activity, which the room shows whatever its tool previews are set to.
    """

    async def run(
        child: DelegationChild,
        *,
        prompt: str,
        config: Config,
        runtime_paths: RuntimePaths,
        refresh_scheduler: KnowledgeRefreshScheduler | None,
        prior_usage: Sequence[RunMetrics] = (),
    ) -> str:
        return await run_delegated_child_response(
            child,
            # A continuation the turn itself makes later, as after a model switch, brings its own prompt.
            prompt=sent if prompt == child.task else prompt,
            config=config,
            runtime_paths=runtime_paths,
            refresh_scheduler=refresh_scheduler,
            prior_usage=prior_usage,
        )

    return run


async def _continuation_prompt(child: DelegationChild, config: Config, runtime_paths: RuntimePaths) -> str | None:
    """Return what the child is sent to continue its cut-short turn, or None for a run that cannot continue.

    A run with calls that recovery did not turn into history cannot. A subagent's first turn that called no tool left
    nothing in history and is sent its task again. Any other turn is sent the continuation note, the turn's own task,
    and an account of its run's calls: the child's history limits apply to the recovered run as to any turn, and may
    leave out both.
    """
    run = await read_child_run(child, config, runtime_paths)
    recovered = run is not None and is_recovered_run(run)
    if run is not None and run.tools and not recovered:
        return None
    if not recovered and child.previous_delegation_id is None:
        return child.task
    clipped = len(child.task) > _MAX_TASK_CHARACTERS
    task = f"{child.task[:_MAX_TASK_CHARACTERS]}…" if clipped else child.task
    note = config.get_prompt("SUBAGENT_RESTART_CONTINUATION_PROMPT")
    account = _calls_account(run) if run is not None and run.tools else ""
    return "\n\n".join(part for part in (note, f"<interrupted_task>\n{task}\n</interrupted_task>", account) if part)


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
