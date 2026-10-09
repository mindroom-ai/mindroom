"""Tests for shared Matrix OpenID verification and client origin parsing."""

from pathlib import Path

import pytest
from aiohttp import web

from mindroom.constants import resolve_runtime_paths
from mindroom.matrix_openid import (
    MatrixOpenIDError,
    MatrixOpenIDToken,
    allowed_client_origins,
    verify_matrix_openid,
)
from tests.computer_helpers import ComputerPeer


def test_allowed_client_origins_reads_named_env(tmp_path: Path) -> None:
    """Reads exact origins from the named env var."""
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MINDROOM_CONNECTIONS_ALLOWED_ORIGINS": '["https://chat.example.org", "capacitor://localhost"]',
        },
    )
    assert allowed_client_origins(paths, "MINDROOM_CONNECTIONS_ALLOWED_ORIGINS") == (
        "https://chat.example.org",
        "capacitor://localhost",
    )
    # Computer env unset returns empty tuple
    assert allowed_client_origins(paths, "MINDROOM_COMPUTER_ALLOWED_ORIGINS") == ()


def test_allowed_client_origins_fails_closed(tmp_path: Path) -> None:
    """Invalid configuration fails closed with empty tuple."""
    # Wildcard rejected
    wildcard_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MINDROOM_CONNECTIONS_ALLOWED_ORIGINS": '["https://ok.example.org", "https://*.example.org"]',
        },
    )
    assert allowed_client_origins(wildcard_paths, "MINDROOM_CONNECTIONS_ALLOWED_ORIGINS") == ()
    # Non-list rejected
    non_list_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MINDROOM_CONNECTIONS_ALLOWED_ORIGINS": '"https://chat.example.org"',
        },
    )
    assert allowed_client_origins(non_list_paths, "MINDROOM_CONNECTIONS_ALLOWED_ORIGINS") == ()


@pytest.mark.asyncio
async def test_verify_matrix_openid_rejects_other_server(tmp_path: Path) -> None:
    """Rejects tokens from a different Matrix server."""
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MATRIX_HOMESERVER": "https://example.org",
            "MATRIX_SERVER_NAME": "example.org",
        },
    )
    token = MatrixOpenIDToken(
        access_token="openid-secret",  # noqa: S106 - test token
        token_type="Bearer",  # noqa: S106 - test token
        matrix_server_name="evil.org",
        expires_in=3600,
    )
    with pytest.raises(MatrixOpenIDError) as exc:
        await verify_matrix_openid(token, paths)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_matrix_openid_returns_subject(tmp_path: Path) -> None:
    """Returns the verified Matrix subject from the homeserver."""
    peer = ComputerPeer()
    upstream = web.Application()
    upstream.router.add_get("/_matrix/federation/v1/openid/userinfo", peer.openid)
    runner = web.AppRunner(upstream, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    origin = f"http://127.0.0.1:{port}"

    try:
        paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env={
                "MATRIX_HOMESERVER": origin,
                "MATRIX_SERVER_NAME": "example.org",
            },
        )
        token = MatrixOpenIDToken(
            access_token="openid-secret",  # noqa: S106 - test token
            token_type="Bearer",  # noqa: S106 - test token
            matrix_server_name="example.org",
            expires_in=3600,
        )
        assert await verify_matrix_openid(token, paths) == "@alice:example.org"
    finally:
        await runner.cleanup()
