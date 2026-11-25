"""Exactly-once peer messages and shared state for workflow runs."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from mindroom.redaction import redact_sensitive_text
from mindroom.workflow_runs.models import LIVE_NODE_STATES, WorkflowNode
from mindroom.workflow_runs.store import WorkflowRunStore, WorkflowRunStoreError, redact_json_text

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Collection, Iterable

MAX_PEER_BYTES = 1024 * 1024
_NAME_CHARS = 80
_TEXT_CHARS = 200
_PREVIEW_READ_CHARS = _TEXT_CHARS + 512
_MAX_FULL_PREVIEW_BYTES = 64 * 1024
_TRAILING_TOKEN_RE = re.compile(r"\S+\Z")

_LIVE = frozenset(state.value for state in LIVE_NODE_STATES)
_UNREACHABLE = "'{name}' has finished and will not read this. Use spawn() to keep an agent you want to talk to."
_TOO_LARGE = "Too large ({size:,} bytes, limit 1 MiB). Write it to a workspace file and send the path."
_SKIP = object()


class PeerUnreachableError(WorkflowRunStoreError):
    """Raised when messaging a peer name whose agents have all finished."""


class PeerTooLargeError(WorkflowRunStoreError):
    """Raised when a peer message or shared-state value exceeds the 1 MiB limit."""


@dataclass(frozen=True, slots=True)
class NodeRef:
    """Identity of a workflow sender or reader (script, agent node, or session turn)."""

    key: str
    generation: int
    name: str
    session_key: str | None = None


def peer_op_id(node_key: str, gen: int, model_run_id: str, call_id: str, op: str, args: object) -> str:
    """Return the deterministic op_id for one agent peer tool call (idempotent across retries)."""
    parts = [node_key, gen, model_run_id, call_id, op, args]
    raw = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _check_size(raw: bytes) -> None:
    if len(raw) > MAX_PEER_BYTES:
        raise PeerTooLargeError(_TOO_LARGE.format(size=len(raw)))


def _once(
    store: WorkflowRunStore,
    run_id: str,
    op_id: str,
    effect: Callable[[sqlite3.Connection], object],
    changed: str | None = None,
) -> object:
    with store.write_transaction(changed) as conn:
        q = "SELECT result_json FROM workflow_peer_calls WHERE run_id = ? AND op_id = ?"
        if (row := conn.execute(q, (run_id, op_id)).fetchone()) is not None:
            return json.loads(row["result_json"])
        if (result := effect(conn)) is _SKIP:
            return _SKIP
        encoded = json.dumps(result, separators=(",", ":"))
        conn.execute("INSERT INTO workflow_peer_calls VALUES (?, ?, ?)", (run_id, op_id, encoded))
        return result


def _self_keys(conn: sqlite3.Connection, run_id: str, ref: NodeRef) -> set[str]:
    keys = {ref.key}
    if ref.session_key is not None:
        q = "SELECT key FROM workflow_nodes WHERE run_id = ? AND session_key = ?"
        keys.update([ref.session_key, *(r["key"] for r in conn.execute(q, (run_id, ref.session_key)).fetchall())])
    return keys


def _unread_rows(conn: sqlite3.Connection, run_id: str, reader: NodeRef, names: Collection[str]) -> list[sqlite3.Row]:
    cur_q = "SELECT seq FROM workflow_cursors WHERE run_id = ? AND reader_key = ? AND reader_generation = ?"
    row = conn.execute(cur_q, (run_id, reader.key, reader.generation)).fetchone()
    cursor, self_keys, name_set = (0 if row is None else int(row["seq"])), _self_keys(conn, run_id, reader), set(names)
    target = reader.session_key or reader.key
    msg_q = (
        "SELECT seq, sender_key, sender_name, recipient_name, recipient_keys_json, text, created_at "
        "FROM workflow_messages WHERE run_id = ? AND seq > ? AND withdrawn = 0 ORDER BY seq ASC"
    )
    return [
        r
        for r in conn.execute(msg_q, (run_id, cursor)).fetchall()
        if r["recipient_name"] in name_set
        and r["sender_key"] not in self_keys
        and (
            r["recipient_name"] != "*" or not r["recipient_keys_json"] or target in json.loads(r["recipient_keys_json"])
        )
    ]


def _all_state_rows(conn: sqlite3.Connection, run_id: str) -> dict[str, object]:
    q = "SELECT key, value_json FROM workflow_state WHERE run_id = ? ORDER BY key ASC"
    return {r["key"]: json.loads(r["value_json"]) for r in conn.execute(q, (run_id,)).fetchall()}


def send(store: WorkflowRunStore, run_id: str, *, op_id: str, sender: NodeRef, to: str, text: str) -> dict[str, object]:
    """Send one message in one transaction keyed by *op_id*; freeze `*` recipients at send time."""
    if not to:
        msg = "Recipient 'to' must be a non-empty peer name, '*', or 'script'."
        raise WorkflowRunStoreError(msg)
    _check_size(text.encode("utf-8"))

    def _effect(conn: sqlite3.Connection) -> dict[str, object]:
        self_keys, frozen, delivered = _self_keys(conn, run_id, sender), None, 1
        if to != "script":
            q = (
                "SELECT key, kind, name, state FROM workflow_nodes "
                "WHERE run_id = ? AND stale = 0 "
                "AND attempt = (SELECT attempt FROM workflow_runs WHERE run_id = ?) "
                "AND kind IN ('agent', 'session')"
            )
            rows = conn.execute(q, (run_id, run_id)).fetchall()
            if to == "*":
                live = [
                    r["key"]
                    for r in rows
                    if r["key"] not in self_keys and (r["kind"] == "session" or r["state"] in _LIVE)
                ]
                frozen, delivered = json.dumps(live, separators=(",", ":")), len(live)
            else:
                matching = [r for r in rows if r["name"] == to]
                live_rows = [r for r in matching if r["kind"] == "session" or r["state"] in _LIVE]
                if matching and not live_rows:
                    raise PeerUnreachableError(_UNREACHABLE.format(name=to))
                delivered = sum(1 for r in live_rows if r["key"] not in self_keys)
        seq_q = "SELECT COALESCE(MAX(seq), 0) + 1 FROM workflow_messages WHERE run_id = ?"
        seq = int(conn.execute(seq_q, (run_id,)).fetchone()[0])
        ins = (
            "INSERT INTO workflow_messages "
            "(run_id, seq, sender_key, sender_generation, sender_name, recipient_name, recipient_keys_json, text, withdrawn, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)"
        )
        now = datetime.now(UTC).isoformat()
        conn.execute(ins, (run_id, seq, sender.key, sender.generation, sender.name, to, frozen, text, now))
        return {"delivered": delivered, "held": to != "script" and delivered == 0}

    return cast("dict[str, object]", _once(store, run_id, op_id, _effect, changed=run_id))


def read_call(
    store: WorkflowRunStore,
    run_id: str,
    *,
    op_id: str,
    reader: NodeRef,
    names: Collection[str],
    record_empty: bool = True,
    empty_note: str | None = None,
) -> dict[str, object] | None:
    """Read unread messages for *reader* in one transaction keyed by *op_id*, or None if empty and `not record_empty`."""

    def _effect(conn: sqlite3.Connection) -> object:
        matched = _unread_rows(conn, run_id, reader, names)
        if not matched and not record_empty:
            return _SKIP
        if matched:
            q = (
                "INSERT INTO workflow_cursors VALUES (?, ?, ?, ?) "
                "ON CONFLICT(run_id, reader_key, reader_generation) DO UPDATE SET seq = excluded.seq"
            )
            conn.execute(q, (run_id, reader.key, reader.generation, int(matched[-1]["seq"])))
            conn.executemany(
                "UPDATE workflow_messages SET was_read = 1 WHERE run_id = ? AND seq = ?",
                [(run_id, int(r["seq"])) for r in matched],
            )
        msgs = [{"from": r["sender_name"], "text": r["text"], "sent_at": r["created_at"]} for r in matched]
        return {"messages": msgs, **({"note": empty_note} if not msgs and empty_note else {})}

    saved = _once(store, run_id, op_id, _effect)
    return None if saved is _SKIP else cast("dict[str, object]", saved)


def get_state(store: WorkflowRunStore, run_id: str, *, op_id: str, key: str | None = None) -> dict[str, object]:
    """Read one key (`{found, value}`) or all shared-state keys (`{k: v}`) in one transaction keyed by *op_id*."""

    def _effect(conn: sqlite3.Connection) -> dict[str, object]:
        if key is None:
            return _all_state_rows(conn, run_id)
        q = "SELECT value_json FROM workflow_state WHERE run_id = ? AND key = ?"
        row = conn.execute(q, (run_id, key)).fetchone()
        return {"found": row is not None, "value": None if row is None else json.loads(row["value_json"])}

    return cast("dict[str, object]", _once(store, run_id, op_id, _effect))


def set_state(
    store: WorkflowRunStore,
    run_id: str,
    *,
    op_id: str,
    key: str,
    value: object,
) -> dict[str, object]:
    """Write one shared-state key in one transaction keyed by *op_id*; last write wins."""
    encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
    _check_size(encoded.encode("utf-8"))

    def _effect(conn: sqlite3.Connection) -> dict[str, object]:
        q = (
            "INSERT INTO workflow_state VALUES (?, ?, ?) ON CONFLICT(run_id, key) "
            "DO UPDATE SET value_json = excluded.value_json"
        )
        conn.execute(q, (run_id, key, encoded))
        return {"key": key, "saved": True}

    return cast("dict[str, object]", _once(store, run_id, op_id, _effect))


def valid_wait_seconds(value: object) -> float:
    """Return *value* as a finite non-negative float, or raise `WorkflowRunStoreError`."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(wait := float(value))
        or wait < 0
    ):
        msg = "wait_seconds must be a finite non-negative number."
        raise WorkflowRunStoreError(msg)
    return wait


