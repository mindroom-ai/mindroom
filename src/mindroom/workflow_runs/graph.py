"""The graph of a workflow run as frames and cards show it, as pure functions: rows, the fitting of a snapshot, deltas.

A script's data dependencies are invisible, so the graph shows what the engine knows: phases, the groups that calls
ran in, and the calls. Every text is bounded here, in what is shown, and never in the run.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from mindroom.redaction import redact_sensitive_text
from mindroom.workflow_runs.models import TERMINAL_RUN_STATES, WorkflowNodeState
from mindroom.workflow_runs.replay import parent_group

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from mindroom.workflow_runs.models import WorkflowNode, WorkflowRun

# What `graph_delta` answers when the frame after a snapshot cannot be a delta.
REPLACE = "replace"
_NAME_CHARS = 80
_TEXT_CHARS = 200
# How many of its newest log lines a snapshot shows: a client that adds a delta's lines keeps as many.
LOG_LINES = 20
_COUNTED = ("queued", "running", "waiting", "completed", "failed")
_USAGE = ("input_tokens", "output_tokens", "tool_calls", "cost_usd")
# A call that was cancelled or may have run is counted with the failed ones wherever calls are counted by status.
_AS_FAILED = frozenset({WorkflowNodeState.FAILED, WorkflowNodeState.CANCELLED, WorkflowNodeState.INDETERMINATE})
AGENT_KINDS = frozenset({"agent", "turn"})

type _Row = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Graph:
    """Every row of a run's current view, before any is left out to fit a frame."""

    phases: list[str]
    groups: list[_Row]
    nodes: list[_Row]
    edges: list[_Row]
    log: list[str]


def node_label(kind: str, args: Mapping[str, object], name: object = None) -> str:
    """Return what a call is shown as: its name, else the first line of its prompt or question.

    A call with neither is named by its function or its operation. The label goes into the call's job, the room and
    the run's view, so it is redacted and cut to 80 characters.
    """
    text = name or args.get("prompt") or (args.get("message") if kind == "turn" else None) or args.get("question")
    if not text:
        if kind == "tool":
            text = f"{args.get('toolkit')}.{args.get('function')}"
        elif kind == "send" and args.get("to"):
            text = f"send -> {args.get('to')}"
        elif kind == "state" and args.get("op"):
            text = f"state.{args.get('op')}({args.get('key')})" if args.get("key") else f"state.{args.get('op')}"
        else:
            text = args.get("op") or args.get("type") or kind
    return redact_sensitive_text(str(text).strip().partition("\n")[0], max_length=_NAME_CHARS)


def redacted_title(title: str | None) -> str | None:
    """Return a room-safe run title, redacted and capped at 80 characters as the run card shows it."""
    return redact_sensitive_text(title, max_length=_NAME_CHARS) if title else None


def projected_cost_usd(turn_costs: Sequence[float], open_agents: int, spent: float) -> float:
    """Return what a run will have cost if each agent still to end costs what a finished turn cost on average.

    The average is over every finished turn, failed ones and schema corrections included.
    """
    return spent + (sum(turn_costs) / len(turn_costs) * open_agents if turn_costs else 0.0)


def _epoch(timestamp: str | None) -> int | None:
    return None if timestamp is None else int(datetime.fromisoformat(timestamp).timestamp())


def _bucket(state: WorkflowNodeState) -> str:
    return "failed" if state in _AS_FAILED else state.value


def graph_meta(
    run: WorkflowRun,
    nodes: Iterable[WorkflowNode],
    usage: Mapping[str, object],
    *,
    large: bool,
) -> dict[str, object]:
    """Return what every frame says about its run; the counts are of every current call, shown or not."""
    calls = [node for node in nodes if not node.stale and node.kind != "group"]
    counts = dict.fromkeys((*_COUNTED, "indeterminate", "cached"), 0)
    for node in calls:
        counts["indeterminate" if node.state is WorkflowNodeState.INDETERMINATE else _bucket(node.state)] += 1
        counts["cached"] += node.cached and node.state is WorkflowNodeState.COMPLETED
    return {
        "agent": run.agent_name,
        "thread_id": run.thread_root_event_id,
        "title": redacted_title(run.title),
        "status": run.state.value,
        "attempt": run.attempt,
        "started_at": _epoch(run.created_at),
        "ended_at": _epoch(run.finished_at) if run.state in TERMINAL_RUN_STATES else None,
        "max_concurrent": run.max_concurrent,
        "max_cost_usd": run.max_cost_usd,
        "large": large,
        "counts": counts,
        "usage": {name: usage[name] for name in (*_USAGE, "attempt_cost_usd", "projected_cost_usd", "cost_unknown")},
    }


