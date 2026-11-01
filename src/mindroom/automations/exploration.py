"""The ``exploration`` automation: unattended exploration of the agent's sources, feeding its knowledge graph.

The check runs when conversations used tools since the last run, or a revisit is due, and asks the agent in a new
thread to explore with its own tools and approval rules. Facts reach the graph only through ingestion of the thread's
tool results, never through anything the agent writes.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING

from mindroom.automations.registry import automation
from mindroom.automations.steps import Ask, AutomationContext, Done
from mindroom.automations.threads import automation_threads
from mindroom.config.automations import ExplorationAutomation
from mindroom.config.resolution import edits_knowledge_graph
from mindroom.contacts.turns import contact_session_check
from mindroom.durable_write import write_json_file_durable
from mindroom.history.session_runs import matrix_turns, start_seq, updated_sessions
from mindroom.knowledge_graph.episodes import classify_thread, is_evidence, tool_arguments
from mindroom.knowledge_graph.ingestion import wake_knowledge_graph_ingestion
from mindroom.llm.runs import RunStatus
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


def _collect_leads(
    ctx: AutomationContext,
    *,
    since: int,
    read_through: dict[str, int],
    log_end: int | None,
    limit: int,
) -> tuple[list[_Lead], bool, int]:
    """Return distinct tool calls of new turns in conversations updated after ``since``, newest first, at most ``limit``.

    A turn is new once it completes after the place ``read_through`` holds for its conversation, which moves to the
    conversation's last place read. A conversation without one starts at ``log_end``, the log's end at the last
    check, or, before any check recorded it, with the turns created after ``since``. Also return whether any
    conversation was updated after ``since``, and the log's end at this check.
    """
    storage = create_session_storage(
        ctx.agent_name,
        ctx.config,
        ctx.runtime_paths,
        execution_identity=ctx.execution_identity,
    )
    # Read before the conversations, so every turn stored after this check's reads takes a place after it.
    checked_end = storage.log_end()
    automation_thread_ids = automation_threads(ctx.runtime_paths)
    in_contact_room = contact_session_check(ctx.config, ctx.runtime_paths, ctx.agent_name)
    seen: dict[tuple[str, str], tuple[int, int]] = {}
    active = False
    for session_id, _updated_at in updated_sessions(storage, since=since):
        # The other side of a contact drove the tool calls in its contact room, so they never steer exploration.
        if in_contact_room(session_id):
            continue
        try:
            turns = matrix_turns(storage, session_id)
        except Exception as exc:
            # One conversation that cannot be read must not stop every later check.
            logger.warning("Exploration skipped an unreadable conversation", agent=ctx.agent_name, error=str(exc))
            continue
        if classify_thread(turns, automation_thread_ids) != "conversation":
            continue
        active = True
        start = read_through.get(session_id)
        if start is None:
            # A turn is stored when it completes, so a conversation first listed now can hold a turn created before
            # the last check that completed after it.
            start = log_end if log_end is not None else start_seq(turns, older=lambda run: run.created_at <= since)
        # A turn waiting for approval takes a later place once it completes, so the next check reads it.
        read_through[session_id] = max(start, turns[-1].seq) if turns else start
        for seq, run in turns:
            # An updated conversation's earlier turns were listed by an earlier run.
            if run.status != RunStatus.completed or seq <= start:
                continue
            for execution in run.tools or []:
                if not is_evidence(execution):
                    continue
                key = (execution.tool_name, tool_arguments(execution)[:_MAX_ARGUMENT_CHARS])
                count, last_seen = seen.get(key, (0, 0))
                seen[key] = (count + 1, max(last_seen, run.created_at))
    leads = [_Lead(function, arguments, count, last_seen) for (function, arguments), (count, last_seen) in seen.items()]
    leads.sort(key=lambda lead: lead.last_seen, reverse=True)
    return leads[:limit], active, checked_end


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


def _state(ctx: AutomationContext) -> tuple[float | None, dict[str, int], int | None]:
    """Return the check time of the last finished exploration, each conversation's place read through, and the log's end."""
    try:
        state = json.loads((ctx.state_dir / _STATE_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, {}, None
    # LEGACY_COMPAT: Exploration state without conversation places.
    # Legacy format: `exploration.json` holding only `last_run_at`, the time of the check whose run ended last.
    # Last legacy release: v2026.10.725; replacement: the next release also keeps `sessions`, each conversation's place,
    # and `log_end`, the log's end at the check.
    # Handling: Every conversation starts with its completed turns created after the last check, as before any check
    # ran; a turn created before that check that completed after it is offered only when it completed after one of
    # those turns.
    # Coverage: tests/test_exploration_automation.py::test_a_state_file_without_places_offers_turns_created_since_the_check.
    return float(state["last_run_at"]), state.get("sessions", {}), state.get("log_end")


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
    last_run_at, read_through, log_end = _state(ctx)
    revisit_due = last_run_at is None or now - last_run_at >= entry.revisit_days * _DAY_SECONDS
    # Update times are whole seconds, so the second of the last check is listed again rather than missed.
    since = int(last_run_at) - 1 if last_run_at is not None else int(now) - entry.revisit_days * _DAY_SECONDS
    leads, active, checked_end = _collect_leads(
        ctx,
        since=since,
        read_through=read_through,
        log_end=log_end,
        limit=entry.max_leads,
    )
    # A revisit catches changes in the sources people use; an agent or instance nobody talked to since needs none.
    if not leads and not (revisit_due and active):
        return None
    fixes = edits_knowledge_graph(ctx.config, ctx.agent_name)
    prompt = ctx.config.render_prompt(
        "EXPLORATION_PROMPT_TEMPLATE",
        max_tool_calls=entry.max_tool_calls,
        leads=_render_leads(leads),
        fix_step=ctx.config.get_prompt("EXPLORATION_FIX_STEP") if fixes else "",
        fix_exception=ctx.config.get_prompt("EXPLORATION_FIX_EXCEPTION") if fixes else "",
    )
    return Ask(prompt, new_thread=True, then=partial(_after_run, ctx.state_dir, now, read_through, checked_end))


def _after_run(
    state_dir: Path,
    checked_at: float,
    read_through: dict[str, int],
    log_end: int,
    config: Config,
    thread_id: str,
    timed_out: bool,
) -> Done:
    """Record the run and what its check read, resolve its thread, and ingest its tool results once it counts as idle.

    The run is recorded only once it ends, so a prompt that never posted offers the same leads next time.
    """
    write_json_file_durable(
        state_dir / _STATE_FILE,
        {"last_run_at": checked_at, "sessions": read_through, "log_end": log_end},
    )
    notice = "⚠️ Exploration did not finish within an hour." if timed_out else "🔭 Exploration ended."
    return Done(
        notice,
        resolve=(thread_id,),
        on_loop=partial(wake_knowledge_graph_ingestion, after_seconds=config.knowledge_graph.ingest.idle_seconds + 1),
    )
