"""Exactly-once peer messages and shared state for a workflow run, each operation in one SQLite transaction."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from mindroom.script_runs.models import utc_timestamp
from mindroom.workflow_runs.models import LIVE_NODE_STATES
from mindroom.workflow_runs.replay import result_digest
from mindroom.workflow_runs.store import WorkflowRunStoreError

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Collection, Iterable

    from mindroom.workflow_runs.models import WorkflowNode
    from mindroom.workflow_runs.store import WorkflowRunStore

MAX_PEER_BYTES = 1024 * 1024
DEADLOCK_NOTE = "Every agent is waiting for a message and nobody is left to send one."
_LIVE_VALUES = frozenset(state.value for state in LIVE_NODE_STATES)
_UNREACHABLE = "'{name}' has finished and will not read this. Use spawn() to keep an agent you want to talk to."
_TOO_LARGE = "Too large ({size:,} bytes, limit 1 MiB). Write it to a workspace file and send the path."


class PeerUnreachableError(WorkflowRunStoreError):
    """Raised when messaging a peer name whose agents have all finished."""


class PeerTooLargeError(WorkflowRunStoreError):
    """Raised when a peer message or shared-state value exceeds the 1 MiB limit."""


@dataclass(frozen=True, slots=True)
class NodeRef:
    """Identifies the sender or reader of a peer operation; a session turn reads through its session cursor."""

    key: str
    generation: int
    name: str
    reader_key: str | None = None
    reader_generation: int | None = None

    @property
    def cursor_key(self) -> tuple[str, int]:
        """Return the `(reader_key, reader_generation)` pair that owns this reader's cursor."""
        return (
            self.key if self.reader_key is None else self.reader_key,
            self.generation if self.reader_generation is None else self.reader_generation,
        )


def peer_op_id(
    node_key: str,
    generation: int,
    model_run_id: str,
    tool_call_id: str,
    operation: str,
    arguments: dict[str, object],
) -> str:
    """Return the stable SHA-256 operation ID for one agent peer tool call."""
    return result_digest([node_key, generation, model_run_id, tool_call_id, operation, arguments])


def _check_size(raw: bytes) -> None:
    if len(raw) > MAX_PEER_BYTES:
        raise PeerTooLargeError(_TOO_LARGE.format(size=len(raw)))


def _saved_call(connection: sqlite3.Connection, run_id: str, op_id: str) -> tuple[bool, object]:
    row = connection.execute(
        "SELECT result_json FROM workflow_peer_calls WHERE run_id = ? AND op_id = ?",
        (run_id, op_id),
    ).fetchone()
    return (False, None) if row is None else (True, json.loads(row["result_json"]))


def _save_call(connection: sqlite3.Connection, run_id: str, op_id: str, result: object) -> None:
    connection.execute(
        "INSERT INTO workflow_peer_calls (run_id, op_id, result_json) VALUES (?, ?, ?)",
        (run_id, op_id, json.dumps(result, separators=(",", ":"))),
    )


def _self_keys(connection: sqlite3.Connection, run_id: str, ref: NodeRef) -> set[str]:
    """Return the keys belonging to *ref* itself (and all turns of its session, when reading/sending as a session)."""
    keys = {ref.key}
    if ref.reader_key is not None:
        keys.add(ref.reader_key)
        rows = connection.execute(
            "SELECT key FROM workflow_nodes WHERE run_id = ? AND session_key = ?",
            (run_id, ref.reader_key),
        ).fetchall()
        keys.update(row["key"] for row in rows)
    return keys


