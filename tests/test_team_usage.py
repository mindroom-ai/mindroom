"""Team usage exports count saved provider calls without counting extra replies."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest
from agno.db.sqlite import SqliteDb
from agno.metrics import MessageMetrics, ModelMetrics, RunMetrics
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.team import TeamSession
from agno.team import Team
from agno.team._session import update_session_metrics

from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.history.session_context import create_scope_session_storage
from mindroom.history.types import HistoryScope
from mindroom.team_scope import ad_hoc_team_scope_id
from mindroom.tool_system.worker_routing import build_tool_execution_identity
from mindroom.usage_stats import collect_admin_usage, collect_private_usage, collect_self_usage
from mindroom.usage_storage import quote_identifier

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_ALICE = "@alice:example.test"
_ALIAS = "@alias:example.test"
_BOB = "@bob:example.test"
_MIDNIGHT = 1_700_006_400


def _metrics(input_tokens: int, model: str) -> RunMetrics:
    return RunMetrics(
        input_tokens=input_tokens,
        output_tokens=1,
        total_tokens=input_tokens + 1,
        cache_read_tokens=input_tokens // 2,
        details={
            "model": [
                ModelMetrics(
                    id=model,
                    provider="test-provider",
                    input_tokens=input_tokens,
                    output_tokens=1,
                    total_tokens=input_tokens + 1,
                    cache_read_tokens=input_tokens // 2,
                ),
            ],
        },
    )


def _request(input_tokens: int, created_at: int) -> Message:
    return Message(
        role="assistant",
        content="private response",
        created_at=created_at,
        metrics=MessageMetrics(
            input_tokens=input_tokens,
            output_tokens=1,
            total_tokens=input_tokens + 1,
            cache_read_tokens=input_tokens // 2,
        ),
    )


@contextmanager
def _stored_team(
    paths: RuntimePaths,
    config: Config,
    team_id: str,
    *,
    requester_id: str = _ALIAS,
) -> Iterator[SqliteDb]:
    grandchild = RunOutput(
        run_id="grandchild",
        parent_run_id="member",
        agent_id="code",
        user_id=requester_id,
        model_provider="test-provider",
        model="member-model",
        created_at=_MIDNIGHT + 20,
        metrics=_metrics(30, "member-model"),
        messages=[_request(30, _MIDNIGHT + 20)],
    )
    member = TeamRunOutput(
        run_id="member",
        parent_run_id="leader",
        team_id="nested-team",
        metadata={"requester_id": requester_id},
        model_provider="test-provider",
        model="member-model",
        created_at=_MIDNIGHT + 10,
        metrics=_metrics(20, "member-model"),
        messages=[_request(20, _MIDNIGHT + 10)],
        member_responses=[grandchild],
    )
    leader = TeamRunOutput(
        run_id="leader",
        team_id=team_id,
        user_id=requester_id,
        model_provider="test-provider",
        model="leader-model",
        created_at=_MIDNIGHT - 10,
        metrics=_metrics(10, "leader-model"),
        messages=[_request(10, _MIDNIGHT - 10)],
        member_responses=[member],
    )
    session = TeamSession(session_id="session", team_id=team_id, user_id=_BOB, session_data={})
    update_session_metrics(Team(id=team_id, members=[]), session, leader)
    storage = create_scope_session_storage(
        agent_name="code",
        scope=HistoryScope(kind="team", scope_id=team_id),
        config=config,
        runtime_paths=paths,
        execution_identity=None,
    )
    assert isinstance(storage, SqliteDb)
    try:
        storage.upsert_session(session)
        for run in (leader, member, grandchild, member):
            storage.upsert_run(run, session_id="session")
        yield storage
    finally:
        storage.close()


@pytest.mark.parametrize("team_id", ["engineering", "team_code+research"])
@pytest.mark.parametrize("retained_conversation", [False, True])
def test_team_export_counts_each_saved_call_once(
    tmp_path: Path,
    team_id: str,
    retained_conversation: bool,
) -> None:
    """Configured and ad hoc team details include descendants and survive conversation cleanup."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    config = Config(
        agents={"code": AgentConfig(display_name="Code"), "research": AgentConfig(display_name="Research")},
        teams={"engineering": TeamConfig(display_name="Engineering", role="Team", agents=["code", "research"])},
        authorization=AuthorizationConfig(aliases={_ALICE: [_ALIAS]}),
    )
    with _stored_team(paths, config, team_id) as storage:
        if not retained_conversation:
            storage.delete_runs(["leader"])
        report = collect_admin_usage(config=config, runtime_paths=paths, include_daily=True, include_requests=True)

    assert report.totals.input_tokens == 60
    assert report.totals.output_tokens == 3
    assert report.totals.cache_read_tokens == 30
    assert report.session_count == 1
    assert [(row.key, row.run_count, row.retained_run_totals.total_tokens) for row in report.breakdown] == [
        (team_id, 1, 63),
    ]
    assert {(row.model, row.totals.total_tokens, row.run_count) for row in report.model_breakdown} == {
        ("leader-model", 11, 1),
        ("member-model", 52, 0),
    }
    assert {(row.model, row.totals.total_tokens) for row in report.cumulative_model_breakdown} == {
        ("leader-model", 11),
        ("member-model", 52),
    }
    assert [(row.user_id, row.totals.total_tokens, row.run_count) for row in report.user_breakdown] == [(_ALICE, 63, 1)]
    assert [(row.date, row.totals.total_tokens, row.run_count) for row in report.daily_breakdown] == [
        ("2023-11-14", 11, 1),
        ("2023-11-15", 52, 0),
    ]
    assert report.user_breakdown[0].daily_breakdown == report.daily_breakdown
    assert [(row.entity, row.user_id, row.model, row.totals.input_tokens) for row in report.request_breakdown] == [
        (team_id, _ALICE, "leader-model", 10),
        (team_id, _ALICE, "member-model", 20),
        (team_id, _ALICE, "member-model", 30),
    ]
    assert report.request_coverage is not None
    assert report.request_coverage.unavailable_sources == 2  # Only the absent standalone agent databases.
    assert "private response" not in json.dumps(report.to_dict())


