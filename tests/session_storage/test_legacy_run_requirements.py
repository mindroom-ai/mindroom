"""Paused runs an earlier release stored twice, as tool rows and as requirements, read once layout version 8 folds them.

A paused call is read from its tool row alone, so a decision, an answer, or a delegation id that only a stored
requirement held must reach the tool row (``mindroom.session_storage.legacy_run_requirements``).
"""
# ruff: noqa: D103

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.delegation.execution import answered_delegation_calls, has_delegation_state
from mindroom.delegation.state import DELEGATION_STATE_KEY, DelegationState
from mindroom.history.types import HistoryScope
from mindroom.llm.runs import RunStatus, TeamRunRecord, ToolExecution
from mindroom.session_storage.agent_storage import create_agent_state_storage, create_state_storage
from mindroom.session_storage.legacy_run_requirements import fold_requirements
from mindroom.session_storage.schema import LOG_VERSION, TableNames, layout_version, quote_identifier
from tests.llm.agno_dict_helpers import expected_tool_dict
from tests.session_storage.goldens import LEGACY_AGNO3
from tests.session_storage.layout_4 import store_rows, upgrade_from_layout

if TYPE_CHECKING:
    from mindroom.session_storage.store import ConversationStore

_ROWS: dict[str, dict[str, Any]] = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "legacy_run_content" / "v2026.10.627_rows.json").read_text(),
)
_AGNO: dict[str, Any] = json.loads((LEGACY_AGNO3 / "codecs.json").read_text(encoding="utf-8"))
_AGENT = HistoryScope(kind="agent", scope_id="code")
_SQUAD = HistoryScope(kind="team", scope_id="squad")
_SESSION = "session-1"


def _row(name: str) -> dict[str, Any]:
    """Return one paused row of v2026.10.627 as the release before layout version 8 left it: without its notice."""
    row = deepcopy(_ROWS[name])
    row.pop("content", None)
    return row


def _upgraded(store: ConversationStore, owner: HistoryScope, rows: list[dict[str, Any]]) -> ConversationStore:
    store_rows(store, _SESSION, rows)
    upgrade_from_layout(store, owner, 7)
    return store


def _column(store: ConversationStore, column: str, run_id: str) -> object:
    names = TableNames.of_database(store.path)
    with closing(sqlite3.connect(store.path)) as connection:
        [(value,)] = connection.execute(
            f"SELECT {column} FROM {quote_identifier(names.log_runs)} WHERE run_id = ?",  # noqa: S608
            (run_id,),
        ).fetchall()
    return value


# --- one run dict ------------------------------------------------------------------------------------------------


# Stored requirement keys beside a pending tool execution, by case.
_DECISIONS_ON_THE_REQUIREMENT: dict[str, dict[str, Any]] = {
    "rejection": {"confirmation": False, "confirmation_note": "No."},
    "approval": {"confirmation": True},
    "external-result": {"external_execution_result": "done"},
    "explicit-tool-decision-wins": {
        "confirmation": False,
        "tool_execution": {
            "tool_call_id": "call-1",
            "tool_name": "t",
            "requires_confirmation": True,
            "confirmed": True,
            "created_at": 3,
        },
    },
}


@pytest.mark.parametrize("case", list(_DECISIONS_ON_THE_REQUIREMENT))
def test_a_decision_only_the_requirement_held_reaches_its_call(case: str) -> None:
    """Agno's reader moved a requirement's decision onto its call unless the call held one; the fold does the same."""
    waiting = {"tool_call_id": "call-1", "tool_name": "t", "requires_confirmation": True}
    entry = {
        "id": "req-1",
        "created_at": "2026-01-02T03:04:05+00:00",
        "tool_execution": {**waiting, "created_at": 3},
        **_DECISIONS_ON_THE_REQUIREMENT[case],
    }
    run: dict[str, Any] = {"run_id": "r", "tools": [waiting], "requirements": [entry]}

    fold_requirements(run, {}, {})

    # Agno's golden is the requirement as its reader read it, its call holding the decision; it read without a call id.
    expected = expected_tool_dict(_AGNO["run_requirement_decision_moved"][case]["tool_execution"])
    expected = {**expected, "tool_call_id": "call-1"}
    assert ToolExecution.from_dict(run["tools"][0]).to_dict() == expected
    assert "requirements" not in run