def send(
    store: WorkflowRunStore,
    run_id: str,
    *,
    op_id: str,
    sender: NodeRef,
    to: str,
    text: str,
) -> dict[str, object]:
    """Send one message in one transaction keyed by *op_id*; return `{delivered, held}`."""
    if not to:
        msg = "Recipient 'to' must be a non-empty peer name, '*', or 'script'."
        raise WorkflowRunStoreError(msg)
    _check_size(text.encode("utf-8"))
    with store.write_transaction(run_id) as connection:
        found, saved = _saved_call(connection, run_id, op_id)
        if found:
            return cast("dict[str, object]", saved)
        self_keys = _self_keys(connection, run_id, sender)
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
                    r
                    for r in rows
                    if r["key"] not in self_keys and (r["kind"] == "session" or r["state"] in _LIVE_VALUES)
                ]
                delivered, held = len(live), len(live) == 0
            else:
                sessions = [r for r in rows if r["kind"] == "session" and r["name"] == to]
                agents = [r for r in rows if r["kind"] == "agent" and r["name"] == to]
                live_agents = [r for r in agents if r["state"] in _LIVE_VALUES]
                if agents and not live_agents and not sessions:
                    raise PeerUnreachableError(_UNREACHABLE.format(name=to))
                if not agents and not sessions:
                    delivered, held = 0, True
                else:
                    recipients = [r for r in (*live_agents, *sessions) if r["key"] not in self_keys]
                    delivered, held = len(recipients), len(recipients) == 0
        seq = int(
            connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM workflow_messages WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0],
        )
        connection.execute(
            "INSERT INTO workflow_messages "
            "(run_id, seq, sender_key, sender_generation, sender_name, recipient_name, text, withdrawn, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (run_id, seq, sender.key, sender.generation, sender.name, to, text, utc_timestamp()),
        )
        result: dict[str, object] = {"delivered": delivered, "held": held}
        _save_call(connection, run_id, op_id, result)
        return result


def read_call(
    store: WorkflowRunStore,
    run_id: str,
    *,
    op_id: str,
    reader: NodeRef,
    names: Collection[str],
    record_empty: bool = True,
) -> tuple[list[dict[str, object]], str | None] | None:
    """Read unread messages for *reader* in one transaction keyed by *op_id*.

    Returns `(messages, note)` when saved or delivered (or empty with `record_empty=True`), else `None` so a
    waiting caller can sleep without committing an empty result under *op_id*.
    """
    reader_key, reader_gen = reader.cursor_key
    name_set = frozenset(names)
    with store.write_transaction() as connection:
        found, saved = _saved_call(connection, run_id, op_id)
        if found:
            if isinstance(saved, dict):
                return cast("list[dict[str, object]]", saved.get("messages", [])), cast("str | None", saved.get("note"))
            return cast("list[dict[str, object]]", saved), None
        row = connection.execute(
            "SELECT seq FROM workflow_cursors WHERE run_id = ? AND reader_key = ? AND reader_generation = ?",
            (run_id, reader_key, reader_gen),
        ).fetchone()
        cursor = 0 if row is None else int(row["seq"])
        self_keys = _self_keys(connection, run_id, reader)
        rows = connection.execute(
            "SELECT seq, sender_key, sender_name, recipient_name, text, created_at "
            "FROM workflow_messages WHERE run_id = ? AND seq > ? AND withdrawn = 0 ORDER BY seq ASC",
            (run_id, cursor),
        ).fetchall()
        matched = [r for r in rows if r["recipient_name"] in name_set and r["sender_key"] not in self_keys]
        if not matched and not record_empty:
            return None
        messages: list[dict[str, object]] = [
            {"from": r["sender_name"], "text": r["text"], "sent_at": r["created_at"]} for r in matched
        ]
        if matched:
            new_cursor = int(matched[-1]["seq"])
            connection.execute(
                "INSERT INTO workflow_cursors (run_id, reader_key, reader_generation, seq) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(run_id, reader_key, reader_generation) DO UPDATE SET seq = excluded.seq",
                (run_id, reader_key, reader_gen, new_cursor),
            )
        _save_call(connection, run_id, op_id, messages)
        return messages, None


def has_unread(store: WorkflowRunStore, run_id: str, *, reader: NodeRef, names: Collection[str]) -> bool:
    """Return whether *reader* has any unread, non-withdrawn message waiting in *run_id*."""
    reader_key, reader_gen = reader.cursor_key
    name_set = frozenset(names)
    with store.read_connection() as connection:
        row = connection.execute(
            "SELECT seq FROM workflow_cursors WHERE run_id = ? AND reader_key = ? AND reader_generation = ?",
            (run_id, reader_key, reader_gen),
        ).fetchone()
        cursor = 0 if row is None else int(row["seq"])
        self_keys = _self_keys(connection, run_id, reader)
        rows = connection.execute(
            "SELECT sender_key, recipient_name FROM workflow_messages WHERE run_id = ? AND seq > ? AND withdrawn = 0",
            (run_id, cursor),
        ).fetchall()
    return any(r["recipient_name"] in name_set and r["sender_key"] not in self_keys for r in rows)


