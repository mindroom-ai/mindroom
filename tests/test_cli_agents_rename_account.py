"""Tests for `mindroom agents rename-account`, run through the CLI against a synthetic stopped install."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from functools import partial
from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

from mindroom.agent_rename import account_files
from mindroom.agent_rename import steps as steps_module
from mindroom.agent_rename.guard import stopped_runtime
from mindroom.agent_rename.steps import RenameUndoError
from mindroom.cli import agents as cli_agents
from mindroom.cli.config import activate_cli_runtime
from mindroom.cli.main import app
from mindroom.constants import matrix_state_file, tracking_dir
from mindroom.credentials.manager import PRIMARY_RUNTIME_SCOPED_CREDENTIALS_DIRNAME, scoped_credentials_dir_part
from mindroom.event_journal import EventJournalStore, EventKind, encode_thread_id
from mindroom.event_journal.opening import event_journal_sqlite_path
from mindroom.file_locks import release_file_lock, try_exclusive_file_lock
from mindroom.matrix.client_session import olm_store_dir
from mindroom.matrix.state import MatrixState
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, WorkerScope, resolve_worker_key, worker_dir_name
from mindroom.workers import runtime as workers_runtime
from mindroom.yaml_io import safe_dump, safe_load
from tests.agent_rename_helpers import tree_snapshot
from tests.conftest import TEST_ACCESS_TOKEN

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_rename.steps import RenamePlan
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

AGENT = "nova"
OLD = "@old:x"
NEW = "@new:x"
OLD_PRINCIPAL = "nova@@old:x"
NEW_PRINCIPAL = "nova@@new:x"
ROUTER = "router@@router:x"
PASSWORD = "stored-secret"  # noqa: S105
CONFIG = "agents:\n  nova:\n    display_name: Nova\nbot_accounts:\n  - '@old:x'   # nova himself\n"

runner = CliRunner()


class _MatrixServer:
    """A fake homeserver for the login check that records every request."""

    def __init__(self, login_status: int = 200) -> None:
        self.login_status = login_status
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/_matrix/client/v3/login":
            if self.login_status != 200:
                return httpx.Response(self.login_status, json={"errcode": "M_FORBIDDEN", "error": "Invalid password"})
            return httpx.Response(200, json={"user_id": NEW, "access_token": "check-token", "device_id": "CHECK"})
        return httpx.Response(200, json={})


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> _MatrixServer:
    """Answer the login check from a fake homeserver instead of the network."""
    fake = _MatrixServer()
    monkeypatch.setattr(httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(fake.handle)))
    return fake


def _identity(requester_id: str) -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id=requester_id,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )


def _key(scope: WorkerScope, requester_id: str) -> str:
    key = resolve_worker_key(scope, _identity(requester_id), agent_name="code")
    assert key is not None
    return key


def _insert(connection: sqlite3.Connection, table: str, **values: object) -> None:
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values()))  # noqa: S608


def _event(connection: sqlite3.Connection, principal: str, *, sender: str) -> None:
    _insert(
        connection,
        "journal_events",
        principal_id=principal,
        event_id="$from-nova",
        room_id="!room:x",
        thread_id=encode_thread_id(None),
        kind=EventKind.MESSAGE.value,
        sender=sender,
        origin_server_ts=10,
        source_json=json.dumps({"sender": sender}),
        membership_epoch=0,
        state="settled",
    )


def _journal(storage_root: Path, *, new_principal_rows: bool = False) -> Path:
    """Build a journal with the real schema: nova's event seen by nova and the router, and nova's sync stream."""
    database = event_journal_sqlite_path(storage_root)
    asyncio.run(EventJournalStore.open_sqlite(database).close())
    with closing(sqlite3.connect(database)) as connection, connection:
        _event(connection, OLD_PRINCIPAL, sender=OLD)
        _event(connection, ROUTER, sender=OLD)
        _sync_stream(connection, OLD_PRINCIPAL, "1", next_sequence=5, membership_epoch=1)
        if new_principal_rows:
            # MindRoom ran after the server rename and bound a sync stream under the new principal.
            _sync_stream(connection, NEW_PRINCIPAL, "2", next_sequence=1, membership_epoch=0)
    return database


def _sync_stream(
    connection: sqlite3.Connection,
    principal: str,
    name: str,
    *,
    next_sequence: int,
    membership_epoch: int,
) -> None:
    """Write a principal's sync consumer and the producer position its stream reached in the room."""
    _insert(
        connection,
        "matrix_sync_consumers",
        principal_id=principal,
        consumer_generation=f"generation-{name}",
        stream_id=f"stream-{name}",
        next_sequence=next_sequence,
    )
    _insert(
        connection,
        "matrix_ingestion_membership",
        principal_id=principal,
        room_id="!room:x",
        membership="join",
        membership_epoch=membership_epoch,
    )


