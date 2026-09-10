"""Tests for the atomic SQLite OAuth credential store."""

from __future__ import annotations

import asyncio
import base64
import multiprocessing
import os
import shutil
import sqlite3
import stat
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest

import mindroom.durable_write as durable_write_module
import mindroom.oauth.credential_store as credential_store_module
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials import get_runtime_credentials_manager, save_scoped_credentials
from mindroom.oauth.credential_lifecycle import OAuthCredentialContext
from mindroom.oauth.credential_store import (
    _oauth_credential_database_path,
    oauth_credential_reader,
    oauth_credential_transaction,
)
from mindroom.oauth.providers import OAuthProvider, OAuthProviderError
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target

if TYPE_CHECKING:
    from multiprocessing.synchronize import Barrier, Event

    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, WorkerScope


class _Provider:
    id = "demo_provider"
    credential_service = "demo_oauth"
    requester_scoped_credentials = True


def _hold_sqlite_transaction(
    database_path: str,
    ready: Event,
    release: Event,
    *,
    write: bool,
) -> None:
    connection = sqlite3.connect(database_path, isolation_level=None, timeout=0)
    try:
        connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        connection.execute("SELECT generation FROM oauth_credential_state WHERE singleton = 1").fetchone()
        ready.set()
        release.wait()
        connection.execute("ROLLBACK")
    finally:
        connection.close()


def _commit_sqlite_generation(database_path: str, committing: Event, committed: Event) -> None:
    """Publish a generation while another process keeps COMMIT in the pending-lock window."""
    connection = sqlite3.connect(database_path, isolation_level=None, timeout=5)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE oauth_credential_state SET generation = ? WHERE singleton = 1",
            ("committed-generation",),
        )
        committing.set()
        connection.execute("COMMIT")
        committed.set()
    finally:
        connection.close()


async def _wait_for_sqlite_pending_commit(database_path: Path) -> None:
    """Wait until a committing writer prevents a new reader from taking a shared lock."""
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        probe = sqlite3.connect(database_path, isolation_level=None, timeout=0)
        try:
            probe.execute("BEGIN")
            probe.execute("PRAGMA user_version").fetchone()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            return
        finally:
            if probe.in_transaction:
                probe.execute("ROLLBACK")
            probe.close()
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.01)


def _open_cold_store_after_absence_barrier(storage_path: str, barrier: Barrier) -> None:
    """Force concurrent creators past the database absence check before either creates it."""
    context = _context(Path(storage_path))
    database_path = _oauth_credential_database_path(context)
    original_exists = Path.exists
    observed_absence = False

    def synchronized_exists(path: Path) -> bool:
        nonlocal observed_absence
        exists = original_exists(path)
        if path == database_path and not exists and not observed_absence:
            observed_absence = True
            barrier.wait(timeout=5)
        return exists

    async def open_store() -> None:
        async with oauth_credential_transaction(context) as transaction:
            await transaction.commit()

    with patch.object(Path, "exists", synchronized_exists):
        asyncio.run(open_store())


def _runtime_paths(tmp_path: Path, *, encryption_key: str | None = None) -> RuntimePaths:
    process_env = {"MINDROOM_CREDENTIALS_ENCRYPTION_KEY": encryption_key} if encryption_key is not None else {}
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env=process_env,
    )


def _target(requester_id: str) -> ResolvedWorkerTarget:
    return resolve_worker_target(
        "user",
        "code",
        ToolExecutionIdentity(
            channel="matrix",
            agent_name="code",
            requester_id=requester_id,
            room_id="!room:example.test",
            thread_id="$thread",
            resolved_thread_id="$thread",
            session_id=None,
            tenant_id="tenant",
            account_id=None,
        ),
    )


def _context(
    tmp_path: Path,
    *,
    requester_id: str = "@alice:example.test",
    encryption_key: str | None = None,
) -> OAuthCredentialContext:
    runtime_paths = _runtime_paths(tmp_path, encryption_key=encryption_key)
    return OAuthCredentialContext(
        provider=cast("OAuthProvider", _Provider()),
        runtime_paths=runtime_paths,
        credentials_manager=get_runtime_credentials_manager(runtime_paths),
        worker_target=_target(requester_id),
    )


async def _publish(context: OAuthCredentialContext, token: str) -> tuple[str, str]:
    async with oauth_credential_transaction(context) as transaction:
        record = transaction.publish(
            {"token": token, "refresh_token": f"refresh-{token}"},
            advance_connection_generation=True,
        )
        await transaction.commit()
        return record.generation, record.connection_generation