@pytest.mark.parametrize("invalid_total", [-1, "invalid", 1.5])
def test_invalid_team_child_counter_preserves_valid_usage(tmp_path: Path, invalid_total: str | float) -> None:
    """One corrupt child must not hide valid leader/member detail or claim complete coverage."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    config = Config(agents={"code": AgentConfig(display_name="Code")})
    with _stored_team(paths, config, "team_code", requester_id=_ALICE) as storage:
        with sqlite3.connect(storage.db_file) as connection:
            table = quote_identifier(storage.session_table_name + "_usage")
            query = f"SELECT usage_data FROM {table} WHERE run_id = ?"  # noqa: S608
            payload = json.loads(connection.execute(query, ("grandchild",)).fetchone()[0])
            payload["metrics"]["total_tokens"] = invalid_total
            query = f"UPDATE {table} SET usage_data = ? WHERE run_id = ?"  # noqa: S608
            connection.execute(query, (json.dumps(payload), "grandchild"))
        report = collect_admin_usage(config=config, runtime_paths=paths, include_daily=True, include_requests=True)

    assert report.totals.total_tokens == 63  # Authoritative session counters remain valid.
    assert {(row.model, row.totals.total_tokens) for row in report.model_breakdown} == {
        ("leader-model", 11),
        ("member-model", 21),
    }
    assert report.breakdown[0].retained_run_totals.total_tokens == 32
    assert [(row.user_id, row.totals.total_tokens) for row in report.user_breakdown] == [(_ALICE, 32)]
    assert sum(row.totals.total_tokens for row in report.daily_breakdown) == 32
    assert [row.totals.total_tokens for row in report.request_breakdown] == [11, 21]
    assert report.model_coverage.unavailable_sources == 2  # Missing standalone store and the damaged team.
    assert report.daily_coverage is not None
    assert report.daily_coverage.unavailable_sources == 2
    assert report.request_coverage is not None
    assert report.request_coverage.unavailable_sources == 2


def test_team_child_without_requester_does_not_inherit_session_creator(tmp_path: Path) -> None:
    """A shared team's first session user cannot own an unattributed child from another turn."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    config = Config(agents={"code": AgentConfig(display_name="Code")})
    with _stored_team(paths, config, "team_code", requester_id=_ALICE) as storage:
        with sqlite3.connect(storage.db_file) as connection:
            table = quote_identifier(storage.session_table_name + "_usage")
            query = f"SELECT usage_data FROM {table} WHERE run_id = ?"  # noqa: S608
            payload = json.loads(connection.execute(query, ("grandchild",)).fetchone()[0])
            payload.pop("user_id")
            query = f"UPDATE {table} SET usage_data = ? WHERE run_id = ?"  # noqa: S608
            connection.execute(query, (json.dumps(payload), "grandchild"))
        report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)

    assert {(row.user_id, row.totals.input_tokens) for row in report.user_breakdown} == {(_ALICE, 30), (None, 30)}
    assert [row.user_id for row in report.request_breakdown] == [_ALICE, _ALICE, None]


def test_private_team_usage_stays_out_of_personal_agent_reports(tmp_path: Path) -> None:
    """Organization team discovery never broadens either personal agent discovery path."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    config = Config(agents={"code": AgentConfig(display_name="Code", private=AgentPrivateConfig(per="user"))})
    for requester in (_ALICE, _BOB):
        team_id = ad_hoc_team_scope_id(["code"], config.agents, requester_user_id=requester)
        assert team_id is not None
        with _stored_team(paths, config, team_id, requester_id=requester):
            pass

    report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
    assert {(row.user_id, row.totals.input_tokens) for row in report.user_breakdown} == {(_ALICE, 60), (_BOB, 60)}
    assert report.private_agent_breakdown == ()
    private = collect_private_usage(requester_id=_ALICE, config=config, runtime_paths=paths)
    identity = build_tool_execution_identity(
        channel="matrix",
        agent_name="code",
        runtime_paths=paths,
        requester_id=_ALICE,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    current = collect_self_usage(
        agent_name="code",
        requester_id=_ALICE,
        config=config,
        runtime_paths=paths,
        execution_identity=identity,
    )
    assert private.totals.total_tokens == current.totals.total_tokens == 0
    assert private.user_breakdown == current.user_breakdown == ()
