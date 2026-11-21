"""SQLite-backed durable state of workflow runs: runs, nodes, usage entries and log lines."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, fields, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from mindroom.durable_write import create_directory_durable, write_json_file_durable
from mindroom.script_runs.models import utc_timestamp
from mindroom.workflow_runs.models import (
    TERMINAL_RUN_STATES,
    DispatchHandle,
    WorkflowNode,
    WorkflowNodeState,
    WorkflowRun,
    WorkflowRunState,
    validated_workflow_run_id,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Iterator
    from pathlib import Path

_BUSY_TIMEOUT_SECONDS = 10
_MAX_INLINE_RESULT_BYTES = 8 * 1024 * 1024
_RESULT_PREVIEW_BYTES = 64 * 1024
_RUN_JSON_FIELDS = frozenset({"execution_identity", "persona", "args", "result"})
_NODE_JSON_FIELDS = frozenset({"request", "handle", "members", "result", "error", "links"})
# A run keeps the owner and the origin key it was reserved with; a redelivered start is checked against both.
_OWNER_COLUMNS = ("agent_name", "owner_user_id", "room_id", "thread_root_event_id")
_RUN_UPDATE_FIELDS = frozenset(field.name for field in fields(WorkflowRun)) - {
    "run_id",
    "origin_job_id",
    *_OWNER_COLUMNS,
}
_ENDED_NODE_STATES = frozenset(WorkflowNodeState) - {
    WorkflowNodeState.QUEUED,
    WorkflowNodeState.RUNNING,
    WorkflowNodeState.WAITING,
}
# What a stamp may change on a node: never its identity, its generation, or its outcome.
_NODE_STAMP_FIELDS = frozenset(
    {"state", "waiting_on", "handle", "attempt", "stale", "label", "name", "phase", "group_key", "members"}
    | {"session_key", "cached", "started_at", "links"},
)
_BLANK_NODE = WorkflowNode(run_id="", key="", kind="", lane="", idx=0, digest="", request={}, attempt=0)
_TERMINAL_STATE_VALUES = tuple(sorted(TERMINAL_RUN_STATES))
_TERMINAL_STATE_PLACEHOLDERS = ", ".join("?" * len(_TERMINAL_STATE_VALUES))

type UsageTotals = dict[str, float | bool]


class WorkflowRunStoreError(ValueError):
    """Raised when durable workflow state cannot be used safely."""


class WorkflowRunNotFoundError(WorkflowRunStoreError):
    """Raised for a run ID that is unknown, or that the caller does not own."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"No workflow run {run_id} in your runs.")


class WorkflowRunConflictError(WorkflowRunStoreError):
    """Raised when a reserve would reuse a run ID, or hits a run of another owner, script or arguments."""


