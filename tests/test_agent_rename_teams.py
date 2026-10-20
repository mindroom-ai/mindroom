"""Tests for moving the team history scopes that include a renamed agent."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from agno.session.team import TeamSession

from mindroom import constants
from mindroom.agent_rename.steps import (
    FileRewrite,
    MovePath,
    RenameApplyError,
    RenamePlan,
    RenameRefusedError,
    SqliteEdit,
    apply_plan,
)
from mindroom.agent_rename.teams import team_steps
from mindroom.config.agent import AgentConfig
from mindroom.history.session_context import (
    TEAM_STATE_ROOT_DIRNAME,
    scope_session_storage_name,
)
from mindroom.history.team_scope import ad_hoc_team_scope_id
from mindroom.history.types import HistoryScope
from tests.agent_rename_fixtures.teams import (
    AD_HOC,
    ALICE,
    CONFIGURED,
    CREW,
    HELPER,
    NEW,
    OLD,
    OTHER_CONFIGURED,
    PREFIX_AD_HOC,
    PRIVATE_AD_HOC,
    TeamScope,
    install_teams,
    seed_team,
    team_config,
    team_context,
    team_history,
)
from tests.agent_rename_helpers import sqlite_schema, tree_rows, tree_snapshot
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_rename.steps import Step
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

NOW = datetime(2026, 10, 20, 9, 30, 0, tzinfo=UTC)


def _scope_id(config: Config, team: TeamScope) -> str:
    if team.team_name is not None:
        return team.team_name
    scope_id = ad_hoc_team_scope_id(team.members, config.agents, requester_user_id=ALICE)
    assert scope_id is not None
    return scope_id


def _store(runtime_paths: RuntimePaths, scope_id: str) -> Path:
    """Return the state root of a team scope's session store."""
    storage_name = scope_session_storage_name(HistoryScope(kind="team", scope_id=scope_id))
    return runtime_paths.storage_root / TEAM_STATE_ROOT_DIRNAME / storage_name


def _apply(runtime_paths: RuntimePaths, steps: list[Step]) -> None:
    apply_plan(
        RenamePlan(storage_root=runtime_paths.storage_root, steps=tuple(steps), reports=()),
        kind="rename",
        now=NOW,
    )


def _plan(runtime_paths: RuntimePaths) -> list[Step]:
    return team_steps(runtime_paths, config=team_config(OLD), old_name=OLD, new_name=NEW)


def _touches(step: Step, store: Path) -> bool:
    """Whether a step reads or writes a path inside ``store``."""
    paths = (getattr(step, "source", None), getattr(step, "destination", None), getattr(step, "database", None))
    return any(path is not None and path.is_relative_to(store) for path in paths)


@pytest.mark.parametrize("team", [AD_HOC, PRIVATE_AD_HOC], ids=["shared", "private"])
def test_ad_hoc_history_visible_under_recomputed_scope(tmp_path: Path, team: TeamScope) -> None:
    """An ad-hoc team with the agent replays its history under the scope id its members resolve to after the rename."""
    runtime_paths = install_teams(test_runtime_paths(tmp_path))
    old_scope_id = _scope_id(team_config(OLD), team)
    before = tree_snapshot(runtime_paths.storage_root)

    steps = _plan(runtime_paths)

    # Planning reads the stores without writing a byte, side files included.
    assert tree_snapshot(runtime_paths.storage_root) == before
    _apply(runtime_paths, steps)

    history = team_history(runtime_paths, team_config(NEW), team.renamed())
    renamed = "+".join(sorted(team.renamed().members))
    assert history.scope_id == old_scope_id.replace("+".join(team.members), renamed)
    assert history.scope_id == _scope_id(team_config(NEW), team.renamed())
    assert history.replayed == ["r2"]
    assert history.summary == f"summary of {old_scope_id}'s r1"
    assert history.seen == {"$preserved", "$r1", "$r2"}
    assert history.queued
    other = team.members[1]
    # r1 and its member runs are folded behind the summary, so only r2's load.
    assert history.member_runs == [(NEW, f"r2-{OLD}"), (other, f"r2-{other}")]
    assert history.member_responses == [[NEW, other]]
    assert history.requirement_members == [NEW]
    assert history.member_messages == ["task r2", "done"]
    assert not _store(runtime_paths, old_scope_id).exists()
    assert sorted(path.name for path in (_store(runtime_paths, history.scope_id) / "sessions").iterdir()) == [
        f"{_store(runtime_paths, history.scope_id).name}.db",
    ]