def script_op(
    store: WorkflowRunStore,
    run_id: str,
    key: str,
    gen: int,
    kind: str,
    args: dict[str, object],
) -> tuple[bool, object]:
    """Run one script `send`, `state`, or immediate `inbox` node; return `(completed, result)`."""
    op_id = f"{key}:{gen}"
    if kind == "send":
        to, text = str(args.get("to") or ""), str(args.get("text") or "")
        return True, send(store, run_id, op_id=op_id, sender=NodeRef(key, gen, "script"), to=to, text=text)
    if kind == "inbox":
        raw = args.get("wait_seconds")
        wait = 0.0 if raw is None else valid_wait_seconds(raw)
        reader = NodeRef("script", 1, "script")
        got = read_call(store, run_id, op_id=op_id, reader=reader, names=("script",), record_empty=wait <= 0)
        return (True, got) if got is not None else (False, wait)
    op, state_key = args.get("op"), str(args.get("key") or "")
    if op == "get":
        return True, get_state(store, run_id, op_id=op_id, key=state_key)
    if op == "set":
        return True, set_state(store, run_id, op_id=op_id, key=state_key, value=args.get("value"))
    msg = f"Unknown state operation '{op}'."
    raise WorkflowRunStoreError(msg)


def session_turn_message(
    store: WorkflowRunStore,
    run_id: str,
    session: WorkflowNode,
    turn_key: str,
    turn_gen: int,
    message: str,
    *,
    prev_turn: WorkflowNode | None = None,
) -> str:
    """Seed `(turn_key, turn_gen)` from *prev_turn*'s ending cursor, drain between-turn mail, and format."""
    with store.write_transaction() as conn:
        seed = 0
        if prev_turn is not None:
            q = "SELECT seq FROM workflow_cursors WHERE run_id = ? AND reader_key = ? AND reader_generation = ?"
            row = conn.execute(q, (run_id, prev_turn.key, prev_turn.generation)).fetchone()
            seed = 0 if row is None else int(row["seq"])
        conn.execute(
            "INSERT OR IGNORE INTO workflow_cursors VALUES (?, ?, ?, ?)",
            (run_id, turn_key, turn_gen, seed),
        )
    if prev_turn is None:
        return message
    name = session.name or str(session.request.get("name") or "")
    reader = NodeRef(turn_key, turn_gen, name or f"n{session.seq}", session.key)
    names = (name, "*") if name else ("*",)
    res = read_call(store, run_id, op_id=f"session_inbox:{turn_key}:{turn_gen}", reader=reader, names=names)
    msgs = cast("list[dict[str, object]]", res["messages"]) if res is not None else ()
    if not msgs:
        return message
    lines = "\n".join(f"{m['from']}: {m['text']}" for m in msgs)
    return f"Messages received since your last turn:\n{lines}\n\n{message}"


