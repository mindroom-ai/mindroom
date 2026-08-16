"""Tests for the atomic SQLite OAuth credential store."""

from __future__ import annotations

import asyncio
import base64
import multiprocessing
import shutil
import sqlite3
from typing import TYPE_CHECKING, cast

import pytest

from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials import CredentialsManager, get_runtime_credentials_manager, save_scoped_credentials
from mindroom.oauth.credential_lifecycle import OAuthCredentialContext
from mindroom.oauth.credential_store import _oauth_credential_database_path, oauth_credential_transaction
from mindroom.oauth.providers import OAuthProvider, OAuthProviderError
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target

if TYPE_CHECKING:
    from multiprocessing.synchronize import Event
    from pathlib import Path

    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


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
async def test_completed_reset_replay_never_deletes_later_credentials(tmp_path: Path) -> None:
    """A stable completed operation returns its receipt before checking CAS or deleting again."""
    context = _context(tmp_path)
    _generation, original_connection = await _publish(context, "old")
    async with oauth_credential_transaction(context) as transaction:
        assert (
            transaction.reset(
                "browser:stable-reset",
                expected_connection_generation=original_connection,
                replayable=True,
            )
            is True
        )
        await transaction.commit()

    await _publish(context, "replacement")
    async with oauth_credential_transaction(context) as transaction:
        assert (
            transaction.reset(
                "browser:stable-reset",
                expected_connection_generation=original_connection,
                replayable=True,
            )
            is True
        )
        assert transaction.snapshot().credentials == {
            "token": "replacement",
            "refresh_token": "refresh-replacement",
        }
        await transaction.commit()


@pytest.mark.asyncio
async def test_corrupt_encrypted_legacy_credential_can_be_reset_without_plaintext_storage(tmp_path: Path) -> None:
    """Unreadable plaintext never enters an encrypted DB, but its presence remains resettable."""
    encryption_key = base64.urlsafe_b64encode(b"k" * 32).decode()
    context = _context(tmp_path, encryption_key=encryption_key)
    save_scoped_credentials(
        context.provider.credential_service,
        {"token": "temporary"},
        credentials_manager=context.credentials_manager,
        worker_target=context.worker_target,
    )
    legacy_path = context.credentials_manager.for_primary_runtime_scope(
        "@alice:example.test",
        None,
    ).get_credentials_path(context.provider.credential_service)
    legacy_path.write_bytes(b"corrupt-plaintext-secret")

    async with oauth_credential_transaction(context) as transaction:
        with pytest.raises(OAuthProviderError, match="could not be loaded"):
            transaction.snapshot()
        generations = transaction.generations()
        await transaction.commit()

    database_path = _oauth_credential_database_path(context)
    assert b"corrupt-plaintext-secret" not in database_path.read_bytes()
    async with oauth_credential_transaction(context) as transaction:
        assert (
            transaction.reset(
                "browser:corrupt-reset",
                expected_connection_generation=generations.connection_generation,
                replayable=True,
            )
            is True
        )
        await transaction.commit()


@pytest.mark.asyncio
async def test_wrong_key_legacy_ciphertext_recovers_when_original_key_returns(tmp_path: Path) -> None:
    """Opaque legacy ciphertext is retried and normalized after the right key returns."""
    original_key = base64.urlsafe_b64encode(b"a" * 32).decode()
    wrong_key = base64.urlsafe_b64encode(b"b" * 32).decode()
    original_context = _context(tmp_path, encryption_key=original_key)
    save_scoped_credentials(
        original_context.provider.credential_service,
        {"token": "recoverable"},
        credentials_manager=original_context.credentials_manager,
        worker_target=original_context.worker_target,
    )

    wrong_context = _context(tmp_path, encryption_key=wrong_key)
    async with oauth_credential_transaction(wrong_context) as transaction:
        with pytest.raises(OAuthProviderError, match="could not be loaded"):
            transaction.snapshot()
        await transaction.commit()

    recovered_context = OAuthCredentialContext(
        provider=original_context.provider,
        runtime_paths=original_context.runtime_paths,
        credentials_manager=CredentialsManager(
            original_context.credentials_manager.base_path,
            shared_base_path=original_context.credentials_manager.shared_base_path,
            encryption_key=original_key,
        ),
        worker_target=original_context.worker_target,
    )
    async with oauth_credential_transaction(recovered_context) as transaction:
        snapshot = transaction.snapshot()
        await transaction.commit()
    assert snapshot.credentials == {"token": "recoverable"}


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
