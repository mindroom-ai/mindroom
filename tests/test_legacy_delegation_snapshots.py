"""Delegation child snapshots earlier releases stored are rewritten once, and every reader reads them as before.

The fixtures under ``tests/fixtures/delegation_snapshots`` are what each release's own writers stored, with what
v2026.10.683's readers, the last that filled absent fields on every read, showed of them (see the README there).
"""

from __future__ import annotations

import importlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths, subagent_sessions_dir
from mindroom.delegation.legacy_snapshots import migrate_subagent_sessions
from mindroom.delegation.sessions import update_subagent_turn_sync
from mindroom.delegation.state import DELEGATION_STATE_KEY, DelegationChild, DelegationState, SubagentPersona
from mindroom.history.types import HistoryScope
from mindroom.session_storage.agent_storage import create_session_storage, create_state_storage
from mindroom.session_storage.legacy_agno3_conversion import migrate_conversation_storage
from mindroom.session_storage.schema import LOG_VERSION, TableNames
from mindroom.tool_system.worker_routing import ToolExecutionIdentity

_FIXTURES = Path(__file__).parent / "fixtures" / "delegation_snapshots"
_RELEASES = sorted(path.stem for path in _FIXTURES.glob("v*.json"))
_CONFIG = Config(
    agents={
        "leader": AgentConfig(display_name="Leader", delegate_to=["helper"]),
        "helper": AgentConfig(display_name="Helper"),
    },
)
_OWNER = ToolExecutionIdentity(
    channel="matrix",
    agent_name="leader",
    requester_id="@alice:example.org",
    room_id="!room:example.org",
    thread_id="$thread",
    resolved_thread_id="$thread",
    session_id="parent-session",
)
_CHILD_FIELDS = {field.name for field in fields(DelegationChild)}
_PERSONA_FIELDS = {field.name for field in fields(SubagentPersona)}


def _fixture(release: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / f"{release}.json").read_text())


def _paths(root: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=root / "config.yaml",
        storage_path=root / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": ""},
    )


def _database(paths: RuntimePaths) -> Path:
    return TableNames("leader").database_path(paths.storage_root / "agents" / "leader")


def _query(database: Path, sql: str, *parameters: object) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(sql, parameters).fetchall()


def _restore(release: str, root: Path) -> RuntimePaths:
    """Put ``release``'s session files and conversation database where that release left them."""
    fixture = _fixture(release)
    paths = _paths(root)
    sessions = subagent_sessions_dir(paths)
    sessions.mkdir(parents=True)
    for name, payload in fixture["subagent_sessions"].items():
        (sessions / name).write_text(json.dumps(payload))
    database = _database(paths)
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript("\n".join(fixture["conversation_database"]))
    return paths


async def _start(paths: RuntimePaths) -> None:
    """Run the startup steps that rewrite stored snapshots, in the order startup runs them."""
    await migrate_subagent_sessions(paths)
    await migrate_conversation_storage(paths)


def _session_children(paths: RuntimePaths) -> dict[str, dict[str, Any]]:
    return {
        path.name: json.loads(path.read_text())["child"] for path in sorted(subagent_sessions_dir(paths).glob("*.json"))
    }


def _run_state(paths: RuntimePaths) -> dict[str, Any]:
    run = create_session_storage("leader", _CONFIG, paths, _OWNER).get_run("parent-run")
    assert run is not None
    assert run.metadata is not None
    return run.metadata[DELEGATION_STATE_KEY]


def _view(child: DelegationChild) -> dict[str, Any]:
    """Every value a reader takes from ``child``, as the fixtures' views record it."""
    seen = asdict(child)
    if child.persona is not None:
        seen["persona"] |= {"kept_skills": child.persona.kept_skills, "kept_knowledge": child.persona.kept_knowledge}
    return json.loads(json.dumps(seen))


def _views(paths: RuntimePaths) -> dict[str, Any]:
    state = DelegationState.from_metadata({DELEGATION_STATE_KEY: _run_state(paths)})
    return {
        "subagent_sessions": {
            name: _view(DelegationChild.from_dict(child)) for name, child in _session_children(paths).items()
        },
        "run_children": [_view(child) for child in state.children],
        "run_pending_sources": {call_id: _view(source.child) for call_id, source in state.pending_tool_sources.items()},
    }


def _stored(paths: RuntimePaths) -> tuple[dict[str, bytes], str]:
    sessions = {path.name: path.read_bytes() for path in sorted(subagent_sessions_dir(paths).iterdir())}
    with closing(sqlite3.connect(_database(paths))) as connection:
        return sessions, "\n".join(connection.iterdump())


@pytest.mark.asyncio
@pytest.mark.parametrize("release", _RELEASES)
@pytest.mark.parametrize("swept", [True, False], ids=["startup", "store_open"])
async def test_every_reader_reads_a_rewritten_snapshot_as_the_release_before_the_rewrite_did(
    release: str,
    swept: bool,
    tmp_path: Path,
) -> None:
    """Each child in a session file, among a run's children, or behind its pending call reads as v2026.10.683 read it.

    The conversation database converts at startup, or at its store's first open when the startup sweep skipped it.
    """
    paths = _restore(release, tmp_path)

    await migrate_subagent_sessions(paths)
    if swept:
        await migrate_conversation_storage(paths)

    assert _views(paths) == _fixture(release)["views"]