def withdraw_unread(store: WorkflowRunStore, run_id: str, sender_key: str, old_generation: int) -> int:
    """Mark unread messages from `(sender_key, old_generation)` withdrawn; return count withdrawn."""
    sql = (
        "UPDATE workflow_messages SET withdrawn = 1 "
        "WHERE run_id = ? AND sender_key = ? AND sender_generation = ? AND withdrawn = 0 AND was_read = 0"
    )
    with store.write_transaction(run_id) as conn:
        return int(conn.execute(sql, (run_id, sender_key, old_generation)).rowcount)


def list_peers(store: WorkflowRunStore, run_id: str) -> list[dict[str, object]]:
    """Return `name`, `status` and `label` of every current agent and session in *run_id*."""
    nodes = store.nodes(run_id, light=True)
    turns = {n.session_key: n for n in nodes if n.kind == "turn" and n.session_key}
    out: list[dict[str, object]] = []
    for n in nodes:
        if n.kind == "agent":
            out.append({"id": f"n{n.seq}", "name": n.name, "status": n.state.value, "label": n.label})
        elif n.kind == "session":
            t = turns.get(n.key)
            status = t.state.value if t and t.state in LIVE_NODE_STATES else "idle"
            out.append({"id": f"n{n.seq}", "name": n.name, "status": status, "label": n.label, "session": True})
    return out


