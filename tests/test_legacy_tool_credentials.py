"""The one-time startup cleanups of tool settings saved with former insecure defaults or placements."""

from __future__ import annotations

import asyncio
import base64
import importlib
import os
import threading
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import _reset_credentials_manager_cache, get_runtime_credentials_manager
from mindroom.file_locks import advisory_file_lock
from mindroom.legacy_tool_credentials import migrate_tool_credential_defaults
from mindroom.runtime_env_policy import CREDENTIALS_ENCRYPTION_KEY_ENV

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths


@pytest.fixture(autouse=True)
def _fresh_credentials_managers() -> Iterator[None]:
    _reset_credentials_manager_cache()
    yield
    _reset_credentials_manager_cache()


def _runtime(tmp_path: Path, env: dict[str, str] | None = None) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    return resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "data", process_env=env or {})


@pytest.mark.asyncio
@pytest.mark.parametrize("encrypted", [False, True], ids=["plain", "encrypted"])
async def test_startup_drops_saved_daytona_verify_ssl_false_once(tmp_path: Path, *, encrypted: bool) -> None:
    """Every stored Daytona `verify_ssl: false` is dropped once; other settings and later choices are kept."""
    env = {CREDENTIALS_ENCRYPTION_KEY_ENV: base64.urlsafe_b64encode(b"k" * 32).decode()} if encrypted else {}
    runtime_paths = _runtime(tmp_path, env)
    primary = get_runtime_credentials_manager(runtime_paths)
    worker_credentials = primary.for_worker("v1:tenant:user:@alice:example.org")
    worker_shared = worker_credentials.shared_manager()
    legacy = {"api_key": "dt-secret", "api_url": "https://api.daytona.io", "verify_ssl": False, "persistent": True}
    primary.save_credentials("daytona", legacy)
    worker_credentials.save_credentials("daytona", legacy)
    worker_shared.save_credentials("daytona", legacy)
    primary.save_credentials("custom_api", {"api_key": "sk", "verify_ssl": False})

    await migrate_tool_credential_defaults(runtime_paths)

    migrated = {"api_key": "dt-secret", "api_url": "https://api.daytona.io", "persistent": True}
    assert primary.load_credentials("daytona") == migrated
    # Daytona only runs in the primary, so the worker's own copy is deleted rather than migrated.
    assert worker_credentials.load_credentials("daytona") is None
    assert worker_shared.load_credentials("daytona") == migrated
    assert primary.load_credentials("custom_api") == {"api_key": "sk", "verify_ssl": False}
    if encrypted:
        assert b"dt-secret" not in primary.get_credentials_path("daytona").read_bytes()

    # A false saved deliberately after the upgrade survives later restarts.
    primary.save_credentials("daytona", {**migrated, "verify_ssl": False})
    await migrate_tool_credential_defaults(runtime_paths)

    assert primary.load_credentials("daytona") == {**migrated, "verify_ssl": False}


@pytest.mark.asyncio
async def test_startup_deletes_worker_copies_of_primary_only_tool_settings_once(tmp_path: Path) -> None:
    """Worker copies of settings for tools that never run in a worker are deleted once; every other store is kept."""
    runtime_paths = _runtime(tmp_path)
    primary = get_runtime_credentials_manager(runtime_paths)
    shared_worker = primary.for_worker("v1:default:shared:general")
    user_worker = primary.for_worker("v1:default:user:@alice:example.org")
    slack = {"token": "xoxb-secret"}
    shared_worker.save_credentials("slack", slack)
    user_worker.save_credentials("sql", {"password": "db-secret"})
    user_worker.save_credentials("matrix_voice_message", {"api_key": "room-only-secret"})
    shared_worker.save_credentials("shell", {"extra_env_passthrough": "PATH"})
    shared_worker.save_credentials("custom_api", {"api_key": "worker-key"})
    shared_worker.shared_manager().save_credentials("slack", slack)
    primary.save_credentials("slack", slack)
    primary.for_primary_runtime_agent_scope("general").save_credentials("slack", slack)

    await migrate_tool_credential_defaults(runtime_paths)

    assert shared_worker.list_services() == ["custom_api", "shell"]
    assert user_worker.list_services() == []
    assert shared_worker.shared_manager().load_credentials("slack") == slack
    assert primary.load_credentials("slack") == slack
    assert primary.for_primary_runtime_agent_scope("general").load_credentials("slack") == slack

    # The cleanup runs once; a later document is not the former dashboard placement.
    shared_worker.save_credentials("slack", slack)
    await migrate_tool_credential_defaults(runtime_paths)

    assert shared_worker.load_credentials("slack") == slack