def _oauth_store(path: Path, worker_key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE oauth_credential_state "
            "(singleton INTEGER PRIMARY KEY, provider_id TEXT, worker_key TEXT, credential_present INTEGER)",
        )
        connection.execute("INSERT INTO oauth_credential_state VALUES (1, 'demo', ?, 1)", (worker_key,))


def _stored_worker_key(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT worker_key FROM oauth_credential_state").fetchone()[0]


def _oauth_path(storage_root: Path, requester_id: str) -> Path:
    oauth_root = storage_root / PRIMARY_RUNTIME_SCOPED_CREDENTIALS_DIRNAME
    return (
        oauth_root
        / scoped_credentials_dir_part(requester_id)
        / scoped_credentials_dir_part("code")
        / "demo_oauth_credentials.sqlite3"
    )


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, new_principal_rows: bool = False) -> RuntimePaths:
    """Build a stopped install whose server already renamed nova from @old:x to @new:x."""
    for name in ("MINDROOM_WORKER_BACKEND", "MATRIX_SERVER_NAME", "MATRIX_SSL_VERIFY", "MINDROOM_CONFIG_TEMPLATE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MATRIX_HOMESERVER", "http://localhost:8008")
    monkeypatch.setenv("MINDROOM_NAMESPACE", "")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    runtime_paths = activate_cli_runtime(config_path, storage_path=tmp_path / "mindroom_data")
    storage = runtime_paths.storage_root

    state = MatrixState()
    state.add_account(
        f"agent_{AGENT}",
        "old",
        PASSWORD,
        domain="x",
        device_id="OLDDEVICE",
        access_token=TEST_ACCESS_TOKEN,
    )
    state.add_account("agent_router", "router", "router-secret", domain="x")
    state.save(runtime_paths)

    _journal(storage, new_principal_rows=new_principal_rows)
    _oauth_store(_oauth_path(storage, OLD), _key("user_agent", OLD))
    worker = storage / "workers" / worker_dir_name(_key("user", OLD))
    (worker / "metadata").mkdir(parents=True)
    (worker / "metadata" / "worker.json").write_text(json.dumps({"worker_key": _key("user", OLD)}), encoding="utf-8")
    (worker / "workspace").mkdir()
    (worker / "workspace" / "notes.md").write_text("notes", encoding="utf-8")
    # A MindRoom that ran here left its lock files behind.
    with stopped_runtime(runtime_paths):
        pass
    return runtime_paths


def _invoke(runtime_paths: RuntimePaths, *args: str, new_user_id: str = NEW, agent: str = AGENT) -> object:
    return runner.invoke(
        app,
        [
            "agents",
            "rename-account",
            agent,
            new_user_id,
            "--config",
            str(runtime_paths.config_path),
            "--storage-path",
            str(runtime_paths.storage_root),
            *args,
        ],
    )


def _journal_rows(runtime_paths: RuntimePaths, sql: str) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(event_journal_sqlite_path(runtime_paths.storage_root))) as connection:
        return connection.execute(sql).fetchall()


def test_dry_run_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: _MatrixServer) -> None:
    """Without --apply the plan is printed and nothing on disk changes, not even the lock files."""
    runtime_paths = _install(tmp_path, monkeypatch)
    before = tree_snapshot(tmp_path)

    result = _invoke(runtime_paths)

    assert result.exit_code == 0, result.output
    assert tree_snapshot(tmp_path) == before
    # Rich would print the ":x:" of "@old:x:" as an emoji.
    assert f"Plan to adopt {NEW} for agent {AGENT}, whose stored account is {OLD}:" in result.output
    assert f"Set the username of agent_{AGENT}" in result.output
    assert "Rewrite the event journal" in result.output
    assert f"Replace {OLD} with {NEW} in {runtime_paths.config_path}" in result.output
    assert "Dry run: nothing was changed." in result.output
    assert "only show up when --apply rewrites the journal" in result.output
    # The login check belongs to --apply.
    assert server.requests == []


