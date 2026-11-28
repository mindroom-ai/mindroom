"""SQLite-backed durable state of workflow runs: runs, nodes, usage entries and log lines."""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from mindroom.durable_write import create_directory_durable, write_json_file_durable
from mindroom.redaction import redact_sensitive_data, redact_sensitive_text
from mindroom.script_runs.models import utc_timestamp
from mindroom.sqlite_utils import begun, enter_wal
from mindroom.workflow_runs.models import (
    LIVE_NODE_STATES,
    LIVE_RUN_STATES,
    SERVED_RUN_STATES,
    TERMINAL_RUN_STATES,
    DispatchHandle,
    WorkflowNode,
    WorkflowNodeState,
    WorkflowRun,
    WorkflowRunState,
    validated_workflow_run_id,
)
from mindroom.workflow_runs.replay import DIRTY_DIGEST, parent_group, result_digest, split_key

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
    from pathlib import Path

_BUSY_TIMEOUT_SECONDS = 10
# The first SQLite release with a built-in ``octet_length``.
_OCTET_LENGTH_VERSION = (3, 43, 0)
_MAX_INLINE_RESULT_BYTES = 8 * 1024 * 1024
_RESULT_PREVIEW_BYTES = 64 * 1024
_MAX_PREVIEW_JSON_BYTES = 16 * 1024
_PREVIEW_CHARS = 200
# The longest report a run keeps for the call it was taken from; a longer one is not kept.
_MAX_KEPT_REPORT_CHARS = 64 * 1024
_RUN_JSON_FIELDS = frozenset({"execution_identity", "persona", "args", "result", "claims"})
_NODE_JSON_FIELDS = frozenset({"request", "handle", "members", "result", "error", "links"})
# A run keeps the owner and the origin key it was reserved with; a redelivered start is checked against both.
_OWNER_COLUMNS = ("agent_name", "owner_user_id", "room_id", "thread_root_event_id")
_RUN_UPDATE_FIELDS = frozenset(field.name for field in fields(WorkflowRun)) - {
    "run_id",
    "origin_job_id",
    "epoch",
    "depth",
    *_OWNER_COLUMNS,
}
_ENDED_NODE_STATES = frozenset(WorkflowNodeState) - LIVE_NODE_STATES
# What a stamp may change on a node: never its identity, its generation, or its outcome.
_NODE_STAMP_FIELDS = frozenset(
    {"state", "waiting_on", "handle", "attempt", "stale", "label", "name", "phase", "members"}
    | {"session_key", "cached", "started_at", "links"},
)
_TERMINAL_STATE_VALUES = tuple(sorted(TERMINAL_RUN_STATES))
_TERMINAL_STATE_PLACEHOLDERS = ", ".join("?" * len(_TERMINAL_STATE_VALUES))

type UsageTotals = dict[str, float | bool]


@dataclass(frozen=True, slots=True)
class UsageSummary:
    """One read of a run's usage ledger: run totals, one attempt's cost, per-node sums, and priced own-turn costs."""

    totals: UsageTotals
    attempt_cost_usd: float
    by_node: dict[str, UsageTotals]
    turn_costs: list[float]


@dataclass(frozen=True, slots=True)
class WorkflowRunWorker:
    """Immutable persisted spec of one named shell worker admitted for a workflow run."""

    run_id: str
    worker_name: str
    owner_scope: str
    resource_profile: str | None
    sandbox_json: str | None
    created_at: str


class WorkflowRunStoreError(ValueError):
    """Raised when durable workflow state cannot be used safely."""