def read(
    store: WorkflowRunStore,
    run_id: str,
    *,
    op_id: str,
    reader: NodeRef,
    names: Collection[str],
) -> list[dict[str, object]]:
    """Return unread, non-withdrawn messages for *reader* in one transaction keyed by *op_id*."""
    result = read_call(store, run_id, op_id=op_id, reader=reader, names=names, record_empty=True)
    assert result is not None
    return result[0]


def record_deadlock(store: WorkflowRunStore, run_id: str, *, op_id: str) -> tuple[list[dict[str, object]], str]:
    """Persist and return the deadlock notice for *op_id* unless *op_id* already saved a result."""
    with store.write_transaction() as connection:
        found, saved = _saved_call(connection, run_id, op_id)
        if found:
            if isinstance(saved, dict):
                return (
                    cast("list[dict[str, object]]", saved.get("messages", [])),
                    str(saved.get("note") or DEADLOCK_NOTE),
                )
            return cast("list[dict[str, object]]", saved), DEADLOCK_NOTE
        payload: dict[str, object] = {"messages": [], "note": DEADLOCK_NOTE}
        _save_call(connection, run_id, op_id, payload)
        return [], DEADLOCK_NOTE


def get_state_entry(store: WorkflowRunStore, run_id: str, *, op_id: str, key: str) -> dict[str, object]:
    """Read one key from shared state in one transaction keyed by *op_id*; returns `{found, value}`."""
    with store.write_transaction() as connection:
        found, saved = _saved_call(connection, run_id, op_id)
        if found:
            return cast("dict[str, object]", saved)
        row = connection.execute(
            "SELECT value_json FROM workflow_state WHERE run_id = ? AND key = ?",
            (run_id, key),
        ).fetchone()
        result: dict[str, object] = {
            "found": row is not None,
            "value": None if row is None else json.loads(row["value_json"]),
        }
        _save_call(connection, run_id, op_id, result)
        return result


def get_state(
    store: WorkflowRunStore,
    run_id: str,
    *,
    op_id: str,
    key: str | None = None,
    default: object = None,
) -> object:
    """Read one shared-state key (or all keys when *key* is None) in one transaction keyed by *op_id*."""
    if key is not None:
        entry = get_state_entry(store, run_id, op_id=op_id, key=key)
        return entry["value"] if entry["found"] else default
    with store.write_transaction() as connection:
        found, saved = _saved_call(connection, run_id, op_id)
        if found:
            return saved
        rows = connection.execute(
            "SELECT key, value_json FROM workflow_state WHERE run_id = ? ORDER BY key ASC",
            (run_id,),
        ).fetchall()
        result = {row["key"]: json.loads(row["value_json"]) for row in rows}
        _save_call(connection, run_id, op_id, result)
        return result


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
    with store.write_transaction() as connection:
        found, saved = _saved_call(connection, run_id, op_id)
        if found:
            return cast("dict[str, object]", saved)
        connection.execute(
            "INSERT INTO workflow_state (run_id, key, value_json, updated_by, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, key) DO UPDATE SET "
            "value_json = excluded.value_json, updated_by = excluded.updated_by, updated_at = excluded.updated_at",
            (run_id, key, encoded, updated_by, utc_timestamp()),
        )
        result: dict[str, object] = {"key": key, "saved": True}
        _save_call(connection, run_id, op_id, result)
        return result