class WorkflowRunStore:
    """Persist workflow runs and their nodes with atomic SQLite transactions."""

    def __init__(self, path: Path) -> None:
        self.database_path = path
        self.directory = path.parent
        self.directory.mkdir(parents=True, exist_ok=True)
        self._enter_wal()
        self._migrate()

    def reserve_run(self, run: WorkflowRun, *, max_active: int | None = None) -> tuple[WorkflowRun, bool]:
        """Store *run*, or return the run its `origin_job_id` already reserved and `False`.

        A new top-level run is refused once its agent and owner have *max_active* live ones, counted in the write
        that would store it, so racing starts never exceed the limit and a refused one leaves no row.
        """
        validated_workflow_run_id(run.run_id)
        values = {**_row_values(run, _RUN_JSON_FIELDS), "reservation_digest": _reservation_digest(run)}
        with self._write_transaction() as connection:
            if run.origin_job_id is not None:
                row = connection.execute(
                    "SELECT * FROM workflow_runs WHERE origin_job_id = ?",
                    (run.origin_job_id,),
                ).fetchone()
                if row is not None:
                    same_owner = all(row[column] == values[column] for column in _OWNER_COLUMNS)
                    if not same_owner or row["reservation_digest"] != values["reservation_digest"]:
                        # Nothing of the run it hit, its ID included, is told to a caller that may not own it.
                        msg = "This call already started a workflow run with another owner, script or arguments."
                        raise WorkflowRunConflictError(msg)
                    return _run_from_row(row), False
            if max_active is not None and run.parent_run_id is None:
                active = connection.execute(
                    "SELECT COUNT(*) FROM workflow_runs WHERE agent_name = ? AND owner_user_id = ? "  # noqa: S608
                    f"AND parent_run_id IS NULL AND state NOT IN ({_TERMINAL_STATE_PLACEHOLDERS})",
                    (run.agent_name, run.owner_user_id, *_TERMINAL_STATE_VALUES),
                ).fetchone()[0]
                if active >= max_active:
                    msg = f"You have {max_active} workflow runs in progress, the limit. Wait for one to end or cancel one."
                    raise WorkflowRunStoreError(msg)
            try:
                _insert(connection, "INSERT INTO workflow_runs", values)
            except sqlite3.IntegrityError as exc:
                msg = f"Workflow run {run.run_id} already exists."
                raise WorkflowRunConflictError(msg) from exc
        return run, True

    def delete_intent(self, run_id: str) -> bool:
        """Delete a run that never got an attempt; return whether one was deleted."""
        with self._write_transaction() as connection:
            deleted = connection.execute(
                "DELETE FROM workflow_runs WHERE run_id = ? AND state = ? AND attempt = 0",
                (run_id, WorkflowRunState.STARTING),
            )
        return deleted.rowcount == 1

    def get_run(self, run_id: str) -> WorkflowRun:
        """Return one run, or raise `WorkflowRunNotFoundError`."""
        with self._connection() as connection:
            return _select_run(connection, run_id)

    def update_run(self, run_id: str, *, when: Collection[WorkflowRunState] = (), **changes: object) -> WorkflowRun:
        """Write the named fields of one run and return the stored run.

        With *when*, nothing is written unless the run is in one of those states, so of two writers that each move
        the run on from the same state, the first wins and the second gets what it stored.
        """
        unknown = changes.keys() - _RUN_UPDATE_FIELDS
        if unknown:
            msg = f"Workflow runs have no field to update named {', '.join(sorted(unknown))}."
            raise WorkflowRunStoreError(msg)
        with self._write_transaction() as connection:
            run = _select_run(connection, run_id)
            if when and run.state not in when:
                return run
            run = replace(run, **changes)
            values = _row_values(run, _RUN_JSON_FIELDS)
            columns = [_column(name, _RUN_JSON_FIELDS) for name in changes]
            if columns:
                connection.execute(
                    f"UPDATE workflow_runs SET {', '.join(f'{column} = ?' for column in columns)} WHERE run_id = ?",  # noqa: S608
                    (*(values[column] for column in columns), run_id),
                )
        return run

    def list_runs(
        self,
        *,
        agent_name: str | None = None,
        owner_user_id: str | None = None,
        room_id: str | None = None,
        thread_root_event_id: str | None = None,
        top_level_only: bool = False,
        include_finished: bool = True,
    ) -> list[WorkflowRun]:
        """List runs, newest first, narrowed by each filter that is given."""
        filters = {
            "agent_name": agent_name,
            "owner_user_id": owner_user_id,
            "room_id": room_id,
            "thread_root_event_id": thread_root_event_id,
        }
        clauses = [f"{column} = ?" for column, value in filters.items() if value is not None]
        params: list[object] = [value for value in filters.values() if value is not None]
        if top_level_only:
            clauses.append("parent_run_id IS NULL")
        if not include_finished:
            clauses.append(f"state NOT IN ({_TERMINAL_STATE_PLACEHOLDERS})")
            params.extend(_TERMINAL_STATE_VALUES)
        return self._select_runs(" AND ".join(clauses) or "1", params)

    def run_for_script(self, script_run_id: str) -> WorkflowRun | None:
        """Return the run whose current attempt is the script run *script_run_id*."""
        runs = self._select_runs("script_run_id = ?", [script_run_id])
        return runs[0] if runs else None

    def child_runs(self, run_id: str) -> list[WorkflowRun]:
        """Return the runs that nodes of *run_id* started."""
        return self._select_runs("parent_run_id = ?", [run_id])

    def put_node(self, node: WorkflowNode) -> WorkflowNode:
        """Claim a new key, or the next generation of a saved one, and return the node as stored.

        A new key gets the run's next sequence number and a saved one keeps its own.
        Any other generation is refused, so a writer that read an older row cannot replace a newer one.
        A result over the inline limit moves to a file.
        """
        with self._write_transaction() as connection:
            saved = connection.execute(
                "SELECT seq, generation FROM workflow_nodes WHERE run_id = ? AND key = ?",
                (node.run_id, node.key),
            ).fetchone()
            if saved is None:
                saved = connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM workflow_nodes WHERE run_id = ?",
                    (node.run_id,),
                ).fetchone()
            elif node.generation != saved["generation"] + 1:
                msg = f"Node {node.key} is at generation {saved['generation']}; generation {node.generation} cannot replace it."
                raise WorkflowRunConflictError(msg)
            result, result_file = self._inline_result(node.run_id, saved["seq"], node.generation, node.result)
            node = replace(node, seq=saved["seq"], result=result, result_file=result_file)
            _insert(connection, "INSERT OR REPLACE INTO workflow_nodes", _row_values(node, _NODE_JSON_FIELDS))
            _count_progress(connection, node.run_id, node.state)
        return node

    def update_node(self, run_id: str, key: str, generation: int, **changes: object) -> bool:
        """Stamp fields on *generation* of a node; false and no change when the node has moved on.

        A stamp never writes an outcome, and it cannot change the state of a node that has ended.
        """
        if not changes or changes.keys() - _NODE_STAMP_FIELDS or changes.get("state") in _ENDED_NODE_STATES:
            msg = f"A node stamp cannot set {sorted(changes)}; outcomes go through settle_node."
            raise WorkflowRunStoreError(msg)
        values = _row_values(replace(_BLANK_NODE, **changes), _NODE_JSON_FIELDS)
        columns = [_column(name, _NODE_JSON_FIELDS) for name in changes]
        unsettled = " AND state IN ('queued', 'running', 'waiting')" if "state" in changes else ""
        with self._write_transaction() as connection:
            updated = connection.execute(
                f"UPDATE workflow_nodes SET {', '.join(f'{column} = ?' for column in columns)} "  # noqa: S608
                f"WHERE run_id = ? AND key = ? AND generation = ?{unsettled}",
                (*(values[column] for column in columns), run_id, key, generation),
            )
        return updated.rowcount == 1

    def get_node(self, run_id: str, key: str) -> WorkflowNode | None:
        """Return the node at *key*, if the run has one."""
        nodes = self._select_nodes("run_id = ? AND key = ?", (run_id, key))
        return nodes[0] if nodes else None

    def node_by_seq(self, run_id: str, seq: int) -> WorkflowNode | None:
        """Return the node with the public ID `n<seq>`, if the run has one."""
        nodes = self._select_nodes("run_id = ? AND seq = ?", (run_id, seq))
        return nodes[0] if nodes else None

    def nodes(self, run_id: str, *, include_stale: bool = False) -> list[WorkflowNode]:
        """Return a run's nodes in the order they were first submitted."""
        return self._select_nodes("run_id = ?" if include_stale else "run_id = ? AND stale = 0", (run_id,))

    def settle_node(
        self,
        run_id: str,
        key: str,
        generation: int,
        *,
        state: WorkflowNodeState,
        result: object | None = None,
        error: dict[str, object] | None = None,
    ) -> bool:
        """Record how *generation* of a node ended; false and no change when it has moved on or has an outcome.

        An unsettled node takes any end, and an `indeterminate` one takes the proven outcome of a late receipt.
        """
        if state not in _ENDED_NODE_STATES:
            msg = f"A node settles into an ended state, not {state}."
            raise WorkflowRunStoreError(msg)
        with self._write_transaction() as connection:
            saved = connection.execute(
                "SELECT seq, state, result_file FROM workflow_nodes WHERE run_id = ? AND key = ? AND generation = ?",
                (run_id, key, generation),
            ).fetchone()
            if saved is None or saved["result_file"] is not None:
                return False
            proven = saved["state"] == WorkflowNodeState.INDETERMINATE and state is not WorkflowNodeState.INDETERMINATE
            if saved["state"] in _ENDED_NODE_STATES and not proven:
                return False
            stored, result_file = self._inline_result(run_id, saved["seq"], generation, result)
            connection.execute(
                """
                UPDATE workflow_nodes
                SET state = ?, result_json = ?, result_file = ?, error_json = ?, ended_at = ?, waiting_on = NULL
                WHERE run_id = ? AND key = ?
                """,
                (state, _dumps(stored), result_file, _dumps(error), utc_timestamp(), run_id, key),
            )
            _count_progress(connection, run_id, state)
        return True

    def mark_stale(self, run_id: str, keys: Iterable[str]) -> None:
        """Take the nodes at *keys* out of the run's current view."""
        with self._write_transaction() as connection:
            connection.executemany(
                "UPDATE workflow_nodes SET stale = 1 WHERE run_id = ? AND key = ?",
                [(run_id, key) for key in keys],
            )

    def add_usage(
        self,
        run_id: str,
        key: str,
        generation: int,
        turn: int,
        attempt: int,
        usage: dict[str, object],
    ) -> bool:
        """Append one model turn's usage; a repeat of the same turn changes nothing and returns false."""
        with self._write_transaction() as connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO workflow_usage (run_id, key, generation, turn, attempt, usage_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, key, generation, turn, attempt, _dumps(usage)),
            )
        return inserted.rowcount == 1

    def usage_totals(self, run_id: str, *, attempt: int | None = None) -> UsageTotals:
        """Sum a run's usage, of every attempt or of one."""
        return _totals(usage for _key, entry_attempt, usage in self._usage(run_id) if attempt in (None, entry_attempt))

    def node_usage(self, run_id: str) -> dict[str, UsageTotals]:
        """Sum a run's usage per node key, over every generation of the node."""
        by_key: dict[str, list[dict[str, Any]]] = {}
        for key, _attempt, usage in self._usage(run_id):
            by_key.setdefault(key, []).append(usage)
        return {key: _totals(entries) for key, entries in by_key.items()}

    def append_log(self, run_id: str, text: str) -> None:
        """Append one line to a run's log."""
        with self._write_transaction() as connection:
            connection.execute(
                "INSERT INTO workflow_log (run_id, created_at, text) VALUES (?, ?, ?)",
                (run_id, utc_timestamp(), text),
            )

    def log(self, run_id: str, limit: int = 50) -> list[str]:
        """Return the newest *limit* lines of a run's log, oldest first."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT text FROM workflow_log WHERE run_id = ? ORDER BY id DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        return [row["text"] for row in reversed(rows)]

    def prune(self, finished_before: str) -> int:
        """Delete finished runs that ended before *finished_before*, with all they own; return how many.

        The timestamps are compared as instants, because their text leaves out a zero fraction of a second.
        """
        cutoff = datetime.fromisoformat(finished_before)
        with self._write_transaction() as connection:
            rows = connection.execute(
                f"SELECT run_id, finished_at FROM workflow_runs "  # noqa: S608
                f"WHERE state IN ({_TERMINAL_STATE_PLACEHOLDERS}) AND finished_at IS NOT NULL",
                _TERMINAL_STATE_VALUES,
            ).fetchall()
            run_ids = [row["run_id"] for row in rows if datetime.fromisoformat(row["finished_at"]) < cutoff]
            connection.executemany("DELETE FROM workflow_runs WHERE run_id = ?", [(run_id,) for run_id in run_ids])
        for run_id in run_ids:
            shutil.rmtree(self.directory / "results" / run_id, ignore_errors=True)
        return len(run_ids)

    def _inline_result(self, run_id: str, seq: int, generation: int, result: object) -> tuple[object, str | None]:
        """Return the result to keep in the row and, when it was too large, the file that holds all of it.

        The file is published whole and durably before the caller commits the row that names it.
        Callers never ask for the file of a row that is already committed with one.
        """
        encoded = _dumps(result)  # ASCII, so its length is its size in bytes
        if len(encoded) <= _MAX_INLINE_RESULT_BYTES:
            return result, None
        result_file = f"results/{run_id}/{seq}-{generation}.json"
        path = self.directory / result_file
        for directory in (path.parent.parent, path.parent):
            create_directory_durable(directory, mode=0o700)
        write_json_file_durable(path, result, strict_atomic_replace=True)
        return encoded[:_RESULT_PREVIEW_BYTES], result_file

    def _select_runs(self, where: str, params: Iterable[object]) -> list[WorkflowRun]:
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM workflow_runs WHERE {where} ORDER BY created_at DESC, run_id DESC",  # noqa: S608
                tuple(params),
            ).fetchall()
        return [_run_from_row(row) for row in rows]

    def _select_nodes(self, where: str, params: tuple[object, ...]) -> list[WorkflowNode]:
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM workflow_nodes WHERE {where} ORDER BY seq",  # noqa: S608
                params,
            ).fetchall()
        return [_node_from_row(row) for row in rows]

    def _usage(self, run_id: str) -> list[tuple[str, int, dict[str, Any]]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT key, attempt, usage_json FROM workflow_usage WHERE run_id = ?",
                (run_id,),
            ).fetchall()
        return [(row["key"], row["attempt"], json.loads(row["usage_json"])) for row in rows]

    def _migrate(self) -> None:
        with self._write_transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS workflow_schema (version INTEGER NOT NULL)")
            row = connection.execute("SELECT version FROM workflow_schema").fetchone()
            version = 0 if row is None else int(row["version"])
            if version > len(_MIGRATIONS):
                msg = f"Workflow store is from a newer MindRoom (schema {version})."
                raise WorkflowRunStoreError(msg)
            pending = _MIGRATIONS[version:]
            for migration in pending:
                migration(connection)
            if pending:
                connection.execute("DELETE FROM workflow_schema")
                connection.execute("INSERT INTO workflow_schema (version) VALUES (?)", (len(_MIGRATIONS),))

    def _enter_wal(self) -> None:
        """Switch the database to WAL, which it then keeps, when the store opens.

        SQLite refuses this one statement at once, without waiting out the busy timeout, while
        another opener holds the database, so it is retried within the same bound.
        """
        deadline = time.monotonic() + _BUSY_TIMEOUT_SECONDS
        with self._connection() as connection:
            while True:
                try:
                    connection.execute("PRAGMA journal_mode = WAL")
                except sqlite3.OperationalError as error:
                    if error.sqlite_errorcode != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
                else:
                    return

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, isolation_level=None, timeout=_BUSY_TIMEOUT_SECONDS)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")


def _dumps(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


def _column(field_name: str, json_fields: frozenset[str]) -> str:
    return f"{field_name}_json" if field_name in json_fields else field_name


def _row_values(record: WorkflowRun | WorkflowNode, json_fields: frozenset[str]) -> dict[str, object]:
    """Return a record's column values; the fields in *json_fields* are stored as JSON text."""
    return {
        _column(name, json_fields): _dumps(value) if name in json_fields else value
        for name, value in asdict(record).items()
    }