def graph_rows(
    nodes: Iterable[WorkflowNode],
    usage_by_node: Mapping[str, Mapping[str, object]],
    edges: Iterable[tuple[str, str, int]],
    log: Sequence[str],
    *,
    inside: Mapping[str, Mapping[str, int]] | None = None,
) -> Graph:
    """Return the rows of the current view of a run: its phases, groups, calls, message edges and newest log lines.

    A group counts every call in it, at any depth. *inside* gives, by the key of the group of a run inside this
    one, how many calls of that run are in each state. A call's error is cut to 200 characters.
    """
    current = [node for node in nodes if not node.stale]
    ids = {node.key: f"n{node.seq}" for node in current}
    phases = list(dict.fromkeys(node.phase for node in current if node.phase))
    groups: dict[str, _Row] = {}
    for node in current:
        if node.kind == "group":
            # A run inside this one hangs under the call that started it; any other group under the group around it.
            under = node.lane if node.request.get("op") == "workflow" else parent_group(node.lane)
            groups[node.key] = {
                "id": ids[node.key],
                "kind": str(node.request.get("op") or "group"),
                "phase": phases.index(node.phase) if node.phase else None,
                "parent": ids.get(under or ""),
                "total": 0,
                "done": 0,
                "failed": 0,
            }

    def count(key: str | None, state: str, number: int = 1) -> None:
        while key is not None and (group := groups.get(key)) is not None:
            group["total"] += number
            group["done"] += number * (state == "completed")
            group["failed"] += number * (state == "failed")
            key = parent_group(current_lane[key])

    current_lane = {node.key: node.lane for node in current}
    session_turns = Counter(node.session_key for node in current if node.kind == "turn" and node.session_key)
    rows: list[_Row] = []
    for node in current:
        if node.kind == "group":
            for state, number in (inside or {}).get(node.key, {}).items():
                count(node.key, _bucket(WorkflowNodeState(state)), number)
            continue
        count(parent_group(node.lane), _bucket(node.state))
        used, links = usage_by_node.get(node.key, {}), node.links
        message = None if node.error is None else str(node.error.get("message") or node.error.get("kind") or "")
        rows.append(
            {
                "id": ids[node.key],
                "kind": node.kind,
                "label": node.label,
                "name": redact_sensitive_text(node.name, max_length=_NAME_CHARS) if node.name else None,
                "phase": phases.index(node.phase) if node.phase else None,
                "group": ids.get(parent_group(node.lane) or "") if parent_group(node.lane) in groups else None,
                "status": node.state.value,
                "waiting_on": node.waiting_on,
                "agent": links.get("agent"),
                "model": links.get("model"),
                "run": links.get("delegation_id"),
                "subagent": links.get("subagent_id"),
                "job": links.get("job_id"),
                "session": ids.get(node.session_key) if node.session_key else None,
                "turns": (
                    session_turns[node.key]
                    if node.kind == "session"
                    else (int(links.get("turn", 1)) if node.kind in AGENT_KINDS else 0)
                ),
                "started_at": _epoch(node.started_at),
                "ended_at": _epoch(node.ended_at),
                "cached": node.cached,
                "error": None if message is None else redact_sensitive_text(message, max_length=_TEXT_CHARS),
                "usage": {name: used.get(name) or 0 for name in _USAGE},
            },
        )
    return Graph(
        phases=[phase[:_NAME_CHARS] for phase in phases],
        groups=list(groups.values()),
        nodes=rows,
        edges=[
            {"from": sender, "to": receiver, "kind": "message", "count": number} for sender, receiver, number in edges
        ],
        log=[line[:_TEXT_CHARS] for line in log[-LOG_LINES:]],
    )