@pytest.mark.asyncio
@pytest.mark.parametrize("release", _RELEASES)
async def test_a_rewritten_snapshot_holds_every_field_the_current_writer_writes(release: str, tmp_path: Path) -> None:
    """After startup, no stored child lacks a field or holds a null requested model, so readers need no default."""
    paths = _restore(release, tmp_path)

    await _start(paths)

    state = _run_state(paths)
    children = [
        *_session_children(paths).values(),
        *state["children"],
        *(source["child"] for source in state["pending_tool_sources"].values()),
    ]
    assert len(children) == 7
    for child in children:
        assert set(child) == _CHILD_FIELDS
        assert isinstance(child["requested_model_name"], str)
        assert child["persona"] is None or set(child["persona"]) == _PERSONA_FIELDS


@pytest.mark.asyncio
@pytest.mark.parametrize("release", _RELEASES)
async def test_a_second_start_changes_nothing(release: str, tmp_path: Path) -> None:
    """The rewrite runs once: a later start, or a later store open, leaves every file and row as the first left them."""
    paths = _restore(release, tmp_path)
    await _start(paths)
    _run_state(paths)
    first = _stored(paths)

    await _start(paths)
    _run_state(paths)

    assert _stored(paths) == first
    [(version,)] = _query(_database(paths), 'SELECT version FROM "leader_sessions_log_meta"')
    assert version == LOG_VERSION


@pytest.mark.asyncio
async def test_a_snapshot_in_the_current_shape_is_left_as_stored(tmp_path: Path) -> None:
    """Session files and a run the current writers wrote keep their bytes through the rewrite; only the version moves."""
    paths = _restore("v2026.10.683", tmp_path)
    await _start(paths)
    for child in _session_children(paths).values():
        update_subagent_turn_sync(DelegationChild.from_dict(child), paths)
    state = DelegationState.from_metadata({DELEGATION_STATE_KEY: _run_state(paths)})
    store = create_session_storage("leader", _CONFIG, paths, _OWNER)
    assert store.patch_run_metadata("parent-session", "parent-run", {DELEGATION_STATE_KEY: state.to_dict()})
    written = _stored(paths)
    with closing(sqlite3.connect(_database(paths))) as connection, connection:
        connection.execute('UPDATE "leader_sessions_log_meta" SET version = 4')

    await _start(paths)

    assert _stored(paths) == written


@pytest.mark.asyncio
async def test_the_step_from_version_4_takes_no_backup(tmp_path: Path) -> None:
    """A release at layout version 4 reads what the step to version 5 leaves, so the step backs nothing up."""
    paths = _restore("v2026.10.683", tmp_path)
    before = sorted(path.name for path in _database(paths).parent.iterdir())

    await _start(paths)

    assert sorted(path.name for path in _database(paths).parent.iterdir()) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        "not json",
        json.dumps({DELEGATION_STATE_KEY: ["not", "an", "object"]}),
        json.dumps({DELEGATION_STATE_KEY: {"children": [{"model_name": "default"}], "gates": {}}}),
        json.dumps(
            {DELEGATION_STATE_KEY: {"children": "none", "pending_tool_sources": {"call": {"tool_call_id": "c"}}}},
        ),
    ],
    ids=["undecodable", "state_not_an_object", "child_the_reader_rejects", "no_children"],
)
async def test_a_snapshot_the_reader_rejects_stays_as_stored(metadata: str, tmp_path: Path) -> None:
    """Worker code can write conversation databases, so a state the current reader cannot take is left for it to reject."""
    paths = _restore("v2026.10.683", tmp_path)
    database = _database(paths)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute('UPDATE "leader_sessions_log_runs" SET metadata = ?', (metadata,))

    await _start(paths)

    assert _query(database, 'SELECT metadata FROM "leader_sessions_log_runs"') == [(metadata,)]
    assert _query(database, 'SELECT version FROM "leader_sessions_log_meta"') == [(LOG_VERSION,)]


@pytest.mark.asyncio
async def test_an_unreadable_session_file_neither_stops_startup_nor_changes(tmp_path: Path) -> None:
    """A session file that is not JSON or holds no child stays as it is, with a warning, and the others are rewritten."""
    paths = _restore("v2026.10.18", tmp_path)
    sessions = subagent_sessions_dir(paths)
    unreadable = {"a" * 32 + ".json": b"{not json", "b" * 32 + ".json": b'{"owner": {}}'}
    for name, content in unreadable.items():
        (sessions / name).write_bytes(content)

    with capture_logs() as logs:
        await migrate_subagent_sessions(paths)

    assert {name: (sessions / name).read_bytes() for name in unreadable} == unreadable
    skipped = [entry["path"] for entry in logs if entry["event"] == "subagent_session_rewrite_skipped"]
    assert sorted(skipped) == sorted(str(sessions / name) for name in unreadable)
    rewritten = [
        json.loads(path.read_text())["child"] for path in sessions.glob("*.json") if path.name not in unreadable
    ]
    assert len(rewritten) == 3
    assert all(set(child) == _CHILD_FIELDS for child in rewritten)