@pytest.mark.asyncio
async def test_encrypted_credentials_are_atomic_and_private(tmp_path: Path) -> None:
    """SQLite stores ciphertext with private modes while state and token commit together."""
    encryption_key = base64.urlsafe_b64encode(b"k" * 32).decode()
    context = _context(tmp_path, encryption_key=encryption_key)

    generation, connection_generation = await _publish(context, "secret-access")

    database_path = _oauth_credential_database_path(context)
    assert database_path.stat().st_mode & 0o777 == 0o600
    assert database_path.parent.stat().st_mode & 0o777 == 0o700
    assert b"secret-access" not in database_path.read_bytes()
    async with oauth_credential_transaction(context) as transaction:
        snapshot = transaction.snapshot()
        await transaction.commit()
    assert snapshot.credentials == {"token": "secret-access", "refresh_token": "refresh-secret-access"}
    assert snapshot.generation == generation
    assert snapshot.connection_generation == connection_generation


@pytest.mark.asyncio
async def test_new_database_skips_directory_fsync_when_unsupported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A platform without directory fsync can still create a credential database."""
    context = _context(tmp_path)
    database_path = _oauth_credential_database_path(context)
    original_fsync = os.fsync

    def reject_directory_fsync(file_descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
            msg = "directory fsync is unsupported"
            raise OSError(msg)
        original_fsync(file_descriptor)

    monkeypatch.setattr(durable_write_module, "_DIRECTORY_FSYNC_SUPPORTED", False)
    monkeypatch.setattr(os, "fsync", reject_directory_fsync)

    async with oauth_credential_transaction(context) as transaction:
        await transaction.commit()

    assert database_path.stat().st_size > 0


@pytest.mark.skipif(os.name == "nt", reason="directory fsync is not supported on Windows")
@pytest.mark.asyncio
async def test_failed_database_directory_publication_is_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed directory flush must remain pending when the empty file survives."""
    context = _context(tmp_path)
    database_path = _oauth_credential_database_path(context)
    original_fsync = os.fsync
    directory_fsync_attempts = 0

    def fail_first_directory_fsync(file_descriptor: int) -> None:
        nonlocal directory_fsync_attempts
        if stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
            directory_fsync_attempts += 1
            if directory_fsync_attempts == 1:
                msg = "directory fsync failed"
                raise OSError(msg)
        original_fsync(file_descriptor)

    monkeypatch.setattr(durable_write_module, "_DIRECTORY_FSYNC_SUPPORTED", True)
    monkeypatch.setattr(os, "fsync", fail_first_directory_fsync)

    with pytest.raises(OSError, match="directory fsync failed"):
        credential_store_module._prepare_database_path(database_path)

    async with oauth_credential_transaction(context) as transaction:
        await transaction.commit()

    assert directory_fsync_attempts == 2
    assert database_path.stat().st_size > 0


def test_multiprocess_cold_start_admits_both_database_creators(tmp_path: Path) -> None:
    """Two processes may create the same new credential scope without a losing-creator error."""
    process_context = multiprocessing.get_context("spawn")
    barrier = process_context.Barrier(2)
    creators = [
        process_context.Process(
            target=_open_cold_store_after_absence_barrier,
            args=(str(tmp_path), barrier),
        )
        for _ in range(2)
    ]

    for creator in creators:
        creator.start()
    for creator in creators:
        creator.join(timeout=10)
        if creator.is_alive():
            creator.terminate()
            creator.join()

    assert [creator.exitcode for creator in creators] == [0, 0]


@pytest.mark.asyncio
async def test_copied_database_is_rejected_by_scope_binding(tmp_path: Path) -> None:
    """A database copied from another requester cannot be adopted."""
    alice = _context(tmp_path, requester_id="@alice:example.test")
    bob = _context(tmp_path, requester_id="@bob:example.test")
    await _publish(alice, "alice")
    bob_path = _oauth_credential_database_path(bob)
    bob_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_oauth_credential_database_path(alice), bob_path)

    with pytest.raises(OAuthProviderError, match="different credential scope"):
        async with oauth_credential_transaction(bob):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_scope", ["user", "user_agent"])