def withdraw_unread(store: WorkflowRunStore, run_id: str, sender_key: str, old_generation: int) -> int:
    """Mark unread messages from `(sender_key, old_generation)` withdrawn; return count withdrawn."""
    with store.write_transaction(run_id) as connection:
        msgs = connection.execute(
            "SELECT seq, recipient_name FROM workflow_messages "
            "WHERE run_id = ? AND sender_key = ? AND sender_generation = ? AND withdrawn = 0",
            (run_id, sender_key, old_generation),
        ).fetchall()
        if not msgs:
            return 0
        cursors = connection.execute(
            "SELECT reader_key, MAX(seq) AS max_seq FROM workflow_cursors WHERE run_id = ? GROUP BY reader_key",
            (run_id,),
        ).fetchall()
        by_reader = {row["reader_key"]: int(row["max_seq"]) for row in cursors}
        nodes = connection.execute(
            "SELECT key, name, session_key FROM workflow_nodes WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        reader_names: dict[str, str | None] = {row["key"]: row["name"] for row in nodes}
        sender_session = next((row["session_key"] for row in nodes if row["key"] == sender_key), None)
        self_readers = {sender_key} | ({sender_session} if sender_session else set())

        def _was_read(seq: int, recipient: str) -> bool:
            if recipient == "script":
                return by_reader.get("script", 0) >= seq
            if recipient == "*":
                return any(
                    r_seq >= seq
                    for r_key, r_seq in by_reader.items()
                    if r_key != "script" and r_key not in self_readers
                )
            return any(
                r_seq >= seq
                for r_key, r_seq in by_reader.items()
                if reader_names.get(r_key) == recipient and r_key not in self_readers
            )

        to_withdraw = [(run_id, int(m["seq"])) for m in msgs if not _was_read(int(m["seq"]), m["recipient_name"])]
        if to_withdraw:
            connection.executemany(
                "UPDATE workflow_messages SET withdrawn = 1 WHERE run_id = ? AND seq = ?",
                to_withdraw,
            )
        return len(to_withdraw)


def list_peers(store: WorkflowRunStore, run_id: str) -> list[dict[str, object]]:
    """Return `name`, `status` and `label` of every current agent and session in *run_id*."""
    nodes = store.nodes(run_id, light=True)
    latest_turn: dict[str, WorkflowNode] = {}
    for node in nodes:
        if node.kind == "turn" and node.session_key:
            latest_turn[node.session_key] = node
    peers_list: list[dict[str, object]] = []
    for node in nodes:
        if node.kind == "agent":
            peers_list.append(
                {"id": f"n{node.seq}", "name": node.name, "status": node.state.value, "label": node.label},
            )
        elif node.kind == "session":
            turn = latest_turn.get(node.key)
            status = turn.state.value if turn is not None and turn.state in LIVE_NODE_STATES else "idle"
            peers_list.append(
                {
                    "id": f"n{node.seq}",
                    "name": node.name,
                    "status": status,
                    "label": node.label,
                    "session": True,
                },
            )
    return peers_list


def message_edges(store: WorkflowRunStore, run_id: str, nodes: Iterable[WorkflowNode]) -> list[tuple[str, str, int]]:
    """Aggregate non-withdrawn messages into `(from_id, to_id, count)` graph edges (counts only, never text)."""
    current = [node for node in nodes if not node.stale]
    ids = {node.key: f"n{node.seq}" for node in current}
    peers_by_name: dict[str, list[str]] = {}
    all_peer_ids: list[tuple[str, str]] = []
    session_of = {node.key: node.session_key for node in current if node.session_key}
    for node in current:
        if node.kind in ("agent", "session"):
            nid = ids[node.key]
            all_peer_ids.append((node.key, nid))
            if node.name:
                peers_by_name.setdefault(node.name, []).append(nid)
    with store.read_connection() as connection:
        rows = connection.execute(
            "SELECT sender_key, recipient_name FROM workflow_messages WHERE run_id = ? AND withdrawn = 0",
            (run_id,),
        ).fetchall()
    counts: Counter[tuple[str, str]] = Counter()
    for row in rows:
        sender_key = row["sender_key"]
        from_id = ids.get(sender_key)
        if from_id is None:
            continue
        self_ids = {from_id}
        if (sess := session_of.get(sender_key)) and sess in ids:
            self_ids.add(ids[sess])
        recipient = row["recipient_name"]
        if recipient == "*":
            targets = [nid for _key, nid in all_peer_ids if nid not in self_ids]
        else:
            targets = [nid for nid in peers_by_name.get(recipient, ()) if nid not in self_ids]
        for to_id in targets:
            counts[(from_id, to_id)] += 1
    return [(sender, receiver, count) for (sender, receiver), count in sorted(counts.items())]


def all_messages(store: WorkflowRunStore, run_id: str) -> list[dict[str, Any]]:
    """Return the full, uncapped message history of *run_id* (for owner-checked `get_workflow(messages=True)`)."""
    with store.read_connection() as connection:
        rows = connection.execute(
            "SELECT seq, sender_key, sender_generation, sender_name, recipient_name, text, withdrawn, created_at "
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
        rows = connection.execute(
            "SELECT key, value_json FROM workflow_state WHERE run_id = ? ORDER BY key ASC",
            (run_id,),
        ).fetchall()
    return {row["key"]: json.loads(row["value_json"]) for row in rows}
