"""Tests for `mindroom agents rename`, run through the CLI against synthetic stopped installs.

The full install holds a store of every family the rename carries, written by the fixtures of the providers' own
tests, for the renamed agent ``code`` and for ``code_team``, whose name extends it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from mindroom.agent_modes import resolve_agent_mode
from mindroom.agent_rename import steps as steps_module
from mindroom.agent_rename import workers as worker_moves
from mindroom.agent_rename.guard import stopped_runtime
from mindroom.agent_rename.steps import BACKUPS_DIRECTORY_NAME, RenamePlan, apply_plan
from mindroom.cli import agent_rename_command
from mindroom.cli.agents import load_config_or_exit
from mindroom.cli.main import app
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import matrix_state_file, tracking_dir
from mindroom.event_journal import EventJournalStore, IngestionBatchAdmission
from mindroom.event_journal.opening import event_journal_sqlite_path
from mindroom.file_locks import release_file_lock, try_exclusive_file_lock
from mindroom.history.replay import scope_visible_runs
from mindroom.history.types import HistoryScope
from mindroom.knowledge.indexing_config import knowledge_git_dir
from mindroom.matrix.state import MatrixState
from mindroom.matrix.sync_continuity import SyncContinuityStore
from mindroom.session_storage.agent_storage import create_session_storage, get_agent_session
from mindroom.tool_system.worker_routing import agent_state_root_path, worker_dir_name
from mindroom.workers import runtime as workers_runtime
from mindroom.yaml_io import safe_load
from tests.agent_rename_fixtures import indexes as index_fixture
from tests.agent_rename_fixtures import teams as team_fixture
from tests.agent_rename_fixtures.entity_state import seed_agent
from tests.agent_rename_fixtures.journal import bound_principal, membership_admission, message_admission, record_turn
from tests.agent_rename_fixtures.memory import install_memories, memory_config, visible_memories
from tests.agent_rename_fixtures.side_stores import install_side_stores
from tests.agent_rename_fixtures.workers import (
    agent_worker_key,
    publish_credentials,
    read_credentials,
    write_docker_startup_manifest,
    write_worker,
)
from tests.agent_rename_helpers import tree_snapshot
from tests.conftest import TEST_ACCESS_TOKEN, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from click.testing import Result

    from mindroom.constants import RuntimePaths

OLD = "code"
NEW = "coder"
# Its name extends the renamed agent's, so a substring match would wrongly touch it.
PREFIX = "code_team"
HELPER = "helper"
DIARY = "diary"
# Shares the renamed agent's state root through the link agents/sidekick -> code.
SHARER = "sidekick"
OLD_USER_ID = "@mindroom_code:x"
OLD_PRINCIPAL = f"{OLD}@{OLD_USER_ID}"
NEW_PRINCIPAL = f"{NEW}@{OLD_USER_ID}"
PREFIX_PRINCIPAL = "code_team@@mindroom_code_team:x"
# Worker roots named like workers of an agent, without metadata naming their keys.
UNKNOWN_WORKERS = {name: f"v1_acme_shared_{name}-0123456789abcdef" for name in ("code", "code_team")}
NOW = datetime(2026, 10, 20, 9, 30, 0, tzinfo=UTC)
CONNECTIONS_AGENT = "MINDROOM_CONNECTIONS_AGENT"

# The renamed agent is in a configured team, another agent delegates to it, a knowledge base is checked out in its
# workspace, and another agent's instructions mention it in prose, which the rename leaves to the operator.
CONFIG = """\
models:
  default:
    provider: openai
    id: gpt-5
knowledge_bases:
  handbook:
    path: ${MINDROOM_STORAGE_PATH}/agents/code/workspace/docs
    git:
      repo_url: https://example.org/handbook.git
agents:
  code:  # renamed by the tests
    display_name: Code
  code_team:
    display_name: Code Team
  helper:
    display_name: Helper
    delegate_to: [code]
    instructions:
      - Ask code for reviews.
  diary:
    display_name: Diary
    private:
      per: user
teams:
  crew:
    display_name: Crew
    role: Ships releases
    agents: [code, helper]
  docs:
    display_name: Docs
    role: Writes docs
    agents: [code_team, helper]