def _message_index(
    nodes: Iterable[WorkflowNode],
) -> tuple[dict[str, str], dict[str, int], dict[str, str], dict[str, list[str]]]:
    current = [n for n in nodes if not n.stale]
    ids = {n.key: f"n{n.seq}" for n in current}
    gens = {n.key: n.generation for n in current}
    sess_of = {n.key: n.session_key for n in current if n.session_key}
    by_name: dict[str, list[str]] = {}
    for n in current:
        if n.kind in ("agent", "session") and n.name:
            by_name.setdefault(n.name, []).append(ids[n.key])
    return ids, gens, sess_of, by_name


def _resolved_recipients(
    row: sqlite3.Row,
    from_id: str,
    ids: dict[str, str],
    sess_of: dict[str, str],
    by_name: dict[str, list[str]],
) -> list[str]:
    self_ids = {from_id} | ({ids[s]} if (s := sess_of.get(row["sender_key"])) and s in ids else set())
    frozen = row["recipient_keys_json"]
    pool = (
        [ids[k] for k in json.loads(frozen) if k in ids]
        if frozen is not None
        else by_name.get(row["recipient_name"], ())
    )
    return [to_id for to_id in pool if to_id not in self_ids]


def message_edges(store: WorkflowRunStore, run_id: str, nodes: Iterable[WorkflowNode]) -> list[tuple[str, str, int]]:
    """Aggregate non-withdrawn current-generation messages into `(from_id, to_id, count)` graph edges."""
    ids, gens, sess_of, by_name = _message_index(nodes)
    q = (
        "SELECT sender_key, sender_generation, recipient_name, recipient_keys_json "
        "FROM workflow_messages WHERE run_id = ? AND withdrawn = 0"
    )
    with store.read_connection() as conn:
        rows = conn.execute(q, (run_id,)).fetchall()
    counts: Counter[tuple[str, str]] = Counter()
    for r in rows:
        sender_key = r["sender_key"]
        if (from_id := ids.get(sender_key)) is None or int(r["sender_generation"]) != gens[sender_key]:
            continue
        for to_id in _resolved_recipients(r, from_id, ids, sess_of, by_name):
            counts[(from_id, to_id)] += 1
    return [(s, r, c) for (s, r), c in sorted(counts.items())]