def test_the_paused_call_under_the_id_takes_the_requirement_and_a_delegation_call_its_id() -> None:
    """A provider may reuse a call id in a later request of the run; the earlier, finished call keeps what it stored.

    Only a delegation call keeps the requirement's id, which its child and hooks are kept under.
    """
    finished = {"tool_call_id": "c1", "tool_name": "run_subagent", "result": "done"}
    waiting = {"tool_call_id": "c1", "tool_name": "run_subagent", "external_execution_required": True}
    gated = {"tool_call_id": "c2", "tool_name": "add", "requires_confirmation": True}
    run: dict[str, Any] = {
        "run_id": "r",
        "tools": [finished, waiting, gated],
        "requirements": [{"id": "q", "tool_execution": waiting}, {"id": "g", "tool_execution": gated}],
    }

    fold_requirements(run, {}, {})

    assert run["tools"] == [finished, {**waiting, "pause_id": "q"}, gated]


def test_a_resolved_requirement_never_takes_a_later_call_that_reuses_its_id() -> None:
    """Earlier releases kept a requirement the run resolved beside the one it paused on next, under the same call id.

    Only the waiting one folds, into the paused call; two waiting calls under one id take their calls in call order,
    and a team row's copy takes the call its member's own entry took.
    """
    approved = {
        "tool_call_id": "c1",
        "tool_name": "add",
        "tool_args": {"a": 1},
        "requires_confirmation": False,
        "confirmed": True,
        "result": "3",
    }
    waiting = {"tool_call_id": "c1", "tool_name": "add", "tool_args": {"a": 2}, "requires_confirmation": True}
    again = {**waiting, "tool_args": {"a": 3}}
    run: dict[str, Any] = {
        "run_id": "r",
        "tools": [dict(approved), dict(waiting), dict(again)],
        "requirements": [
            {"id": "q1", "confirmation": True, "tool_execution": approved},
            {"id": "q2", "tool_execution": waiting},
            {"id": "q3", "tool_execution": again},
        ],
    }

    fold_requirements(run, {}, {})

    assert run["tools"] == [approved, waiting, again]

    member: dict[str, Any] = {
        "run_id": "m",
        "tools": [dict(approved), dict(waiting), dict(again)],
        "requirements": [
            {"id": "q1", "confirmation": True, "tool_execution": approved},
            {"id": "q2", "tool_execution": waiting},
            {"id": "q3", "tool_execution": again},
        ],
    }
    named = {"member_agent_id": "checker", "member_run_id": "m"}
    team: dict[str, Any] = {
        "run_id": "t",
        "tools": [],
        "requirements": [
            {"id": "q1", **named, "confirmation": True, "tool_execution": approved},
            {"id": "q3", **named, "tool_execution": {**again, "confirmation_note": "the team's copy"}},
        ],
    }
    claims: dict[int, dict[str, int]] = {}
    fold_requirements(member, {}, claims)

    fold_requirements(team, {"m": member}, claims)

    assert member["tools"] == [approved, waiting, {**again, "confirmation_note": "the team's copy"}]


def test_an_entry_no_paused_call_matches_folds_into_nothing() -> None:
    """No release is known to write one: no call, call id, or id, a call the run does not hold, or a run not waiting."""
    waiting = {"tool_call_id": "c1", "tool_name": "add", "requires_confirmation": True}
    without_id = {"tool_call_id": None, "tool_name": "add", "requires_confirmation": True}
    member = {"run_id": "m", "tools": [{"tool_call_id": "c9", "tool_name": "add", "result": "1"}]}
    run: dict[str, Any] = {
        "run_id": "r",
        "tools": [waiting, without_id],
        "requirements": [
            {"id": "q1", "confirmation": True},
            {"tool_execution": waiting},
            {"id": "q0", "tool_execution": without_id},
            {"id": "q2", "tool_execution": {"tool_call_id": "c2", "tool_name": "add", "requires_confirmation": True}},
            {"id": "q3", "member_run_id": "gone", "tool_execution": waiting},
            {"id": "q4", "member_run_id": "m", "tool_execution": {**member["tools"][0], "requires_confirmation": True}},
            "not an entry",
        ],
    }

    fold_requirements(run, {"m": member}, {})

    assert run == {"run_id": "r", "tools": [waiting, without_id]}
    assert member["tools"] == [{"tool_call_id": "c9", "tool_name": "add", "result": "1"}]


