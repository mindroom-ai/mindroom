"""Paused runs earlier releases stored twice, as tool rows and as requirements, folded into one copy at layout 8.

A paused run once kept each paused call twice: its tool row, and an entry of the row's ``requirements`` column that
copied the call beside the id its delegation child and hooks are kept under. A paused team row also copied each
paused member call, naming the member, and a delegation answer recorded while a run was loaded reached only the copy.
The copies are folded into the calls they repeat once, so every reader reads a paused call from its tool row alone.

``fold_run_requirements`` folds every row in the write transaction that moves a database to layout version 8;
``fold_requirements`` folds one run dict the same way, for the Agno-layout conversion, whose paused team rows embed
their member runs. Worker code can write conversation databases, so a row that does not decode keeps its tool rows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from mindroom.delegation.state import DELEGATION_STATE_KEY
from mindroom.llm.runs import RunStatus
from mindroom.session_storage.codec import decode_json
from mindroom.session_storage.schema import quote_identifier
from mindroom.session_storage.store import decoded_mapping

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from mindroom.session_storage.schema import TableNames


# LEGACY_COMPAT: Paused runs that store each paused call a second time, in their ``requirements``.
# Legacy format: a run row's ``requirements`` column, or a run dict's ``requirements`` key in an Agno-layout row,
#   listing one entry per paused call: ``tool_execution``, a copy of the call's tool execution; ``id``, which the
#   delegation child and hooks of a delegation call are kept under (``parent_requirement_id``, ``hooks``); the
#   decision ``confirmation``, ``confirmation_note``, or ``external_execution_result``; and, on a paused team row,
#   ``member_agent_id``, ``member_agent_name``, and ``member_run_id`` for a copy of a paused member call. A loaded run
#   whose delegation calls the driver answered stored the answers, and a failed one's ``tool_call_error``, only on
#   these copies. A run's delegation state also listed its pending calls as ``pending_requirements`` beside
#   ``pending_tools``.
# Last legacy release: v2026.10.744, which wrote them as every earlier release did; replacement: the next release,
#   which stores each paused call once, as its tool execution with the id as ``pause_id``, and reads a paused team's
#   member calls from its paused member rows.
# Handling: in the step to layout version 8, each entry of a paused run folds into the latest paused tool row of the
#   run it names, the team's own or its paused member's, with the entry's decision on it where the copy has none and
#   the entry's id as its ``pause_id``. A team row's member entries fold after the member's own, since only they hold
#   the answers the driver recorded while the team waited. An entry without a call, one naming a run that is not
#   paused, and one matching no paused call fold into nothing, as no release is known to write one that a resume
#   could answer. Delegation state drops ``pending_requirements``, and every row's ``requirements`` column becomes
#   NULL. The Agno-layout conversion folds each paused row and the members a paused team row embeds the same way as it
#   decodes them. A release at layout version 7 opens the folded database but cannot resume a run paused before or
#   after the step, a cost accepted instead of writing both shapes.
# Coverage: tests/session_storage/test_legacy_run_requirements.py::test_a_decision_only_the_requirement_held_reaches_its_call,
#   tests/session_storage/test_legacy_run_requirements.py::test_the_latest_paused_call_under_the_id_takes_the_requirement,
#   tests/session_storage/test_legacy_run_requirements.py::test_an_entry_no_paused_call_matches_folds_into_nothing,
#   tests/session_storage/test_legacy_run_requirements.py::test_a_team_row_s_copy_of_a_member_call_folds_over_the_member_s_own,
#   tests/session_storage/test_legacy_run_requirements.py::test_a_paused_agent_row_keeps_each_call_once_with_its_requirement_s_id,
#   tests/session_storage/test_legacy_run_requirements.py::test_a_delegation_answered_only_on_its_requirement_stays_answered,
#   tests/session_storage/test_legacy_run_requirements.py::test_a_paused_team_row_waits_on_its_member_s_call_from_the_member_row,
#   tests/session_storage/test_legacy_run_requirements.py::test_a_member_row_s_own_requirement_folds_whatever_its_team_row_copied,
#   tests/agent_loop/test_run.py::test_a_run_an_earlier_release_paused_with_its_call_stored_twice_resumes_after_the_upgrade,
#   tests/teams/test_continue_team.py::test_a_team_row_an_earlier_release_paused_with_its_member_call_copied_resumes_after_the_upgrade,
#   tests/session_storage/test_legacy_agno3_conversion.py::test_a_paused_team_runs_embedded_members_become_its_member_rows,
#   tests/session_storage/test_legacy_agno3_conversion.py::test_an_embedded_member_s_own_requirement_folds_into_its_call
_PAUSED = RunStatus.paused.value


def fold_requirements(run: dict[str, Any], members: Mapping[str, dict[str, Any]]) -> None:
    """Fold the ``requirements`` of ``run``, a paused run's stored dict, into the calls they copy, then drop them.

    ``members`` holds a paused team run's paused member run dicts by run id, whose own requirements fold first; an
    entry naming one of them folds into that member's tool executions.
    """
    entries = run.pop("requirements", None)
    if not isinstance(entries, list):
        return
    for entry in cast("list[Any]", entries):
        if not isinstance(entry, dict) or not isinstance(call := entry.get("tool_execution"), dict):
            continue
        member_run_id = entry.get("member_run_id")
        owner = run if member_run_id in (None, run.get("run_id")) else members.get(member_run_id)
        if owner is not None:
            _fold_entry(entry, call, owner)


def _fold_entry(entry: dict[str, Any], call: dict[str, Any], owner: dict[str, Any]) -> None:
    """Put the call ``entry`` copies, with its decision and id, over the latest paused tool execution it repeats.

    A provider may reuse a call id within a run, so the paused call is the latest under its id.
    """
    folded = dict(call)
    if (confirmation := entry.get("confirmation")) is not None and folded.get("confirmed") is None:
        folded["confirmed"] = confirmation
        if confirmation is False and (note := entry.get("confirmation_note")) is not None:
            folded["confirmation_note"] = note
    if (answer := entry.get("external_execution_result")) is not None and folded.get("result") is None:
        folded["result"] = answer
    if isinstance(pause_id := entry.get("id"), str):
        folded["pause_id"] = pause_id
    tools = owner.get("tools")
    if not isinstance(tools, list):
        return
    for index in reversed(range(len(tools))):
        tool = tools[index]
        if isinstance(tool, dict) and tool.get("tool_call_id") == folded.get("tool_call_id") and _paused_call(tool):
            tools[index] = folded
            return


def _paused_call(tool: dict[str, Any]) -> bool:
    return bool(tool.get("requires_confirmation") or tool.get("external_execution_required"))


def fold_run_requirements(connection: sqlite3.Connection, names: TableNames) -> None:
    """Fold every paused run's requirements into its tool rows in the caller's write transaction, then clear them all."""
    runs = quote_identifier(names.log_runs)
    # Member rows first, so a paused team row's copies of its members' calls fold over what the members' rows hold.
    paused = connection.execute(
        f"SELECT run_id, requirements FROM {runs} "  # noqa: S608
        "WHERE status = ? AND requirements IS NOT NULL ORDER BY parent_run_id IS NULL",
        (_PAUSED,),
    ).fetchall()
    loaded: dict[str, _StoredRun] = {}
    for run_id, raw_requirements in paused:
        run = _load(connection, names, run_id, raw_requirements, loaded)
        if run is None:
            continue
        members = {
            member_id: member.run
            for (member_id,) in connection.execute(
                f"SELECT run_id FROM {runs} WHERE parent_run_id = ? AND status = ?",  # noqa: S608
                (run_id, _PAUSED),
            )
            if (member := _load(connection, names, member_id, None, loaded)) is not None
        }
        fold_requirements(run.run, members)
    tools = quote_identifier(names.log_tools)
    for run_id, run in loaded.items():
        for position, (tool, data) in enumerate(zip(run.run["tools"], run.stored_tools, strict=True)):
            if tool != decoded_mapping(data):
                connection.execute(
                    f"UPDATE {tools} SET data = ? WHERE run_id = ? AND ord = ?",  # noqa: S608
                    (json.dumps(tool), run_id, position),
                )
    _drop_pending_requirements(connection, names)
    connection.execute(f"UPDATE {runs} SET requirements = NULL WHERE requirements IS NOT NULL")  # noqa: S608


@dataclass(frozen=True, slots=True)
class _StoredRun:
    """One run as the fold reads it: its id, tool executions, and requirements as a run dict, and its tool rows."""

    run: dict[str, Any]
    stored_tools: list[str]


def _load(
    connection: sqlite3.Connection,
    names: TableNames,
    run_id: str,
    raw_requirements: object,
    loaded: dict[str, _StoredRun],
) -> _StoredRun | None:
    """Return run ``run_id`` from ``loaded``, else read and keep it there; None when one of its rows does not decode."""
    if run_id in loaded:
        return loaded[run_id]
    rows = connection.execute(
        f"SELECT data FROM {quote_identifier(names.log_tools)} WHERE run_id = ? ORDER BY ord",  # noqa: S608
        (run_id,),
    ).fetchall()
    tools = [decoded_mapping(data) for (data,) in rows]
    if any(tool is None for tool in tools):
        return None
    run: dict[str, Any] = {"run_id": run_id, "tools": tools}
    if raw_requirements is not None:
        try:
            run["requirements"] = decode_json(cast("str", raw_requirements))
        except (RecursionError, TypeError, ValueError):
            return None
    loaded[run_id] = _StoredRun(run, [data for (data,) in rows])
    return loaded[run_id]


def _drop_pending_requirements(connection: sqlite3.Connection, names: TableNames) -> None:
    """Drop ``pending_requirements`` from each run's delegation state, which lists its pending calls once."""
    runs = quote_identifier(names.log_runs)
    candidates = connection.execute(
        f"SELECT run_id, metadata FROM {runs} WHERE instr(metadata, ?)",  # noqa: S608
        ('"pending_requirements"',),
    ).fetchall()
    for run_id, raw_metadata in candidates:
        metadata = decoded_mapping(raw_metadata)
        state = (metadata or {}).get(DELEGATION_STATE_KEY)
        if metadata is None or not isinstance(state, dict) or "pending_requirements" not in state:
            continue
        del state["pending_requirements"]
        connection.execute(
            f"UPDATE {runs} SET metadata = ? WHERE run_id = ?",  # noqa: S608
            (json.dumps(metadata), run_id),
        )


__all__ = ["fold_requirements", "fold_run_requirements"]