_VISIBLE_FROM = (
    "FROM workflow_messages m "
    "JOIN workflow_nodes n "
    "  ON n.run_id = m.run_id AND n.key = m.sender_key AND n.stale = 0 AND n.generation = m.sender_generation "
    "WHERE m.run_id = ? AND m.withdrawn = 0"
)


def visible_messages_with_omitted(
    store: WorkflowRunStore,
    run_id: str,
    nodes: Iterable[WorkflowNode],
    *,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    """Return `(messages, omitted)` for the newest *limit* redacted current-generation peer messages, oldest first."""
    count_q = f"SELECT COUNT(*) {_VISIBLE_FROM}"
    if limit <= 0:
        with store.read_connection() as conn:
            return [], int(conn.execute(count_q, (run_id,)).fetchone()[0])
    ids, gens, sess_of, by_name = _message_index(nodes)
    q = (
        "SELECT m.seq, m.sender_key, m.sender_generation, m.sender_name, m.recipient_name, m.recipient_keys_json, "
        "CASE WHEN octet_length(m.text) <= ? THEN m.text ELSE substr(m.text, 1, ?) END AS text, "
        f"octet_length(m.text) > ? AS truncated_text, m.created_at {_VISIBLE_FROM} "
        "ORDER BY m.seq DESC LIMIT ?"
    )
    with store.read_connection() as conn:
        rows = conn.execute(
            q,
            (_MAX_FULL_PREVIEW_BYTES, _PREVIEW_READ_CHARS, _MAX_FULL_PREVIEW_BYTES, run_id, limit),
        ).fetchall()
        omitted = max(0, int(conn.execute(count_q, (run_id,)).fetchone()[0]) - len(rows)) if len(rows) == limit else 0
    out: list[dict[str, Any]] = []
    for r in reversed(rows):
        sender_key = r["sender_key"]
        if (from_id := ids.get(sender_key)) is None or int(r["sender_generation"]) != gens[sender_key]:
            continue
        raw_text = _TRAILING_TOKEN_RE.sub("", r["text"]) if r["truncated_text"] else r["text"]
        targets = _resolved_recipients(r, from_id, ids, sess_of, by_name)
        out.append(
            {
                "seq": int(r["seq"]),
                "from": from_id,
                "from_name": redact_sensitive_text(r["sender_name"], max_length=_NAME_CHARS),
                "to": targets[0] if len(targets) == 1 else None,
                "to_name": redact_sensitive_text(r["recipient_name"], max_length=_NAME_CHARS),
                "text": redact_sensitive_text(redact_json_text(raw_text), max_length=_TEXT_CHARS),
                "sent_at": int(datetime.fromisoformat(r["created_at"]).timestamp()),
            },
        )
    return out, omitted


def visible_messages(
    store: WorkflowRunStore,
    run_id: str,
    nodes: Iterable[WorkflowNode],
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return the newest *limit* redacted peer message previews for current-generation senders, oldest first."""
    return visible_messages_with_omitted(store, run_id, nodes, limit=limit)[0]


def all_messages(store: WorkflowRunStore, run_id: str) -> list[dict[str, Any]]:
    """Return the full, uncapped message history of *run_id* (for owner-checked `get_workflow(messages=True)`)."""
    q = "SELECT seq, sender_key, sender_name, recipient_name, text, withdrawn, created_at FROM workflow_messages WHERE run_id = ? ORDER BY seq ASC"
    with store.read_connection() as conn:
        rows = conn.execute(q, (run_id,)).fetchall()
    return [
        {
            "seq": int(r["seq"]),
            "from": r["sender_name"],
            "sender_key": r["sender_key"],
            "to": r["recipient_name"],
            "text": r["text"],
            "withdrawn": bool(r["withdrawn"]),
            "sent_at": r["created_at"],
        }
        for r in rows
    ]


def all_state(store: WorkflowRunStore, run_id: str) -> dict[str, object]:
    """Return all full, uncapped shared-state values of *run_id* (for owner-checked `get_workflow(messages=True)`)."""
    with store.read_connection() as conn:
        return _all_state_rows(conn, run_id)
