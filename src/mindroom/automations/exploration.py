"""The ``exploration`` automation: unattended exploration of the agent's sources, feeding its knowledge graph.

The check runs when conversations used tools since the last run, or a revisit is due, and asks the agent in a new
thread to explore with its own tools and approval rules. The thread's tool results reach the graph through ingestion,
never through anything the agent writes.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING

from agno.run.base import RunStatus

from mindroom.automations.registry import automation
from mindroom.automations.steps import Ask, AutomationContext, Done
from mindroom.automations.threads import automation_threads
from mindroom.config.automations import ExplorationAutomation
from mindroom.durable_write import write_json_file_durable
from mindroom.knowledge_graph.episodes import classify_thread, is_evidence, tool_arguments
from mindroom.knowledge_graph.ingestion import wake_knowledge_graph_ingestion
from mindroom.knowledge_graph.session_runs import session_runs, updated_sessions
from mindroom.logging_config import get_logger
from mindroom.session_storage.agent_storage import create_session_storage

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config

logger = get_logger(__name__)

_DAY_SECONDS = 86_400
_STATE_FILE = "exploration.json"
_MAX_ARGUMENT_CHARS = 300


@dataclass(frozen=True)
class _Lead:
    """A tool call conversations made, how often, and when last."""

    function: str
    arguments: str
    count: int
    last_seen: int


def _collect_leads(ctx: AutomationContext, *, since: int, limit: int) -> tuple[list[_Lead], bool]:
    """Return distinct tool calls from conversations updated after ``since``, newest first, at most ``limit``.

    Also return whether any conversation was updated after ``since``.
    """
    storage = create_session_storage(
        ctx.agent_name,
        ctx.config,
        ctx.runtime_paths,
        execution_identity=ctx.execution_identity,
    )
    automation_thread_ids = automation_threads(ctx.runtime_paths)
    seen: dict[tuple[str, str], tuple[int, int]] = {}
    active = False
    try:
        for session_id, _updated_at in updated_sessions(storage, ctx.agent_name, since=since):
            try:
                runs = session_runs(storage, session_id)
            except Exception as exc:
                # One conversation that cannot be read must not stop every later check.
                logger.warning("Exploration skipped an unreadable conversation", agent=ctx.agent_name, error=str(exc))
                continue
            if classify_thread(runs, automation_thread_ids) != "conversation":
                continue
            active = True
            for run in runs:
                # An updated conversation's earlier turns were listed by an earlier run.
                if run.status != RunStatus.completed or (run.created_at or 0) <= since:
                    continue
                for execution in run.tools or []:
                    if not execution.tool_name or not is_evidence(execution):
                        continue
                    key = (execution.tool_name, tool_arguments(execution)[:_MAX_ARGUMENT_CHARS])
                    count, last_seen = seen.get(key, (0, 0))
                    seen[key] = (count + 1, max(last_seen, run.created_at or 0))
    finally:
        storage.close()
    leads = [_Lead(function, arguments, count, last_seen) for (function, arguments), (count, last_seen) in seen.items()]
    leads.sort(key=lambda lead: lead.last_seen, reverse=True)
    return leads[:limit], active


def _render_leads(leads: list[_Lead]) -> str:
    if not leads:
        return "No tool calls in conversations since the last exploration; revisit the sources the graph already knows."
    return "\n".join(
        f"- {lead.function}({_without_mentions(lead.arguments)}): {lead.count} times, last "
        f"{datetime.fromtimestamp(lead.last_seen, UTC).date().isoformat()}"
        for lead in leads
    )


def _without_mentions(text: str) -> str:
    """Replace every `@` with a lookalike no mention parser reads, so the prompt mentions no person and wakes no agent."""
    return text.replace("@", "\uff20")


def _last_run_at(ctx: AutomationContext) -> float | None:
    try:
        state = json.loads((ctx.state_dir / _STATE_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    return float(state["last_run_at"])


@automation("exploration")
def check_exploration(ctx: AutomationContext) -> Ask | None:
    """Return the exploration prompt when conversations used tools since the last run, or a revisit is due after any.

    Raises ``OSError`` or ``ValueError`` when the conversations or the state cannot be read.
    """
    entry = ctx.entry
    if not isinstance(entry, ExplorationAutomation):
        msg = f"exploration cannot run the {entry.name} entry"
        raise ValueError(msg)  # noqa: TRY004 - the runner turns ValueError into a visible notice
    now = time.time()
    last_run_at = _last_run_at(ctx)
    revisit_due = last_run_at is None or now - last_run_at >= entry.revisit_days * _DAY_SECONDS
    # Update times are whole seconds, so the second of the last check is listed again rather than missed.
    since = int(last_run_at) - 1 if last_run_at is not None else int(now) - entry.revisit_days * _DAY_SECONDS
    leads, active = _collect_leads(ctx, since=since, limit=entry.max_leads)
    # A revisit catches changes in the sources people use; an agent or instance nobody talked to since needs none.
    if not leads and not (revisit_due and active):
        return None
    prompt = ctx.config.render_prompt(
        "EXPLORATION_PROMPT_TEMPLATE",
        max_tool_calls=entry.max_tool_calls,
        leads=_render_leads(leads),
    )
    return Ask(prompt, new_thread=True, then=partial(_after_run, ctx.state_dir, now))


def _after_run(state_dir: Path, checked_at: float, config: Config, thread_id: str, timed_out: bool) -> Done:
    """Record the run, resolve its thread, and ingest its tool results once the conversation counts as idle.

    The run is recorded only once it ends, so a prompt that never posted offers the same leads next time.
    """
    write_json_file_durable(state_dir / _STATE_FILE, {"last_run_at": checked_at})
    notice = "⚠️ Exploration did not finish within an hour." if timed_out else "🔭 Exploration ended."
    return Done(
        notice,
        resolve=(thread_id,),
        on_loop=partial(wake_knowledge_graph_ingestion, after_seconds=config.knowledge_graph.ingest.idle_seconds + 1),
    )