def test_ad_hoc_scope_sorted_again_when_the_new_name_sorts_after_a_member(tmp_path: Path) -> None:
    """Renaming ``code`` to ``zeta`` moves ``team_code+helper`` to ``team_helper+zeta``, the scope its members resolve to."""
    runtime_paths = install_teams(test_runtime_paths(tmp_path))
    renamed = TeamScope(("zeta", HELPER))

    _apply(runtime_paths, team_steps(runtime_paths, config=team_config(OLD), old_name=OLD, new_name="zeta"))

    history = team_history(runtime_paths, team_config("zeta"), renamed)
    assert history.scope_id == "team_helper+zeta"
    assert history.replayed == ["r2"]
    assert history.summary == "summary of team_code+helper's r1"
    assert history.member_runs == [("zeta", f"r2-{OLD}"), (HELPER, f"r2-{HELPER}")]
    assert history.member_responses == [["zeta", HELPER]]
    assert not _store(runtime_paths, "team_code+helper").exists()


def test_configured_team_member_runs_visible_as_new_name(tmp_path: Path) -> None:
    """A configured team keeps its store, and the agent's member runs in it carry the new name."""
    runtime_paths = install_teams(test_runtime_paths(tmp_path))
    store = _store(runtime_paths, CREW)

    steps = _plan(runtime_paths)
    _apply(runtime_paths, steps)

    assert [type(step) for step in steps if _touches(step, store)] == [SqliteEdit]
    history = team_history(runtime_paths, team_config(NEW), CONFIGURED.renamed())
    assert history.scope_id == CREW
    assert history.replayed == ["r2"]
    assert history.summary == "summary of crew's r1"
    assert history.seen == {"$preserved", "$r1", "$r2"}
    assert history.queued
    assert history.member_runs == [(NEW, "r2-code"), (HELPER, "r2-helper")]
    assert history.member_responses == [[NEW, HELPER]]
    assert history.requirement_members == [NEW]
    assert history.member_messages == ["task r2", "done"]
    with team_context(runtime_paths, team_config(NEW), CONFIGURED.renamed()) as context:
        assert isinstance(context.session, TeamSession)
        assert context.session.get_messages(member_ids=[OLD]) == []


def test_scopes_without_agent_untouched(tmp_path: Path) -> None:
    """Renaming ``code`` plans nothing on the stores of ``team_code_team+helper`` or the team ``docs``."""
    runtime_paths = install_teams(test_runtime_paths(tmp_path))
    stores = [_store(runtime_paths, _scope_id(team_config(OLD), team)) for team in (PREFIX_AD_HOC, OTHER_CONFIGURED)]
    before = [tree_rows(store) for store in stores]

    steps = _plan(runtime_paths)
    _apply(runtime_paths, steps)

    assert not any(_touches(step, store) for step in steps for store in stores)
    assert [tree_rows(store) for store in stores] == before
    assert team_history(runtime_paths, team_config(NEW), PREFIX_AD_HOC).replayed == ["r2"]


def test_ad_hoc_store_tables_and_rows_renamed(tmp_path: Path) -> None:
    """The moved store's tables, indexes and schema versions take its new name; its rows the new scope and agent."""
    runtime_paths = install_teams(test_runtime_paths(tmp_path))
    old_scope_id, new_scope_id = "team_code+helper", "team_coder+helper"
    old_store, new_store = _store(runtime_paths, old_scope_id), _store(runtime_paths, new_scope_id)
    old_name, new_name = old_store.name, new_store.name
    old_database = old_store / "sessions" / f"{old_name}.db"
    expected = {tuple(part.replace(old_name, new_name) for part in entry) for entry in sqlite_schema(old_database)}

    steps = _plan(runtime_paths)
    _apply(runtime_paths, steps)

    database = new_store / "sessions" / f"{new_name}.db"
    assert [type(step) for step in steps if _touches(step, old_store) or _touches(step, new_store)] == [
        MovePath,
        SqliteEdit,
        MovePath,
    ]
    assert sqlite_schema(database) == expected
    with closing(sqlite3.connect(database)) as connection:
        stored_sql = connection.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
        assert all(old_name not in sql for (sql,) in stored_sql)
        assert set(connection.execute("SELECT table_name FROM agno_schema_versions")) == {
            (f"{new_name}_sessions",),
            (f"{new_name}_sessions_runs",),
        }
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        ((team_id, team_data, metadata, session_data),) = connection.execute(
            f'SELECT team_id, team_data, metadata, session_data FROM "{new_name}_sessions"',  # noqa: S608
        )
        assert (team_id, json.loads(team_data)) == (new_scope_id, {"name": "Team-code-helper", "team_id": new_scope_id})
        for key in (constants.MINDROOM_COMPACTION_METADATA_KEY, constants.MINDROOM_MATRIX_HISTORY_METADATA_KEY):
            assert set(json.loads(metadata)[key]["states"]) == {f"team:{new_scope_id}"}
        assert json.loads(session_data)["session_state"] == {
            constants.PENDING_COMPACTION_SCOPE_KEYS_SESSION_STATE_KEY: [f"team:{new_scope_id}"],
        }
        runs = connection.execute(
            f'SELECT run_id, agent_id, team_id, run_data FROM "{new_name}_sessions_runs" ORDER BY run_index',  # noqa: S608
        ).fetchall()
        assert [(run_id, agent_id, team_id) for run_id, agent_id, team_id, _data in runs] == [
            ("r1-code", NEW, None),
            ("r1-helper", HELPER, None),
            ("r1", None, new_scope_id),
            ("r2-code", NEW, None),
            ("r2-helper", HELPER, None),
            ("r2", None, new_scope_id),
        ]
        team_run = json.loads(runs[5][3])
        assert team_run["team_id"] == new_scope_id
        assert [event["team_id"] for event in team_run["events"]] == [new_scope_id]
        assert [member["agent_id"] for member in team_run["member_responses"]] == [NEW, HELPER]
        assert [member["events"][0]["agent_id"] for member in team_run["member_responses"]] == [NEW, HELPER]
        assert [requirement["member_agent_id"] for requirement in team_run["requirements"]] == [NEW]
        assert json.loads(runs[3][3])["agent_id"] == NEW
        assert connection.execute(f'SELECT scope_key FROM "{new_name}_sessions_summaries"').fetchall() == [  # noqa: S608
            (f"team:{new_scope_id}",),
        ]
        usage_query = f'SELECT usage_data FROM "{new_name}_sessions_usage"'  # noqa: S608
        usage = [json.loads(data) for (data,) in connection.execute(usage_query)]
        assert sorted(entry.get("team_id") or "" for entry in usage) == ["", "", "", "", new_scope_id, new_scope_id]