def test_apply_adopts_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: _MatrixServer) -> None:
    """--apply checks the login, moves every store to the new ID, keeps a backup, and finds no leftovers."""
    runtime_paths = _install(tmp_path, monkeypatch)
    storage = runtime_paths.storage_root

    result = _invoke(runtime_paths, "--apply", "--yes")

    assert result.exit_code == 0, result.output
    login, logout = server.requests
    assert login.url == "http://localhost:8008/_matrix/client/v3/login"
    assert json.loads(login.content) == {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": NEW},
        "password": PASSWORD,
        "initial_device_display_name": "mindroom rename check",
    }
    assert logout.url == "http://localhost:8008/_matrix/client/v3/logout"
    assert logout.headers["Authorization"] == "Bearer check-token"
    assert PASSWORD not in result.output

    account = safe_load(matrix_state_file(runtime_paths).read_bytes())["accounts"][f"agent_{AGENT}"]
    assert (account["username"], account["device_id"], account["access_token"]) == ("new", None, None)
    assert _journal_rows(runtime_paths, "SELECT principal_id, sender FROM journal_events ORDER BY principal_id") == [
        (NEW_PRINCIPAL, NEW),
        (ROUTER, NEW),
    ]
    assert _journal_rows(runtime_paths, "SELECT * FROM matrix_sync_consumers") == []
    assert _journal_rows(runtime_paths, "SELECT * FROM matrix_ingestion_membership") == []
    assert not _oauth_path(storage, OLD).exists()
    assert _stored_worker_key(_oauth_path(storage, NEW)) == _key("user_agent", NEW)
    assert not (storage / "workers" / worker_dir_name(_key("user", OLD))).exists()
    new_worker = storage / "workers" / worker_dir_name(_key("user", NEW))
    assert (new_worker / "workspace" / "notes.md").read_text() == "notes"
    assert runtime_paths.config_path.read_text() == CONFIG.replace(OLD, NEW)

    (backup_dir,) = (storage / "agent-rename-backups").iterdir()
    assert backup_dir.name.endswith("-rename-account")
    assert (backup_dir / "manifest.json").is_file()
    assert str(backup_dir) in result.output
    assert f"Leftover scan: nothing in {storage} names {OLD} any more." in result.output


def test_matrix_state_is_the_last_reversible_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _MatrixServer,
) -> None:
    """The new username marks a finished adoption, so it is written after every other reversible step.

    A run killed before that step leaves the old username, so the rerun does not report the account adopted.
    Final actions, which are never undone, still come after it.
    """
    runtime_paths = _install(tmp_path, monkeypatch)
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "docker")

    result = _invoke(runtime_paths)

    assert result.exit_code == 0, result.output
    steps = [
        line.removeprefix("  - ")
        for line in result.output.splitlines()
        if line.startswith("  - ") and not line.startswith("  - report: ")
    ]
    assert steps[0].startswith("Rewrite the event journal")
    assert steps[-3] == f"Replace {OLD} with {NEW} in {runtime_paths.config_path}"
    assert steps[-2].startswith(f"Set the username of agent_{AGENT}")
    assert steps[-1].startswith("Retire the old Docker worker")
    assert server.requests == []


def test_refuses_while_runtime_lock_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _MatrixServer,
) -> None:
    """A running MindRoom holds runtime.lock, so --apply refuses before the login check or any write."""
    runtime_paths = _install(tmp_path, monkeypatch)
    runtime_lock_path = tracking_dir(runtime_paths) / "runtime.lock"
    runtime_lock = try_exclusive_file_lock(runtime_lock_path)
    assert runtime_lock is not None
    before = tree_snapshot(tmp_path)
    try:
        result = _invoke(runtime_paths, "--apply", "--yes")
    finally:
        release_file_lock(runtime_lock)

    assert result.exit_code == 1, result.output
    assert f"Stop MindRoom first: another process holds {runtime_lock_path}." in result.output
    assert tree_snapshot(tmp_path) == before
    assert server.requests == []