@pytest.mark.asyncio
async def test_a_worker_store_that_cannot_be_cleaned_keeps_the_cleanup_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker store whose copy cannot be deleted is retried at the next start instead of being forgotten."""
    runtime_paths = _runtime(tmp_path)
    worker = get_runtime_credentials_manager(runtime_paths).for_worker("v1:default:shared:general")
    worker.save_credentials("slack", {"token": "xoxb-secret"})
    real_unlink = os.unlink

    def refuse_unlink(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "unlink", refuse_unlink)
    await migrate_tool_credential_defaults(runtime_paths)
    assert worker.load_credentials("slack") == {"token": "xoxb-secret"}

    monkeypatch.setattr(os, "unlink", real_unlink)
    await migrate_tool_credential_defaults(runtime_paths)
    assert worker.load_credentials("slack") is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
@pytest.mark.asyncio
async def test_a_worker_store_hidden_from_discovery_keeps_the_cleanup_pending(tmp_path: Path) -> None:
    """A worker root that worker code made unsearchable hides its store only until the next start."""
    runtime_paths = _runtime(tmp_path)
    worker = get_runtime_credentials_manager(runtime_paths).for_worker("v1:default:shared:general")
    worker.save_credentials("slack", {"token": "xoxb-secret"})
    worker_root = worker.base_path.parent
    worker_root.chmod(0)
    try:
        await migrate_tool_credential_defaults(runtime_paths)
    finally:
        worker_root.chmod(0o700)
    assert worker.load_credentials("slack") == {"token": "xoxb-secret"}

    await migrate_tool_credential_defaults(runtime_paths)
    assert worker.load_credentials("slack") is None


@pytest.mark.asyncio
async def test_startup_keeps_daytona_settings_that_already_verify(tmp_path: Path) -> None:
    """Settings that verify, or never saved the field, are left byte-for-byte alone."""
    runtime_paths = _runtime(tmp_path)
    primary = get_runtime_credentials_manager(runtime_paths)
    primary.save_credentials("daytona", {"api_key": "dt-secret", "verify_ssl": True})
    before = primary.get_credentials_path("daytona").read_bytes()

    await migrate_tool_credential_defaults(runtime_paths)

    assert primary.get_credentials_path("daytona").read_bytes() == before


class _CleanupRanError(Exception):
    """Raised by the stand-in cleanup to stop startup right after it runs."""


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["api", "orchestrator"])
async def test_both_entry_points_clean_up_before_credentials_are_used(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    """Standalone API and orchestrator startup both run the cleanup before any credential work."""
    runtime_paths = _runtime(tmp_path)
    module = importlib.import_module(f"mindroom.{'api.main' if entrypoint == 'api' else 'orchestrator'}")

    async def cleanup(paths: RuntimePaths) -> None:
        assert paths == runtime_paths
        raise _CleanupRanError

    monkeypatch.setattr(module, "migrate_tool_credential_defaults", cleanup)
    monkeypatch.setattr(module, "sync_env_to_credentials", Mock(side_effect=AssertionError("credentials started")))
    if entrypoint == "api":
        monkeypatch.setattr(module, "_app_runtime_paths", lambda _app: runtime_paths)
        with pytest.raises(_CleanupRanError):
            async with module._lifespan(module.app):
                pytest.fail("API admitted runtime work")
    else:
        with pytest.raises(_CleanupRanError):
            await module.main("ERROR", runtime_paths, api=False)


@pytest.mark.asyncio
async def test_unreadable_documents_keep_the_cleanup_pending(tmp_path: Path) -> None:
    """A document encrypted under another key is counted, and the receipt waits until it can be cleaned too."""
    right_key = base64.urlsafe_b64encode(b"r" * 32).decode()
    wrong_key = base64.urlsafe_b64encode(b"w" * 32).decode()
    right = _runtime(tmp_path, {CREDENTIALS_ENCRYPTION_KEY_ENV: right_key})
    get_runtime_credentials_manager(right).save_credentials("daytona", {"api_key": "dt-secret", "verify_ssl": False})
    stored = get_runtime_credentials_manager(right).get_credentials_path("daytona").read_bytes()
    receipt = get_runtime_credentials_manager(right).base_path / ".daytona-verify-ssl-default-dropped.json"

    await migrate_tool_credential_defaults(_runtime(tmp_path, {CREDENTIALS_ENCRYPTION_KEY_ENV: wrong_key}))

    assert get_runtime_credentials_manager(right).get_credentials_path("daytona").read_bytes() == stored
    assert not receipt.exists()

    await migrate_tool_credential_defaults(right)

    assert get_runtime_credentials_manager(right).load_credentials("daytona") == {"api_key": "dt-secret"}
    assert receipt.exists()


def test_a_concurrent_start_rechecks_the_receipt_under_the_lock(tmp_path: Path) -> None:
    """A start that waited for another one to finish trusts its receipt instead of dropping a newer deliberate false."""
    runtime_paths = _runtime(tmp_path)
    primary = get_runtime_credentials_manager(runtime_paths)
    primary.save_credentials("daytona", {"api_key": "dt-secret", "verify_ssl": False})
    lock_path = primary.base_path / ".daytona-verify-ssl-default.lock"
    waiting_start = threading.Thread(target=asyncio.run, args=(migrate_tool_credential_defaults(runtime_paths),))

    with advisory_file_lock(lock_path):
        waiting_start.start()
        waiting_start.join(timeout=0.5)
        assert waiting_start.is_alive(), "the cleanup must wait for the lock"
        # The other process finishes its cleanup, then the user deliberately disables verification.
        primary.save_credentials("daytona", {"api_key": "dt-secret"})
        (primary.base_path / ".daytona-verify-ssl-default-dropped.json").write_text('{"version": 1}', encoding="utf-8")
        primary.save_credentials("daytona", {"api_key": "dt-secret", "verify_ssl": False})
    waiting_start.join(timeout=30)

    assert not waiting_start.is_alive()
    assert primary.load_credentials("daytona") == {"api_key": "dt-secret", "verify_ssl": False}