@pytest.mark.asyncio
async def test_a_session_file_the_reader_rejects_stays_as_stored(tmp_path: Path) -> None:
    """A session file whose child the current reader rejects even with every field filled keeps its bytes."""
    paths = _restore("v2026.10.18", tmp_path)
    path = next(subagent_sessions_dir(paths).glob("*.json"))
    payload = json.loads(path.read_text())
    del payload["child"]["model_name"]
    path.write_text(json.dumps(payload))
    stored = path.read_bytes()

    await migrate_subagent_sessions(paths)

    assert path.read_bytes() == stored


@pytest.mark.asyncio
async def test_the_startup_sweep_takes_a_version_4_team_databases_owner_from_its_meta_row(tmp_path: Path) -> None:
    """A team database at version 4, whose rows no longer name the team, converts at startup without a warning."""
    paths = _paths(tmp_path)
    scope = HistoryScope(kind="team", scope_id="crew")
    names = TableNames.of_team(scope)
    team_root = paths.storage_root / "teams" / names.storage_name
    create_state_storage(names.storage_name, team_root, scope)
    database = names.database_path(team_root)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(f'UPDATE "{names.log_meta}" SET version = 4')  # noqa: S608

    with capture_logs() as logs:
        await migrate_conversation_storage(paths)

    assert [entry for entry in logs if entry["event"] == "conversation_conversion_skipped_store"] == []
    assert _query(database, f'SELECT version, owner_kind, owner_id FROM "{names.log_meta}"') == [  # noqa: S608
        (LOG_VERSION, "team", "crew"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("linked", ["file", "directory"])
async def test_a_linked_session_file_is_left_alone(linked: str, tmp_path: Path) -> None:
    """The runtime refuses a session file or directory reached through a link, so the rewrite does not follow one either."""
    paths = _restore("v2026.10.18", tmp_path / "state")
    sessions = subagent_sessions_dir(paths)
    target = tmp_path / "elsewhere"
    sessions.rename(target)
    if linked == "directory":
        sessions.symlink_to(target)
    else:
        sessions.mkdir()
        for path in target.iterdir():
            (sessions / path.name).symlink_to(path)
    stored = {path.name: path.read_bytes() for path in target.iterdir()}

    await migrate_subagent_sessions(paths)

    assert {path.name: path.read_bytes() for path in target.iterdir()} == stored
    assert sessions.is_symlink() or all(path.is_symlink() for path in sessions.iterdir())


class _RewriteRanError(Exception):
    """Raised by the stand-in rewrite to stop startup right after it runs."""


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["api", "orchestrator"])
async def test_both_entry_points_rewrite_session_files_before_serving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    """Standalone API and orchestrator startup both rewrite the session files before anything reads them."""
    paths = _paths(tmp_path)
    module = importlib.import_module(f"mindroom.{'api.main' if entrypoint == 'api' else 'orchestrator'}")

    async def rewrite(runtime_paths: RuntimePaths) -> None:
        assert runtime_paths == paths
        raise _RewriteRanError

    monkeypatch.setattr(module, "migrate_subagent_sessions", rewrite)
    if entrypoint == "api":
        monkeypatch.setattr(module, "_app_runtime_paths", lambda _app: paths)
        with pytest.raises(_RewriteRanError):
            async with module._lifespan(module.app):
                pytest.fail("API admitted runtime work")
    else:
        with pytest.raises(_RewriteRanError):
            await module.main("ERROR", paths, api=False)


@pytest.mark.parametrize(
    ("field", "persona_field"),
    [("agent_mode", None), ("persona", None), ("requested_model_name", None), (None, "skills"), (None, "knowledge")],
)
def test_the_readers_take_only_the_current_shape(field: str | None, persona_field: str | None) -> None:
    """A child or persona snapshot without a field the current writer writes is refused instead of defaulted."""
    persona = SubagentPersona(source_kind="inline", source_name="", system_prompt="P", tools=None, skills=None)
    child = DelegationChild(
        delegation_id="d",
        parent_tool_call_id="call",
        caller_agent_name="leader",
        child_agent_name="helper",
        task="Review",
        session_id="s",
        run_id="r",
        model_name="default",
        requested_model_name="default",
        depth=1,
        execution_identity={},
        agent_mode="standard",
        persona=persona,
    )
    snapshot = asdict(child)
    assert DelegationChild.from_dict(snapshot) == child
    if field is not None:
        del snapshot[field]
    if persona_field is not None:
        del snapshot["persona"][persona_field]

    with pytest.raises((KeyError, TypeError)):
        DelegationChild.from_dict(snapshot)