def test_refuses_when_login_check_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _MatrixServer,
) -> None:
    """A server that still rejects the new ID means it was not renamed there, so nothing changes."""
    runtime_paths = _install(tmp_path, monkeypatch)
    server.login_status = 403
    before = tree_snapshot(tmp_path)

    result = _invoke(runtime_paths, "--apply", "--yes")

    assert result.exit_code == 1, result.output
    assert (
        f"The server does not accept {NEW} with the stored password; rename the account on the server first."
        in result.output
    )
    assert tree_snapshot(tmp_path) == before
    assert [request.url.path for request in server.requests] == ["/_matrix/client/v3/login"]


@pytest.mark.parametrize(
    ("agent", "new_user_id", "reason"),
    [
        ("ghost", NEW, "ghost is not an agent in"),
        (AGENT, "@new:y", f"@new:y is not on x, the server of {OLD}"),
        (AGENT, "new", "new is not a Matrix user ID"),
        (AGENT, "@router:x", "@router:x is already the stored account agent_router"),
    ],
)
def test_refuses_unknown_agent_and_foreign_domain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _MatrixServer,
    agent: str,
    new_user_id: str,
    reason: str,
) -> None:
    """Requests the command cannot honor refuse with the reason and change nothing, with or without --apply."""
    runtime_paths = _install(tmp_path, monkeypatch)
    before = tree_snapshot(tmp_path)

    for args in ((), ("--apply", "--yes")):
        result = _invoke(runtime_paths, *args, agent=agent, new_user_id=new_user_id)

        assert result.exit_code == 1, result.output
        assert reason in result.output
    assert tree_snapshot(tmp_path) == before
    assert server.requests == []


def test_rerun_after_apply_is_a_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: _MatrixServer) -> None:
    """Once the stored account holds the new ID, running again says so and changes nothing."""
    runtime_paths = _install(tmp_path, monkeypatch)
    assert _invoke(runtime_paths, "--apply", "--yes").exit_code == 0
    requests = len(server.requests)
    after_apply = tree_snapshot(tmp_path)

    for args in ((), ("--apply", "--yes")):
        result = _invoke(runtime_paths, *args)

        assert result.exit_code == 0, result.output
        assert "Already adopted; nothing to do." in result.output
    assert tree_snapshot(tmp_path) == after_apply
    assert len(server.requests) == requests


def test_discard_new_principal_reaches_the_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _MatrixServer,
) -> None:
    """Rows MindRoom wrote under the new ID refuse the rename until --discard-new-principal deletes them."""
    runtime_paths = _install(tmp_path, monkeypatch, new_principal_rows=True)
    before = tree_snapshot(tmp_path)

    refused = _invoke(runtime_paths, "--apply", "--yes", "--skip-login-check")

    assert refused.exit_code == 1, refused.output
    assert "Rerun with --discard-new-principal" in refused.output
    assert tree_snapshot(tmp_path) == before

    applied = _invoke(runtime_paths, "--apply", "--yes", "--skip-login-check", "--discard-new-principal")

    assert applied.exit_code == 0, applied.output
    # The stored device is dropped, so the next start binds a fresh stream: neither principal's stream state stays.
    assert _journal_rows(runtime_paths, "SELECT principal_id FROM matrix_sync_consumers") == []
    assert _journal_rows(runtime_paths, "SELECT principal_id FROM matrix_ingestion_membership") == []
    assert server.requests == []


def _adopt_by_hand(runtime_paths: RuntimePaths) -> None:
    """Change the stored username to the new localpart and bind a new device, as the 2026-10-19 manual fix did."""
    path = matrix_state_file(runtime_paths)
    state = safe_load(path.read_bytes())
    account = state["accounts"][f"agent_{AGENT}"]
    account.update(username="new", device_id="NEWDEVICE", access_token="new-device-token")  # noqa: S106
    path.write_bytes(safe_dump(state, default_flow_style=False, sort_keys=False).encode())


def test_manual_adoption_refuses_without_from(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _MatrixServer,
) -> None:
    """A username already set to the new ID does not count as adopted while the journal still names the old ID."""
    runtime_paths = _install(tmp_path, monkeypatch, new_principal_rows=True)
    _adopt_by_hand(runtime_paths)
    before = tree_snapshot(tmp_path)
    rerun = (
        f"mindroom agents rename-account {AGENT} {NEW} --from {OLD} "
        f"--config {runtime_paths.config_path} --storage-path {runtime_paths.storage_root}"
    )

    for args in ((), ("--apply", "--yes", "--skip-login-check", "--discard-new-principal")):
        result = _invoke(runtime_paths, *args)

        assert result.exit_code == 1, result.output
        assert "Already adopted" not in result.output
        assert OLD_PRINCIPAL in result.output
        assert rerun in result.output
    assert tree_snapshot(tmp_path) == before
    assert server.requests == []


