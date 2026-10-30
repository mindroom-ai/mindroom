"""Usage an earlier release's session totals counted beyond the usage ledger becomes one ledger row, once.

v2026.10.696 moved ``session_data["session_metrics"]`` by every run save, so the totals also held history compacted
away before the ledger began, which no run snapshot holds. Each database here is left as that release left it: the
current store's rows, the session's stored totals, and layout version 5.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import ExitStack, closing
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.history.types import HistoryScope
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.llm.usage import HelperUsage, MessageMetrics, RunMetrics, TokenCounts
from mindroom.session_storage.agent_storage import create_state_storage, prepare_conversation_database
from mindroom.session_storage.legacy_agno3_conversion import convert_conversation_database
from mindroom.session_storage.legacy_usage_totals import add_pre_ledger_baselines
from mindroom.session_storage.schema import LOG_VERSION, TableNames, meta_version
from mindroom.session_storage.store import ConversationStore, connect_by_name
from mindroom.session_storage.types import ConversationSession
from mindroom.usage.stats import collect_admin_usage
from tests.conftest import persist_runs
from tests.session_storage.agno_layout import write_agno_database

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_AGENT = HistoryScope(kind="agent", scope_id="code")
_TEAM = HistoryScope(kind="team", scope_id="crew")


def _metrics(input_tokens: int, output_tokens: int = 0, *, model: str = "model-a") -> RunMetrics:
    metrics = RunMetrics()
    tokens = TokenCounts(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )
    metrics.add(MessageMetrics(tokens=tokens), model_id=model, provider="P")
    return metrics


def _run(
    run_id: str,
    input_tokens: int,
    output_tokens: int = 0,
    *,
    model: str = "model-a",
    parent_run_id: str | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        agent_id="code",
        parent_run_id=parent_run_id,
        user_id="@alice:example.test",
        model_provider="P",
        model=model,
        status=RunStatus.completed,
        metrics=_metrics(input_tokens, output_tokens, model=model),
    )


def _totals(input_tokens: int, output_tokens: int = 0, *, models: dict[str, int] | None = None) -> dict[str, object]:
    """Return session totals as v2026.10.696's run saves stored them, with a details entry per model when given."""
    totals: dict[str, object] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    if models is not None:
        totals["details"] = {
            "model": [
                {"input_tokens": tokens, "total_tokens": tokens, "id": model, "provider": "P"}
                for model, tokens in models.items()
            ],
        }
    return {key: value for key, value in totals.items() if value}


def _session(session_id: str = "s1", owner: HistoryScope = _AGENT) -> ConversationSession:
    return ConversationSession(session_id=session_id, kind=owner.kind, owner_id=owner.scope_id)


def _store(root: Path, owner: HistoryScope = _AGENT) -> ConversationStore:
    """Open ``owner``'s database in ``root`` as a store open does, creating it without recording it as prepared.

    A store open prepares each database file once per process, and ``_as_released`` rewrites a fixture in place, so
    only the open after it may prepare the file.
    """
    names = TableNames("code")
    path = names.database_path(root)
    if path.exists():
        return create_state_storage("code", root, owner)
    path.parent.mkdir(parents=True, exist_ok=True)
    prepare_conversation_database(path, names, owner)
    return ConversationStore(path)


def _helper(store: ConversationStore, input_tokens: int, session_id: str = "s1") -> None:
    store.save_independent_usage(
        session_id=session_id,
        usage_id="compaction_summary:1",
        kind="compaction_summary",
        requester_id="@alice:example.test",
        usage=HelperUsage(
            run_id="1",
            model="summary-model",
            provider="P",
            metrics=_metrics(input_tokens, model="summary-model"),
            created_at=1_700_000_000,
        ),
        initial_session=_session(session_id),
    )


def _as_released(store: ConversationStore, totals: dict[str, object] | str | None, session_id: str = "s1") -> None:
    """Leave ``store``'s database at layout version 5, with ``totals`` as session ``session_id``'s stored totals.

    A string is stored as the row's ``session_data`` text itself.
    """
    session_data = totals if isinstance(totals, str) or totals is None else json.dumps({"session_metrics": totals})
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("UPDATE code_sessions SET session_data = ? WHERE session_id = ?", (session_data, session_id))
        connection.execute("UPDATE code_sessions_log_meta SET version = 5")


def _baseline(store: ConversationStore, session_id: str = "s1") -> dict[str, Any] | None:
    with closing(sqlite3.connect(store.path)) as connection:
        row = connection.execute(
            "SELECT usage_data FROM code_sessions_usage WHERE session_id = ? AND run_id = 'pre_ledger'",
            (session_id,),
        ).fetchone()
    return None if row is None else json.loads(row[0])


def _dump(store: ConversationStore) -> list[str]:
    with closing(sqlite3.connect(store.path)) as connection:
        return list(connection.iterdump())


def _paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )


def _config() -> Config:
    return Config(agents={"code": AgentConfig(display_name="Code")})


