"""Tests for moving the files keyed by an agent's Matrix ID after the server renamed its account."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, cast

import pytest

from mindroom.agent_rename import account_files
from mindroom.agent_rename.account_files import _rewrite_worker_key, account_file_steps, matrix_state_steps
from mindroom.agent_rename.steps import (
    DropPath,
    FinalAction,
    MovePath,
    RenamePlan,
    RenameRefusedError,
    apply_plan,
)
from mindroom.config.main import Config
from mindroom.constants import matrix_state_file, primary_records_dir, resolve_session_state_root
from mindroom.credentials.manager import (
    PRIMARY_RUNTIME_SCOPED_CREDENTIALS_DIRNAME,
    get_runtime_credentials_manager,
    scoped_credentials_dir_part,
)
from mindroom.desktop.native_config import native_config_path
from mindroom.matrix.client_session import olm_store_dir
from mindroom.matrix.personal_room_store import (
    PersonalRoomAdoption,
    PersonalRoomRecord,
    personal_room_record_path,
    read_personal_room,
    write_personal_room,
)
from mindroom.matrix.state import MatrixState
from mindroom.oauth.credential_lifecycle import OAuthCredentialContext
from mindroom.oauth.credential_store import oauth_credential_reader, oauth_credential_transaction
from mindroom.private_storage.legacy_private_storage_aliases import historical_private_instance_worker_key
from mindroom.private_storage.private_instance_identity_store import (
    PrivateInstanceIdentity,
    ensure_private_instance_identity,
    load_private_instance_identity,
    private_instances_for_agent,
)
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    WorkerScope,
    private_instance_scope_root_path,
    resolve_worker_key,
    resolve_worker_target,
    worker_dir_name,
)
from mindroom.workers.backends.docker import WORKER_CONTROL_DIRNAME
from mindroom.workers.backends.docker_projection import PROJECTED_CONFIGS_DIRNAME
from tests.agent_rename_helpers import tree_snapshot
from tests.conftest import TEST_ACCESS_TOKEN, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_rename.steps import Step
    from mindroom.constants import RuntimePaths
    from mindroom.oauth.providers import OAuthProvider

AGENT = "nova"
OLD = "@old:x"
NEW = "@new:x"
NOW = datetime(2026, 10, 19, 21, 45, 58, tzinfo=UTC)


def _identity(requester_id: str, agent_name: str = "code") -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
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


def _install(tmp_path: Path) -> RuntimePaths:
    """Return an install whose matrix_state.yaml holds the agent's account under the old localpart."""
    runtime_paths = test_runtime_paths(tmp_path)
    state = MatrixState()
    state.add_account(f"agent_{AGENT}", "old", "secret", domain="x")
    state.save(runtime_paths)
    return runtime_paths


def _steps(runtime_paths: RuntimePaths, *, discard: bool = False) -> tuple[list[Step], list[str]]:
    """Return the file steps, then the matrix_state.yaml step, in the order the CLI plans them."""
    steps, reports = account_file_steps(
        runtime_paths,
        agent=AGENT,
        old_user_id=OLD,
        new_user_id=NEW,
        config=Config(),
        discard_new_identity_state=discard,
    )
    account_steps, account_reports = matrix_state_steps(runtime_paths, agent=AGENT, old_user_id=OLD, new_user_id=NEW)
    return [*steps, *account_steps], [*account_reports, *reports]


def _apply(runtime_paths: RuntimePaths, *, discard: bool = False) -> list[str]:
    steps, reports = _steps(runtime_paths, discard=discard)
    plan = RenamePlan(storage_root=runtime_paths.storage_root, steps=tuple(steps), reports=tuple(reports))
    result = apply_plan(plan, kind="rename-account", now=NOW)
    assert result.warnings == ()
    return reports