def _fail(_content: bytes) -> bytes:
    msg = "injected failure"
    raise RuntimeError(msg)


def test_failure_after_any_step_restores_the_tree(tmp_path: Path) -> None:
    """A failure after any number of completed steps, the last included, undoes them, leaving every row as before."""
    step_count = len(_plan(install_teams(test_runtime_paths(tmp_path / "count"))))
    assert step_count == 7
    for completed in range(step_count + 1):
        runtime_paths = install_teams(test_runtime_paths(tmp_path / str(completed)))
        sentinel = runtime_paths.storage_root / "sentinel"
        sentinel.write_text("unchanged", encoding="utf-8")
        before = tree_rows(runtime_paths.storage_root)
        steps = _plan(runtime_paths)[:completed]

        with pytest.raises(RenameApplyError, match="injected failure"):
            _apply(runtime_paths, [*steps, FileRewrite(sentinel, _fail, "Fail on purpose")])

        assert tree_rows(runtime_paths.storage_root) == before


def test_existing_new_scope_store_refuses(tmp_path: Path) -> None:
    """A store already under the recomputed scope refuses while planning, naming it."""
    runtime_paths = install_teams(test_runtime_paths(tmp_path))
    existing = _store(runtime_paths, "team_coder+helper")
    existing.mkdir(parents=True)

    with pytest.raises(RenameRefusedError, match=re.escape(str(existing))):
        _plan(runtime_paths)


def test_scope_already_naming_new_name_refuses(tmp_path: Path) -> None:
    """A scope with both names would merge two members into one, so it refuses."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = team_config(OLD)
    config.agents[NEW] = AgentConfig(display_name="Coder")
    seed_team(runtime_paths, config, TeamScope((OLD, NEW)))

    with pytest.raises(RenameRefusedError, match=re.escape("team_code+coder")):
        _plan(runtime_paths)


def test_separate_session_storage_moves_too(tmp_path: Path) -> None:
    """With ``MINDROOM_SESSION_STORAGE_PATH`` set, the team stores in that root move and are edited there."""
    base = test_runtime_paths(tmp_path)
    session_root = tmp_path / "session_state"
    runtime_paths = replace(
        base,
        process_env={**base.process_env, "MINDROOM_SESSION_STORAGE_PATH": str(session_root)},
    )
    seed_team(runtime_paths, team_config(OLD), AD_HOC)
    old_store = session_root / TEAM_STATE_ROOT_DIRNAME / _store(runtime_paths, "team_code+helper").name
    assert old_store.is_dir()

    _apply(runtime_paths, _plan(runtime_paths))

    history = team_history(runtime_paths, team_config(NEW), AD_HOC.renamed())
    assert (history.scope_id, history.replayed, history.member_responses) == (
        "team_coder+helper",
        ["r2"],
        [[NEW, HELPER]],
    )
    assert not old_store.exists()


def test_no_team_stores_plans_nothing(tmp_path: Path) -> None:
    """An install without team history has nothing to move."""
    assert _plan(test_runtime_paths(tmp_path)) == []