def _version(store: ConversationStore) -> int:
    with store.transaction(write=False) as connection:
        return meta_version(connection, store.names)


def _totals_of(tokens: TokenCounts) -> tuple[int, int, int]:
    return tokens.input_tokens, tokens.output_tokens, tokens.total_tokens


def test_a_baseline_keeps_the_report_of_a_compacted_session(tmp_path: Path) -> None:
    """Totals that counted compacted history report as before: the stored totals plus helper usage."""
    paths = _paths(tmp_path)
    root = paths.storage_root / "agents" / "code"
    store = _store(root)
    persist_runs(store, _session(), _run("r1", 5), _run("r2", 3))
    _helper(store, 3)
    # The release reported its stored 10 plus the helper's 3, by model: model-a 10 and summary-model 3.
    _as_released(store, _totals(10, models={"model-a": 10}))

    report = collect_admin_usage(config=_config(), runtime_paths=paths, include_requests=True)
    assert (report.totals.input_tokens, report.session_count, report.coverage.unavailable_sources) == (0, 0, 1)

    store = _store(root)
    report = collect_admin_usage(config=_config(), runtime_paths=paths, include_requests=True)

    assert _baseline(store) == {
        "run_id": "pre_ledger",
        "kind": "pre_ledger",
        "metrics": {
            "input_tokens": 2,
            "total_tokens": 2,
            "details": {"model": [{"id": "model-a", "provider": "P", "input_tokens": 2, "total_tokens": 2}]},
        },
    }
    assert (report.totals.input_tokens, report.session_count, report.coverage.unavailable_sources) == (13, 1, 0)
    assert [(row.model, row.totals.input_tokens, row.session_count) for row in report.cumulative_model_breakdown] == [
        ("model-a", 10, 1),
        ("summary-model", 3, 1),
    ]
    assert report.cumulative_model_coverage.unavailable_sources == 0
    assert [(row.model, row.totals.input_tokens) for row in report.model_breakdown] == [
        ("model-a", 8),
        ("summary-model", 3),
    ]
    # Usage stored before per-run snapshots has no request detail, as the release reported.
    assert report.request_coverage is not None
    assert report.request_coverage.unavailable_sources == 1
    dated = collect_admin_usage(config=_config(), runtime_paths=paths, since=0)
    assert dated.totals.input_tokens == 11


def test_a_baseline_holds_each_counter_above_the_run_rows(tmp_path: Path) -> None:
    """A counter stored below what the runs count adds nothing, and then reports the runs' larger count."""
    paths = _paths(tmp_path)
    root = paths.storage_root / "agents" / "code"
    store = _store(root)
    persist_runs(store, _session(), _run("r1", 8, 2))
    _as_released(store, _totals(6, 6))

    store = _store(root)

    assert _baseline(store) == {
        "run_id": "pre_ledger",
        "kind": "pre_ledger",
        "metrics": {"output_tokens": 4, "total_tokens": 2},
    }
    report = collect_admin_usage(config=_config(), runtime_paths=paths)
    assert _totals_of(report.totals) == (8, 6, 12)


@pytest.mark.parametrize(
    "totals",
    [
        _totals(10, models={"model-a": 10}),
        _totals(10, models={"model-b": 9}),
        _totals(10),
        {**_totals(10), "details": {"model": [{"input_tokens": 10, "total_tokens": 10, "id": "model-a"}]}},
        {**_totals(10), "details": {"model": ["not an object"]}},
    ],
    ids=["contradictory", "unreconciled", "absent", "unnamed", "malformed"],
)
def test_contradictory_stored_attribution_leaves_an_unknown_residual(
    tmp_path: Path,
    totals: dict[str, object],
) -> None:
    """Runs of model B under totals stored for model A, or unusable details, leave the residual under no model."""
    store = _store(tmp_path)
    persist_runs(store, _session(), _run("r1", 8, model="model-b"))
    _as_released(store, totals)

    store = _store(tmp_path)

    assert _baseline(store) == {
        "run_id": "pre_ledger",
        "kind": "pre_ledger",
        "metrics": {"input_tokens": 2, "total_tokens": 2},
    }


@pytest.mark.parametrize(
    "totals",
    [
        _totals(8),
        _totals(7),
        {"input_tokens": "x", "total_tokens": 10},
        {"input_tokens": -1, "total_tokens": 10},
        {"input_tokens": True, "total_tokens": 10},
        {"input_tokens": 10.5},
        "garbage",
        {},
        None,
    ],
    ids=["equal", "below", "text", "negative", "boolean", "fraction", "not-an-object", "none", "absent"],
)
def test_unusable_totals_or_none_beyond_the_runs_add_no_row(tmp_path: Path, totals: object) -> None:
    """Totals the reporter could not read, or that hold nothing beyond the runs, leave the ledger as it was."""
    store = _store(tmp_path)
    persist_runs(store, _session(), _run("r1", 8))
    _as_released(store, json.dumps({"session_metrics": totals}) if totals is not None else None)

    store = _store(tmp_path)

    assert _baseline(store) is None
    assert _version(store) == LOG_VERSION