def _insert(connection: sqlite3.Connection, insert_into: str, values: dict[str, object]) -> None:
    connection.execute(
        f"{insert_into} ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
        tuple(values.values()),
    )


def _record_fields(row: sqlite3.Row) -> dict[str, Any]:
    return {
        column.removesuffix("_json"): json.loads(row[column]) if column.endswith("_json") else row[column]
        for column in row.keys()  # noqa: SIM118
    }


def _run_from_row(row: sqlite3.Row) -> WorkflowRun:
    values = _record_fields(row)
    del values["reservation_digest"]
    values["state"] = WorkflowRunState(values["state"])
    return WorkflowRun(**values)


def _node_from_row(row: sqlite3.Row) -> WorkflowNode:
    values = _record_fields(row)
    values.update(state=WorkflowNodeState(values["state"]), stale=bool(values["stale"]), cached=bool(values["cached"]))
    if values["handle"] is not None:
        values["handle"] = DispatchHandle(**values["handle"])
    if values["members"] is not None:
        values["members"] = tuple(values["members"])
    return WorkflowNode(**values)


def _select_run(connection: sqlite3.Connection, run_id: str) -> WorkflowRun:
    row = connection.execute("SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise WorkflowRunNotFoundError(run_id)
    return _run_from_row(row)


def owned_run(store: WorkflowRunStore, run_id: str, *, agent_name: str, requester_id: str) -> WorkflowRun:
    """Return the run *run_id* for its owner; another owner's ID raises exactly as an unknown one does.

    Every lookup of a run for a caller goes through here.
    """
    run = store.get_run(run_id)
    if run.agent_name != agent_name or run.owner_user_id != requester_id:
        raise WorkflowRunNotFoundError(run_id)
    return run


def _reservation_digest(run: WorkflowRun) -> str:
    """Return the digest of what a redelivered start must share with the run it hits.

    It is taken once, when the run is reserved: the owner, the script as first submitted, and the
    arguments in canonical JSON, in which key order does not matter and `true` is not `1`.
    """
    identity = [run.agent_name, run.owner_user_id, run.room_id, run.thread_root_event_id, run.source_digest, run.args]
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _totals(entries: Iterable[dict[str, Any]]) -> UsageTotals:
    """Sum usage entries; an entry without a price makes the cost a lower bound."""
    totals: UsageTotals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "tool_calls": 0,
        "cost_usd": 0.0,
        "cost_unknown": False,
    }
    for entry in entries:
        for name in ("input_tokens", "output_tokens", "tool_calls"):
            totals[name] += entry.get(name) or 0
        if entry.get("cost_usd") is None:
            totals["cost_unknown"] = True
        else:
            totals["cost_usd"] += entry["cost_usd"]
    return totals


