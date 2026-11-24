"""Exactly-once peer messages and shared state for workflow runs."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from mindroom.workflow_runs.models import LIVE_NODE_STATES, WorkflowNode
from mindroom.workflow_runs.store import WorkflowRunStore, WorkflowRunStoreError

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Collection, Iterable

MAX_PEER_BYTES = 1024 * 1024
DEADLOCK_NOTE = "Every agent is waiting for a message and nobody is left to send one."

_LIVE_VALUES = frozenset(state.value for state in LIVE_NODE_STATES)
_UNREACHABLE = "'{name}' has finished and will not read this. Use spawn() to keep an agent you want to talk to."
_TOO_LARGE = "Too large ({size:,} bytes, limit 1 MiB). Write it to a workspace file and send the path."
_SKIP = object()


class PeerUnreachableError(WorkflowRunStoreError):
    """Raised when messaging a peer name whose agents have all finished."""


class PeerTooLargeError(WorkflowRunStoreError):
    """Raised when a peer message or shared-state value exceeds the 1 MiB limit."""


@dataclass(frozen=True, slots=True)
class NodeRef:
    """Identity of a workflow sender or reader (the script, an agent node, or a session turn)."""

    key: str
    generation: int
    name: str
    reader_key: str | None = None
    reader_generation: int | None = None

    @property
    def cursor_key(self) -> tuple[str, int]:
        """Return `(reader_key, reader_generation)` for cursor lookup (shared across turns of a session)."""
        if self.reader_key is not None and self.reader_generation is not None:
            return self.reader_key, self.reader_generation
        return self.key, self.generation


def peer_op_id(
    node_key: str,
    generation: int,
    model_run_id: str,
    tool_call_id: str,
    operation: str,
    arguments: object,
) -> str:
    """Return the deterministic op_id for one agent peer tool call (idempotent across provider retries)."""
    payload = json.dumps(
        [node_key, generation, model_run_id, tool_call_id, operation, arguments],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _check_size(raw: bytes) -> None:
    if len(raw) > MAX_PEER_BYTES:
        raise PeerTooLargeError(_TOO_LARGE.format(size=len(raw)))


def _once(
    store: WorkflowRunStore,
    run_id: str,
    op_id: str,
    effect: Callable[[sqlite3.Connection], object],
    *,
    changed: str | None = None,
) -> object:
    """Run *effect* once in a single transaction keyed by *op_id*; return `_SKIP` unsaved when *effect* does."""
    with store.write_transaction(changed) as connection:
        row = connection.execute(
            "SELECT result_json FROM workflow_peer_calls WHERE run_id = ? AND op_id = ?",
            (run_id, op_id),
        ).fetchone()
        if row is not None:
            return json.loads(row["result_json"])
        result = effect(connection)
        if result is _SKIP:
            return _SKIP
        connection.execute(
            "INSERT INTO workflow_peer_calls (run_id, op_id, result_json) VALUES (?, ?, ?)",
            (run_id, op_id, json.dumps(result, separators=(",", ":"))),
        )
        return result


def _self_keys(connection: sqlite3.Connection, run_id: str, ref: NodeRef) -> set[str]:
    keys = {ref.key}
    if ref.reader_key is not None:
        keys.add(ref.reader_key)
        rows = connection.execute(
            "SELECT key FROM workflow_nodes WHERE run_id = ? AND session_key = ?",
            (run_id, ref.reader_key),
        ).fetchall()
        keys.update(row["key"] for row in rows)
    return keys


def _unread_rows(
    connection: sqlite3.Connection,
    run_id: str,
    reader: NodeRef,
    names: Collection[str],
) -> list[sqlite3.Row]:
    reader_key, reader_gen = reader.cursor_key
    row = connection.execute(
        "SELECT seq FROM workflow_cursors WHERE run_id = ? AND reader_key = ? AND reader_generation = ?",
        (run_id, reader_key, reader_gen),
    ).fetchone()
    cursor = 0 if row is None else int(row["seq"])
    self_keys, name_set = _self_keys(connection, run_id, reader), frozenset(names)
    rows = connection.execute(
        "SELECT seq, sender_key, sender_name, recipient_name, recipient_keys_json, text, created_at "
        "FROM workflow_messages WHERE run_id = ? AND seq > ? AND withdrawn = 0 ORDER BY seq ASC",
        (run_id, cursor),
    ).fetchall()
    return [
        r
        for r in rows
        if r["recipient_name"] in name_set
        and r["sender_key"] not in self_keys
        and (
            r["recipient_name"] != "*"
            or r["recipient_keys_json"] is None
            or reader_key in json.loads(r["recipient_keys_json"])
        )
    ]


def _all_state_rows(connection: sqlite3.Connection, run_id: str) -> dict[str, object]:
    rows = connection.execute(
        "SELECT key, value_json FROM workflow_state WHERE run_id = ? ORDER BY key ASC",
        (run_id,),
    ).fetchall()
    return {row["key"]: json.loads(row["value_json"]) for row in rows}


def send(
    store: WorkflowRunStore,
    run_id: str,
    *,
    op_id: str,
    sender: NodeRef,
    to: str,
    text: str,
) -> dict[str, object]:
    """Send one message in one transaction keyed by *op_id*; freeze `*` recipients at send time."""
    if not to:
        msg = "Recipient 'to' must be a non-empty peer name, '*', or 'script'."
        raise WorkflowRunStoreError(msg)
    _check_size(text.encode("utf-8"))

    def _effect(connection: sqlite3.Connection) -> dict[str, object]:
        self_keys = _self_keys(connection, run_id, sender)
        frozen_json: str | None = None
        if to == "script":
            delivered, held = 1, False
        else:
            rows = connection.execute(
                "SELECT key, kind, name, state FROM workflow_nodes "
                "WHERE run_id = ? AND stale = 0 AND kind IN ('agent', 'session')",
                (run_id,),
            ).fetchall()
            if to == "*":
                live = [
                    r["key"]
                    for r in rows
                    if r["key"] not in self_keys and (r["kind"] == "session" or r["state"] in _LIVE_VALUES)
                ]
                frozen_json = json.dumps(live, separators=(",", ":"))
                delivered, held = len(live), not live
            else:
                sessions = [r for r in rows if r["kind"] == "session" and r["name"] == to]
                agents = [r for r in rows if r["kind"] == "agent" and r["name"] == to]
                live_agents = [r for r in agents if r["state"] in _LIVE_VALUES]
                if agents and not live_agents and not sessions:
                    raise PeerUnreachableError(_UNREACHABLE.format(name=to))
                recipients = [r for r in (*live_agents, *sessions) if r["key"] not in self_keys]
                delivered, held = len(recipients), not recipients
        seq = int(
            connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM workflow_messages WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0],
        )
        connection.execute(
            "INSERT INTO workflow_messages "
            "(run_id, seq, sender_key, sender_generation, sender_name, recipient_name, recipient_keys_json, text, withdrawn, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (
                run_id,
                seq,
                sender.key,
                sender.generation,
                sender.name,
                to,
                frozen_json,
                text,
                datetime.now(UTC).isoformat(),
            ),
        )
        return {"delivered": delivered, "held": held}

    return cast("dict[str, object]", _once(store, run_id, op_id, _effect, changed=run_id))


def read_call(
    store: WorkflowRunStore,
    run_id: str,
    *,
    op_id: str,
    reader: NodeRef,
    names: Collection[str],
    record_empty: bool = True,
) -> list[dict[str, object]] | None:
    """Read unread messages for *reader* in one transaction keyed by *op_id*, or None if empty and `not record_empty`."""
    reader_key, reader_gen = reader.cursor_key

    def _effect(connection: sqlite3.Connection) -> object:
        matched = _unread_rows(connection, run_id, reader, names)
        if not matched and not record_empty:
            return _SKIP
        messages = [{"from": r["sender_name"], "text": r["text"], "sent_at": r["created_at"]} for r in matched]
        if matched:
            connection.execute(
                "INSERT INTO workflow_cursors (run_id, reader_key, reader_generation, seq) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(run_id, reader_key, reader_generation) DO UPDATE SET seq = excluded.seq",
                (run_id, reader_key, reader_gen, int(matched[-1]["seq"])),
            )
        return messages

    saved = _once(store, run_id, op_id, _effect)
    return None if saved is _SKIP else cast("list[dict[str, object]]", saved)


def has_unread(store: WorkflowRunStore, run_id: str, *, reader: NodeRef, names: Collection[str]) -> bool:
    """Return whether *reader* has any unread, non-withdrawn message waiting in *run_id*."""
    with store.read_connection() as connection:
        return bool(_unread_rows(connection, run_id, reader, names))


def get_state(store: WorkflowRunStore, run_id: str, *, op_id: str, key: str | None = None) -> dict[str, object]:
    """Read one key (`{found, value}`) or all shared-state keys (`{k: v}`) in one transaction keyed by *op_id*."""

    def _effect(connection: sqlite3.Connection) -> dict[str, object]:
        if key is None:
            return _all_state_rows(connection, run_id)
        row = connection.execute(
            "SELECT value_json FROM workflow_state WHERE run_id = ? AND key = ?",
            (run_id, key),
        ).fetchone()
        return {"found": row is not None, "value": None if row is None else json.loads(row["value_json"])}

    return cast("dict[str, object]", _once(store, run_id, op_id, _effect))


def set_state(
    store: WorkflowRunStore,
    run_id: str,
    *,
    op_id: str,
    key: str,
    value: object,
    updated_by: str,
) -> dict[str, object]:
    """Write one shared-state key in one transaction keyed by *op_id*; last write wins."""
    encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
    _check_size(encoded.encode("utf-8"))

    def _effect(connection: sqlite3.Connection) -> dict[str, object]:
        connection.execute(
            "INSERT INTO workflow_state (run_id, key, value_json, updated_by, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, key) DO UPDATE SET "
            "value_json = excluded.value_json, updated_by = excluded.updated_by, updated_at = excluded.updated_at",
            (run_id, key, encoded, updated_by, datetime.now(UTC).isoformat()),
        )
        return {"key": key, "saved": True}

    return cast("dict[str, object]", _once(store, run_id, op_id, _effect))


def script_op(
    store: WorkflowRunStore,
    run_id: str,
    *,
    key: str,
    generation: int,
    kind: str,
    args: dict[str, object],
) -> tuple[bool, object]:
    """Run one script `send`, `state`, or immediate `inbox` node; return `(completed, result)`."""
    op_id = f"{key}:{generation}"
    if kind == "send":
        return True, send(
            store,
            run_id,
            op_id=op_id,
            sender=NodeRef(key=key, generation=generation, name="script"),
            to=str(args.get("to") or ""),
            text=str(args.get("text") or ""),
        )
    if kind == "inbox":
        raw_wait = args.get("wait_seconds")
        wait_seconds = (
            max(0.0, float(raw_wait)) if isinstance(raw_wait, int | float) and not isinstance(raw_wait, bool) else 0.0
        )
        immediate = read_call(
            store,
            run_id,
            op_id=op_id,
            reader=NodeRef(key="script", generation=1, name="script"),
            names=("script",),
            record_empty=wait_seconds <= 0,
        )
        return (True, immediate) if immediate is not None else (False, wait_seconds)
    op, state_key = args.get("op"), str(args.get("key") or "")
    if op == "get":
        return True, get_state(store, run_id, op_id=op_id, key=state_key)
    if op == "set":
        return True, set_state(store, run_id, op_id=op_id, key=state_key, value=args.get("value"), updated_by="script")
    msg = f"Unknown state operation '{op}'."
    raise WorkflowRunStoreError(msg)


def format_session_message(message: str, msgs: Collection[dict[str, object]]) -> str:
    """Prepend between-turn peer *msgs* to a session turn *message*."""
    if not msgs:
        return message
    lines = "\n".join(f"{m['from']}: {m['text']}" for m in msgs)
    return f"Messages received since your last turn:\n{lines}\n\n{message}"


def session_turn_message(
    store: WorkflowRunStore,
    run_id: str,
    session: WorkflowNode,
    turn_key: str,
    turn_generation: int,
    message: str,
    *,
    saved_mail: list[dict[str, object]] | None = None,
) -> tuple[str, list[dict[str, object]]]:
    """Drain unread mail queued for *session*, merge with any *saved_mail* from a prior generation, and format."""
    session_name = session.name or str(session.request.get("name") or "")
    reader = NodeRef(
        key=turn_key,
        generation=turn_generation,
        name=session_name or f"n{session.seq}",
        reader_key=session.key,
        reader_generation=session.generation,
    )
    new_msgs = read_call(
        store,
        run_id,
        op_id=f"session_inbox:{turn_key}:{turn_generation}",
        reader=reader,
        names=(session_name, "*") if session_name else ("*",),
        record_empty=True,
    )
    mail = [*(saved_mail or ()), *(new_msgs or ())]
    return format_session_message(message, mail), mail


def withdraw_unread(store: WorkflowRunStore, run_id: str, sender_key: str, old_generation: int) -> int:
    """Mark unread messages from `(sender_key, old_generation)` withdrawn; return count withdrawn."""
    with store.write_transaction(run_id) as connection:
        cursor = connection.execute(
            """
            UPDATE workflow_messages SET withdrawn = 1
            WHERE run_id = ? AND sender_key = ? AND sender_generation = ? AND withdrawn = 0
              AND NOT EXISTS (
                SELECT 1 FROM workflow_cursors c
                WHERE c.run_id = workflow_messages.run_id
                  AND c.seq >= workflow_messages.seq
                  AND c.reader_key != workflow_messages.sender_key
                  AND c.reader_key != COALESCE(
                    (SELECT session_key FROM workflow_nodes
                     WHERE run_id = workflow_messages.run_id AND key = workflow_messages.sender_key),
                    ''
                  )
                  AND (
                    (workflow_messages.recipient_name = 'script' AND c.reader_key = 'script')
                    OR (
                      workflow_messages.recipient_name = '*' AND c.reader_key != 'script'
                      AND (
                        workflow_messages.recipient_keys_json IS NULL
                        OR EXISTS (
                          SELECT 1 FROM json_each(workflow_messages.recipient_keys_json) WHERE value = c.reader_key
                        )
                      )
                    )
                    OR EXISTS (
                      SELECT 1 FROM workflow_nodes n
                      WHERE n.run_id = c.run_id AND n.key = c.reader_key AND n.name = workflow_messages.recipient_name
                    )
                  )
              )
            """,
            (run_id, sender_key, old_generation),
        )
        return int(cursor.rowcount)


def list_peers(store: WorkflowRunStore, run_id: str) -> list[dict[str, object]]:
    """Return `name`, `status` and `label` of every current agent and session in *run_id*."""
    nodes = store.nodes(run_id, light=True)
    latest_turn = {n.session_key: n for n in nodes if n.kind == "turn" and n.session_key}
    out: list[dict[str, object]] = []
    for n in nodes:
        if n.kind == "agent":
            out.append({"id": f"n{n.seq}", "name": n.name, "status": n.state.value, "label": n.label})
        elif n.kind == "session":
            turn = latest_turn.get(n.key)
            status = turn.state.value if turn is not None and turn.state in LIVE_NODE_STATES else "idle"
            out.append({"id": f"n{n.seq}", "name": n.name, "status": status, "label": n.label, "session": True})
    return out


def message_edges(store: WorkflowRunStore, run_id: str, nodes: Iterable[WorkflowNode]) -> list[tuple[str, str, int]]:
    """Aggregate non-withdrawn messages into `(from_id, to_id, count)` graph edges (counts only, never text)."""
    current = [n for n in nodes if not n.stale]
    ids = {n.key: f"n{n.seq}" for n in current}
    session_of = {n.key: n.session_key for n in current if n.session_key}
    by_name: dict[str, list[str]] = {}
    for n in current:
        if n.kind in ("agent", "session") and n.name:
            by_name.setdefault(n.name, []).append(ids[n.key])
    with store.read_connection() as connection:
        rows = connection.execute(
            "SELECT sender_key, recipient_name, recipient_keys_json FROM workflow_messages "
            "WHERE run_id = ? AND withdrawn = 0",
            (run_id,),
        ).fetchall()
    counts: Counter[tuple[str, str]] = Counter()
    for row in rows:
        sender_key = row["sender_key"]
        if (from_id := ids.get(sender_key)) is None:
            continue
        self_ids = {from_id} | ({ids[sess]} if (sess := session_of.get(sender_key)) and sess in ids else set())
        frozen = row["recipient_keys_json"]
        pool = (
            [ids[k] for k in json.loads(frozen) if k in ids]
            if frozen is not None
            else by_name.get(row["recipient_name"], ())
        )
        for to_id in pool:
            if to_id not in self_ids:
                counts[(from_id, to_id)] += 1
    return [(s, r, c) for (s, r), c in sorted(counts.items())]


def all_messages(store: WorkflowRunStore, run_id: str) -> list[dict[str, Any]]:
    """Return the full, uncapped message history of *run_id* (for owner-checked `get_workflow(messages=True)`)."""
    with store.read_connection() as connection:
        rows = connection.execute(
            "SELECT seq, sender_key, sender_name, recipient_name, text, withdrawn, created_at "
            "FROM workflow_messages WHERE run_id = ? ORDER BY seq ASC",
            (run_id,),
        ).fetchall()
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
    with store.read_connection() as connection:
        return _all_state_rows(connection, run_id)