def test_from_adopts_a_manually_renamed_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _MatrixServer,
) -> None:
    """--from moves the journal and files from the old ID and keeps the new device the manual fix bound."""
    runtime_paths = _install(tmp_path, monkeypatch, new_principal_rows=True)
    _adopt_by_hand(runtime_paths)
    storage = runtime_paths.storage_root

    result = _invoke(runtime_paths, "--from", OLD, "--discard-new-principal", "--apply", "--yes", "--skip-login-check")

    assert result.exit_code == 0, result.output
    account = safe_load(matrix_state_file(runtime_paths).read_bytes())["accounts"][f"agent_{AGENT}"]
    assert (account["username"], account["device_id"], account["access_token"]) == (
        "new",
        "NEWDEVICE",
        "new-device-token",
    )
    assert _journal_rows(runtime_paths, "SELECT principal_id, sender FROM journal_events ORDER BY principal_id") == [
        (NEW_PRINCIPAL, NEW),
        (ROUTER, NEW),
    ]
    # The kept device continues the stream it bound under the new ID, so that stream's state stays.
    assert _journal_rows(runtime_paths, "SELECT * FROM matrix_sync_consumers") == [
        (NEW_PRINCIPAL, "generation-2", "stream-2", 1),
    ]
    assert _journal_rows(runtime_paths, "SELECT * FROM matrix_ingestion_membership") == [
        (NEW_PRINCIPAL, "!room:x", "join", 0),
    ]
    assert _stored_worker_key(_oauth_path(storage, NEW)) == _key("user_agent", NEW)
    assert (storage / "workers" / worker_dir_name(_key("user", NEW)) / "workspace" / "notes.md").is_file()
    assert runtime_paths.config_path.read_text() == CONFIG.replace(OLD, NEW)
    assert f"Leftover scan: nothing in {storage} names {OLD} any more." in result.output
    assert server.requests == []

    rerun = _invoke(runtime_paths)

    assert rerun.exit_code == 0, rerun.output
    assert "Already adopted; nothing to do." in rerun.output


def _change_only_the_username(runtime_paths: RuntimePaths) -> Path:
    """Set the stored username to the new localpart but keep the old login's device, whose crypto store is the old ID's.

    Returns that crypto store.
    """
    old_store = olm_store_dir(OLD, runtime_paths) / f"{OLD}_OLDDEVICE.db"
    old_store.parent.mkdir(parents=True)
    old_store.write_bytes(b"")
    path = matrix_state_file(runtime_paths)
    state = safe_load(path.read_bytes())
    state["accounts"][f"agent_{AGENT}"]["username"] = "new"
    path.write_bytes(safe_dump(state, default_flow_style=False, sort_keys=False).encode())
    return old_store


def test_from_drops_the_old_device_of_a_username_changed_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _MatrixServer,
) -> None:
    """A manual edit of only the username leaves the old login's device, which MindRoom cannot start with; --from drops it."""
    runtime_paths = _install(tmp_path, monkeypatch)
    old_store = _change_only_the_username(runtime_paths)
    path = matrix_state_file(runtime_paths)

    result = _invoke(runtime_paths, "--from", OLD, "--apply", "--yes", "--skip-login-check")

    assert result.exit_code == 0, result.output
    assert f"Drop the device OLDDEVICE and access token of agent_{AGENT} in {path}" in result.output
    account = safe_load(path.read_bytes())["accounts"][f"agent_{AGENT}"]
    assert (account["username"], account["device_id"], account["access_token"]) == ("new", None, None)
    # No device continues the old stream, so the next start binds a fresh one.
    assert _journal_rows(runtime_paths, "SELECT principal_id FROM matrix_sync_consumers") == []
    assert old_store.is_file()
    assert server.requests == []