@pytest.mark.parametrize("session_data", ["not json", '"text"', "[1]"])
def test_unreadable_session_data_adds_no_row(tmp_path: Path, session_data: str) -> None:
    """A row whose session data does not decode to an object keeps its bytes and adds no row."""
    store = _store(tmp_path)
    persist_runs(store, _session(), _run("r1", 8))
    _as_released(store, session_data)

    store = _store(tmp_path)

    assert _baseline(store) is None


@pytest.mark.parametrize(
    ("totals", "details"),
    [
        (_totals(5, models={"model-a": 5}), [{"id": "model-a", "provider": "P", "input_tokens": 5, "total_tokens": 5}]),
        ({**_totals(5), "details": {"model": [{"input_tokens": 5, "total_tokens": 5, "id": "model-a"}]}}, None),
    ],
    ids=["named", "unnamed"],
)
def test_helper_rows_are_never_subtracted(tmp_path: Path, totals: dict[str, object], details: object) -> None:
    """Helper usage was never part of the stored totals, so a session with only helper rows keeps all its totals.

    Its stored details name each model, or the reporter could not use them and the residual has no model.
    """
    store = _store(tmp_path)
    _helper(store, 3)
    _as_released(store, totals)

    store = _store(tmp_path)

    baseline = _baseline(store)
    assert baseline is not None
    assert baseline["metrics"]["input_tokens"] == 5
    assert baseline["metrics"].get("details", {}).get("model") == details


def test_member_runs_count_only_in_a_team_database(tmp_path: Path) -> None:
    """The reporter counts member rows in a team's database only, so an agent's database keeps them in its baseline."""
    team = _store(tmp_path / "team", _TEAM)
    team_session = _session(owner=_TEAM)
    team_run = TeamRunRecord(run_id="t1", team_id="crew", status=RunStatus.completed, metrics=_metrics(4))
    persist_runs(team, team_session, _run("m1", 3, parent_run_id="t1"), team_run)
    _as_released(team, _totals(7, models={"model-a": 7}))
    agent = _store(tmp_path / "agent")
    agent_session = _session()
    agent.save_run(agent_session, _run("m1", 3, parent_run_id="elsewhere"), record_usage=True)
    persist_runs(agent, agent_session, _run("r1", 4))
    _as_released(agent, _totals(7, models={"model-a": 7}))

    team, agent = _store(tmp_path / "team", _TEAM), _store(tmp_path / "agent")

    assert _baseline(team) is None
    baseline = _baseline(agent)
    assert baseline is not None
    assert baseline["metrics"]["details"] == {
        "model": [{"id": "model-a", "provider": "P", "input_tokens": 3, "total_tokens": 3}],
    }


def test_upgrading_adds_baselines_once_and_rolls_back_with_its_step(tmp_path: Path) -> None:
    """The step lands with the version bump or not at all, and a reopened database, or a second run, changes nothing."""
    store = _store(tmp_path)
    persist_runs(store, _session(), _run("r1", 8))
    _as_released(store, _totals(10))
    released = _dump(store)
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER refuse BEFORE INSERT ON code_sessions_usage WHEN NEW.run_id = 'pre_ledger' "
            "BEGIN SELECT RAISE(ABORT, 'baseline refused'); END",
        )

    with pytest.raises(sqlite3.IntegrityError, match="baseline refused"):
        _store(tmp_path)

    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("DROP TRIGGER refuse")
    assert _dump(store) == released

    store = _store(tmp_path)
    upgraded = _dump(store)
    assert _baseline(store) is not None
    _store(tmp_path)
    assert _dump(store) == upgraded
    persist_runs(store, _session(), _run("r2", 1))
    after_run = _dump(store)
    with store.transaction(write=True) as connection:
        add_pre_ledger_baselines(connection, store.names)
    assert _dump(store) == after_run


def test_converting_an_agno_database_adds_its_baselines(tmp_path: Path) -> None:
    """An Agno-layout database converts straight to the current version, with the baseline its totals leave."""
    names = TableNames("code")
    database = names.database_path(tmp_path)
    write_agno_database(
        database,
        ConversationSession(session_id="s1", kind="agent", owner_id="code", runs=[_run("r1", 8)], created_at=1),
        usage=False,
    )
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE code_sessions SET session_data = ?", (json.dumps({"session_metrics": _totals(10)}),))

    with ExitStack() as descriptors, closing(connect_by_name(database)) as connection:
        convert_conversation_database(connection, database, names, descriptors, owner=_AGENT)

    store = _store(tmp_path)
    assert _version(store) == LOG_VERSION
    assert _baseline(store) == {
        "run_id": "pre_ledger",
        "kind": "pre_ledger",
        "metrics": {"input_tokens": 2, "total_tokens": 2},
    }