def _docker(runtime_paths: RuntimePaths) -> RuntimePaths:
    return replace(runtime_paths, process_env={**runtime_paths.process_env, "MINDROOM_WORKER_BACKEND": "docker"})


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _oauth_store(path: Path, worker_key: str, *, credential_present: int = 1) -> None:
    """Create a minimal OAuth credential store bound to ``worker_key``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "CREATE TABLE oauth_credential_state "
            "(singleton INTEGER PRIMARY KEY, provider_id TEXT, worker_key TEXT, credential_present INTEGER)",
        )
        connection.execute(
            "INSERT INTO oauth_credential_state VALUES (1, 'demo', ?, ?)",
            (worker_key, credential_present),
        )
        connection.commit()


def _requester_store(storage: Path, requester_id: str) -> Path:
    """Return where a ``user_agent`` OAuth store of ``requester_id`` with the agent ``code`` lives."""
    requester_dir = scoped_credentials_dir_part(requester_id)
    store = PRIMARY_RUNTIME_SCOPED_CREDENTIALS_DIRNAME, requester_dir, scoped_credentials_dir_part("code")
    return storage.joinpath(*store, "demo_oauth_credentials.sqlite3")


def _stored_worker_key(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT worker_key FROM oauth_credential_state").fetchone()[0]


class _Provider:
    id = "demo_provider"
    credential_service = "demo_oauth"
    requester_scoped_credentials = True


def _oauth_context(runtime_paths: RuntimePaths, requester_id: str, scope: WorkerScope) -> OAuthCredentialContext:
    return OAuthCredentialContext(
        provider=cast("OAuthProvider", _Provider()),
        runtime_paths=runtime_paths,
        credentials_manager=get_runtime_credentials_manager(runtime_paths),
        worker_target=resolve_worker_target(scope, "code", _identity(requester_id)),
    )


class _FakeDockerManager:
    backend_name = "docker"

    def __init__(self) -> None:
        self.retired: list[str] = []

    def retire_worker(self, worker_key: str) -> None:
        self.retired.append(worker_key)


class _FakeLease:
    def __init__(self, manager: _FakeDockerManager) -> None:
        self.manager = manager
        self.released = False

    def __enter__(self) -> _FakeDockerManager:
        return self.manager

    def __exit__(self, *_exc: object) -> bool:
        self.release()
        return False

    def release(self) -> None:
        self.released = True


def test_rewrite_worker_key_rewrites_both_requester_scopes() -> None:
    """Both requester scopes get the new requester; other users and requester-less scopes keep their keys."""
    rewrite = partial(_rewrite_worker_key, old_user_id=OLD, new_user_id=NEW)

    assert rewrite("v1:default:user:~@old:x") == "v1:default:user:~@new:x"
    assert rewrite("v1:acme:user_agent:~@old:x:code") == "v1:acme:user_agent:~@new:x:code"
    for scope in ("user", "user_agent"):
        assert rewrite(_key(scope, OLD)) == _key(scope, NEW)
    # The requester goes through the same encoder as live keys, so characters it escapes are escaped.
    assert _rewrite_worker_key("v1:default:user:~@old:x", old_user_id=OLD, new_user_id="@n=1:x") == (
        "v1:default:user:~@n%3D1:x"
    )
    # A different user whose ID merely starts with the old one keeps its keys.
    assert rewrite("v1:default:user:~@old:x.example.org") is None
    assert rewrite("v1:default:user_agent:~@old:x.example.org:code") is None
    assert rewrite("v1:default:user_agent:~@old:x:8448:code") is None
    assert rewrite("v1:default:shared:code") is None
    assert rewrite("v1:default:unscoped:code") is None


def test_rewrite_worker_key_keeps_the_legacy_unprefixed_requester() -> None:
    """OAuth stores from before the `~` requester prefix keep that spelling, which MindRoom still accepts."""
    rewrite = partial(_rewrite_worker_key, old_user_id=OLD, new_user_id=NEW)

    assert rewrite("v1:default:user:@old:x") == "v1:default:user:@new:x"
    assert rewrite("v1:default:user_agent:@old:x:code") == "v1:default:user_agent:@new:x:code"
    assert rewrite("v1:default:user_agent:@old:x.example.org:code") is None


def test_matrix_state_username_changed_device_cleared(tmp_path: Path) -> None:
    """The account takes the new localpart and drops its device, written the way MindRoom writes the file."""
    runtime_paths = test_runtime_paths(tmp_path / "install")
    state = MatrixState()
    state.add_account(
        f"agent_{AGENT}",
        "old",
        "secret",
        domain="x",
        device_id="OLDDEVICE",
        access_token=TEST_ACCESS_TOKEN,
    )
    state.add_account("agent_router", "router", "other", domain="x", device_id="ROUTER", access_token=TEST_ACCESS_TOKEN)
    state.add_room("lobby", "!lobby:x", "#lobby:x", "Lobby")
    state.save(runtime_paths)

    _apply(runtime_paths)

    expected = state.model_copy(deep=True)
    expected.add_account(f"agent_{AGENT}", "new", "secret", domain="x")
    expected_paths = test_runtime_paths(tmp_path / "expected")
    expected.save(expected_paths)
    assert matrix_state_file(runtime_paths).read_bytes() == matrix_state_file(expected_paths).read_bytes()


@pytest.mark.parametrize("store_owner", [OLD, NEW, None])
def test_username_set_by_hand_keeps_only_a_device_of_the_new_id(tmp_path: Path, store_owner: str | None) -> None:
    """A username already changed by hand keeps its device, unless the device's crypto store is the old ID's.

    That device and token come from the old login: MindRoom refuses to start a device without a store under its ID,
    so they are dropped as in a normal run, and the next start logs in on a fresh device.
    """
    runtime_paths = test_runtime_paths(tmp_path)
    state = MatrixState()
    state.add_account(f"agent_{AGENT}", "new", "secret", domain="x", device_id="DEVICE", access_token=TEST_ACCESS_TOKEN)
    state.save(runtime_paths)
    if store_owner is not None:
        store = olm_store_dir(store_owner, runtime_paths) / f"{store_owner}_DEVICE.db"
        store.parent.mkdir(parents=True)
        store.write_bytes(b"")

    steps, reports = matrix_state_steps(runtime_paths, agent=AGENT, old_user_id=OLD, new_user_id=NEW)

    if store_owner != OLD:
        assert steps == []
        assert reports == [
            f"{matrix_state_file(runtime_paths)} already names {NEW} for agent_{AGENT}; its device and access token stay.",
        ]
        return
    assert reports == []
    plan = RenamePlan(storage_root=runtime_paths.storage_root, steps=tuple(steps), reports=())
    apply_plan(plan, kind="rename-account", now=NOW)
    expected = MatrixState()
    expected.add_account(f"agent_{AGENT}", "new", "secret", domain="x")
    expected_paths = test_runtime_paths(tmp_path / "expected")
    expected.save(expected_paths)
    assert matrix_state_file(runtime_paths).read_bytes() == matrix_state_file(expected_paths).read_bytes()


def test_matrix_state_without_the_account_refuses(tmp_path: Path) -> None:
    """A rename for an agent with no stored account refuses before any write."""
    runtime_paths = test_runtime_paths(tmp_path)
    MatrixState().save(runtime_paths)

    with pytest.raises(RenameRefusedError, match=f"agent_{AGENT}"):
        _steps(runtime_paths)


@pytest.mark.asyncio
async def test_oauth_dir_moved_and_worker_key_rewritten(tmp_path: Path) -> None:
    """The requester's OAuth stores move to the new requester's directory and open for the new requester."""
    runtime_paths = _install(tmp_path)
    for scope in ("user", "user_agent"):
        async with oauth_credential_transaction(_oauth_context(runtime_paths, OLD, scope)) as transaction:
            await transaction.publish({"token": f"{scope}-token"}, advance_connection_generation=True)
            await transaction.commit()
    oauth_root = runtime_paths.storage_root / PRIMARY_RUNTIME_SCOPED_CREDENTIALS_DIRNAME
    assert len(list((oauth_root / scoped_credentials_dir_part(OLD)).glob("*/*_credentials.sqlite3"))) == 2

    _apply(runtime_paths)

    assert not (oauth_root / scoped_credentials_dir_part(OLD)).exists()
    assert len(list((oauth_root / scoped_credentials_dir_part(NEW)).glob("*/*_credentials.sqlite3"))) == 2
    for scope in ("user", "user_agent"):
        async with oauth_credential_reader(_oauth_context(runtime_paths, NEW, scope)) as reader:
            snapshot = await reader.snapshot()
        assert snapshot.credentials == {"token": f"{scope}-token"}


def test_destination_that_exists_refuses(tmp_path: Path) -> None:
    """Without the discard flag, a store that already exists under the new ID refuses, naming it and the flag."""
    runtime_paths = _install(tmp_path)
    oauth_root = runtime_paths.storage_root / PRIMARY_RUNTIME_SCOPED_CREDENTIALS_DIRNAME
    _oauth_store(oauth_root / scoped_credentials_dir_part(OLD) / "code-1" / "demo_oauth_credentials.sqlite3", "")
    (oauth_root / scoped_credentials_dir_part(NEW)).mkdir(parents=True)

    with pytest.raises(RenameRefusedError) as raised:
        _steps(runtime_paths)

    assert str(oauth_root / scoped_credentials_dir_part(NEW)) in str(raised.value)
    assert "--discard-new-principal drops new-ID state that holds no credentials" in str(raised.value)


def test_discard_drops_credential_free_destinations_before_moving(tmp_path: Path) -> None:
    """With the discard flag, new-ID trees that hold no credentials are dropped into the backup, then replaced."""
    runtime_paths = _install(tmp_path)
    storage = runtime_paths.storage_root
    old_key, new_key = _key("user_agent", OLD), _key("user_agent", NEW)
    _oauth_store(_requester_store(storage, OLD), old_key)
    new_oauth = storage / PRIMARY_RUNTIME_SCOPED_CREDENTIALS_DIRNAME / scoped_credentials_dir_part(NEW)
    _oauth_store(_requester_store(storage, NEW), new_key, credential_present=0)
    (_requester_store(storage, NEW).parent / "demo_oauth_credentials.lock").write_text("", encoding="utf-8")
    (new_oauth / "empty").mkdir()
    workers = storage / "workers"
    old_worker = workers / worker_dir_name(old_key)
    new_worker = workers / worker_dir_name(new_key)
    (old_worker / "workspace").mkdir(parents=True)
    (old_worker / "workspace" / "notes.md").write_text("notes", encoding="utf-8")
    for name in ("credentials", ".shared_credentials"):
        (new_worker / name).mkdir(parents=True)

    steps, _reports = _steps(runtime_paths, discard=True)

    for destination in (new_oauth, new_worker):
        index = next(i for i, step in enumerate(steps) if isinstance(step, DropPath) and step.path == destination)
        move = steps[index + 1]
        assert isinstance(move, MovePath)
        assert move.destination == destination

    _apply(runtime_paths, discard=True)

    assert _stored_worker_key(_requester_store(storage, NEW)) == new_key
    assert (new_worker / "workspace" / "notes.md").read_text() == "notes"
    assert not (new_worker / "credentials").exists()
    backups = storage / "agent-rename-backups"
    assert any(path.name == "demo_oauth_credentials.lock" for path in backups.rglob("*"))


def test_discard_refuses_destination_holding_credentials(tmp_path: Path) -> None:
    """Even with the discard flag, a new-ID store holding credentials refuses, naming the store."""
    runtime_paths = _install(tmp_path)
    storage = runtime_paths.storage_root
    _oauth_store(_requester_store(storage, OLD), _key("user_agent", OLD))
    _oauth_store(_requester_store(storage, NEW), _key("user_agent", NEW), credential_present=1)

    with pytest.raises(RenameRefusedError) as raised:
        _steps(runtime_paths, discard=True)

    assert f"credentials in {_requester_store(storage, NEW)}" in str(raised.value)


def test_worker_dir_moved_metadata_rewritten_and_oauth_store_rewritten(tmp_path: Path) -> None:
    """A sandbox-runner worker moves to its new key's directory with its metadata and OAuth store rewritten."""
    runtime_paths = _install(tmp_path)
    workers = runtime_paths.storage_root / "workers"
    old_key, new_key = _key("user", OLD), _key("user", NEW)
    other_key = _key("user", "@old:x.example.org")
    old_root = workers / worker_dir_name(old_key)
    _write_json(
        old_root / "metadata" / "worker.json",
        {"worker_id": worker_dir_name(old_key), "worker_key": old_key, "status": "idle"},
    )
    (old_root / "workspace").mkdir()
    (old_root / "workspace" / "notes.md").write_text("notes", encoding="utf-8")
    _oauth_store(old_root / "credentials" / "demo_oauth_credentials.sqlite3", old_key)
    _write_json(workers / worker_dir_name(other_key) / "metadata" / "worker.json", {"worker_key": other_key})

    reports = _apply(runtime_paths)

    assert not any("looks like a worker" in report for report in reports)
    new_root = workers / worker_dir_name(new_key)
    assert not old_root.exists()
    assert json.loads((new_root / "metadata" / "worker.json").read_text()) == {
        "status": "idle",
        "worker_id": worker_dir_name(new_key),
        "worker_key": new_key,
    }
    assert (new_root / "workspace" / "notes.md").read_text() == "notes"
    assert _stored_worker_key(new_root / "credentials" / "demo_oauth_credentials.sqlite3") == new_key
    assert (workers / worker_dir_name(other_key) / "metadata" / "worker.json").exists()