def test_from_that_drops_the_device_keeps_no_new_stream_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _MatrixServer,
) -> None:
    """When --from drops the stored device, the new principal's sync stream state is deleted with its other rows.

    The username already names the new ID, but the next start logs in on a fresh device that binds a fresh stream,
    so no device continues the stream the new principal's consumer and producer rows describe.
    """
    runtime_paths = _install(tmp_path, monkeypatch, new_principal_rows=True)
    _change_only_the_username(runtime_paths)

    result = _invoke(runtime_paths, "--from", OLD, "--discard-new-principal", "--apply", "--yes", "--skip-login-check")

    assert result.exit_code == 0, result.output
    account = safe_load(matrix_state_file(runtime_paths).read_bytes())["accounts"][f"agent_{AGENT}"]
    assert (account["device_id"], account["access_token"]) == (None, None)
    assert _journal_rows(runtime_paths, "SELECT principal_id FROM matrix_sync_consumers") == []
    assert _journal_rows(runtime_paths, "SELECT principal_id FROM matrix_ingestion_membership") == []
    assert server.requests == []


@pytest.mark.parametrize(
    ("from_user_id", "reason"),
    [
        ("old", "old is not a Matrix user ID"),
        ("@old:y", f"{NEW} is not on y, the server of @old:y"),
        (NEW, f"--from {NEW} is the new ID itself"),
        ("@router:x", "--from @router:x is the stored account agent_router"),
    ],
)
def test_from_is_validated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _MatrixServer,
    from_user_id: str,
    reason: str,
) -> None:
    """--from must be a Matrix ID on the new ID's server that differs from it and is no other stored account."""
    runtime_paths = _install(tmp_path, monkeypatch)
    _adopt_by_hand(runtime_paths)
    before = tree_snapshot(tmp_path)

    for args in ((), ("--apply", "--yes")):
        result = _invoke(runtime_paths, "--from", from_user_id, *args)

        assert result.exit_code == 1, result.output
        assert reason in result.output
    assert tree_snapshot(tmp_path) == before
    assert server.requests == []


def test_from_must_match_a_stored_account_not_yet_renamed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _MatrixServer,
) -> None:
    """While the stored account still has its own ID, --from may only repeat that ID."""
    runtime_paths = _install(tmp_path, monkeypatch)
    before = tree_snapshot(tmp_path)

    result = _invoke(runtime_paths, "--from", "@other:x")

    assert result.exit_code == 1, result.output
    assert f"--from @other:x does not match the stored account {OLD} of agent_{AGENT}" in result.output
    assert tree_snapshot(tmp_path) == before
    assert server.requests == []


def _commit_journal_step_only(runtime_paths: RuntimePaths, *, stored_user_id: str, discard: bool) -> None:
    """Leave the install the way a run killed right after its journal step does: journal moved, nothing else."""
    config = cli_agents._load_config_or_exit(runtime_paths)
    adoption = cli_agents._Adoption(AGENT, OLD, NEW, stored_user_id, PASSWORD)
    journal_step = cli_agents._plan(runtime_paths, config, adoption, discard_new_principal=discard).steps[0]
    assert journal_step.description.startswith("Rewrite the event journal")
    journal_step.apply(runtime_paths.storage_root.parent / "killed-run-backup")


def _journal_events(runtime_paths: RuntimePaths) -> list[tuple[object, ...]]:
    return _journal_rows(runtime_paths, "SELECT principal_id, sender FROM journal_events ORDER BY principal_id")


def test_rerun_after_a_killed_run_keeps_the_moved_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _MatrixServer,
) -> None:
    """After a run killed past its journal step, the rows under the new principal are the agent's moved history.

    Rerunning, even with --discard-new-principal, leaves them and finishes the files.
    """
    runtime_paths = _install(tmp_path, monkeypatch)
    _commit_journal_step_only(runtime_paths, stored_user_id=OLD, discard=False)
    moved = [(NEW_PRINCIPAL, NEW), (ROUTER, NEW)]
    assert _journal_events(runtime_paths) == moved

    result = _invoke(runtime_paths, "--discard-new-principal", "--apply", "--yes", "--skip-login-check")

    assert result.exit_code == 0, result.output
    assert _journal_events(runtime_paths) == moved
    assert f"has no rows for {OLD_PRINCIPAL}, so it stays as it is" in result.output
    account = safe_load(matrix_state_file(runtime_paths).read_bytes())["accounts"][f"agent_{AGENT}"]
    assert account["username"] == "new"
    assert _stored_worker_key(_oauth_path(runtime_paths.storage_root, NEW)) == _key("user_agent", NEW)
    assert f"Leftover scan: nothing in {runtime_paths.storage_root} names {OLD} any more." in result.output
    assert server.requests == []


