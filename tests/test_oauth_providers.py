"""Tests for shared OAuth provider behavior."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import pytest

from mindroom import credentials as credentials_module
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.oauth.providers import OAuthProvider, OAuthRuntimeEndpoints

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


@pytest.mark.asyncio
@pytest.mark.parametrize("bootstrap_required", [False, True], ids=["initial-read", "after-bootstrap"])
async def test_async_client_config_file_reads_stay_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    bootstrap_required: bool,
) -> None:
    """Stored client settings are read off-loop, including the read after bootstrap."""
    owner_thread = threading.get_ident()
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={},
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    client_settings = {"client_id": "example-client", "client_secret": "example-secret"}
    service = "example_oauth_client"
    client_path = manager.get_credentials_path(service)
    bootstrap_calls = 0

    async def bootstrap(received_provider: OAuthProvider, received_paths: RuntimePaths) -> OAuthRuntimeEndpoints:
        nonlocal bootstrap_calls
        assert received_provider is provider
        assert received_paths is runtime_paths
        assert threading.get_ident() == owner_thread
        bootstrap_calls += 1
        await asyncio.to_thread(manager.save_credentials, service, client_settings)
        return OAuthRuntimeEndpoints(
            authorization_url="https://auth.example.test/authorize",
            token_url="https://auth.example.test/token",  # noqa: S106
        )

    provider = OAuthProvider(
        id="example",
        display_name="Example",
        authorization_url="https://auth.example.test/authorize",
        token_url="https://auth.example.test/token",  # noqa: S106
        scopes=("read",),
        credential_service="example_oauth",
        client_config_services=(service,),
        runtime_bootstrapper=bootstrap,
    )
    if not bootstrap_required:
        manager.save_credentials(service, client_settings)

    original_read = credentials_module._read_credentials_payload
    read_threads: list[int] = []

    def observed_read(path: Path) -> bytes | None:
        if path == client_path:
            read_threads.append(threading.get_ident())
            assert threading.get_ident() != owner_thread, "Client config file read blocked the event loop"
        return original_read(path)

    monkeypatch.setattr(credentials_module, "_read_credentials_payload", observed_read)

    resolution = await provider.client_config_resolution_async(runtime_paths)

    assert resolution is not None
    assert resolution.config.client_id == "example-client"
    assert resolution.config.client_secret == "example-secret"  # noqa: S105
    assert resolution.service == service
    assert resolution.custom is True
    assert bootstrap_calls == int(bootstrap_required)
    assert read_threads