def test_moved_worker_venv_is_dropped_into_the_backup(tmp_path: Path) -> None:
    """A moved worker's venv is dropped right after the move, because its scripts' shebangs name the old root.

    MindRoom rebuilds a missing venv on the worker's next use; the old one stays in the backup.
    """
    runtime_paths = _install(tmp_path)
    workers = runtime_paths.storage_root / "workers"
    old_key, new_key = _key("user", OLD), _key("user", NEW)
    old_root, new_root = workers / worker_dir_name(old_key), workers / worker_dir_name(new_key)
    _write_json(old_root / "metadata" / "worker.json", {"worker_key": old_key})
    (old_root / "venv" / "bin").mkdir(parents=True)
    (old_root / "venv" / "bin" / "pip").write_text(f"#!{old_root}/venv/bin/python\n", encoding="utf-8")

    steps, _reports = _steps(runtime_paths)
    move = next(index for index, step in enumerate(steps) if isinstance(step, MovePath) and step.source == old_root)
    assert steps[move + 1] == DropPath(
        new_root / "venv",
        f"Drop the moved worker's virtual environment {new_root / 'venv'}; MindRoom rebuilds it on the worker's "
        "next use, so packages installed into it must be installed again",
    )

    _apply(runtime_paths)

    assert not (new_root / "venv").exists()
    (backup,) = (runtime_paths.storage_root / "agent-rename-backups").iterdir()
    assert (backup / f"{move + 1:03d}" / "venv" / "bin" / "pip").read_text() == f"#!{old_root}/venv/bin/python\n"