def test_rerun_after_a_killed_from_run_keeps_the_moved_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _MatrixServer,
) -> None:
    """A killed --from run leaves the journal moved, and no rerun deletes those rows.

    Without --from the journal shows no other ID. With --from @old:x nothing shows any more that it was nova's,
    because @old:x is not the ID MindRoom generates for nova, so the rerun refuses and the manifest is the way back.
    """
    runtime_paths = _install(tmp_path, monkeypatch, new_principal_rows=True)
    _adopt_by_hand(runtime_paths)
    _commit_journal_step_only(runtime_paths, stored_user_id=NEW, discard=True)
    moved = [(NEW_PRINCIPAL, NEW), (ROUTER, NEW)]
    assert _journal_events(runtime_paths) == moved
    before = tree_snapshot(tmp_path)

    without_from = _invoke(runtime_paths)

    assert without_from.exit_code == 0, without_from.output
    assert "Already adopted; nothing to do." in without_from.output

    result = _invoke(runtime_paths, "--from", OLD, "--discard-new-principal", "--apply", "--yes", "--skip-login-check")

    assert result.exit_code == 1, result.output
    assert f"--from {OLD} must be a former Matrix ID of agent {AGENT}" in result.output
    assert "restore from its backup manifest" in result.output
    assert _journal_events(runtime_paths) == moved
    assert tree_snapshot(tmp_path) == before
    assert server.requests == []


def test_repeating_a_successful_from_run_changes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _MatrixServer,
) -> None:
    """The same --from --discard-new-principal --apply run a second time writes nothing and keeps the journal.

    The first run moved nova's journal rows away from @old:x, so nothing shows any more that @old:x was his.
    """
    runtime_paths = _install(tmp_path, monkeypatch, new_principal_rows=True)
    _adopt_by_hand(runtime_paths)
    command = ("--from", OLD, "--discard-new-principal", "--apply", "--yes", "--skip-login-check")
    assert _invoke(runtime_paths, *command).exit_code == 0
    after_first = tree_snapshot(tmp_path)

    again = _invoke(runtime_paths, *command)

    assert again.exit_code == 1, again.output
    assert f"--from {OLD} must be a former Matrix ID of agent {AGENT}" in again.output
    assert tree_snapshot(tmp_path) == after_first
    assert _journal_events(runtime_paths) == [(NEW_PRINCIPAL, NEW), (ROUTER, NEW)]
    assert server.requests == []


def test_from_naming_someone_else_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _MatrixServer,
) -> None:
    """A --from that nothing shows to be nova's refuses, so a person's credentials and mentions never move to him."""
    runtime_paths = _install(tmp_path, monkeypatch)
    _adopt_by_hand(runtime_paths)
    alice = "@alice:x"
    _oauth_store(_oauth_path(runtime_paths.storage_root, alice), _key("user_agent", alice))
    with closing(sqlite3.connect(event_journal_sqlite_path(runtime_paths.storage_root))) as connection, connection:
        _event(connection, "code@@code:x", sender=alice)
    before = tree_snapshot(tmp_path)

    for args in ((), ("--discard-new-principal", "--apply", "--yes", "--skip-login-check")):
        result = _invoke(runtime_paths, "--from", alice, *args)

        assert result.exit_code == 1, result.output
        assert f"--from {alice} must be a former Matrix ID of agent {AGENT}" in result.output
        assert f"has no rows for {AGENT}@{alice}" in result.output
        assert "is not @mindroom_nova:x, the ID MindRoom generates for nova" in result.output
    assert tree_snapshot(tmp_path) == before
    assert server.requests == []


