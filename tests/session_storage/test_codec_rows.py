"""A run's stored dict splits over its storage v2 run row and its message rows, and joins again unchanged."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord, ToolExecution
from mindroom.session_storage.codec import run_columns, run_kind, run_record
from mindroom.session_storage.legacy_agno3_schema import run_from_row

_RUNS = Path(__file__).resolve().parents[1] / "fixtures" / "agno_parity" / "runs"
# The stored-dict keys a run row keeps in columns of its own or in rows of other tables, never in ``record``.
_NOT_IN_RECORD = {
    "run_id",
    "session_id",
    "parent_run_id",
    "status",
    "messages",
    "tools",
    "requirements",
    "metadata",
    "member_responses",
}


def _stored_cases() -> dict[str, tuple[str, dict[str, Any]]]:
    """Return every case of the stored run goldens, agent and team, with the row type it was stored under."""
    return {
        case: ("team" if name == "team_run_output" else "agent", data)
        for name in ("run_output", "team_run_output")
        for case, data in json.loads((_RUNS / f"{name}.json").read_text(encoding="utf-8")).items()
    }


_CASES = _stored_cases()


def _round_trip(run: RunRecord | TeamRunRecord) -> RunRecord | TeamRunRecord:
    """Split ``run`` into its row columns and message dicts, as the store writes it, and decode it again."""
    data = run.to_dict()
    columns = run_columns(data, run_kind(run))
    return run_record(data["run_id"], data["session_id"], data.get("parent_run_id"), columns, data["messages"])


@pytest.mark.parametrize("case", sorted(_CASES))
def test_every_stored_run_round_trips_through_its_row(case: str) -> None:
    """Each golden run decodes from its row and messages to the dict it was split from; team runs lose members."""
    row_type, stored = _CASES[case]
    run = run_from_row(row_type, json.dumps(stored))
    assert run is not None
    expected = run.to_dict()
    if isinstance(run, TeamRunRecord):
        expected["member_responses"] = []

    columns = run_columns(run.to_dict(), run_kind(run))
    decoded = _round_trip(run)

    assert decoded.to_dict() == expected
    assert list(decoded.to_dict()) == list(expected)
    assert type(decoded) is type(run)
    assert columns.status == stored["status"]
    assert _NOT_IN_RECORD.isdisjoint(json.loads(columns.record))


def test_the_goldens_hold_a_paused_team_run_with_members_and_runs_with_and_without_tools() -> None:
    """The round trip above covers embedded members, ``tools = []``, tool lists, and runs without tools."""
    team_paused = _CASES["team_squad:team-paused"][1]
    tools = {json.dumps(data.get("tools")) for _, data in _CASES.values() if not data.get("tools")}

    assert team_paused["member_responses"]
    assert tools == {"[]", "null"}
    assert any(data.get("tools") for _, data in _CASES.values())
    assert any("metadata" not in data for _, data in _CASES.values())


@pytest.mark.parametrize("value", [None, []], ids=["none", "empty"])
def test_tools_and_requirements_keep_empty_apart_from_none(value: list[Any] | None) -> None:
    """``[]`` is stored as JSON and None as SQL null, and each decodes as it was written."""
    run = RunRecord(run_id="r", session_id="s", agent_id="code", tools=value, requirements=value)

    columns = run_columns(run.to_dict(), "agent")
    decoded = _round_trip(run)

    assert (columns.tools, columns.requirements) == ((None, None) if value is None else ("[]", "[]"))
    assert (decoded.tools, decoded.requirements) == (value, value)


@pytest.mark.parametrize("value", [None, {}], ids=["none", "empty"])
def test_metadata_keeps_empty_apart_from_none(value: dict[str, Any] | None) -> None:
    """``{}`` is stored as JSON and None as SQL null, and each decodes as it was written."""
    run = RunRecord(run_id="r", session_id="s", agent_id="code", metadata=value)

    columns = run_columns(run.to_dict(), "agent")

    assert columns.metadata == (None if value is None else "{}")
    assert _round_trip(run).metadata == value


@pytest.mark.parametrize(
    ("run", "scope_key"),
    [
        (RunRecord(run_id="r", agent_id="code"), "agent:code"),
        (RunRecord(run_id="m", parent_run_id="t", agent_id="writer"), "agent:writer"),
        (TeamRunRecord(run_id="t", team_id="squad"), "team:squad"),
        (RunRecord(run_id="r"), "agent:"),
        (TeamRunRecord(run_id="t", team_id=None), "team:"),
    ],
    ids=["agent", "member", "team", "agent-without-id", "team-without-id"],
)
def test_the_scope_key_names_the_runs_agent_or_team(run: RunRecord | TeamRunRecord, scope_key: str) -> None:
    """A run's scope key is its ``HistoryScope.key``; a run without its owner's id gets a key no scope has."""
    assert run_columns(run.to_dict(), run_kind(run)).scope_key == scope_key


def test_the_rows_ids_and_status_win_over_the_record_json() -> None:
    """A record JSON that holds ids or a status, which ``run_columns`` never writes, cannot override the row's."""
    run = RunRecord(
        run_id="r",
        session_id="s",
        parent_run_id="team",
        agent_id="code",
        status=RunStatus.paused,
        messages=[Message(role="user", content="hi")],
        tools=[ToolExecution(tool_call_id="call", tool_name="shell")],
    )
    data = run.to_dict()
    columns = run_columns(data, "agent")
    stale = {"run_id": "old", "session_id": "old", "parent_run_id": "old", "status": "COMPLETED", "messages": []}
    planted = replace(columns, record=json.dumps({**json.loads(columns.record), **stale}))

    decoded = run_record("r", "s", "root", planted, data["messages"])

    assert (decoded.run_id, decoded.session_id, decoded.parent_run_id) == ("r", "s", "root")
    assert decoded.status is RunStatus.paused
    assert decoded.messages == run.messages
    assert decoded.tools == run.tools