@pytest.mark.parametrize(
    ("tenant_id", "account_id", "legacy_tenant"),
    [(None, None, "default"), ("tenant", "account", "tenant"), (None, "account", "account")],
)
async def test_requester_key_upgrade_preserves_primary_runtime_oauth_credentials(
    tmp_path: Path,
    worker_scope: WorkerScope,
    tenant_id: str | None,
    account_id: str | None,
    legacy_tenant: str,
) -> None:
    """Previously stored credentials remain readable after requester key encoding changes."""
    context = _context(tmp_path, requester_id="@alice:example.org")
    assert context.worker_target is not None
    assert context.worker_target.execution_identity is not None
    identity = replace(
        context.worker_target.execution_identity,
        agent_name="transport",
        tenant_id=tenant_id,
        account_id=account_id,
    )
    target = resolve_worker_target(worker_scope, "assistant", identity)
    context = replace(context, worker_target=target)
    legacy_key = f"v1:{legacy_tenant}:{worker_scope}:@alice:example.org"
    if worker_scope == "user_agent":
        legacy_key += ":assistant"
    legacy_context = replace(context, worker_target=replace(target, worker_key=legacy_key))
    generations = await _publish(legacy_context, "existing-access")
    database_path = _oauth_credential_database_path(legacy_context)
    assert database_path == _oauth_credential_database_path(context)
    original_bytes = database_path.read_bytes()

    async with oauth_credential_reader(context) as reader:
        snapshot = reader.snapshot()
        assert snapshot.credentials == {"token": "existing-access", "refresh_token": "refresh-existing-access"}
        assert (snapshot.generation, snapshot.connection_generation) == generations

    assert database_path.read_bytes() == original_bytes
    async with oauth_credential_transaction(context) as transaction:
        assert transaction.snapshot().credentials == snapshot.credentials
        await transaction.commit()
    assert database_path.read_bytes() == original_bytes
    async with oauth_credential_reader(legacy_context) as reader:
        assert reader.snapshot().credentials == snapshot.credentials
    async with oauth_credential_transaction(context) as transaction:
        refreshed = transaction.publish({"token": "refreshed"}, advance_connection_generation=False)
        assert refreshed.generation != generations[0]
        assert refreshed.connection_generation == generations[1]
        await transaction.commit()
    async with oauth_credential_reader(legacy_context) as reader:
        assert reader.snapshot().credentials == {"token": "refreshed"}
    async with oauth_credential_transaction(context) as transaction:
        assert transaction.reset("reset-upgraded")
        await transaction.commit()
    async with oauth_credential_reader(legacy_context) as reader:
        assert reader.snapshot().credentials is None


