"""Continue the child turn a restart cut short in a subagent job, as the follow-up its caller would send."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.ai import run_delegated_child_response
from mindroom.delegation.background import delegation_child
from mindroom.delegation.execution import delegation_call_job
from mindroom.delegation.lifecycle import authorize_delegation, delegation_grant, prepare_child_turn
from mindroom.delegation.personas import follow_up_refusal
from mindroom.delegation.recovery import read_child_run, settled_subagent
from mindroom.delegation.sessions import SubagentSessionError
from mindroom.delegation.state import CONTINUE_SUBAGENT, interrupted_by_restart
from mindroom.history.run_checkpoint import is_recovered_run
from mindroom.tool_system.output_files import OUTPUT_PATH_ARGUMENT
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import require_tool_runtime_context

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild
    from mindroom.tool_jobs.runtime import BackgroundJob, JobSpec, ToolJobRuntime

# How many of a job's continuations its saved adapter counts, so the count survives further restarts.
_CONTINUATIONS_KEY = "restart_continuations"
# How many restarts may cut one subagent job short before the next one ends it, in case continuing is what stops it.
_RESTART_CONTINUATION_LIMIT = 2


async def continued_subagent_job(job: BackgroundJob, runtime: ToolJobRuntime) -> JobSpec | str:
    """Return what subagent job ``job`` runs on as after a restart cut its child's turn short, or why it ends instead.

    Called in the tool context of the job's conversation, as a reply's ``continue_subagent`` call is. The child's
    handle is settled first, so this writes state. The job continues only from a turn the restart settled: a turn
    that ended on its own stands, and a turn left paused, for an approval or a nested subagent, holds no run history
    replays. The continuation is a follow-up turn of the same subagent, which the caller's delegation and the
    requester's access must still allow; it sends the continuation prompt after the cut-short run's own messages, or
    the turn's task again when that run called no tool and so left nothing in history.
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
        return "its turn's run was left paused, which history never replays"
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
        prompt,
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
    arguments = {"subagent_id": child.subagent_id, "message": prompt}
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
    # The handle names the turn the job ran, or the turn a continuation saved before a second restart was prepared from.
    if child.delegation_id not in {saved.delegation_id, saved.previous_delegation_id}:
        return "its subagent is on another turn than the one the job ran"
    return child if interrupted_by_restart(child) else f"its turn ended {child.status} before the restart settled it"


async def _continuation_prompt(child: DelegationChild, config: Config, runtime_paths: RuntimePaths) -> str | None:
    """Return what continues ``child``'s cut-short turn, or None for a paused run, which history never replays."""
    run = await read_child_run(child, config, runtime_paths)
    recovered = run is not None and is_recovered_run(run)
    if not recovered and run is not None and run.tools:
        return None
    return config.get_prompt("SUBAGENT_RESTART_CONTINUATION_PROMPT") if recovered else child.task
