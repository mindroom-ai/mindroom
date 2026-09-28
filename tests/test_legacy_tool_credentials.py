"""The one-time startup cleanup re-enables TLS verification for Daytona settings saved with the old default."""

from __future__ import annotations

import base64
import importlib
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import _reset_credentials_manager_cache, get_runtime_credentials_manager
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
    assert worker_credentials.load_credentials("daytona") == migrated
    assert worker_shared.load_credentials("daytona") == migrated
    assert primary.load_credentials("custom_api") == {"api_key": "sk", "verify_ssl": False}
    if encrypted:
        assert b"dt-secret" not in primary.get_credentials_path("daytona").read_bytes()

    # A false saved deliberately after the upgrade survives later restarts.
    primary.save_credentials("daytona", {**migrated, "verify_ssl": False})
    await migrate_tool_credential_defaults(runtime_paths)

    assert primary.load_credentials("daytona") == {**migrated, "verify_ssl": False}


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