"""
RENAMED_CONFIG = (
    CONFIG.replace("  code:  # renamed", "  coder:  # renamed")
    .replace("agents/code/workspace/docs", "agents/coder/workspace/docs")
    .replace("delegate_to: [code]", "delegate_to: [coder]")
    .replace("agents: [code, helper]", "agents: [coder, helper]")
)

runner = CliRunner()


def _environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Give the command a clean environment whose mem0 directory is inside ``tmp_path``."""
    for name in ("MINDROOM_WORKER_BACKEND", "MATRIX_SERVER_NAME", "MATRIX_SSL_VERIFY", "MINDROOM_CONFIG_TEMPLATE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MATRIX_HOMESERVER", "http://localhost:8008")
    monkeypatch.setenv("MINDROOM_NAMESPACE", "")
    monkeypatch.setenv("MEM0_DIR", str(tmp_path / "mem0"))


async def _write_journal(database: Path) -> None:
    """Sync a batch for the agent and the prefix agent, then record a turn of each and a team turn naming the agent."""
    store = EventJournalStore.open_sqlite(database)
    try:
        for principal_id in (OLD_PRINCIPAL, PREFIX_PRINCIPAL):
            stream = uuid4()
            principal = await bound_principal(store, principal_id, stream)
            batch = (membership_admission(None, "join", 0, 0), message_admission("$from-alice"))
            await principal.admit_ingestion_batch(IngestionBatchAdmission(stream, 1, batch))
        for agent in (OLD, PREFIX):
            await record_turn(store, agent, "$from-alice", HistoryScope(kind="agent", scope_id=agent))
        await record_turn(store, HELPER, "$team", HistoryScope(kind="team", scope_id=f"team_{OLD}+{HELPER}"))
    finally:
        await store.close()


def _save_accounts(runtime_paths: RuntimePaths) -> None:
    """Store the Matrix accounts MindRoom registered for each agent."""
    state = MatrixState()
    state.add_account(
        f"agent_{OLD}",
        "mindroom_code",
        "code-secret",
        domain="x",
        device_id="CODEDEVICE",
        access_token=TEST_ACCESS_TOKEN,
    )
    for agent in (PREFIX, HELPER, "router"):
        state.add_account(f"agent_{agent}", f"mindroom_{agent}", f"{agent}-secret", domain="x")
    state.save(runtime_paths)


def _finish_install(tmp_path: Path, runtime_paths: RuntimePaths) -> RuntimePaths:
    """Write the config and its .env, and leave the lock files a MindRoom that ran here leaves behind."""
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / ".env").write_text(f"{CONNECTIONS_AGENT}=code\n", encoding="utf-8")
    with stopped_runtime(runtime_paths):
        pass
    return runtime_paths