def _count_progress(connection: sqlite3.Connection, run_id: str, state: WorkflowNodeState) -> None:
    """Start the run's count of idle attempts again with the node that completed, in that node's own transaction."""
    if state is WorkflowNodeState.COMPLETED:
        connection.execute("UPDATE workflow_runs SET idle_resumes = 0 WHERE run_id = ?", (run_id,))


def _create_schema(connection: sqlite3.Connection) -> None:
    for statement in _SCHEMA_STATEMENTS:
        connection.execute(statement)


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE workflow_runs (
        run_id TEXT PRIMARY KEY, origin_job_id TEXT UNIQUE, reservation_digest TEXT NOT NULL,
        parent_run_id TEXT, parent_node_key TEXT,
        agent_name TEXT NOT NULL, owner_user_id TEXT NOT NULL, room_id TEXT NOT NULL, thread_root_event_id TEXT,
        execution_identity_json TEXT NOT NULL, persona_json TEXT NOT NULL, title TEXT,
        source TEXT NOT NULL, source_digest TEXT NOT NULL, previous_source_digest TEXT, args_json TEXT NOT NULL,
        max_concurrent INTEGER NOT NULL, max_cost_usd REAL, resource_profile TEXT, runtime_seconds_left REAL,
        state TEXT NOT NULL, attempt INTEGER NOT NULL, idle_resumes INTEGER NOT NULL,
        script_run_id TEXT, job_id TEXT, card_event_id TEXT, result_json TEXT NOT NULL, error TEXT,
        created_at TEXT NOT NULL, finished_at TEXT
    )
    """,
    "CREATE INDEX workflow_runs_script_run_idx ON workflow_runs(script_run_id)",
    "CREATE INDEX workflow_runs_parent_run_idx ON workflow_runs(parent_run_id)",
    """
    CREATE TABLE workflow_nodes (
        run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
        key TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL, lane TEXT NOT NULL, idx INTEGER NOT NULL,
        digest TEXT NOT NULL, request_json TEXT NOT NULL, generation INTEGER NOT NULL, attempt INTEGER NOT NULL,
        state TEXT NOT NULL, waiting_on TEXT, handle_json TEXT NOT NULL, stale INTEGER NOT NULL,
        label TEXT NOT NULL, name TEXT, phase TEXT, group_key TEXT, members_json TEXT NOT NULL, session_key TEXT,
        cached INTEGER NOT NULL, started_at TEXT, ended_at TEXT,
        result_json TEXT NOT NULL, result_file TEXT, error_json TEXT NOT NULL, links_json TEXT NOT NULL,
        PRIMARY KEY (run_id, key), UNIQUE (run_id, seq)
    )
    """,
    """
    CREATE TABLE workflow_usage (
        run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
        key TEXT NOT NULL, generation INTEGER NOT NULL, turn INTEGER NOT NULL, attempt INTEGER NOT NULL,
        usage_json TEXT NOT NULL,
        PRIMARY KEY (run_id, key, generation, turn)
    )
    """,
    """
    CREATE TABLE workflow_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
        created_at TEXT NOT NULL, text TEXT NOT NULL
    )
    """,
    "CREATE INDEX workflow_log_run_idx ON workflow_log(run_id, id)",
)

# Each entry upgrades the database by one schema version; the stored version is how many have run.
_MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (_create_schema,)
SCHEMA_VERSION = len(_MIGRATIONS)