class WorkflowRunNotFoundError(WorkflowRunStoreError):
    """Raised for a run ID that is unknown, or that the caller does not own."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"No workflow run {run_id} in your runs.")


class WorkflowRunConflictError(WorkflowRunStoreError):
    """Raised when a reserve would reuse a run ID, or hits a run of another owner, script or arguments."""


class SupersededAttemptError(RuntimeError):
    """Raised for a write of an attempt, or of a decision about one, that its run has moved on from."""


class WorkflowRunStore:
    """Persist workflow runs and their nodes with atomic SQLite transactions."""

    def __init__(self, path: Path) -> None:
        self.database_path = path
        self.directory = path.parent
        # Told the run of each write that changed what the run shows, after the write is saved, on the writer's thread.
        self.on_change: Callable[[str], None] | None = None
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.read_connection() as connection:
            enter_wal(connection, timeout_seconds=_BUSY_TIMEOUT_SECONDS)
        self._migrate()

    def reserve_run(
        self,
        run: WorkflowRun,
        *,
        max_active: int | None = None,
    ) -> tuple[WorkflowRun, bool]:
        """Store *run*, or return the run its `origin_job_id` already reserved and `False`.

        A new top-level run is refused once its agent and owner have *max_active* live ones, counted in the write
        that would store it, so racing starts never exceed the limit and a refused one leaves no row.
        """
        validated_workflow_run_id(run.run_id)
        values = {**_row_values(asdict(run), _RUN_JSON_FIELDS), "reservation_digest": _reservation_digest(run)}
        with self.write_transaction() as connection:
            row = None
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
                run = _run_from_row(row)
            else:
                _require_slot(connection, run, max_active)
                try:
                    _insert(connection, "INSERT INTO workflow_runs", values)
                except sqlite3.IntegrityError as exc:
                    msg = f"Workflow run {run.run_id} already exists."
                    raise WorkflowRunConflictError(msg) from exc
        return run, row is None

    def delete_intent(self, run_id: str, owner: str | None, epoch: int) -> bool:
        """Delete a run that never got an attempt and is still *owner*'s at *epoch*; return whether one was deleted."""
        with self.write_transaction() as connection:
            deleted = connection.execute(
                "DELETE FROM workflow_runs WHERE run_id = ? AND state = ? AND attempt = 0 AND owner IS ? AND epoch = ?",
                (run_id, WorkflowRunState.STARTING, owner, epoch),
            )
        return deleted.rowcount == 1

    def get_run(self, run_id: str) -> WorkflowRun:
        """Return one run, or raise `WorkflowRunNotFoundError`."""
        with self.read_connection() as connection:
            return _select_run(connection, run_id)

    def update_run(
        self,
        run_id: str,
        epoch: int,
        *,
        when: Collection[WorkflowRunState] = LIVE_RUN_STATES,
        rerun: Iterable[str] = (),
        max_active: int | None = None,
        **changes: object,
    ) -> WorkflowRun:
        """Write the named fields of one run for the holder of *epoch*, and return the stored run.

        The write is refused (`SupersededAttemptError`) unless the run still has that epoch and is in one of the
        states *when*. A write that takes the run, begins an attempt or ends one raises the epoch, so whoever held
        the old one writes nothing more; an attempt that waits for a person and goes on stays the same attempt. A
        write that takes an ended run counts its owner's live runs against *max_active* first, and only when it
        lands are the calls at the keys *rerun* marked. It names the new owner, which the run then remembers
        (`claims`): an invocation takes a run once, so a call delivered again never takes it a second time.
        """
        unknown = changes.keys() - _RUN_UPDATE_FIELDS
        if unknown:
            msg = f"Workflow runs have no field to update named {', '.join(sorted(unknown))}."
            raise WorkflowRunStoreError(msg)
        with self.write_transaction(run_id) as connection:
            run = _fenced(connection, run_id, epoch, when)
            if (taker := changes.get("owner")) is not None:
                if taker in run.claims:
                    msg = f"Run {run_id} was taken by this invocation before."
                    raise SupersededAttemptError(msg)
                # The owner it is taken from is remembered too: a run from before runs kept their takers names
                # nobody else. If that owner waited for the run itself, no job keeps its report, so the run does,
                # for that one owner and while the report is small: the run's own result is the next attempt's.
                report = None if run.job_id else run.outcome_text()
                kept = report if report is not None and len(report) <= _MAX_KEPT_REPORT_CHARS else None
                changes["claims"] = dict.fromkeys(run.claims) | {run.owner: kept, taker: None}
            _require_slot(connection, run, max_active)
            connection.executemany(
                "UPDATE workflow_nodes SET digest = '' WHERE run_id = ? AND key = ?",
                [(run_id, key) for key in rerun],
            )
            moves = "state" in changes and not {run.state, changes["state"]} <= SERVED_RUN_STATES
            return _write_run(connection, run, changes | ({"epoch": epoch + 1} if moves else {}))

    def list_runs(
        self,
        *,
        agent_name: str | None = None,
        owner_user_id: str | None = None,
        room_id: str | None = None,
        thread_root_event_id: str | None = None,
        top_level_only: bool = False,
        include_finished: bool = True,
        limit: int | None = None,
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
        return self._select_runs(" AND ".join(clauses) or "1", params, limit=limit)

    def unshown_runs(self) -> list[str]:
        """Return the runs with a card of their own whose card does not say how they ended: all that have not, too."""
        runs = self._select_runs("parent_run_id IS NULL AND attempt > 0 AND card_epoch IS NOT epoch", ())
        return [run.run_id for run in runs]

    def child_runs(self, run_id: str) -> list[WorkflowRun]:
        """Return the runs that nodes of *run_id* started."""
        return self._select_runs("parent_run_id = ?", [run_id])

    def put_node(self, node: WorkflowNode, epoch: int, *, dirty_after: bool = False) -> WorkflowNode:
        """Claim a new key, or the next generation of a saved one, for the holder of *epoch*; return the node as stored.

        A new key gets the run's next sequence number and a saved one keeps its own.
        Any other generation is refused, so a writer that read an older row cannot replace a newer one.
        A result over the inline limit moves to a file. With *dirty_after*, here and where a node is stamped or
        settled, the same write makes what earlier attempts left after the node run again (`_forget_after`).
        """
        with self.write_transaction(node.run_id) as connection:
            _fenced(connection, node.run_id, epoch)
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
            _insert(connection, "INSERT OR REPLACE INTO workflow_nodes", _row_values(asdict(node), _NODE_JSON_FIELDS))
            _count_progress(connection, node.run_id, node.state)
            if dirty_after:
                _forget_after(connection, node.run_id, node.key)
        return node

    def save_dispatched(self, node: WorkflowNode, epoch: int, *, dirty_after: bool = True) -> WorkflowNode | None:
        """Persist a dispatched *node*: insert when unsaved (`seq == 0`), or update an already-saved generation."""
        if node.seq == 0:
            return self.put_node(node, epoch, dirty_after=dirty_after)
        n = node
        ok = self.update_node(n.run_id, n.key, n.generation, epoch, state=n.state, handle=n.handle, links=n.links)
        return n if ok else None

    def update_node(
        self,
        run_id: str,
        key: str,
        generation: int,
        epoch: int,
        *,
        dirty_after: bool = False,
        **changes: object,
    ) -> bool:
        """Stamp fields on *generation* of a node for the holder of *epoch*; false when the node has moved on.

        A stamp never writes an outcome, and it cannot change the state of a node that has ended.
        """
        if not changes or changes.keys() - _NODE_STAMP_FIELDS or changes.get("state") in _ENDED_NODE_STATES:
            msg = f"A node stamp cannot set {sorted(changes)}; outcomes go through settle_node."
            raise WorkflowRunStoreError(msg)
        values = _row_values(changes, _NODE_JSON_FIELDS)
        unsettled = " AND state IN ('queued', 'running', 'waiting')" if "state" in changes else ""
        with self.write_transaction(run_id) as connection:
            _fenced(connection, run_id, epoch)
            updated = connection.execute(
                f"UPDATE workflow_nodes SET {', '.join(f'{column} = ?' for column in values)} "  # noqa: S608
                f"WHERE run_id = ? AND key = ? AND generation = ?{unsettled}",
                (*values.values(), run_id, key, generation),
            )
            if dirty_after and updated.rowcount:
                _forget_after(connection, run_id, key)
        return updated.rowcount == 1

    def get_node(self, run_id: str, key: str) -> WorkflowNode | None:
        """Return the node at *key*, if the run has one."""
        nodes = self._select_nodes("run_id = ? AND key = ?", (run_id, key))
        return nodes[0] if nodes else None

    def node_by_id(self, run_id: str, node_id: str) -> WorkflowNode | None:
        """Return the node with canonical display ID `n<seq>` in *run_id*, if one exists."""
        seq = int(node_id[1:]) if len(node_id) < 20 and node_id[1:].isdecimal() else 0
        if not (0 < seq < 2**63 and node_id == f"n{seq}"):
            return None
        nodes = self._select_nodes("run_id = ? AND seq = ?", (run_id, seq))
        return nodes[0] if nodes else None

    def nodes(self, run_id: str, *, include_stale: bool = False, light: bool = False) -> list[WorkflowNode]:
        """Return a run's nodes in the order they were first submitted.

        A *light* node is what a view of the run shows: its request holds only the operation of a group, and it has
        neither its result nor its members, each of which can be megabytes.
        """
        where = "run_id = ?" if include_stale else "run_id = ? AND stale = 0"
        return self._select_nodes(where, (run_id,), _LIGHT_COLUMNS if light else "*")

    def unposted_questions(self, run_id: str) -> list[WorkflowNode]:
        """Return every waiting, non-stale question of *run_id* whose card has no `event_id` yet."""
        where = (
            "run_id = ? AND kind = 'ask' AND state = 'waiting' AND stale = 0 "
            "AND json_type(links_json, '$.event_id') IS NULL"
        )
        return self._select_nodes(where, (run_id,))

    def session_turns(self, run_id: str, session_key: str) -> list[WorkflowNode]:
        """Return every `turn` node of *session_key* in *run_id* (stale included), in submission order."""
        return self._select_nodes("run_id = ? AND kind = 'turn' AND session_key = ?", (run_id, session_key))

    def node_states(self, run_id: str) -> frozenset[WorkflowNodeState]:
        """Return the states the run's current nodes are in, excluding peer waits (which do not pause for a person)."""
        with self.read_connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT state FROM workflow_nodes "
                "WHERE run_id = ? AND stale = 0 AND NOT (state = 'waiting' AND COALESCE(waiting_on, '') = 'peer')",
                (run_id,),
            ).fetchall()
        return frozenset(WorkflowNodeState(row["state"]) for row in rows)

    def settle_node(
        self,
        run_id: str,
        key: str,
        generation: int,
        epoch: int,
        *,
        state: WorkflowNodeState,
        result: object | None = None,
        error: dict[str, object] | None = None,
        dirty_after: bool = False,
    ) -> bool:
        """Record how *generation* of a node ended, for the holder of *epoch*; false when it moved on or has an outcome.

        An unsettled node takes any end, and an `indeterminate` one takes the proven outcome of a late receipt.
        """
        if state not in _ENDED_NODE_STATES:
            msg = f"A node settles into an ended state, not {state}."
            raise WorkflowRunStoreError(msg)
        with self.write_transaction(run_id) as connection:
            _fenced(connection, run_id, epoch)
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
            if dirty_after:
                _forget_after(connection, run_id, key)
        return True

    def whole_result(self, node: WorkflowNode) -> object:
        """Return the node's whole result, read from its file when the row keeps only the start of it."""
        if node.result_file is None:
            return node.result
        return json.loads((self.directory / node.result_file).read_bytes())

    def mark_stale(self, run_id: str, epoch: int) -> None:
        """Take nodes not reached by *run_id*'s current attempt out of its current view, for the holder of *epoch*."""
        with self.write_transaction(run_id) as connection:
            run = _fenced(connection, run_id, epoch)
            connection.execute(
                "UPDATE workflow_nodes SET stale = 1 WHERE run_id = ? AND attempt != ? AND stale = 0",
                (run_id, run.attempt),
            )

    def add_usage(
        self,
        run_id: str,
        key: str,
        generation: int,
        turn: int | str,
        attempt: int,
        usage: dict[str, object],
        *,
        epoch: int | None = None,
    ) -> bool:
        """Append one model turn's usage; a repeat of the same turn changes nothing and returns false.

        *turn* is a number for a turn of the node's own agent, and `sub:<delegation id>` for what the subagents of
        that agent's turn cost. With *epoch*, a write from a superseded attempt is refused.
        """
        with self.write_transaction(run_id) as connection:
            if epoch is not None:
                _fenced(connection, run_id, epoch)
            inserted = connection.execute(
                "INSERT OR IGNORE INTO workflow_usage (run_id, key, generation, turn, attempt, usage_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, key, generation, turn, attempt, _dumps(usage)),
            )
        return inserted.rowcount == 1

    def usage_summary(
        self,
        run_id: str,
        *,
        attempt: int | None = None,
        current_generations: Mapping[str, int] | None = None,
    ) -> UsageSummary:
        """Read *run_id*'s usage ledger once: totals, optional *attempt* cost, per-node sums, and priced own turns."""
        with self.read_connection() as connection:
            rows = connection.execute(
                "SELECT key, generation, turn, attempt, usage_json FROM workflow_usage WHERE run_id = ?",
                (run_id,),
            ).fetchall()
        all_entries: list[dict[str, Any]] = []
        attempt_entries: list[dict[str, Any]] = []
        by_key: dict[str, list[dict[str, Any]]] = {}
        turn_costs: list[float] = []
        for row in rows:
            used = json.loads(row["usage_json"])
            all_entries.append(used)
            if attempt is None or row["attempt"] == attempt:
                attempt_entries.append(used)
            if current_generations is None or current_generations.get(row["key"]) == row["generation"]:
                by_key.setdefault(row["key"], []).append(used)
            if "sub:" not in str(row["turn"]) and (cost := used.get("cost_usd")) is not None:
                turn_costs.append(cost)
        totals = _totals(all_entries)
        attempt_cost = float(totals["cost_usd"]) if attempt is None else float(_totals(attempt_entries)["cost_usd"])
        return UsageSummary(
            totals=totals,
            attempt_cost_usd=attempt_cost,
            by_node={key: _totals(entries) for key, entries in by_key.items()},
            turn_costs=turn_costs,
        )

    def node_previews(self, run_id: str, *, max_chars: int = _PREVIEW_CHARS) -> dict[str, str]:
        """Return bounded, redacted result previews for completed non-stale agent, turn and tool nodes."""
        if max_chars <= 0:
            return {}
        sql = (
            (
                "SELECT key, CAST(result_json AS BLOB) AS result_blob "
                "FROM workflow_nodes "
                "WHERE run_id = ? AND stale = 0 AND state = 'completed' "
                "AND kind IN ('agent', 'turn', 'tool') AND result_file IS NULL "
                "AND octet_length(result_json) <= ? AND result_json != 'null' "
                "ORDER BY seq"
            )
            if sqlite3.sqlite_version_info >= _OCTET_LENGTH_VERSION
            else (
                "SELECT key, CAST(result_json AS BLOB) AS result_blob "
                "FROM workflow_nodes "
                "WHERE run_id = ? AND stale = 0 AND state = 'completed' "
                "AND kind IN ('agent', 'turn', 'tool') AND result_file IS NULL "
                "AND length(CAST(result_json AS BLOB)) <= ? AND result_json != 'null' "
                "ORDER BY seq"
            )
        )
        with self.read_connection() as connection:
            rows = connection.execute(sql, (run_id, _MAX_PREVIEW_JSON_BYTES)).fetchall()
        previews: dict[str, str] = {}
        for row in rows:
            with suppress(Exception):
                if (preview := _format_node_preview(row["result_blob"], max_chars)) is not None:
                    previews[row["key"]] = preview
        return previews

    def append_log(self, run_id: str, epoch: int, text: str) -> None:
        """Append one line to a run's log, for the holder of *epoch*."""
        with self.write_transaction(run_id) as connection:
            run = _fenced(connection, run_id, epoch)
            connection.execute(
                "INSERT INTO workflow_log (run_id, attempt, created_at, text) VALUES (?, ?, ?, ?)",
                (run_id, run.attempt, utc_timestamp(), text),
            )

    def log(self, run_id: str, limit: int = 50, *, attempt: int | None = None) -> list[str]:
        """Return the newest *limit* lines of a run's log, oldest first; optional *attempt* narrows to one attempt."""
        query = (
            "SELECT text FROM workflow_log WHERE run_id = ? AND attempt = ? ORDER BY id DESC LIMIT ?"
            if attempt is not None
            else "SELECT text FROM workflow_log WHERE run_id = ? ORDER BY id DESC LIMIT ?"
        )
        params: tuple[object, ...] = (run_id, attempt, limit) if attempt is not None else (run_id, limit)
        with self.read_connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [row["text"] for row in reversed(rows)]

    def get_source(self, run_id: str, attempt: int, name: str) -> tuple[bytes, str] | None:
        """Return the saved workflow bytes and SHA-256 snapshotted for *name* in *attempt*, if any."""
        with self.read_connection() as connection:
            row = connection.execute(
                "SELECT source, sha256 FROM workflow_sources WHERE run_id = ? AND attempt = ? AND name = ?",
                (run_id, attempt, name),
            ).fetchone()
        return None if row is None else (bytes(row["source"]), str(row["sha256"]))

    def put_source(
        self,
        run_id: str,
        attempt: int,
        epoch: int,
        name: str,
        source: bytes,
        sha256: str,
    ) -> tuple[bytes, str]:
        """Snapshot *source* for *name* in *attempt* once, returning the winning bytes and SHA-256."""
        with self.write_transaction() as connection:
            _fenced(connection, run_id, epoch)
            connection.execute(
                "INSERT OR IGNORE INTO workflow_sources (run_id, attempt, name, source, sha256) VALUES (?, ?, ?, ?, ?)",
                (run_id, attempt, name, source, sha256),
            )
            row = connection.execute(
                "SELECT source, sha256 FROM workflow_sources WHERE run_id = ? AND attempt = ? AND name = ?",
                (run_id, attempt, name),
            ).fetchone()
        return bytes(row["source"]), str(row["sha256"])

    def get_run_worker(self, run_id: str, worker_name: str) -> WorkflowRunWorker | None:
        """Return the persisted immutable lock for *worker_name* in *run_id*, if admitted."""
        with self.read_connection() as connection:
            return _select_run_worker(connection, run_id, worker_name)

    def admit_run_worker(
        self,
        run_id: str,
        worker_name: str,
        epoch: int,
        *,
        owner_scope: str,
        resource_profile: str | None,
        sandbox_json: str | None,
        commit: bool = True,
    ) -> WorkflowRunWorker | None:
        """Return *worker_name*'s immutable lock, atomically inserting it on first admission when *commit* is True."""
        if not commit:
            with self.read_connection() as connection:
                return _select_run_worker(connection, run_id, worker_name)
        with self.write_transaction() as connection:
            _fenced(connection, run_id, epoch)
            existing = _select_run_worker(connection, run_id, worker_name)
            if existing is not None:
                return existing
            created = WorkflowRunWorker(
                run_id,
                worker_name,
                owner_scope,
                resource_profile,
                sandbox_json,
                utc_timestamp(),
            )
            connection.execute(
                "INSERT INTO workflow_run_workers "
                "(run_id, worker_name, owner_scope, resource_profile, sandbox_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (created.run_id, created.worker_name, owner_scope, resource_profile, sandbox_json, created.created_at),
            )
            return created

    def prune(self, finished_before: str, held: Collection[str] | None = ()) -> int:
        """Delete finished runs that ended before *finished_before*, with all they own; return how many.

        A run of one of the jobs *held* stays: that job has not saved the run's report as its own end yet.
        So does every run of a job while nobody can say which those are (None). A run inside a run stays for as
        long as the run around it: what it spent is part of that run's totals, and its report is its call's.
        The timestamps are compared as instants, because their text leaves out a zero fraction of a second.
        """
        cutoff = datetime.fromisoformat(finished_before)
        with self.write_transaction() as connection:
            rows = connection.execute(
                f"SELECT run_id, job_id, finished_at, parent_run_id FROM workflow_runs "  # noqa: S608
                f"WHERE state IN ({_TERMINAL_STATE_PLACEHOLDERS}) AND finished_at IS NOT NULL",
                _TERMINAL_STATE_VALUES,
            ).fetchall()
            rows = [row for row in rows if row["job_id"] is None or (held is not None and row["job_id"] not in held)]
            outer = {
                row["run_id"]: row["parent_run_id"]
                for row in rows
                if datetime.fromisoformat(row["finished_at"]) < cutoff
            }
            kept = {row["run_id"] for row in connection.execute("SELECT run_id FROM workflow_runs")} - outer.keys()
            # From the outermost run inwards: a run goes when the run around it goes with it, or is gone already.
            while staying := {run_id for run_id, parent in outer.items() if parent in kept}:
                kept |= staying
                outer = {run_id: parent for run_id, parent in outer.items() if run_id not in staying}
            run_ids = list(outer)
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

    def _select_runs(
        self,
        where: str,
        params: Iterable[object],
        *,
        limit: int | None = None,
    ) -> list[WorkflowRun]:
        query = f"SELECT * FROM workflow_runs WHERE {where} ORDER BY created_at DESC, run_id DESC"  # noqa: S608
        bound = tuple(params)
        if limit is not None:
            query += " LIMIT ?"
            bound = (*bound, limit)
        with self.read_connection() as connection:
            rows = connection.execute(query, bound).fetchall()
        return [_run_from_row(row) for row in rows]

    def _select_nodes(self, where: str, params: tuple[object, ...], columns: str = "*") -> list[WorkflowNode]:
        with self.read_connection() as connection:
            rows = connection.execute(
                f"SELECT {columns} FROM workflow_nodes WHERE {where} ORDER BY seq",  # noqa: S608
                params,
            ).fetchall()
        return [_node_from_row(row) for row in rows]

    def _migrate(self) -> None:
        with self.write_transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS workflow_schema (version INTEGER NOT NULL)")
            row = connection.execute("SELECT version FROM workflow_schema").fetchone()
            version = 0 if row is None else int(row["version"])
            if version > len(_MIGRATIONS):
                msg = f"Workflow store is from a newer MindRoom (schema {version})."
                raise WorkflowRunStoreError(msg)
            # Always apply the folded CREATE IF NOT EXISTS statements so a v1 store from an earlier branch build
            # picks up newly added tables without migration numbering, then run any later migrations in order.
            _create_schema(connection)
            for migration in _MIGRATIONS[max(version, 1) :]:
                migration(connection)
            if version < len(_MIGRATIONS):
                connection.execute("DELETE FROM workflow_schema")
                connection.execute("INSERT INTO workflow_schema (version) VALUES (?)", (len(_MIGRATIONS),))

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        """Open a read connection to the workflow store."""
        connection = sqlite3.connect(self.database_path, isolation_level=None, timeout=_BUSY_TIMEOUT_SECONDS)
        try:
            connection.row_factory = sqlite3.Row
            if sqlite3.sqlite_version_info < _OCTET_LENGTH_VERSION:
                connection.create_function("octet_length", 1, _octet_length, deterministic=True)
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def write_transaction(self, changed: str | None = None) -> Iterator[sqlite3.Connection]:
        """Run one write transaction; notify `on_change` for *changed* once committed."""
        with self.read_connection() as connection, begun(connection, write=True):
            yield connection
        if changed is not None and self.on_change is not None:
            self.on_change(changed)