@pytest.mark.parametrize(
    "odd",
    [
        {"member_run_id": ["m"]},
        {"member_run_id": {"m": 1}},
        {"id": ["q"]},
        {"tool_call_id": ["c1"]},
        {"tool_call_id": ""},
    ],
    ids=["member-run-list", "member-run-object", "id-list", "call-id-list", "empty-call-id"],
)
def test_an_entry_whose_ids_are_not_text_folds_into_nothing(odd: dict[str, Any]) -> None:
    """Worker code can write the row; an entry naming its run, itself, or its call by anything but text folds nowhere.

    That holds even when a call of the run was stored under the same odd id.
    """
    waiting = {"tool_call_id": "c1", "tool_name": "run_subagent", "external_execution_required": True}
    if "tool_call_id" in odd:
        waiting["tool_call_id"] = odd["tool_call_id"]
    entry: dict[str, Any] = {"id": "q", "tool_execution": dict(waiting), **odd}
    entry.pop("tool_call_id", None)
    member = {"run_id": "m", "tools": [dict(waiting)]}
    run: dict[str, Any] = {"run_id": "r", "tools": [dict(waiting)], "requirements": [entry]}

    fold_requirements(run, {"m": member}, {})

    assert (run, member) == ({"run_id": "r", "tools": [waiting]}, {"run_id": "m", "tools": [waiting]})


def test_a_team_row_s_copy_of_a_member_call_folds_over_the_member_s_own() -> None:
    """The driver recorded a member delegation's answer only on the team's copy, so the copy's call wins."""
    waiting = {"tool_call_id": "sub", "tool_name": "run_subagent", "external_execution_required": True}
    member: dict[str, Any] = {
        "run_id": "m",
        "tools": [dict(waiting)],
        "requirements": [{"id": "q", "tool_execution": waiting}],
    }
    claims: dict[int, dict[str, int]] = {}
    fold_requirements(member, {}, claims)
    answered = {**waiting, "result": "Helper says 42", "tool_call_error": False}
    team: dict[str, Any] = {
        "run_id": "t",
        "tools": [],
        "requirements": [
            {"id": "q", "member_agent_id": "checker", "member_run_id": "m", "tool_execution": answered},
        ],
    }

    fold_requirements(team, {"m": member}, claims)

    assert member["tools"] == [{**answered, "pause_id": "q"}]
    assert team == {"run_id": "t", "tools": []}


# --- a database at layout version 7 ------------------------------------------------------------------------------


def test_a_paused_agent_row_keeps_each_call_once(tmp_path: Path) -> None:
    row = _row("agent_paused")
    [requirement] = row["requirements"]

    store = _upgraded(create_agent_state_storage(_AGENT.scope_id, tmp_path), _AGENT, [row])

    run = store.get_run(row["run_id"])
    assert run is not None
    [waiting] = run.tools or ()
    assert waiting == ToolExecution.from_dict(requirement["tool_execution"])
    assert [item.tool_execution for item in run.requirements] == [waiting]
    assert _column(store, "requirements", row["run_id"]) is None
    with closing(sqlite3.connect(store.path)) as connection:
        assert layout_version(connection, TableNames.of_database(store.path)) == LOG_VERSION == 8


def test_a_worker_written_entry_with_a_list_for_its_member_run_does_not_stop_the_upgrade(tmp_path: Path) -> None:
    """The step finishes and the run keeps waiting on its call; a rolled-back step would fail every later open."""
    row = _row("agent_paused")
    [requirement] = row["requirements"]
    row["requirements"].append({**requirement, "id": "planted", "member_run_id": ["someone"]})

    store = _upgraded(create_agent_state_storage(_AGENT.scope_id, tmp_path), _AGENT, [row])

    with closing(sqlite3.connect(store.path)) as connection:
        assert layout_version(connection, TableNames.of_database(store.path)) == LOG_VERSION
    run = store.get_run(row["run_id"])
    assert run is not None
    assert [item.tool_execution for item in run.requirements] == [
        ToolExecution.from_dict(requirement["tool_execution"]),
    ]
    assert _column(store, "requirements", row["run_id"]) is None