def test_worker_without_venv_gets_no_drop(tmp_path: Path) -> None:
    """Only a venv that exists is dropped."""
    runtime_paths = _install(tmp_path)
    old_key = _key("user", OLD)
    old_root = runtime_paths.storage_root / "workers" / worker_dir_name(old_key)
    _write_json(old_root / "metadata" / "worker.json", {"worker_key": old_key})

    steps, _reports = _steps(runtime_paths)

    assert [step.source for step in steps if isinstance(step, MovePath)] == [old_root]
    assert not [step for step in steps if isinstance(step, DropPath)]


def test_other_users_workers_are_neither_moved_nor_reported(tmp_path: Path) -> None:
    """Workers of users whose IDs start like the old one stay put and unreported; only possible old-ID roots report."""
    runtime_paths = _install(tmp_path)
    workers = runtime_paths.storage_root / "workers"
    others = [_key(scope, user) for scope in ("user", "user_agent") for user in ("@old:x.example.org", "@older:x")]
    for key in others:
        _write_json(workers / worker_dir_name(key) / "metadata" / "worker.json", {"worker_key": key})
    (workers / "v1_default_user_@older_x-0123456789abcdef").mkdir()
    possibly_old = workers / "v1_default_user_agent_@old_x_code-0123456789abcdef"
    possibly_old.mkdir()

    steps, reports = _steps(runtime_paths)

    assert not [step for step in steps if isinstance(step, MovePath)]
    assert reports[:-1] == [
        f"{possibly_old} looks like a worker of {OLD}, but its worker key is unknown, so it stays in place.",
    ]


