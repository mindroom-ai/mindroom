"""The ``exploration`` automation: unattended exploration of the agent's sources, feeding its knowledge graph.

The check runs when conversations used tools since the last run, or a revisit is due, and asks the agent in a new
thread to explore. The turn that prompt starts is restricted by ``exploration_policy``, and its tool results reach the
graph through ingestion, never through anything the agent writes.
"""

from __future__ import annotations

import json
import re
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
from mindroom.knowledge_graph.episodes import KNOWLEDGE_GRAPH_FUNCTIONS, classify_thread, tool_arguments
from mindroom.knowledge_graph.ingestion import wake_knowledge_graph_ingestion
from mindroom.knowledge_graph.session_runs import session_runs, updated_sessions
from mindroom.session_storage.agent_storage import create_session_storage

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config

_DAY_SECONDS = 86_400
_STATE_FILE = "exploration.json"
_MAX_ARGUMENT_CHARS = 300
# An `@` that starts a word, as in `@alice:example.org` or `@research`; one inside an email address stays.
_MENTION_SIGN = re.compile(r"(?<![\w.+-])@")


@dataclass(frozen=True)
class _Lead:
    """A tool call conversations made, how often, and when last."""

    function: str
    arguments: str
    count: int
    last_seen: int


def _collect_leads(ctx: AutomationContext, *, since: int, limit: int) -> list[_Lead]:
    """Return distinct tool calls from conversations updated after ``since``, newest first, at most ``limit``."""
    storage = create_session_storage(
        ctx.agent_name,
        ctx.config,
        ctx.runtime_paths,
        execution_identity=ctx.execution_identity,
    )
    automation_thread_ids = automation_threads(ctx.runtime_paths)
    seen: dict[tuple[str, str], tuple[int, int]] = {}
    try:
        for session_id, _updated_at in updated_sessions(storage, ctx.agent_name, since=since):
            runs = session_runs(storage, session_id)
            if classify_thread(runs, automation_thread_ids) != "conversation":
                continue
            for run in runs:
                # An updated conversation's earlier turns were listed by an earlier run.
                if run.status != RunStatus.completed or (run.created_at or 0) <= since:
                    continue
                for execution in run.tools or []:
                    if not execution.tool_name or execution.tool_name in KNOWLEDGE_GRAPH_FUNCTIONS:
                        continue
                    key = (execution.tool_name, tool_arguments(execution)[:_MAX_ARGUMENT_CHARS])
                    count, last_seen = seen.get(key, (0, 0))
                    seen[key] = (count + 1, max(last_seen, run.created_at or 0))
    finally:
        storage.close()
    leads = [_Lead(function, arguments, count, last_seen) for (function, arguments), (count, last_seen) in seen.items()]
    leads.sort(key=lambda lead: lead.last_seen, reverse=True)
    return leads[:limit]


def _render_leads(leads: list[_Lead]) -> str:
    if not leads:
        return "No tool calls in conversations since the last exploration; revisit the sources the graph already knows."
    return "\n".join(
        f"- {lead.function}({_without_mentions(lead.arguments)}): {lead.count} times, last "
        f"{datetime.fromtimestamp(lead.last_seen, UTC).date().isoformat()}"
        for lead in leads
    )


def _without_mentions(text: str) -> str:
    """Drop the `@` that starts a name, so posting the prompt mentions no person and wakes no other agent."""
    return _MENTION_SIGN.sub("", text)


def _last_run_at(ctx: AutomationContext) -> float | None:
    try:
        state = json.loads((ctx.state_dir / _STATE_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    return float(state["last_run_at"])


@automation("exploration")
def check_exploration(ctx: AutomationContext) -> Ask | None:
    """Return the exploration prompt when conversations used tools since the last run or a revisit is due.

    Raises ``OSError`` or ``ValueError`` when the conversations or the state cannot be read.
    """
    entry = ctx.entry
    if not isinstance(entry, ExplorationAutomation):
        return None
    now = time.time()
    last_run_at = _last_run_at(ctx)
    revisit_due = last_run_at is None or now - last_run_at >= entry.revisit_days * _DAY_SECONDS
    # Update times are whole seconds, so the second of the last check is listed again rather than missed.
    since = int(last_run_at) - 1 if last_run_at is not None else int(now) - entry.revisit_days * _DAY_SECONDS
    leads = _collect_leads(ctx, since=since, limit=entry.max_leads) if entry.max_leads else []
    if not leads and not revisit_due:
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
    state_dir.mkdir(parents=True, exist_ok=True)
    write_json_file_durable(state_dir / _STATE_FILE, {"last_run_at": checked_at})
    notice = (
        "⚠️ Exploration did not finish within an hour; what it read so far still joins the knowledge graph."
        if timed_out
        else "🔭 Exploration finished; what it read joins the knowledge graph shortly."
    )
    return Done(
        notice,
        resolve=(thread_id,),
        on_loop=partial(wake_knowledge_graph_ingestion, after_seconds=config.knowledge_graph.ingest.idle_seconds + 1),
    )