def test_a_delegation_answered_only_on_its_requirement_stays_answered(tmp_path: Path) -> None:
    """A loaded parent the driver answered stored the answer and the child's id only on the requirement.

    After the fold, the parent's call holds both, so the driver hands the answer to the continuation instead of running
    the call again, and finds the child its id keeps; the delegation state lists its pending calls once.
    """
    row = _row("agent_paused")
    delegation = {
        "tool_call_id": "call-delegate",
        "tool_name": "run_subagent",
        "tool_args": {"agent_name": "helper", "task": "Multiply"},
        "external_execution_required": True,
    }
    row["tools"] = [*row["tools"], delegation]
    row["requirements"].append(
        {
            "id": "parent-requirement",
            "external_execution_result": "Helper says 42",
            "tool_execution": {**delegation, "result": "Helper says 42", "tool_call_error": False},
        },
    )
    state = DelegationState(pending_tools=[{"tool_call_id": "c1", "tool_name": "add"}]).to_dict()
    row["metadata"] = {**(row.get("metadata") or {}), DELEGATION_STATE_KEY: {**state, "pending_requirements": [{}]}}

    store = _upgraded(create_agent_state_storage(_AGENT.scope_id, tmp_path), _AGENT, [row])

    run = store.get_run(row["run_id"])
    assert run is not None
    assert answered_delegation_calls(run) == {(None, "call-delegate"): "Helper says 42"}
    assert has_delegation_state(run)
    # Only the delegation call keeps its requirement's id; the confirmation call beside it needs none.
    assert [tool.pause_id for tool in run.tools or ()] == [None, "parent-requirement"]
    stored_state = (run.metadata or {})[DELEGATION_STATE_KEY]
    assert "pending_requirements" not in stored_state
    assert DelegationState.from_metadata(run.metadata).pending_tools == state["pending_tools"]


def test_a_member_row_s_own_requirement_folds_whatever_its_team_row_copied(tmp_path: Path) -> None:
    team_row, member_row = _row("team_paused"), _row("member_paused")
    team_row["requirements"] = []
    [own] = member_row["requirements"]
    own["tool_execution"]["confirmation_note"] = "only on the member's own entry"

    store = _upgraded(
        create_state_storage(TableNames.of_team(_SQUAD).storage_name, tmp_path, _SQUAD),
        _SQUAD,
        [team_row, member_row],
    )

    team = store.get_run(team_row["run_id"])
    assert isinstance(team, TeamRunRecord)
    assert [item.tool_execution.confirmation_note for item in team.requirements] == ["only on the member's own entry"]


def test_a_paused_team_row_waits_on_its_member_s_call_from_the_member_row(tmp_path: Path) -> None:
    team_row, member_row = _row("team_paused"), _row("member_paused")
    [copy] = team_row["requirements"]
    copy["tool_execution"]["confirmation_note"] = "only on the team's copy"

    store = _upgraded(
        create_state_storage(TableNames.of_team(_SQUAD).storage_name, tmp_path, _SQUAD),
        _SQUAD,
        [team_row, member_row],
    )

    team = store.get_run(team_row["run_id"])
    assert isinstance(team, TeamRunRecord)
    [member] = team.member_responses
    assert member.run_id == member_row["run_id"]
    [requirement] = team.requirements
    assert (requirement.member_agent_id, requirement.member_display_name, requirement.member_run_id) == (
        "checker",
        "Checker",
        member.run_id,
    )
    assert requirement.tool_execution.confirmation_note == "only on the team's copy"
    stored_member = store.get_run(member_row["run_id"])
    assert stored_member is not None
    assert stored_member.tools == member.tools
    assert [_column(store, "requirements", run_id) for run_id in (team.run_id, member.run_id)] == [None, None]
    assert team.status is RunStatus.paused