def test_docker_workers_moved_control_dropped_and_retired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Docker workers move, lose their control record and projections, and are retired only after apply.

    One worker is known from its control record, one only from the requester's OAuth store, as on the production
    install, and one directory whose key cannot be determined is reported and left alone.
    """
    runtime_paths = _docker(_install(tmp_path))
    storage = runtime_paths.storage_root
    workers = storage / "workers"
    user_key, agent_key = _key("user", OLD), _key("user_agent", OLD)
    for key in (user_key, agent_key):
        (workers / worker_dir_name(key) / "credentials").mkdir(parents=True)
        (workers / worker_dir_name(key) / ".shared_credentials").mkdir()
    _write_json(
        workers / WORKER_CONTROL_DIRNAME / worker_dir_name(user_key) / "metadata" / "worker.json",
        {
            "worker_id": "mindroom-worker-old",
            "worker_key": user_key,
        },
    )
    (workers / PROJECTED_CONFIGS_DIRNAME / worker_dir_name(user_key)).mkdir(parents=True)
    _oauth_store(_requester_store(storage, OLD), agent_key)
    unknown = workers / "v1_default_user_@old_x_legacy-0123456789abcdef"
    unknown.mkdir()
    manager = _FakeDockerManager()
    leases: list[_FakeLease] = []

    def lease(paths: RuntimePaths, *, runtime_config: Config | None) -> _FakeLease:
        assert paths == runtime_paths
        assert runtime_config is not None
        leases.append(_FakeLease(manager))
        return leases[-1]

    monkeypatch.setattr(account_files, "lease_configured_primary_worker_manager", lease)
    before = tree_snapshot(tmp_path)

    steps, reports = _steps(runtime_paths)

    assert tree_snapshot(tmp_path) == before
    assert leases == []
    assert sum(isinstance(step, FinalAction) for step in steps) == 2
    assert any(str(unknown) in report for report in reports)

    _apply(runtime_paths)

    for old_key, new_key in ((user_key, _key("user", NEW)), (agent_key, _key("user_agent", NEW))):
        assert not (workers / worker_dir_name(old_key)).exists()
        assert (workers / worker_dir_name(new_key) / "credentials").is_dir()
        assert (workers / worker_dir_name(new_key) / ".shared_credentials").is_dir()
    assert not (workers / WORKER_CONTROL_DIRNAME / worker_dir_name(user_key)).exists()
    assert not (workers / PROJECTED_CONFIGS_DIRNAME / worker_dir_name(user_key)).exists()
    assert _stored_worker_key(_requester_store(storage, NEW)) == _key("user_agent", NEW)
    assert unknown.is_dir()
    assert sorted(manager.retired) == sorted([user_key, agent_key])
    assert all(lease.released for lease in leases)


def test_private_instance_scope_moved_and_record_rewritten(tmp_path: Path) -> None:
    """A private scope owned by the old ID moves with both identity records, which then validate for the new ID."""
    runtime_paths = _install(tmp_path)
    storage = runtime_paths.storage_root
    old_key, new_key = _key("user_agent", OLD), _key("user_agent", NEW)
    ensure_private_instance_identity(storage, worker_key=old_key, requester_id=OLD)
    old_scope = private_instance_scope_root_path(storage, old_key)
    (old_scope / "code" / "workspace").mkdir(parents=True)
    (old_scope / "code" / "workspace" / "notes.md").write_text("notes", encoding="utf-8")
    other_key = _key("user_agent", "@alice:x")
    ensure_private_instance_identity(storage, worker_key=other_key, requester_id="@alice:x")
    (private_instance_scope_root_path(storage, other_key) / "code").mkdir()

    _apply(runtime_paths)

    new_scope = private_instance_scope_root_path(storage, new_key)
    assert not old_scope.exists()
    assert not primary_records_dir(old_scope, runtime_paths).exists()
    assert (new_scope / "code" / "workspace" / "notes.md").read_text() == "notes"
    assert load_private_instance_identity(storage, new_scope) == PrivateInstanceIdentity(new_key, NEW)
    instances = private_instances_for_agent(storage, "code", "user_agent")
    assert {instance.requester_id for instance in instances} == {"@alice:x", NEW}
    other_scope = private_instance_scope_root_path(storage, other_key)
    assert load_private_instance_identity(storage, other_scope) == PrivateInstanceIdentity(other_key, "@alice:x")


def test_private_instance_session_mirror_and_aliases_follow_the_scope(tmp_path: Path) -> None:
    """With separate session storage, the scope's session mirror moves too, and links to the moved scope are dropped.

    The links are the aliases a startup upgrade published at the old owner's pre-``~`` key; MindRoom only accepts
    such an alias at the historical key of the scope's current owner, which the new ID never had.
    """
    runtime_paths = _install(tmp_path)
    runtime_paths = replace(
        runtime_paths,
        process_env={**runtime_paths.process_env, "MINDROOM_SESSION_STORAGE_PATH": str(tmp_path / "sessions")},
    )
    storage = runtime_paths.storage_root
    old_key, new_key = _key("user_agent", OLD), _key("user_agent", NEW)
    ensure_private_instance_identity(storage, worker_key=old_key, requester_id=OLD)
    old_scope, new_scope = (private_instance_scope_root_path(storage, key) for key in (old_key, new_key))
    old_sessions = resolve_session_state_root(old_scope / "code", runtime_paths)
    assert old_sessions.is_relative_to(tmp_path / "sessions")
    (old_sessions / "sessions").mkdir(parents=True)
    (old_sessions / "sessions" / "code.db").write_text("history", encoding="utf-8")
    alias_name = worker_dir_name(historical_private_instance_worker_key(old_key, OLD))
    aliases = [old_scope.parent / alias_name, old_sessions.parent.parent / alias_name]
    for alias in aliases:
        alias.symlink_to(old_scope.name, target_is_directory=True)

    steps, _reports = _steps(runtime_paths)

    assert [step.path for step in steps if isinstance(step, DropPath)] == aliases

    _apply(runtime_paths)

    assert not old_sessions.parent.exists()
    new_sessions = resolve_session_state_root(new_scope / "code", runtime_paths)
    assert (new_sessions / "sessions" / "code.db").read_text() == "history"
    assert not any(os.path.lexists(alias) for alias in aliases)
    assert load_private_instance_identity(storage, new_scope) == PrivateInstanceIdentity(new_key, NEW)


def test_personal_room_and_intent_files(tmp_path: Path) -> None:
    """Personal-room records take the new ID with the server's boundary rule; the old ID's send intents drop."""
    runtime_paths = _install(tmp_path)
    adopted_path = personal_room_record_path(runtime_paths, "router", "@alice:x")
    write_personal_room(
        adopted_path,
        PersonalRoomRecord(
            user_id="@alice:x",
            alias="#alice:x",
            source_room_id="!source:x",
            room_id="!room:x",
            welcome_content={"body": f"Meet {OLD} and @old:x.example.org.", "m.mentions": {"user_ids": [OLD]}},
            adoption=PersonalRoomAdoption(creator_user_id="@alice:x", agent_user_id=OLD, router_user_id="@router:x"),
        ),
    )
    own_path = personal_room_record_path(runtime_paths, AGENT, OLD)
    write_personal_room(own_path, PersonalRoomRecord(user_id=OLD, alias="#old:x", source_room_id="!source:x"))
    untouched_path = personal_room_record_path(runtime_paths, "router", "@bob:x")
    write_personal_room(
        untouched_path,
        PersonalRoomRecord(user_id="@bob:x", alias="#bob:x", source_room_id="!source:x"),
    )
    untouched = untouched_path.read_bytes()
    sends = runtime_paths.control_state_root / "matrix_message_sends"
    intent = {"payload": None, "transaction_id": "t", "device_id": "DEVICE", "recipient": None}
    _write_json(sends / "old.json", {"version": 1, "sends": {"k": {**intent, "sender_id": OLD}}})
    _write_json(sends / "other.json", {"version": 1, "sends": {"k": {**intent, "sender_id": "@router:x"}}})

    _apply(runtime_paths)

    adopted = read_personal_room(adopted_path)
    assert adopted is not None
    assert adopted.adoption is not None
    assert adopted.adoption.agent_user_id == NEW
    assert adopted.adoption.router_user_id == "@router:x"
    assert adopted.welcome_content == {
        "body": f"Meet {NEW} and @old:x.example.org.",
        "m.mentions": {"user_ids": [NEW]},
    }
    assert not own_path.exists()
    own = read_personal_room(personal_room_record_path(runtime_paths, AGENT, NEW))
    assert own is not None
    assert own.user_id == NEW
    assert untouched_path.read_bytes() == untouched
    assert json.loads((sends / "old.json").read_text()) == {"version": 1, "sends": {}}
    assert json.loads((sends / "other.json").read_text()) == {
        "version": 1,
        "sends": {"k": {**intent, "sender_id": "@router:x"}},
    }


def test_send_intents_of_the_new_id_stay(tmp_path: Path) -> None:
    """A send store is keyed by requester, agent, and room, so it can hold sends of both IDs; only the old ID's go.

    A pending send MindRoom recorded under the new ID keeps its transaction ID, so a retry cannot send it twice.
    """
    runtime_paths = _install(tmp_path)
    path = runtime_paths.control_state_root / "matrix_message_sends" / "mixed.json"
    common = {"recipient": None, "recipient_user_id": None, "thread_id": None, "starts_thread": False}
    old_receipt = {
        **common,
        "payload": None,
        "transaction_id": "matrix-message-old",
        "sender_id": OLD,
        "device_id": "OLDDEVICE",
        "event_id": "$sent:x",
        "completed_at": 1.0,
    }
    new_pending = {
        **common,
        "payload": {"msgtype": "m.text", "body": "hi"},
        "transaction_id": "matrix-message-new",
        "sender_id": NEW,
        "device_id": "NEWDEVICE",
        "event_id": None,
        "completed_at": None,
    }
    store = {"version": 1, "scope": ["@alice:x", AGENT, "!room:x"], "sends": {"a": old_receipt, "b": new_pending}}
    _write_json(path, store)

    _apply(runtime_paths)

    assert json.loads(path.read_text()) == {**store, "sends": {"b": new_pending}}
    (backup,) = (runtime_paths.storage_root / "agent-rename-backups").iterdir()
    assert [json.loads(saved.read_text()) for saved in backup.glob("*/mixed.json")] == [store]


def test_reports_encryption_keys_and_desktop_pin(tmp_path: Path) -> None:
    """The old crypto store stays and is reported, as is a desktop bridge pinned to the old ID."""
    runtime_paths = _install(tmp_path)
    old_store = olm_store_dir(OLD, runtime_paths)
    old_store.mkdir(parents=True)
    _write_json(native_config_path(runtime_paths.storage_root), {"controller": {"user_id": OLD, "device_id": "D"}})

    reports = _apply(runtime_paths)

    assert old_store.is_dir()
    assert any(str(old_store) in report for report in reports)
    assert any("re-pair the desktop bridge" in report for report in reports)
    assert any("history" in report for report in reports)


def test_reports_without_crypto_store_or_desktop_pin(tmp_path: Path) -> None:
    """A fresh install only gets the line about history records."""
    runtime_paths = _install(tmp_path)
    _write_json(native_config_path(runtime_paths.storage_root), {"controller": {"user_id": "@alice:x"}})

    _steps_list, reports = _steps(runtime_paths)

    assert len(reports) == 1
    assert "history" in reports[0]