def _seed(runtime_paths: RuntimePaths, *, journal: bool) -> None:
    """Write the accounts, and for the agent and code_team their journal state, history, workspace and shared worker."""
    _save_accounts(runtime_paths)
    if journal:
        asyncio.run(_write_journal(event_journal_sqlite_path(runtime_paths.storage_root)))
    for agent in (OLD, PREFIX):
        seed_agent(runtime_paths, agent)
        workspace = agent_state_root_path(runtime_paths.storage_root, agent) / "workspace"
        workspace.mkdir(exist_ok=True)
        (workspace / "notes.md").write_text(f"{agent} notes\n", encoding="utf-8")
        write_worker(runtime_paths, agent_worker_key("shared", agent))


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, journal: bool = True) -> RuntimePaths:
    """Build a stopped install with the agent's account, journal state, session history and a shared worker."""
    _environment(monkeypatch, tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    _seed(runtime_paths, journal=journal)
    return _finish_install(tmp_path, runtime_paths)


def _full_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RuntimePaths:
    """Build a stopped install with a store of every family the rename carries, drops or reports."""
    _environment(monkeypatch, tmp_path)
    # Side stores: tracking records, automations, join fences, calls, avatars, workflows, todos, triggers, ...
    install_side_stores(tmp_path)
    # Memory in mem0's Chroma stores and memory files, of the agent, its configured team and an ad-hoc team.
    install_memories(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    # Team history: configured teams and ad-hoc team scopes with and without the agent.
    team_fixture.install_teams(runtime_paths)
    _seed(runtime_paths, journal=True)
    for agent in (OLD, PREFIX):
        # A requester's worker of the agent, and the OAuth credentials of the agent and of that requester's use of it.
        write_worker(runtime_paths, agent_worker_key("user_agent", agent))
        for scope in ("shared", "user_agent"):
            asyncio.run(publish_credentials(runtime_paths, scope, agent))
    # What the rename reports instead of changing: files of older versions, state from when the agent was private,
    # and plugin state named after it.
    index_fixture.legacy_files(runtime_paths, OLD)
    index_fixture.legacy_files(runtime_paths, PREFIX)
    index_fixture.private_state_root(OLD, index_fixture.private_config(), runtime_paths)
    for root in UNKNOWN_WORKERS.values():
        (runtime_paths.storage_root / "workers" / root / "workspace").mkdir(parents=True)
    # Derived state: the file-memory search index of the agent, and the Git directory of the checkout in its workspace.
    file_memory = index_fixture.index_config(memory_backend="file", memory_search={"mode": "semantic"})
    index_fixture.file_memory_index(OLD, file_memory, runtime_paths)
    index_fixture.knowledge_git("handbook", index_fixture.git_config({"handbook": OLD}), runtime_paths)
    plugin_state = runtime_paths.storage_root / "plugins" / "demo" / f"{OLD}.json"
    plugin_state.parent.mkdir(parents=True)
    plugin_state.write_text("{}", encoding="utf-8")
    # Another agent shares the agent's state root through a link, and its history lives there.
    (runtime_paths.storage_root / "agents" / SHARER).symlink_to(OLD)
    seed_agent(runtime_paths, SHARER)
    return _finish_install(tmp_path, runtime_paths)


def _invoke(
    runtime_paths: RuntimePaths,
    *args: str,
    old: str = OLD,
    new: str = NEW,
    prompt: str | None = None,
) -> Result:
    return runner.invoke(
        app,
        [
            "agents",
            "rename",
            old,
            new,
            "--config",
            str(runtime_paths.config_path),
            "--storage-path",
            str(runtime_paths.storage_root),
            *args,
        ],
        input=prompt,
    )


def _journal_rows(runtime_paths: RuntimePaths, sql: str, *params: object) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(event_journal_sqlite_path(runtime_paths.storage_root))) as connection:
        return connection.execute(sql, params).fetchall()


def _principals(runtime_paths: RuntimePaths) -> list[object]:
    return [row[0] for row in _journal_rows(runtime_paths, "SELECT DISTINCT principal_id FROM journal_events")]


def _steps(output: str) -> list[str]:
    """Return the plan's step lines from the command output, without its reports."""
    return [
        line.removeprefix("  - ")
        for line in output.splitlines()
        if line.startswith("  - ") and not line.startswith("  - report: ")
    ]


def _leftovers(output: str) -> tuple[list[str], list[str]]:
    """Return the leftover scan's lines reported as staying, and the lines nobody reported."""
    groups: dict[str, list[str]] = {"expected": [], "unexpected": []}
    current = None
    for line in output.splitlines():
        if line == "  Reported above as staying:":
            current = "expected"
        elif line == "  Not reported by the plan, or not scanned:":
            current = "unexpected"
        elif current is not None and line.startswith("    - "):
            groups[current].append(line.removeprefix("    - "))
    return groups["expected"], groups["unexpected"]


def test_dry_run_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without --apply the full plan is printed in order and nothing on disk changes, not even the lock files."""
    runtime_paths = _full_install(tmp_path, monkeypatch)
    before = tree_snapshot(tmp_path)

    result = _invoke(runtime_paths)

    assert result.exit_code == 0, result.output
    assert tree_snapshot(tmp_path) == before
    assert (
        f"Plan to rename agent {OLD} to {NEW}; it keeps its Matrix account {OLD_USER_ID} and device:" in result.output
    )
    assert "Dry run: nothing was changed. Rerun with --apply to make these changes." in result.output
    steps = _steps(result.output)
    config_path = runtime_paths.config_path
    # The journal goes first, then the state root, every other store, matrix_state.yaml, .env, and the config last.
    assert steps[0].startswith("Rewrite the event journal")
    assert steps[1].startswith(f"Move the state of agent {OLD} ")
    # The link into the state root follows it once it moved.
    sharer_link = runtime_paths.storage_root / "agents" / SHARER
    retarget = f"Point the symlink {sharer_link} at {NEW} instead of {OLD}, where the state of agent {OLD} moved"
    assert steps.index(retarget) > 1
    assert steps[-3].startswith(f"Rename the account agent_{OLD} in {matrix_state_file(runtime_paths)}")
    assert steps[-2] == f"Set {CONNECTIONS_AGENT} from {OLD} to {NEW} in {runtime_paths.env_path}"
    assert steps[-1] == f"Rename the agent {OLD} to {NEW} in {config_path}"
    assert "report: These files from older MindRoom versions name agent code" in result.output
    assert "report: These config lines mention code" in result.output
    unknown = runtime_paths.storage_root / "workers" / UNKNOWN_WORKERS[OLD]
    assert f"nothing records their worker keys, so they stay as they are: {unknown}." in result.output
    assert UNKNOWN_WORKERS[PREFIX] not in result.output
    assert any(step.startswith("Drop the file-memory search index") for step in steps)
    assert any(step.startswith("Move the Git directory of knowledge base handbook") for step in steps)


def test_apply_renames_every_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--apply carries every store to the new name, keeps a backup, and the scan finds only what the plan reported."""
    runtime_paths = _full_install(tmp_path, monkeypatch)
    storage = runtime_paths.storage_root
    state_before = safe_load(matrix_state_file(runtime_paths).read_bytes())

    result = _invoke(runtime_paths, "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert f"Renamed: agent {OLD} is now {NEW}." in result.output
    (backup_dir,) = (storage / BACKUPS_DIRECTORY_NAME).iterdir()
    assert backup_dir.name.endswith("-rename")
    assert (backup_dir / "manifest.json").is_file()
    assert str(backup_dir) in result.output

    # Config and .env, with comments and the prefix agent left as they are.
    assert runtime_paths.config_path.read_text(encoding="utf-8") == RENAMED_CONFIG
    assert runtime_paths.env_path.read_text(encoding="utf-8") == f"{CONNECTIONS_AGENT}={NEW}\n"
    # The Matrix account keeps its username, device and token under the new key.
    accounts = safe_load(matrix_state_file(runtime_paths).read_bytes())["accounts"]
    assert accounts[f"agent_{NEW}"] == state_before["accounts"][f"agent_{OLD}"]
    assert f"agent_{OLD}" not in accounts
    # The journal principal and turn records.
    assert _principals(runtime_paths) == sorted([NEW_PRINCIPAL, PREFIX_PRINCIPAL])
    assert _journal_rows(runtime_paths, "SELECT principal_id FROM matrix_sync_consumers ORDER BY 1") == sorted(
        [(NEW_PRINCIPAL,), (PREFIX_PRINCIPAL,)],
    )
    assert _journal_rows(runtime_paths, "SELECT agent_name FROM turn_records ORDER BY 1") == sorted(
        [(NEW,), (PREFIX,), (HELPER,)],
    )
    # History, memory and team history load under the new name through MindRoom's own code.
    storage_db = create_session_storage(NEW, team_fixture.team_config(NEW), runtime_paths, execution_identity=None)
    try:
        session = get_agent_session(storage_db, "s1")
        assert session is not None
        assert [run.run_id for run in scope_visible_runs(session, HistoryScope(kind="agent", scope_id=NEW))] == ["r2"]
    finally:
        storage_db.close()
    memories = visible_memories(NEW, memory_config(NEW), runtime_paths)
    assert memories[f"agent_{NEW}", "code prefers tabs"] == 1
    for team in (team_fixture.AD_HOC, team_fixture.CONFIGURED):
        assert team_fixture.team_history(runtime_paths, team_fixture.team_config(NEW), team.renamed()).replayed == [
            "r2",
        ]
    # Side stores, workers and credentials.
    new_root = agent_state_root_path(storage, NEW)
    assert (new_root / "workspace" / "notes.md").read_text(encoding="utf-8") == "code notes\n"
    assert resolve_agent_mode(runtime_paths, new_root, NEW, "s1") == "minimal"
    assert SyncContinuityStore(storage, NEW).load().pending_join_decrypt_fences == {"!code-fence:x"}
    for scope in ("shared", "user_agent"):
        assert not (storage / "workers" / worker_dir_name(agent_worker_key(scope, OLD))).exists()
        assert (
            storage / "workers" / worker_dir_name(agent_worker_key(scope, NEW)) / "workspace" / "notes.md"
        ).is_file()
        assert asyncio.run(read_credentials(runtime_paths, scope, NEW)) == {"token": f"{scope}-{OLD}"}

    assert not list((storage / "knowledge_db").iterdir())
    assert knowledge_git_dir(storage, new_root / "workspace" / "docs").is_dir()
    _assert_sharer_keeps_its_history(runtime_paths)

    expected, unexpected = _leftovers(result.output)
    assert f"Leftover scan: places in {storage} that still name agent {OLD}" in result.output
    assert unexpected == []
    assert "plugins/demo/code.json" in expected
    assert any(line.startswith("private_instances/") for line in expected)
    assert f"tracking/{OLD}_responded.json.imported" in expected
    assert f"workers/{UNKNOWN_WORKERS[OLD]}" in expected


def _assert_sharer_keeps_its_history(runtime_paths: RuntimePaths) -> None:
    """The agent sharing the state root through a link still finds its history, now in the moved root."""
    storage = runtime_paths.storage_root
    sharer_root = agent_state_root_path(storage, SHARER)
    sessions = agent_state_root_path(storage, NEW) / "sessions" / f"{SHARER}.db"
    assert sharer_root.readlink().as_posix() == NEW
    assert sharer_root.resolve() / "sessions" / f"{SHARER}.db" == sessions
    sharer_config = Config(agents={SHARER: AgentConfig(display_name="Flash")})
    sharer_db = create_session_storage(SHARER, sharer_config, runtime_paths, execution_identity=None)
    try:
        assert getattr(sharer_db, "db_file", None) == str(sessions)
        sharer_session = get_agent_session(sharer_db, "s1")
        assert sharer_session is not None
        sharer_scope = HistoryScope(kind="agent", scope_id=SHARER)
        assert [run.run_id for run in scope_visible_runs(sharer_session, sharer_scope)] == ["r2"]
    finally:
        sharer_db.close()
    # Resolving its paths and opening its store went through the retargeted link, so no old state root came back.
    assert not os.path.lexists(storage / "agents" / OLD)


def test_prefix_agent_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Renaming code leaves every store of code_team as it was, in files, databases and the journal.

    Memory stores and memory files of team scopes listing both agents change on purpose, so they are left out.
    """
    runtime_paths = _full_install(tmp_path, monkeypatch)
    storage = runtime_paths.storage_root

    def prefix_stores() -> dict[str, bytes | str]:
        return {
            path: value
            for path, value in tree_snapshot(storage).items()
            if PREFIX in path and "chroma" not in path.split("/") and "memory_files" not in path.split("/")
        }

    prefix_journal = "SELECT * FROM journal_events WHERE principal_id = ? ORDER BY event_id"
    before, journal_before = prefix_stores(), _journal_rows(runtime_paths, prefix_journal, PREFIX_PRINCIPAL)
    assert before

    result = _invoke(runtime_paths, "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert prefix_stores() == before
    assert _journal_rows(runtime_paths, prefix_journal, PREFIX_PRINCIPAL) == journal_before
    assert f"agent_{PREFIX}" in safe_load(matrix_state_file(runtime_paths).read_bytes())["accounts"]


def test_leftovers_inside_a_moved_store_are_not_taken_for_staying_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A report that names a moved store does not make what still names the agent inside it expected."""
    runtime_paths = _install(tmp_path, monkeypatch)
    calls = runtime_paths.storage_root / "calls" / OLD
    calls.mkdir(parents=True)
    (calls / "notes.json").write_text(json.dumps({"agent": OLD}), encoding="utf-8")

    result = _invoke(runtime_paths, "--apply", "--yes")

    assert result.exit_code == 0, result.output
    # The report names both the old and the moved transcripts directory.
    assert f"the transcripts move to {runtime_paths.storage_root / 'calls' / NEW}." in result.output
    assert _leftovers(result.output) == ([], [f"calls/{NEW}/notes.json"])


def test_files_the_scan_cannot_parse_are_listed_without_failing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JSON file that holds the name but does not parse is listed, and the rename still succeeds.

    Data files in the moved workspace whose text never holds the name are not parsed, so they add no line.
    """
    runtime_paths = _install(tmp_path, monkeypatch)
    (tracking_dir(runtime_paths) / "broken.json").write_text(f'{{"agent": "{OLD}"', encoding="utf-8")
    workspace = agent_state_root_path(runtime_paths.storage_root, OLD) / "workspace"
    (workspace / "tsconfig.json").write_text('{\n  // compiler options\n  "strict": true,\n}\n', encoding="utf-8")
    (workspace / "deploy.yaml").write_text("{{- if .Values.enabled }}\nkind: x\n", encoding="utf-8")

    result = _invoke(runtime_paths, "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert _leftovers(result.output) == (
        [],
        ["tracking/broken.json: not scanned for names, it is not valid JSON or YAML"],
    )


@pytest.mark.parametrize(
    ("old", "new", "reason"),
    [
        ("ghost", NEW, "ghost is not a configured agent in"),
        ("crew", "squad", "crew is a team; renaming teams is not supported yet."),
        (
            DIARY,
            "journal",
            "agent diary is private (agents.diary.private is set); renaming private agents is not supported yet.",
        ),
        ("router", "dispatcher", "router is MindRoom's router, which cannot be renamed."),
        (OLD, OLD, "code is the agent's name already."),
        (OLD, PREFIX, "code_team is already a configured agent."),
        (OLD, "crew", "crew is already a configured team."),
        (OLD, "code-x", "code-x is not a valid agent name"),
        (OLD, "user", "user is reserved for MindRoom"),
        (OLD, "_shared", "_shared is reserved for MindRoom"),
        (OLD, "router", "router is reserved for MindRoom"),
        (OLD, "code_", "code_ is written code in worker keys, like agent code"),
        (OLD, "_code", "_code is written code in worker keys, like agent code"),
        (OLD, "helper_", "helper_ is written helper in worker keys, like agent helper"),
        (OLD, "Helper", "Helper has the id helper, like agent helper"),
        (OLD, "codeTeam", "codeTeam has the id code-team, like agent code_team"),
        (OLD, "router_x", "already holds an account agent_router_x"),
    ],
)
def test_refusals_change_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    old: str,
    new: str,
    reason: str,
) -> None:
    """Renames the command cannot do refuse with the reason and change nothing, with or without --apply."""
    runtime_paths = _install(tmp_path, monkeypatch)
    state = MatrixState.model_validate(safe_load(matrix_state_file(runtime_paths).read_bytes()))
    # An account left by an agent that is no longer configured.
    state.add_account("agent_router_x", "mindroom_router_x", "secret", domain="x")
    state.save(runtime_paths)
    before = tree_snapshot(tmp_path)

    for args in ((), ("--apply", "--yes")):
        result = _invoke(runtime_paths, *args, old=old, new=new)

        assert result.exit_code == 1, result.output
        assert reason in result.output
    assert tree_snapshot(tmp_path) == before


def test_kubernetes_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kubernetes workers keep their state where the rename cannot reach it yet."""
    runtime_paths = _install(tmp_path, monkeypatch)
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    before = tree_snapshot(tmp_path)

    result = _invoke(runtime_paths, "--apply", "--yes")

    assert result.exit_code == 1, result.output
    assert "The worker backend 'kubernetes' is not supported yet" in result.output
    assert tree_snapshot(tmp_path) == before


def test_refuses_while_mindroom_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A running MindRoom holds runtime.lock, so --apply refuses before any write."""
    runtime_paths = _install(tmp_path, monkeypatch)
    lock_path = tracking_dir(runtime_paths) / "runtime.lock"
    lock = try_exclusive_file_lock(lock_path)
    assert lock is not None
    before = tree_snapshot(tmp_path)
    try:
        result = _invoke(runtime_paths, "--apply", "--yes")
    finally:
        release_file_lock(lock)

    assert result.exit_code == 1, result.output
    assert f"Stop MindRoom first: another process holds {lock_path}." in result.output
    assert tree_snapshot(tmp_path) == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes files whatever their mode")
@pytest.mark.parametrize("locked", ["config", "env", "matrix_state", "config directory"])
def test_files_it_cannot_write_refuse_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    locked: str,
) -> None:
    """A config, .env or matrix_state.yaml this user cannot write, or whose directory it cannot write, refuses early.

    They are edited after every store moved, so the dry run refuses, and --apply refuses before it writes anything.
    """
    runtime_paths = _install(tmp_path, monkeypatch)
    path = {
        "config": runtime_paths.config_path,
        "env": runtime_paths.env_path,
        "matrix_state": matrix_state_file(runtime_paths),
        "config directory": runtime_paths.config_path.parent,
    }[locked]
    mode = path.stat().st_mode
    path.chmod(0o555 if path.is_dir() else 0o444)
    try:
        before = tree_snapshot(tmp_path)
        for args in ((), ("--apply", "--yes")):
            result = _invoke(runtime_paths, *args)

            assert result.exit_code == 1, result.output
            assert f"Cannot write {'in ' if path.is_dir() else ''}{path}" in " ".join(result.output.split())
        assert tree_snapshot(tmp_path) == before
    finally:
        path.chmod(mode)


def test_declining_the_confirmation_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without --yes the operator is asked, and answering no exits 1 with nothing changed."""
    runtime_paths = _install(tmp_path, monkeypatch)
    before = tree_snapshot(tmp_path)

    result = _invoke(runtime_paths, "--apply", prompt="n\n")

    assert result.exit_code == 1, result.output
    assert "Apply this plan?" in result.output
    assert tree_snapshot(tmp_path) == before


def test_rerun_after_success_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Once the config names the new agent, running again refuses because the old name is not configured."""
    runtime_paths = _install(tmp_path, monkeypatch)
    assert _invoke(runtime_paths, "--apply", "--yes").exit_code == 0
    after_apply = tree_snapshot(tmp_path)

    for args in ((), ("--apply", "--yes")):
        result = _invoke(runtime_paths, *args)

        assert result.exit_code == 1, result.output
        assert f"{OLD} is not a configured agent in {runtime_paths.config_path}." in result.output
    assert tree_snapshot(tmp_path) == after_apply


def _rename_plan(runtime_paths: RuntimePaths, old: str, new: str) -> RenamePlan:
    """Plan renaming ``old`` to ``new`` the way the command does, for an agent whose account is mindroom_<old>."""
    rename = agent_rename_command._AgentRename(old, new, f"@mindroom_{old}:x", has_account=True)
    return agent_rename_command._rename_plan(runtime_paths, load_config_or_exit(runtime_paths), rename)


def _write_manifest(plan: RenamePlan, when: str) -> Path:
    """Write the manifest a rename run started at ``when`` writes before its first step, listing every step."""
    backup_dir = plan.storage_root / BACKUPS_DIRECTORY_NAME / f"{when}-rename"
    backup_dir.mkdir(parents=True)
    steps_module._write_manifest(plan, kind="rename", now=NOW, backup_dir=backup_dir)
    return backup_dir / "manifest.json"


def _killed_run(runtime_paths: RuntimePaths, last_step: str) -> Path:
    """Leave the install as a run killed right after the step starting with ``last_step``; return its manifest.

    Like a real run, the manifest lists every planned step, not only those that ran.
    """
    plan = _rename_plan(runtime_paths, OLD, NEW)
    end = next(index for index, step in enumerate(plan.steps) if step.description.startswith(last_step)) + 1
    killed = RenamePlan(storage_root=plan.storage_root, steps=plan.steps[:end], reports=())
    backup_dir = apply_plan(killed, kind="rename", now=NOW).backup_dir
    steps_module._write_manifest(plan, kind="rename", now=NOW, backup_dir=backup_dir)
    return backup_dir / "manifest.json"


@pytest.mark.parametrize(
    ("last_step", "existing"),
    [
        ("Rewrite the event journal", lambda _paths: f"rows for {NEW_PRINCIPAL}"),
        (f"Move the state of agent {OLD}", lambda paths: f"{agent_state_root_path(paths.storage_root, NEW)} exists"),
        (f"Rename the account agent_{OLD}", lambda paths: f"{matrix_state_file(paths)} holds the account agent_{NEW}"),
    ],
    ids=["journal", "state root", "matrix_state"],
)
def test_rerun_after_a_killed_run_points_at_the_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    last_step: str,
    existing: Callable[[RuntimePaths], str],
) -> None:
    """A run killed before the config edit leaves OLD configured with state under NEW; a rerun names both and stops.

    The newest manifest of this rename is named, not an older one, and nothing is changed or deleted.
    """
    runtime_paths = _install(tmp_path, monkeypatch)
    older = _write_manifest(_rename_plan(runtime_paths, OLD, NEW), "20200101T000000Z")
    manifest = _killed_run(runtime_paths, last_step)
    before = tree_snapshot(tmp_path)

    for args in ((), ("--apply", "--yes")):
        result = _invoke(runtime_paths, *args)

        assert result.exit_code == 1, result.output
        assert f"Agent {OLD} is still configured" in result.output
        assert existing(runtime_paths) in result.output
        assert str(manifest) in result.output
        assert str(older) not in result.output
    assert tree_snapshot(tmp_path) == before


def test_an_unrelated_earlier_rename_is_not_taken_for_a_killed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished rename of another agent leaves a manifest; a stale account of the new name refuses as itself.

    Restoring that manifest would undo the other rename, so the refusal must not point at it.
    """
    runtime_paths = _install(tmp_path, monkeypatch)
    assert _invoke(runtime_paths, "--apply", "--yes", old=HELPER, new="assistant").exit_code == 0
    (other_backup,) = (runtime_paths.storage_root / BACKUPS_DIRECTORY_NAME).iterdir()
    state = MatrixState.model_validate(safe_load(matrix_state_file(runtime_paths).read_bytes()))
    state.add_account("agent_bob", "mindroom_bob", "secret", domain="x")
    state.save(runtime_paths)
    before = tree_snapshot(tmp_path)

    for args in ((), ("--apply", "--yes")):
        result = _invoke(runtime_paths, *args, new="bob")

        assert result.exit_code == 1, result.output
        assert "already holds an account agent_bob, left by an agent named bob" in result.output
        assert "still configured" not in result.output
        assert str(other_backup) not in result.output
    assert tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("manifest", [None, "other rename", "this rename"])
def test_existing_destination_refuses_and_points_at_a_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest: str | None,
) -> None:
    """A destination already under the new name refuses; only a manifest of this rename is pointed at."""
    runtime_paths = _install(tmp_path, monkeypatch)
    written = None
    if manifest is not None:
        old, new = (OLD, NEW) if manifest == "this rename" else (HELPER, "assistant")
        written = _write_manifest(_rename_plan(runtime_paths, old, new), "20261019T000000Z")
    taken = runtime_paths.storage_root / "workers" / worker_dir_name(agent_worker_key("shared", NEW))
    taken.mkdir()
    before = tree_snapshot(tmp_path)

    for args in ((), ("--apply", "--yes")):
        result = _invoke(runtime_paths, *args)

        assert result.exit_code == 1, result.output
        assert f"{taken} already exists." in result.output
        assert ("still configured" in result.output) is (manifest == "this rename")
        if written is not None:
            assert (str(written) in result.output) is (manifest == "this rename")
    assert tree_snapshot(tmp_path) == before


async def _write_turns_only(database: Path) -> None:
    """Record a turn of the agent in a journal that holds no principal of it."""
    store = EventJournalStore.open_sqlite(database)
    try:
        await record_turn(store, OLD, "$from-alice", HistoryScope(kind="agent", scope_id=OLD))
    finally:
        await store.close()


def test_agent_without_a_stored_account_keeps_matrix_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent with no stored account has none to move; its key, stores and journal turns still take the new name."""
    runtime_paths = _install(tmp_path, monkeypatch, journal=False)
    asyncio.run(_write_turns_only(event_journal_sqlite_path(runtime_paths.storage_root)))
    state = MatrixState.model_validate(safe_load(matrix_state_file(runtime_paths).read_bytes()))
    del state.accounts[f"agent_{OLD}"]
    state.save(runtime_paths)
    matrix_state = matrix_state_file(runtime_paths).read_bytes()

    result = _invoke(runtime_paths, "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert f"Plan to rename agent {OLD} to {NEW}; it has no stored Matrix account yet:" in result.output
    assert matrix_state_file(runtime_paths).read_bytes() == matrix_state
    assert runtime_paths.config_path.read_text(encoding="utf-8") == RENAMED_CONFIG
    assert (agent_state_root_path(runtime_paths.storage_root, NEW) / "workspace" / "notes.md").is_file()
    assert _journal_rows(runtime_paths, "SELECT agent_name FROM turn_records") == [(NEW,)]


def test_journal_rows_without_a_stored_account_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Journal rows under the agent's principal need its account, which the rename keeps; without it, it refuses."""
    runtime_paths = _install(tmp_path, monkeypatch)
    state = MatrixState.model_validate(safe_load(matrix_state_file(runtime_paths).read_bytes()))
    del state.accounts[f"agent_{OLD}"]
    state.save(runtime_paths)
    before = tree_snapshot(tmp_path)

    for args in ((), ("--apply", "--yes")):
        result = _invoke(runtime_paths, *args)

        assert result.exit_code == 1, result.output
        assert f"holds no account agent_{OLD}" in result.output
        assert OLD_PRINCIPAL in result.output
    assert tree_snapshot(tmp_path) == before


class _RetiringManager:
    backend_name = "docker"

    def __init__(self) -> None:
        self.retired: list[str] = []

    def __enter__(self) -> _RetiringManager:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def retire_worker(self, worker_key: str) -> None:
        self.retired.append(worker_key)


def test_apply_retires_old_docker_workers_then_shuts_the_worker_manager_down(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Docker the old worker's container is retired after every move, and the leased manager is shut down.

    The moved root's startup manifest names the old worker key; it is dropped, so the leftover scan stays clean.
    """
    runtime_paths = _install(tmp_path, monkeypatch)
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "docker")
    write_docker_startup_manifest(runtime_paths, agent_worker_key("shared", OLD))
    manager = _RetiringManager()
    events: list[str] = []

    def lease(_runtime_paths: RuntimePaths, *, runtime_config: Config | None) -> _RetiringManager:
        assert runtime_config is not None
        events.append("lease")
        return manager

    monkeypatch.setattr(worker_moves, "lease_configured_primary_worker_manager", lease)
    monkeypatch.setattr(workers_runtime, "shutdown_primary_worker_manager", lambda: events.append("shutdown"))

    result = _invoke(runtime_paths, "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert _steps(result.output)[-1].startswith("Retire the old Docker worker")
    assert manager.retired == [agent_worker_key("shared", OLD)]
    assert events == ["lease", "shutdown"]
    assert _leftovers(result.output)[1] == []