@pytest.mark.parametrize(("namespace", "generated"), [("", "@mindroom_nova:x"), ("ab12", "@mindroom_nova_ab12:x")])
def test_from_with_the_generated_id_needs_no_journal_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _MatrixServer,
    namespace: str,
    generated: str,
) -> None:
    """The ID MindRoom generates for the agent, namespace included, is accepted without journal rows.

    That lets a run killed after its journal step resume; once everything moved, a rerun finds nothing to do.
    """
    runtime_paths = _install(tmp_path, monkeypatch)
    _adopt_by_hand(runtime_paths)
    monkeypatch.setenv("MINDROOM_NAMESPACE", namespace)
    storage = runtime_paths.storage_root
    _oauth_store(_oauth_path(storage, generated), _key("user_agent", generated))
    if namespace:
        unnamespaced = _invoke(runtime_paths, "--from", "@mindroom_nova:x")
        assert unnamespaced.exit_code == 1, unnamespaced.output
        assert "must be a former Matrix ID" in unnamespaced.output

    result = _invoke(runtime_paths, "--from", generated, "--apply", "--yes", "--skip-login-check")

    assert result.exit_code == 0, result.output
    assert not _oauth_path(storage, generated).exists()
    assert _stored_worker_key(_oauth_path(storage, NEW)) == _key("user_agent", NEW)

    again = _invoke(runtime_paths, "--from", generated, "--apply", "--yes", "--skip-login-check")

    assert again.exit_code == 0, again.output
    assert f"Nothing left to move from {generated}; nothing was changed." in again.output
    assert server.requests == []


def test_adoption_check_only_offers_ids_on_the_new_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _MatrixServer,
) -> None:
    """Principals under another server cannot be adopted in place, so they never trigger the --from refusal."""
    runtime_paths = _install(tmp_path, monkeypatch)
    assert _invoke(runtime_paths, "--apply", "--yes").exit_code == 0
    with closing(sqlite3.connect(event_journal_sqlite_path(runtime_paths.storage_root))) as connection, connection:
        _event(connection, "nova@@old:y", sender="@old:y")

    other_server = _invoke(runtime_paths)

    assert other_server.exit_code == 0, other_server.output
    assert "Already adopted; nothing to do." in other_server.output

    with closing(sqlite3.connect(event_journal_sqlite_path(runtime_paths.storage_root))) as connection, connection:
        _event(connection, "nova@@older:x", sender="@older:x")

    same_server = _invoke(runtime_paths)

    assert same_server.exit_code == 1, same_server.output
    assert "--from @older:x" in same_server.output
    assert "@old:y" not in same_server.output
    assert len(server.requests) == 2


def test_declining_the_confirmation_changes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _MatrixServer,
) -> None:
    """Without --yes the operator is asked, and answering no exits 1 with nothing changed."""
    runtime_paths = _install(tmp_path, monkeypatch)
    before = tree_snapshot(tmp_path)

    result = runner.invoke(
        app,
        [
            "agents",
            "rename-account",
            AGENT,
            NEW,
            "--config",
            str(runtime_paths.config_path),
            "--storage-path",
            str(runtime_paths.storage_root),
            "--apply",
        ],
        input="n\n",
    )

    assert result.exit_code == 1, result.output
    assert "Apply this plan?" in result.output
    assert tree_snapshot(tmp_path) == before
    assert len(server.requests) == 2


def test_failed_undo_exits_2_naming_the_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _MatrixServer,
) -> None:
    """When undoing a failed step fails too, the command exits 2 and points at the manifest for a manual restore."""
    runtime_paths = _install(tmp_path, monkeypatch)
    manifest = runtime_paths.storage_root / "agent-rename-backups" / "x-rename-account" / "manifest.json"

    def fail_undo(plan: RenamePlan, *, kind: str, now: object) -> None:
        assert plan.steps
        assert kind == "rename-account"
        assert now is not None
        raise RenameUndoError(manifest, "step 2 failed, and undoing step 1 failed")

    monkeypatch.setattr(steps_module, "apply_plan", fail_undo)

    result = _invoke(runtime_paths, "--apply", "--yes")

    assert result.exit_code == 2, result.output
    assert "step 2 failed, and undoing step 1 failed" in result.output
    assert str(manifest) in result.output
    assert len(server.requests) == 2


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
    server: _MatrixServer,
) -> None:
    """On Docker the old worker's container is retired after the move, and the leased manager is shut down."""
    runtime_paths = _install(tmp_path, monkeypatch)
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "docker")
    manager = _RetiringManager()
    events: list[str] = []

    def lease(_runtime_paths: RuntimePaths, *, runtime_config: Config | None) -> _RetiringManager:
        assert runtime_config is not None
        events.append("lease")
        return manager

    monkeypatch.setattr(account_files, "lease_configured_primary_worker_manager", lease)
    monkeypatch.setattr(workers_runtime, "shutdown_primary_worker_manager", lambda: events.append("shutdown"))

    result = _invoke(runtime_paths, "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert manager.retired == [_key("user", OLD)]
    assert events == ["lease", "shutdown"]
    assert len(server.requests) == 2