def _dumps(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


def _column(field_name: str, json_fields: frozenset[str]) -> str:
    return f"{field_name}_json" if field_name in json_fields else field_name


def _row_values(values: Mapping[str, object], json_fields: frozenset[str]) -> dict[str, object]:
    """Return column values for *values*; fields in *json_fields* are stored as JSON text."""
    return {
        _column(name, json_fields): _dumps(asdict(value) if isinstance(value, DispatchHandle) else value)
        if name in json_fields
        else value
        for name, value in values.items()
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
    values.update(state=WorkflowRunState(values["state"]), large=bool(values["large"]))
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


def _write_run(connection: sqlite3.Connection, run: WorkflowRun, changes: dict[str, object]) -> WorkflowRun:
    """Write *changes* to the row of *run* and return the run as stored."""
    run = replace(run, **changes)
    values = _row_values(changes, _RUN_JSON_FIELDS)
    if values:
        connection.execute(
            f"UPDATE workflow_runs SET {', '.join(f'{column} = ?' for column in values)} WHERE run_id = ?",  # noqa: S608
            (*values.values(), run.run_id),
        )
    return run


def _fenced(
    connection: sqlite3.Connection,
    run_id: str,
    epoch: int,
    when: Collection[WorkflowRunState] = LIVE_RUN_STATES,
) -> WorkflowRun:
    """Return the run inside a write that its *epoch* and the states *when* allow, or refuse the write."""
    run = _select_run(connection, run_id)
    if run.epoch != epoch or run.state not in when:
        msg = f"Run {run_id} has moved on from what this write was decided on."
        raise SupersededAttemptError(msg)
    return run


def _require_slot(connection: sqlite3.Connection, run: WorkflowRun, max_active: int | None) -> None:
    """Refuse a top-level run that would become live once its agent and owner have *max_active* live ones."""
    if max_active is None or run.parent_run_id is not None:
        return
    active = connection.execute(
        "SELECT COUNT(*) FROM workflow_runs WHERE agent_name = ? AND owner_user_id = ? "  # noqa: S608
        f"AND parent_run_id IS NULL AND state NOT IN ({_TERMINAL_STATE_PLACEHOLDERS})",
        (run.agent_name, run.owner_user_id, *_TERMINAL_STATE_VALUES),
    ).fetchone()[0]
    if active >= max_active:
        msg = f"You have {max_active} workflow runs in progress, the limit. Wait for one to end or cancel one."
        raise WorkflowRunStoreError(msg)


def _forget_after(connection: sqlite3.Connection, run_id: str, key: str) -> None:
    """Mark each earlier-attempt call after *key* dirty so it runs again when reached.

    What follows the node in its lane or in a lane opened there, and the same after each group around it, gets
    `DIRTY_DIGEST` (including live rows from earlier attempts). Explicit reruns (`digest = ''`) and indeterminate
    rows keep their own digests and rules, and no row of the node's own attempt or a later one is touched.
    """
    group: str | None = key
    while group is not None:
        lane, index = split_key(group)
        connection.execute(
            "UPDATE workflow_nodes SET digest = :dirty WHERE run_id = :run "
            "AND state != 'indeterminate' AND digest != '' "
            "AND attempt < (SELECT attempt FROM workflow_nodes WHERE run_id = :run AND key = :key) "
            "AND ((lane = :lane AND idx > :idx) "
            "OR (substr(lane, 1, :size + 1) = :lane || '/' AND CAST(substr(lane, :size + 2) AS INTEGER) > :idx))",
            {"dirty": DIRTY_DIGEST, "run": run_id, "key": key, "lane": lane, "idx": index, "size": len(lane)},
        )
        group = parent_group(lane)


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
    return result_digest(
        [run.agent_name, run.owner_user_id, run.room_id, run.thread_root_event_id, run.source_digest, run.args],
    )


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
    CREATE TABLE IF NOT EXISTS workflow_runs (
        run_id TEXT PRIMARY KEY, origin_job_id TEXT UNIQUE, reservation_digest TEXT NOT NULL,
        parent_run_id TEXT, parent_node_key TEXT, depth INTEGER NOT NULL,
        epoch INTEGER NOT NULL, owner TEXT, claims_json TEXT NOT NULL,
        agent_name TEXT NOT NULL, owner_user_id TEXT NOT NULL, room_id TEXT NOT NULL, thread_root_event_id TEXT,
        execution_identity_json TEXT NOT NULL, persona_json TEXT NOT NULL, title TEXT,
        source TEXT NOT NULL, source_digest TEXT NOT NULL, previous_source_digest TEXT, args_json TEXT NOT NULL,
        max_concurrent INTEGER NOT NULL, max_cost_usd REAL, large INTEGER NOT NULL, resource_profile TEXT,
        runtime_seconds_left REAL, state TEXT NOT NULL, attempt INTEGER NOT NULL, idle_resumes INTEGER NOT NULL,
        script_run_id TEXT, job_id TEXT, card_event_id TEXT, card_epoch INTEGER, result_json TEXT NOT NULL, error TEXT,
        created_at TEXT NOT NULL, finished_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS workflow_runs_script_run_idx ON workflow_runs(script_run_id)",
    "CREATE INDEX IF NOT EXISTS workflow_runs_parent_run_idx ON workflow_runs(parent_run_id)",
    """
    CREATE TABLE IF NOT EXISTS workflow_nodes (
        run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
        key TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL, lane TEXT NOT NULL, idx INTEGER NOT NULL,
        digest TEXT NOT NULL, request_json TEXT NOT NULL, generation INTEGER NOT NULL, attempt INTEGER NOT NULL,
        state TEXT NOT NULL, waiting_on TEXT, handle_json TEXT NOT NULL, stale INTEGER NOT NULL,
        label TEXT NOT NULL, name TEXT, phase TEXT, members_json TEXT NOT NULL, session_key TEXT,
        cached INTEGER NOT NULL, started_at TEXT, ended_at TEXT,
        result_json TEXT NOT NULL, result_file TEXT, error_json TEXT NOT NULL, links_json TEXT NOT NULL,
        PRIMARY KEY (run_id, key), UNIQUE (run_id, seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_usage (
        run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
        key TEXT NOT NULL, generation INTEGER NOT NULL, turn TEXT NOT NULL, attempt INTEGER NOT NULL,
        usage_json TEXT NOT NULL,
        PRIMARY KEY (run_id, key, generation, turn)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
        created_at TEXT NOT NULL, text TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS workflow_log_run_idx ON workflow_log(run_id, id)",
    # Saved workflow files snapshotted once per attempt for SDK run(name).
    """
    CREATE TABLE IF NOT EXISTS workflow_sources (
        run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
        attempt INTEGER NOT NULL,
        name TEXT NOT NULL,
        source BLOB NOT NULL,
        sha256 TEXT NOT NULL,
        PRIMARY KEY (run_id, attempt, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_messages (
        run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        sender_key TEXT NOT NULL,
        sender_generation INTEGER NOT NULL,
        sender_name TEXT NOT NULL,
        recipient_name TEXT NOT NULL,
        recipient_keys_json TEXT,
        text TEXT NOT NULL,
        withdrawn INTEGER NOT NULL,
        was_read INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        PRIMARY KEY (run_id, seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_cursors (
        run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
        reader_key TEXT NOT NULL,
        reader_generation INTEGER NOT NULL,
        seq INTEGER NOT NULL,
        PRIMARY KEY (run_id, reader_key, reader_generation)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_state (
        run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
        key TEXT NOT NULL,
        value_json TEXT NOT NULL,
        PRIMARY KEY (run_id, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_peer_calls (
        run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
        op_id TEXT NOT NULL,
        result_json TEXT NOT NULL,
        PRIMARY KEY (run_id, op_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_run_workers (
        run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
        worker_name TEXT NOT NULL,
        owner_scope TEXT NOT NULL,
        resource_profile TEXT,
        sandbox_json TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (run_id, worker_name)
    )
    """,
)


def _select_run_worker(connection: sqlite3.Connection, run_id: str, worker_name: str) -> WorkflowRunWorker | None:
    row = connection.execute(
        "SELECT run_id, worker_name, owner_scope, resource_profile, sandbox_json, created_at "
        "FROM workflow_run_workers WHERE run_id = ? AND worker_name = ?",
        (run_id, worker_name),
    ).fetchone()
    return None if row is None else WorkflowRunWorker(*row)


# What a light read takes in place of the columns that can be megabytes: of a request, the operation of a group.
_LIGHT = {
    "request_json": "json_object('op', json_extract(request_json, '$.op'))",
    "result_json": "'null'",
    "members_json": "'null'",
}
_LIGHT_COLUMNS = ", ".join(
    f"{_LIGHT[column]} AS {column}" if column in _LIGHT else column
    for column in (_column(field.name, _NODE_JSON_FIELDS) for field in fields(WorkflowNode))
)


def _octet_length(value: object) -> int | None:
    """Return a stored value's size in bytes, as SQLite 3.43's ``octet_length`` does."""
    if value is None:
        return None
    if isinstance(value, bytes):
        return len(value)
    return len(str(value).encode("utf-8", errors="surrogatepass"))


def redact_json_text(text: str) -> str:
    """If *text* parses as a JSON object or array, redact structured secret fields before re-serializing."""
    stripped = text.strip()
    if stripped and stripped[0] in "{[":
        with suppress(json.JSONDecodeError):
            parsed = json.loads(stripped)
            if isinstance(parsed, dict | list):
                return json.dumps(redact_sensitive_data(parsed), ensure_ascii=False, separators=(",", ":"))
    return text


def _format_node_preview(blob: bytes | str, max_chars: int) -> str | None:
    """Decode, redact, and bound one node's JSON result for room display without crashing on bad UTF-8."""
    raw = blob.decode("utf-8", errors="replace") if isinstance(blob, bytes) else blob
    obj = json.loads(raw)
    if obj is None:
        return None
    if isinstance(obj, str):
        text = redact_json_text(obj)
    elif isinstance(obj, dict) and isinstance(obj.get("output"), str):
        text = redact_json_text(obj["output"])
    elif isinstance(obj, dict | list):
        text = json.dumps(redact_sensitive_data(obj), ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    trimmed = text.encode("utf-8", errors="replace").decode("utf-8").strip()
    return redact_sensitive_text(trimmed, max_length=max_chars) if trimmed else None


def _add_log_attempt(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(workflow_log)")}
    if "attempt" not in columns:
        connection.execute("ALTER TABLE workflow_log ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0")
        connection.execute(
            "UPDATE workflow_log SET attempt = 1 "
            "WHERE EXISTS (SELECT 1 FROM workflow_runs r WHERE r.run_id = workflow_log.run_id AND r.attempt = 1)",
        )
    connection.execute("CREATE INDEX IF NOT EXISTS workflow_log_run_attempt_idx ON workflow_log(run_id, attempt, id)")


# Each entry upgrades the database by one schema version; the stored version is how many have run.
_MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (_create_schema, _add_log_attempt)
SCHEMA_VERSION = len(_MIGRATIONS)
