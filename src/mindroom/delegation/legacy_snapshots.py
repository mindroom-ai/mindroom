"""Delegation child snapshots that earlier releases stored without a field a later release added, rewritten once.

A child snapshot is ``dataclasses.asdict`` of a ``DelegationChild``. Its copies sit in the subagent's session file,
which nothing deletes, and in a parent run's delegation state, among its children and behind its pending calls. Each
copy is rewritten where it is stored before anything reads it, so the readers in ``state`` take only the current shape.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.constants import subagent_sessions_dir
from mindroom.delegation.state import DELEGATION_STATE_KEY, DelegationChild
from mindroom.durable_write import write_json_file_durable
from mindroom.logging_config import get_logger
from mindroom.session_storage.schema import quote_identifier
from mindroom.session_storage.store import decoded_mapping

if TYPE_CHECKING:
    import sqlite3

    from mindroom.constants import RuntimePaths
    from mindroom.session_storage.schema import TableNames

logger = get_logger(__name__)


# LEGACY_COMPAT: Delegation child snapshots stored without a field a later release added.
# Legacy format: A child snapshot in a subagent session file (`subagent_sessions/<id>.json`) or in a run's
# `mindroom_delegation` metadata, among its children or behind a pending call, that lacks `agent_mode`, `persona`, or
# `requested_model_name`, holds a null `requested_model_name`, or holds a persona that lacks `skills` or `knowledge`.
# Last legacy release: v2026.10.18 without `agent_mode`, v2026.10.230 without `persona`, v2026.10.244 without
# `requested_model_name`, v2026.10.245 without a persona's `skills`, and v2026.10.263 without its `knowledge`; the
# release after each writes the field. Through v2026.10.693, a child read without a requested model was written back
# with a null one. Background job adapters hold no such snapshot: the job store current code reads, the event journal's
# `tool_jobs` table, first shipped in v2026.10.289, and each adapter there holds a child that release or a later one
# prepared, with every field and its requested model; the jobs of v2026.9.165, reverted in v2026.9.166, lived in
# `tool_jobs/` files nothing reads.
# Handling: Before any bot starts, each session file is rewritten (`migrate_subagent_sessions`), and so is every run's
# metadata in the step to conversation layout version 5 (`rewrite_run_snapshots`). Each absent field takes the value
# those releases ran with: standard mode, no persona (the configured agent's own prompt), the recorded model as the
# requested one, no skills, and null knowledge, which follows the persona's tool list. A session file that does not
# decode or holds no child is logged and left, and a snapshot the current reader rejects even so stays as stored, so
# each fails only where it is read, as before. The rewrite has no removal date.
# Coverage: tests/test_legacy_delegation_snapshots.py::test_every_reader_reads_a_rewritten_snapshot_as_the_release_before_the_rewrite_did,
# tests/test_legacy_delegation_snapshots.py::test_a_second_start_changes_nothing,
# tests/test_legacy_delegation_snapshots.py::test_a_snapshot_the_reader_rejects_stays_as_stored,
# tests/test_legacy_delegation_snapshots.py::test_an_unreadable_session_file_neither_stops_startup_nor_changes,
# tests/test_delegation_minimal_mode.py::test_child_snapshot_without_mode_continues_in_standard_mode.
def _current_child(child: object) -> object:
    """Return the stored child snapshot ``child`` with every field it lacks, or ``child`` itself when it gets no rewrite.

    It gets none when it holds every field already, or when the current reader rejects it even with them.
    """
    if not isinstance(child, dict):
        return child
    current: dict[str, Any] = {"agent_mode": "standard", "persona": None, **cast("dict[str, Any]", child)}
    if current.get("requested_model_name") is None:
        current["requested_model_name"] = current.get("model_name")
    if isinstance(persona := current["persona"], dict):
        current["persona"] = {"skills": [], "knowledge": None, **persona}
    if current == child:
        return child
    try:
        DelegationChild.from_dict(current)
    except (KeyError, TypeError):
        return child
    return current


async def migrate_subagent_sessions(runtime_paths: RuntimePaths) -> None:
    """Rewrite each subagent session file whose child an earlier release stored, before any bot reads one."""
    await run_blocking_until_complete(_migrate_subagent_sessions, runtime_paths)


def _migrate_subagent_sessions(runtime_paths: RuntimePaths) -> None:
    root = subagent_sessions_dir(runtime_paths)
    if root.is_symlink() or not root.is_dir():
        return
    for path in sorted(root.glob("*.json")):
        if path.is_symlink():
            continue
        try:
            payload = json.loads(path.read_text())
            stored = payload["child"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            # Its reader fails for this subagent alone, as before the rewrite, so the rest of startup goes on.
            logger.warning("subagent_session_rewrite_skipped", path=str(path), error=str(error))
            continue
        child = _current_child(stored)
        if child is not stored:
            # The replacement is atomic, so a crash leaves the file in one shape or the other and the next start resumes.
            write_json_file_durable(path, {**payload, "child": child}, strict_atomic_replace=True)


def rewrite_run_snapshots(connection: sqlite3.Connection, names: TableNames) -> None:
    """Rewrite, in the caller's write transaction, each run's delegation state that holds a child an earlier release stored.

    Worker code can write conversation databases, so metadata that does not decode, or a state that is not an object,
    stays as stored.
    """
    runs = quote_identifier(names.log_runs)
    marked = connection.execute(
        f"SELECT run_id, metadata FROM {runs} WHERE instr(metadata, ?) > 0",  # noqa: S608
        (json.dumps(DELEGATION_STATE_KEY),),
    ).fetchall()
    for run_id, stored in marked:
        metadata = decoded_mapping(stored)
        if metadata is None or not isinstance(state := metadata.get(DELEGATION_STATE_KEY), dict):
            continue
        current = _current_state(state)
        if current != state:
            connection.execute(
                f"UPDATE {runs} SET metadata = ? WHERE run_id = ?",  # noqa: S608
                (json.dumps({**metadata, DELEGATION_STATE_KEY: current}), run_id),
            )


def _current_state(state: dict[str, Any]) -> dict[str, Any]:
    """Return ``state`` with each child it holds, among its children and behind its pending calls, rewritten."""
    current = dict(state)
    if isinstance(children := state.get("children"), list):
        current["children"] = [_current_child(child) for child in children]
    if isinstance(sources := state.get("pending_tool_sources"), dict):
        current["pending_tool_sources"] = {
            call_id: {**source, "child": _current_child(source["child"])}
            if isinstance(source, dict) and "child" in source
            else source
            for call_id, source in sources.items()
        }
    return current


__all__ = [
    "migrate_subagent_sessions",
    "rewrite_run_snapshots",
]
