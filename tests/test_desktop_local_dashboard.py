"""The native dashboard handoff reads local credentials without exposing them in status."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from mindroom.constants import resolve_runtime_paths
from mindroom.desktop.native_host import NativeDesktopHost
from mindroom.desktop.native_protocol import NativeProtocolError, NativeRequest, parse_native_request

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_dashboard_reads_and_refreshes_config_adjacent_env_without_pairing(tmp_path: Path) -> None:
    """A running helper must pick up edited credentials, without including them in normal status."""
    env = tmp_path / ".env"
    env.write_text('MINDROOM_API_KEY="test-first-key"\nMINDROOM_URL=http://localhost:8877\n')
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})
    host = NativeDesktopHost(paths, helper_version="test")
    request = NativeRequest(str(uuid4()), "dashboard_configuration", {})
    assert await host.handle(request) == {"url": "http://127.0.0.1:8877", "api_key": "test-first-key"}
    env.write_text("MINDROOM_API_KEY='test-rotated-key'\n")
    assert await host.handle(request) == {"url": "http://127.0.0.1:8765", "api_key": "test-rotated-key"}
    assert "test-first-key" not in json.dumps(host.status())
    assert "test-rotated-key" not in json.dumps(host.status())


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://127.0.0.1.example.com",
        "file:///etc/passwd",
        "http://user:secret@127.0.0.1",
        "http://127.0.0.1?api_key=secret",
        "http://127.0.0.1/#secret",
        "http://127.0.0.1/remote",
        "http://127.0.0.1:bad",
        "http://127.0.0.1:0",
        "http://127.0.0.1\n.example.com",
    ],
)
@pytest.mark.asyncio
async def test_dashboard_rejects_nonlocal_or_ambiguous_destination(tmp_path: Path, url: str) -> None:
    """Configuration cannot direct the local key to another server or hide credentials in a URL."""
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        process_env={
            "MINDROOM_URL": url,
            "MINDROOM_API_KEY": "do-not-echo-this-key",
        },
    )
    host = NativeDesktopHost(paths, helper_version="test")
    with pytest.raises(NativeProtocolError) as caught:
        await host.handle(NativeRequest(str(uuid4()), "dashboard_configuration", {}))
    assert caught.value.code == "dashboard_configuration_invalid"
    assert "do-not-echo-this-key" not in str(caught.value)
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_dashboard_supports_no_key_and_ipv6(tmp_path: Path) -> None:
    """An open local dashboard needs no synthetic credential."""
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        process_env={
            "MINDROOM_URL": "http://[::1]:8877/",
        },
    )
    result = await NativeDesktopHost(paths, helper_version="test").handle(
        NativeRequest(str(uuid4()), "dashboard_configuration", {}),
    )
    assert result == {"url": "http://[::1]:8877", "api_key": None}


def test_dashboard_request_is_only_a_native_protocol_action() -> None:
    """The desktop device's private parent pipe accepts the dashboard action."""
    raw = {"v": 1, "request_id": str(uuid4()), "action": "dashboard_configuration", "parameters": {}}
    assert parse_native_request(json.dumps(raw).encode()).action == "dashboard_configuration"