def _order(row: _Row) -> tuple[int, int]:
    """Order calls by what a person can act on: waiting for a person, failed, running, queued, completed newest first."""
    status, number = row["status"], int(row["id"][1:])
    if status == "waiting" and row["waiting_on"] != "peer":
        return 0, number
    if status in ("failed", "cancelled", "indeterminate"):
        return 1, number
    if status == "queued":
        return 3, number
    return (4, -(row["ended_at"] or 0)) if status == "completed" else (2, number)


def fit_snapshot(graph: Graph, *, fits: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    """Return the largest snapshot of *graph* that *fits*, with a count of everything it leaves out.

    Calls are added in the order a person can act on them, then edges, the busiest first. When the groups alone do
    not fit, the innermost ones are left out and no call is shown; the group around them still counts their calls.
    When that does not fit either, the log, then the groups, then the phase names are left out. What remains then,
    the counts, has a fixed size, so a frame can always be sent.
    """
    calls, edges = sorted(graph.nodes, key=_order), sorted(graph.edges, key=lambda edge: -edge["count"])
    depths: dict[str, int] = {}
    for group in graph.groups:
        # A group comes after the group around it, and the call a nested run is under is in no group that counts here.
        depths[group["id"]] = depths.get(group["parent"], -1) + 1
    deepest = max(depths.values(), default=0)

    def built(shown: int, depth: int, *, log: bool = True, phases: bool = True) -> dict[str, Any]:
        groups = [group for group in graph.groups if depths[group["id"]] <= depth]
        omitted = dict.fromkeys(_COUNTED, 0) | Counter(
            _bucket(WorkflowNodeState(row["status"])) for row in calls[shown:]
        )
        omitted |= {
            "edges": len(edges) - max(0, shown - len(calls)),
            "groups": len(graph.groups) - len(groups),
            "phases": 0 if phases else len(graph.phases),
            "log": 0 if log else len(graph.log),
        }
        return {
            "phases": graph.phases if phases else [],
            "groups": groups,
            # Calls are shown only with every group: each names its own.
            "nodes": calls[:shown],
            "edges": edges[: max(0, shown - len(calls))],
            "omitted": omitted,
            "log": graph.log if log else [],
        }

    if not fits(built(0, deepest)):
        if deepest and fits(built(0, 0)):
            depth = bisect_left(range(1, deepest + 1), True, key=lambda d: not fits(built(0, d)))
            return built(0, depth)
        reduced = (built(0, 0, log=False), built(0, -1, log=False))
        return next((form for form in reduced if fits(form)), built(0, -1, log=False, phases=False))
    shown = bisect_left(range(1, len(calls) + len(edges) + 1), True, key=lambda n: not fits(built(n, deepest)))
    return built(shown, deepest)


def graph_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any] | str | None:
    """Return what changed from one snapshot to the next: the rows that changed or came, and the new log lines.

    None when nothing did. `REPLACE` when a row went, or the phases or the counts of what was left out changed: the
    frame that follows is a snapshot then. A client adds `log_append` and keeps the newest `LOG_LINES` lines.
    """
    if before["phases"] != after["phases"] or before["omitted"] != after["omitted"]:
        return REPLACE
    delta: dict[str, Any] = {}
    for rows, identity in (("groups", ("id",)), ("nodes", ("id",)), ("edges", ("from", "to"))):
        old = {tuple(row[name] for name in identity): row for row in before[rows]}
        new = {tuple(row[name] for name in identity): row for row in after[rows]}
        if old.keys() - new.keys():
            return REPLACE
        if changed := [row for key, row in new.items() if old.get(key) != row]:
            delta[rows] = changed
    was, now = before["log"], after["log"]
    # The lines the newer log starts with are the older one's last: the rest came since.
    kept = next(size for size in range(len(was), -1, -1) if was[len(was) - size :] == now[:size])
    if now[kept:]:
        delta["log_append"] = now[kept:]
    return delta or None