@pytest.mark.asyncio
async def test_tagged_oauth_scope_binding_reads_literal_v1_database(tmp_path: Path) -> None:
    """The released v1 binding remains readable without rewriting its key or receipts."""
    context = _context(tmp_path, requester_id="@alice:example.org")
    assert context.worker_target is not None
    database_path = _oauth_credential_database_path(context)
    database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = context.credentials_manager.encode_credentials(
        context.provider.credential_service,
        {"token": "tagged-access", "refresh_token": "tagged-refresh", "provider_extra": {"keep": 1}},
    )
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE oauth_credential_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                provider_id TEXT NOT NULL,
                credential_service TEXT NOT NULL,
                worker_scope TEXT NOT NULL,
                worker_key TEXT NOT NULL,
                routing_agent_name TEXT NOT NULL,
                generation TEXT NOT NULL,
                connection_generation TEXT NOT NULL,
                credential_payload BLOB,
                credential_present INTEGER NOT NULL CHECK (credential_present IN (0, 1)),
                credential_unreadable INTEGER NOT NULL CHECK (credential_unreadable IN (0, 1))
            );
            CREATE TABLE oauth_reset_operations (
                operation_id TEXT PRIMARY KEY,
                credential_existed INTEGER NOT NULL CHECK (credential_existed IN (0, 1))
            );
            PRAGMA user_version = 1;
            """,
        )
        connection.execute(
            """
            INSERT INTO oauth_credential_state VALUES (
                1, 'demo_provider', 'demo_oauth', 'user',
                'v1:tenant:user:@alice:example.org', '',
                'tagged-generation', 'tagged-connection-generation', ?, 1, 0
            )
            """,
            (payload,),
        )
        connection.execute("INSERT INTO oauth_reset_operations VALUES ('tagged-reset', 1)")
    original_bytes = database_path.read_bytes()
    expected_credentials = {
        "token": "tagged-access",
        "refresh_token": "tagged-refresh",
        "provider_extra": {"keep": 1},
    }

    async with oauth_credential_reader(context) as reader:
        snapshot = reader.snapshot()
        assert snapshot.credentials == expected_credentials
        assert (snapshot.generation, snapshot.connection_generation) == (
            "tagged-generation",
            "tagged-connection-generation",
        )
        assert reader.reset_operation_result("tagged-reset") is True
    assert database_path.read_bytes() == original_bytes

    async with oauth_credential_transaction(context) as transaction:
        assert transaction.snapshot().credentials == expected_credentials
        assert transaction.reset_operation_result("tagged-reset") is True
        await transaction.commit()
    assert database_path.read_bytes() == original_bytes

    legacy_context = replace(
        context,
        worker_target=replace(context.worker_target, worker_key="v1:tenant:user:@alice:example.org"),
    )
    async with oauth_credential_reader(legacy_context) as reader:
        assert reader.snapshot().credentials == expected_credentials
        assert reader.reset_operation_result("tagged-reset") is True


async def _assert_scope_rejected(context: OAuthCredentialContext) -> None:
    for open_store in (oauth_credential_reader, oauth_credential_transaction):
        with pytest.raises(OAuthProviderError, match="different credential scope"):
            async with open_store(context):
                pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_id", "other_provider"),
        ("credential_service", "other_oauth"),
        ("routing_agent_name", "other_agent"),
        ("worker_scope", "user_agent"),
        ("worker_key", "v1:tenant:user:@bob:example.test"),
        ("worker_key", "v1:other:user:@alice:example.test"),
        ("worker_key", "v1:tenant:user_agent:@alice:example.test:code"),
        ("worker_key", "v2:tenant:user:@alice:example.test"),
        ("worker_key", "v1:tenant:user:~~@alice:example.test"),
        ("worker_key", "v1:tenant:user:~%40alice:example.test"),
        ("worker_key", "v1:tenant:user:~@bob:example.test"),
    ],
)
async def test_legacy_binding_compatibility_rejects_other_scopes(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    """Legacy spelling never exempts other stored scope fields from validation."""
    context = _context(tmp_path)
    assert context.worker_target is not None
    legacy = replace(
        context,
        worker_target=replace(context.worker_target, worker_key="v1:tenant:user:@alice:example.test"),
    )
    await _publish(legacy, "existing")
    with sqlite3.connect(_oauth_credential_database_path(context)) as connection:
        connection.execute(f"UPDATE oauth_credential_state SET {field} = ? WHERE singleton = 1", (value,))  # noqa: S608
    await _assert_scope_rejected(context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requester", "legacy_requester"),
    [
        ("alice/foo", "alice_foo"),
        (" alice ", "alice"),
        ("_alice", "alice"),
        ("alice_", "alice"),
        ("alice%foo", "alice_foo"),
        ("~alice", "alice"),
        ("álîce", "l_ce"),
    ],
)
async def test_legacy_binding_compatibility_rejects_lossy_requesters(
    tmp_path: Path,
    requester: str,
    legacy_requester: str,
) -> None:
    """A requester that legacy normalization changed must not use compatibility."""
    context = _context(tmp_path, requester_id=requester)
    assert context.worker_target is not None
    legacy = replace(
        context,
        worker_target=replace(context.worker_target, worker_key=f"v1:tenant:user:{legacy_requester}"),
    )
    await _publish(legacy, "existing")
    await _assert_scope_rejected(context)


@pytest.mark.asyncio
async def test_legacy_requester_collision_keeps_distinct_raw_identity_paths(tmp_path: Path) -> None:
    """Lossless upgrade reads its own raw-identity store without importing a colliding store."""
    lossy = _context(tmp_path, requester_id="alice/foo")
    lossless = _context(tmp_path, requester_id="alice_foo")
    for context, token in ((lossy, "lossy"), (lossless, "lossless")):
        assert context.worker_target is not None
        legacy = replace(
            context,
            worker_target=replace(context.worker_target, worker_key="v1:tenant:user:alice_foo"),
        )
        await _publish(legacy, token)
    assert _oauth_credential_database_path(lossy) != _oauth_credential_database_path(lossless)
    await _assert_scope_rejected(lossy)
    async with oauth_credential_reader(lossless) as reader:
        assert reader.snapshot().credentials == {"token": "lossless", "refresh_token": "refresh-lossless"}


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_target", ["worker_key", "tenant_id", "account_id"])
async def test_legacy_binding_compatibility_requires_canonical_current_target(
    tmp_path: Path,
    invalid_target: str,
) -> None:
    """Forged current keys and inconsistent identity metadata cannot authorize legacy access."""
    context = _context(tmp_path)
    assert context.worker_target is not None
    legacy = replace(
        context,
        worker_target=replace(context.worker_target, worker_key="v1:tenant:user:@alice:example.test"),
    )
    await _publish(legacy, "existing")
    target = replace(context.worker_target, **{invalid_target: "other"})
    await _assert_scope_rejected(replace(context, worker_target=target))


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["worker", "runtime", "database"])
async def test_legacy_binding_compatibility_requires_primary_runtime_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
) -> None:
    """Legacy access is limited to the current primary runtime's canonical database path."""
    context = _context(tmp_path)
    if location == "worker":
        context = replace(context, credentials_manager=context.credentials_manager.for_worker("other-worker"))
    assert context.worker_target is not None
    legacy = replace(
        context,
        worker_target=replace(context.worker_target, worker_key="v1:tenant:user:@alice:example.test"),
    )
    await _publish(legacy, "existing")
    if location == "runtime":
        context = replace(context, runtime_paths=_runtime_paths(tmp_path / "other-runtime"))
    elif location == "database":
        original_path = _oauth_credential_database_path(context)
        foreign_path = original_path.with_name("foreign.sqlite3")
        shutil.copyfile(original_path, foreign_path)
        monkeypatch.setattr(credential_store_module, "_oauth_credential_database_path", lambda _context: foreign_path)
    await _assert_scope_rejected(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_depth", [0, 1, 2])
@pytest.mark.parametrize("destination", ["external", "same-runtime"])
async def test_legacy_binding_rejects_redirected_scoped_directory(
    tmp_path: Path,
    parent_depth: int,
    destination: str,
) -> None:
    """Symlinked credential directories cannot authorize a legacy store at another location."""
    context = _context(tmp_path / "runtime")
    assert context.worker_target is not None
    legacy = replace(
        context,
        worker_target=replace(context.worker_target, worker_key="v1:tenant:user:@alice:example.test"),
    )
    await _publish(legacy, "external")
    database_path = _oauth_credential_database_path(context)
    redirected_directory = database_path.parents[parent_depth]
    destination_root = tmp_path if destination == "external" else context.runtime_paths.storage_root
    moved_directory = destination_root / "redirected-credentials"
    redirected_directory.rename(moved_directory)
    redirected_directory.symlink_to(moved_directory, target_is_directory=True)
    original_bytes = database_path.read_bytes()

    await _assert_scope_rejected(context)

    assert database_path.read_bytes() == original_bytes


@pytest.mark.asyncio
async def test_legacy_binding_supports_configured_runtime_root_symlink(tmp_path: Path) -> None:
    """Resolving a configured runtime root preserves its legitimate legacy credentials."""
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    runtime_alias = tmp_path / "runtime-alias"
    runtime_alias.symlink_to(runtime_root, target_is_directory=True)
    context = _context(runtime_alias)
    assert context.runtime_paths.storage_root == runtime_root
    assert context.worker_target is not None
    legacy = replace(
        context,
        worker_target=replace(context.worker_target, worker_key="v1:tenant:user:@alice:example.test"),
    )
    await _publish(legacy, "existing")
    async with oauth_credential_reader(context) as reader:
        assert reader.snapshot().credentials == {"token": "existing", "refresh_token": "refresh-existing"}


@pytest.mark.asyncio
@pytest.mark.parametrize("unsupported", ["shared", "unscoped", "service"])
async def test_legacy_binding_compatibility_rejects_unsupported_storage(
    tmp_path: Path,
    unsupported: str,
) -> None:
    """Shared, unscoped, and non-OAuth stores retain strict worker-key equality."""
    context = _context(tmp_path)
    assert context.worker_target is not None
    if unsupported in {"shared", "unscoped"}:
        target = resolve_worker_target(
            "shared" if unsupported == "shared" else None,
            "assistant",
            context.worker_target.execution_identity,
        )
        context = replace(context, worker_target=target)
    else:
        provider = _Provider()
        provider.credential_service = "demo_service"
        context = replace(context, provider=cast("OAuthProvider", provider))
    assert context.worker_target is not None
    legacy = replace(
        context,
        worker_target=replace(context.worker_target, worker_key="v1:tenant:user:@alice:example.test"),
    )
    await _publish(legacy, "existing")
    if unsupported == "service":
        current_path = _oauth_credential_database_path(context)
        shutil.copyfile(_oauth_credential_database_path(legacy), current_path)
    await _assert_scope_rejected(context)


@pytest.mark.asyncio
async def test_new_stores_mint_unique_generation_nonces(tmp_path: Path) -> None:
    """Independent stores must never reuse cache-fencing generation identities."""
    first = _context(tmp_path / "first")
    second = _context(tmp_path / "second")

    async with oauth_credential_transaction(first) as transaction:
        first_generations = transaction.generations()
        await transaction.commit()
    async with oauth_credential_transaction(second) as transaction:
        second_generations = transaction.generations()
        await transaction.commit()

    assert first_generations.generation != second_generations.generation
    assert first_generations.connection_generation != second_generations.connection_generation


@pytest.mark.asyncio
async def test_sqlite_lock_admission_has_a_bounded_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stuck external lock must fail instead of polling forever."""

    class _LockedConnection:
        @staticmethod
        def execute(_statement: str) -> None:
            message = "database is locked"
            raise sqlite3.OperationalError(message)

    monkeypatch.setattr(credential_store_module, "_LOCK_WAIT_TIMEOUT_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(credential_store_module, "_sqlite_lock_error", lambda _exc: True)

    with pytest.raises(OAuthProviderError, match="Timed out waiting for OAuth credential store"):
        await asyncio.wait_for(
            credential_store_module._begin_immediate(cast("sqlite3.Connection", _LockedConnection())),
            timeout=0.1,
        )


@pytest.mark.asyncio
async def test_database_directory_permission_failure_uses_oauth_error_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Filesystem permission failures should not escape the OAuth store abstraction."""
    context = _context(tmp_path)
    database_path = _oauth_credential_database_path(context)
    database_parent = database_path.parent
    original_chmod = Path.chmod

    def deny_chmod(path: Path, mode: int) -> None:
        if path == database_parent:
            msg = "provider-controlled-path-detail"
            raise PermissionError(msg)
        original_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", deny_chmod)
    monkeypatch.setattr(credential_store_module, "_oauth_credential_database_path", lambda _context: database_path)

    with pytest.raises(OAuthProviderError, match="could not prepare its private directory"):
        async with oauth_credential_transaction(context):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("commit_normalization", [False, True])
async def test_legacy_publication_marker_normalizes_without_changing_credentials_or_generations(
    tmp_path: Path,
    encrypted: bool,
    commit_normalization: bool,
) -> None:
    """Only a committed writer strips the marker, preserving credentials and generations."""
    encryption_key = base64.urlsafe_b64encode(b"k" * 32).decode() if encrypted else None
    context = _context(tmp_path, encryption_key=encryption_key)
    async with oauth_credential_transaction(context) as transaction:
        await transaction.commit()
    database_path = _oauth_credential_database_path(context)
    legacy_credentials = {
        "token": "old-access",
        "refresh_token": "old-refresh",
        "provider_extra": {"keep": 1},
        "_mindroom_oauth_publication": {"generation": "obsolete"},
    }
    expected_credentials = {
        "token": "old-access",
        "refresh_token": "old-refresh",
        "provider_extra": {"keep": 1},
    }
    encoded = context.credentials_manager.encode_credentials(
        context.provider.credential_service,
        legacy_credentials,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE oauth_credential_state
            SET generation = 'legacy-generation',
                connection_generation = 'legacy-connection-generation',
                credential_payload = ?, credential_present = 1, credential_unreadable = 0
            WHERE singleton = 1
            """,
            (encoded,),
        )
    before_read = database_path.read_bytes()

    async with oauth_credential_reader(context) as reader:
        snapshot = reader.snapshot()
        assert snapshot.credentials == expected_credentials
        assert (snapshot.generation, snapshot.connection_generation) == (
            "legacy-generation",
            "legacy-connection-generation",
        )
    assert database_path.read_bytes() == before_read

    async with oauth_credential_transaction(context) as transaction:
        snapshot = transaction.snapshot()
        assert snapshot.credentials == expected_credentials
        assert (snapshot.generation, snapshot.connection_generation) == (
            "legacy-generation",
            "legacy-connection-generation",
        )
        if commit_normalization:
            await transaction.commit()

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT credential_payload, generation, connection_generation
            FROM oauth_credential_state WHERE singleton = 1
            """,
        ).fetchone()
    assert row is not None
    durable_credentials = context.credentials_manager.decode_credentials(
        context.provider.credential_service,
        bytes(row[0]),
    )
    assert durable_credentials == (expected_credentials if commit_normalization else legacy_credentials)
    assert row[1:] == ("legacy-generation", "legacy-connection-generation")
    async with oauth_credential_reader(context) as reader:
        reopened = reader.snapshot()
        assert reopened.credentials == expected_credentials
        assert (reopened.generation, reopened.connection_generation) == (
            "legacy-generation",
            "legacy-connection-generation",
        )

    caller_credentials = dict(legacy_credentials)
    async with oauth_credential_transaction(context) as transaction:
        published = transaction.publish(caller_credentials, advance_connection_generation=False)
        await transaction.commit()
    assert caller_credentials == legacy_credentials
    assert published.credentials == expected_credentials


@pytest.mark.asyncio
@pytest.mark.parametrize("encrypted", [False, True])
async def test_json_only_credentials_and_sidecars_are_ignored(tmp_path: Path, encrypted: bool) -> None:
    """A JSON-only connection requires reconnect while obsolete files remain inert."""
    context = _context(tmp_path, encryption_key=base64.urlsafe_b64encode(b"k" * 32).decode() if encrypted else None)
    save_scoped_credentials(
        context.provider.credential_service,
        {"token": "json-only"},
        credentials_manager=context.credentials_manager,
        worker_target=context.worker_target,
    )
    legacy_path = context.credentials_manager.for_primary_runtime_scope(
        "@alice:example.test",
        None,
    ).get_credentials_path(context.provider.credential_service)
    sidecars = (
        legacy_path.with_name(f"{legacy_path.name}.oauth-generation.json"),
        legacy_path.with_name(f"{legacy_path.name}.oauth-operation.lock"),
        legacy_path.with_name(f"{legacy_path.name}.oauth-refresh.lock"),
    )
    for sidecar in sidecars:
        sidecar.write_text("obsolete", encoding="utf-8")
    original_files = {path: path.read_bytes() for path in (legacy_path, *sidecars)}

    async with oauth_credential_reader(context) as reader:
        assert reader.snapshot().credentials is None

    async with oauth_credential_transaction(context) as transaction:
        transaction.publish({"token": "reconnected"}, advance_connection_generation=True)
        await transaction.commit()
    async with oauth_credential_reader(context) as reader:
        assert reader.snapshot().credentials == {"token": "reconnected"}

    async with oauth_credential_transaction(context) as transaction:
        assert transaction.reset("reset-operation") is True
        await transaction.commit()
    async with oauth_credential_reader(context) as reader:
        assert reader.snapshot().credentials is None

    assert {path: path.read_bytes() for path in original_files} == original_files


def test_database_symlink_is_rejected(tmp_path: Path) -> None:
    """The store never follows a database symlink outside its private scope."""
    context = _context(tmp_path)
    database_path = _oauth_credential_database_path(context)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "outside.sqlite3"
    sqlite3.connect(target).close()
    database_path.symlink_to(target)

    async def open_store() -> None:
        async with oauth_credential_transaction(context):
            pass

    with pytest.raises(OAuthProviderError, match="database path"):
        asyncio.run(open_store())


@pytest.mark.asyncio
async def test_cross_process_writer_wait_is_cancellable_without_leaking_transaction(tmp_path: Path) -> None:
    """A second process owns the same SQLite lock and a cancelled waiter leaves no lock behind."""
    context = _context(tmp_path)
    await _publish(context, "initial")
    process_context = multiprocessing.get_context("spawn")
    ready = process_context.Event()
    release = process_context.Event()
    holder = process_context.Process(
        target=_hold_sqlite_transaction,
        args=(str(_oauth_credential_database_path(context)), ready, release),
        kwargs={"write": True},
    )
    holder.start()
    try:
        assert await asyncio.to_thread(ready.wait, 5)

        async def wait_for_store() -> None:
            async with oauth_credential_transaction(context) as transaction:
                await transaction.commit()

        waiter = asyncio.create_task(wait_for_store())
        await asyncio.sleep(0.1)
        assert not waiter.done()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    finally:
        release.set()
        await asyncio.to_thread(holder.join, 5)
        if holder.is_alive():
            holder.terminate()
            holder.join()
    assert holder.exitcode == 0
    async with oauth_credential_transaction(context) as transaction:
        assert transaction.snapshot().credentials is not None
        await transaction.commit()


@pytest.mark.asyncio
async def test_reader_blocked_commit_retries_same_transaction(tmp_path: Path) -> None:
    """A reader-blocked COMMIT retries without rolling back or republishing."""
    context = _context(tmp_path)
    await _publish(context, "initial")
    process_context = multiprocessing.get_context("spawn")
    ready = process_context.Event()
    release = process_context.Event()
    reader = process_context.Process(
        target=_hold_sqlite_transaction,
        args=(str(_oauth_credential_database_path(context)), ready, release),
        kwargs={"write": False},
    )
    reader.start()
    publish_calls = 0
    try:
        assert await asyncio.to_thread(ready.wait, 5)

        async def publish_once() -> None:
            nonlocal publish_calls
            async with oauth_credential_transaction(context) as transaction:
                publish_calls += 1
                transaction.publish({"token": "rotated"}, advance_connection_generation=False)
                await transaction.commit()

        publication = asyncio.create_task(publish_once())
        await asyncio.sleep(0.1)
        assert not publication.done()
        release.set()
        await publication
    finally:
        release.set()
        await asyncio.to_thread(reader.join, 5)
        if reader.is_alive():
            reader.terminate()
            reader.join()
    assert reader.exitcode == 0
    assert publish_calls == 1
    async with oauth_credential_transaction(context) as transaction:
        assert transaction.snapshot().credentials == {"token": "rotated"}
        await transaction.commit()


@pytest.mark.asyncio
async def test_reader_retries_while_writer_crosses_commit_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A reader arriving during a writer's pending COMMIT waits for the committed snapshot."""
    context = _context(tmp_path)
    await _publish(context, "initial")
    database_path = _oauth_credential_database_path(context)
    process_context = multiprocessing.get_context("spawn")
    reader_ready = process_context.Event()
    release_reader = process_context.Event()
    writer_committing = process_context.Event()
    writer_committed = process_context.Event()
    blocking_reader = process_context.Process(
        target=_hold_sqlite_transaction,
        args=(str(database_path), reader_ready, release_reader),
        kwargs={"write": False},
    )
    writer = process_context.Process(
        target=_commit_sqlite_generation,
        args=(str(database_path), writer_committing, writer_committed),
    )
    reader_connection_open = asyncio.Event()
    enter_reader = asyncio.Event()
    original_begin_read = credential_store_module._begin_read

    async def pause_before_read_lock(connection: sqlite3.Connection) -> None:
        reader_connection_open.set()
        await enter_reader.wait()
        await original_begin_read(connection)

    monkeypatch.setattr(credential_store_module, "_begin_read", pause_before_read_lock)
    blocking_reader.start()
    try:
        assert await asyncio.to_thread(reader_ready.wait, 5)

        async def read_generation() -> str:
            async with oauth_credential_reader(context) as reader:
                return reader.generations().generation

        pending_read = asyncio.create_task(read_generation())
        await asyncio.wait_for(reader_connection_open.wait(), timeout=5)
        writer.start()
        assert await asyncio.to_thread(writer_committing.wait, 5)
        await _wait_for_sqlite_pending_commit(database_path)

        enter_reader.set()
        await asyncio.sleep(0.1)
        assert not pending_read.done()
        release_reader.set()
        assert await asyncio.wait_for(pending_read, timeout=5) == "committed-generation"
        assert await asyncio.to_thread(writer_committed.wait, 5)
    finally:
        release_reader.set()
        await asyncio.to_thread(blocking_reader.join, 5)
        await asyncio.to_thread(writer.join, 5)
        for process in (blocking_reader, writer):
            if process.is_alive():
                process.terminate()
            process.join()
    assert blocking_reader.exitcode == 0
    assert writer.exitcode == 0


@pytest.mark.asyncio
async def test_reader_probe_validates_inside_one_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The initialization probe must hold one materialized read snapshot through validation."""
    context = _context(tmp_path)
    await _publish(context, "initial")
    validation_transactions: list[bool] = []
    original_validate = credential_store_module._validate_initialized_store

    def record_validation_transaction(
        validation_context: OAuthCredentialContext,
        connection: sqlite3.Connection,
        **kwargs: object,
    ) -> None:
        validation_transactions.append(connection.in_transaction)
        original_validate(validation_context, connection, **kwargs)

    monkeypatch.setattr(
        credential_store_module,
        "_validate_initialized_store",
        record_validation_transaction,
    )

    async with oauth_credential_reader(context) as reader:
        assert reader.generations().generation

    assert validation_transactions == [True, True]
