"""The built-in ``trace_audit`` automation: passive friction detection and friction-triggered skill learning."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING

from mindroom.automations.registry import automation
from mindroom.automations.steps import AutomationContext, Done, Run
from mindroom.automations.threads import automation_threads
from mindroom.config.automations import TraceAuditAutomation
from mindroom.config.resolution import cheap_model
from mindroom.contacts.turns import contact_session_check
from mindroom.helper_usage import record_system_usage
from mindroom.logging_config import get_logger
from mindroom.session_storage.agent_storage import agent_session_database_path
from mindroom.trace_audit.auditor import make_llm_audit_judge
from mindroom.trace_audit.models import TraceAuditSettings
from mindroom.trace_audit.pipeline import run_audit_pass

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.trace_audit.models import FailureCluster, NominateClusterFn

logger = get_logger(__name__)
_STATE_FILE = "trace_audit.json"


def _resolve_audit_model(ctx: AutomationContext, entry: TraceAuditAutomation) -> str:
    """Resolve the Stage 2 audit model: entry.model -> cheap_model -> skill_learning.model -> agent model."""
    agent = ctx.config.get_agent(ctx.agent_name)
    return entry.model or cheap_model(ctx.config, ctx.agent_name) or agent.skill_learning.model or agent.model


def _format_report_notice(clusters: Sequence[FailureCluster]) -> str:
    """Format a concise content-free summary notice for report-only mode."""
    lines = [f"🔍 Trace audit found {len(clusters)} recurring friction cluster(s):"]
    for cluster in clusters:
        op = f"/{cluster.operation}" if cluster.operation else ""
        subject = f"`{cluster.tool_name}`{op}" if cluster.tool_name else cluster.category
        detail = next(iter(cluster.proposed_rules), next(iter(cluster.root_causes), cluster.remedy))
        lines.append(f"- {subject} ({cluster.remedy}, {cluster.distinct_sessions} sessions): {detail}")
    return "\n".join(lines)


async def _run_trace_audit(ctx: AutomationContext, entry: TraceAuditAutomation, db_path: Path) -> Done | None:
    """Execute one passive trace-audit pass with off-loop SQLite/Stage-1 scanning."""
    model_name = _resolve_audit_model(ctx, entry)
    judge_fn = make_llm_audit_judge(
        ctx.config,
        ctx.runtime_paths,
        model_name,
        usage_sink=partial(record_system_usage, runtime_paths=ctx.runtime_paths, kind="trace_audit"),
    )
    auto_threads = await asyncio.to_thread(automation_threads, ctx.runtime_paths)
    in_contact_room = await asyncio.to_thread(contact_session_check, ctx.config, ctx.runtime_paths, ctx.agent_name)
    excluded = set(auto_threads) | ({f"{ctx.room_id}:{tid}" for tid in auto_threads} if ctx.room_id else set())

    def _exclude(session_id: str) -> bool:
        return in_contact_room(session_id) or any(session_id.endswith(f":{tid}") for tid in auto_threads)

    skill_learning = ctx.config.get_agent(ctx.agent_name).skill_learning
    friction_mode = skill_learning.enabled and skill_learning.trigger == "friction"
    nominate_fn: NominateClusterFn | None = None
    if friction_mode:
        if (reviewer := ctx._review_audit_cluster) is not None:

            async def _nominate(cluster: FailureCluster) -> object:
                return await reviewer(
                    ctx.config,
                    agent_name=ctx.agent_name,
                    cluster=cluster,
                    identity=ctx.execution_identity,
                    room_id=ctx.room_id,
                )

            nominate_fn = _nominate
        else:
            logger.warning("trace_audit_missing_cluster_reviewer", agent=ctx.agent_name)

    settings = TraceAuditSettings(
        min_idle_seconds=entry.min_idle_seconds,
        max_audits_per_run=entry.max_audits_per_run,
        min_cluster_sessions=entry.min_cluster_sessions,
        cooldown_days=entry.cooldown_days,
        max_nominations_per_run=entry.max_nominations_per_run,
    )
    result = await run_audit_pass(
        db_path=db_path,
        storage_name=ctx.agent_name,
        state_path=ctx.state_dir / _STATE_FILE,
        scope_key=f"{ctx.agent_name}:{ctx.requester_id}" if ctx.requester_id else ctx.agent_name,
        judge_fn=judge_fn,
        allowed_room_id=ctx.room_id,
        excluded_sessions=excluded,
        contact_session_check=_exclude,
        settings=settings,
        nominate_fn=nominate_fn,
    )
    logger.info(
        "trace_audit_pass_finished",
        agent=ctx.agent_name,
        model=model_name,
        scanned_runs=result.scanned_runs,
        incidents_audited=result.incidents_audited,
        promoted_clusters=len(result.promoted_clusters),
        friction_mode=friction_mode,
    )
    if not friction_mode and entry.notify and result.promoted_clusters:
        return Done(notice=_format_report_notice(result.promoted_clusters))
    return None


@automation("trace_audit", requires_file_memory=False)
def check_trace_audit(ctx: AutomationContext) -> Run | None:
    """Return a passive ``Run`` step when the agent's session database exists, else None."""
    entry = ctx.entry
    if not isinstance(entry, TraceAuditAutomation):
        msg = f"trace_audit cannot run the {entry.name} entry"
        raise ValueError(msg)  # noqa: TRY004 - the runner turns ValueError into a visible notice
    db_path = agent_session_database_path(ctx.agent_name, ctx.config, ctx.runtime_paths, ctx.execution_identity)
    return Run(execute=partial(_run_trace_audit, ctx, entry, db_path)) if db_path.exists() else None
