"""Tests for shared Matrix OpenID verification and client origin parsing."""

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web
from structlog.testing import capture_logs

from mindroom import matrix_openid
from mindroom.constants import RuntimePaths, resolve_runtime_paths
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


AUDIENCE = "https://portal.example.org"
TOKEN = MatrixOpenIDToken(
    access_token="openid-secret",  # noqa: S106 - test token
    token_type="Bearer",  # noqa: S106 - test token
    matrix_server_name="example.org",
    expires_in=3600,
)


@pytest_asyncio.fixture
async def homeserver(tmp_path: Path) -> AsyncIterator[tuple[ComputerPeer, RuntimePaths]]:
    """Serve the fake homeserver's `/versions` and userinfo over real HTTP."""
    peer = ComputerPeer()
    upstream = web.Application()
    upstream.router.add_get("/_matrix/client/versions", peer.versions)
    upstream.router.add_get("/_matrix/federation/v1/openid/userinfo", peer.openid)
    runner = web.AppRunner(upstream, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MATRIX_HOMESERVER": f"http://127.0.0.1:{runner.addresses[0][1]}",
            "MATRIX_SERVER_NAME": "example.org",
            "MINDROOM_PUBLIC_URL": AUDIENCE,
        },
    )
    try:
        yield peer, paths
    finally:
        await runner.cleanup()


def _with_public_url(paths: RuntimePaths, public_url: str) -> RuntimePaths:
    return replace(paths, process_env={**paths.process_env, "MINDROOM_PUBLIC_URL": public_url})


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
    token = TOKEN.model_copy(update={"matrix_server_name": "evil.org"})
    with pytest.raises(MatrixOpenIDError) as exc:
        await verify_matrix_openid(token, paths)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_matrix_openid_returns_subject(homeserver: tuple[ComputerPeer, RuntimePaths]) -> None:
    """Returns the verified Matrix subject from the homeserver."""
    _, paths = homeserver
    assert await verify_matrix_openid(TOKEN, paths) == "@alice:example.org"


@pytest.mark.asyncio
async def test_audience_is_sent_only_when_the_homeserver_advertises_it(
    homeserver: tuple[ComputerPeer, RuntimePaths],
) -> None:
    """A stock homeserver never sees the audience, an advertising one always does."""
    peer, paths = homeserver
    await verify_matrix_openid(TOKEN, paths)
    assert peer.userinfo_audiences == [None]
    matrix_openid._audience_support.clear()
    peer.advertise_audience = True
    await verify_matrix_openid(TOKEN, paths)
    assert peer.userinfo_audiences == [None, AUDIENCE]


@pytest.mark.asyncio
async def test_audience_mismatch_from_userinfo_is_unauthorized(homeserver: tuple[ComputerPeer, RuntimePaths]) -> None:
    """A homeserver refusing the audience is a 401, and the right audience is accepted."""
    peer, paths = homeserver
    peer.advertise_audience = True
    peer.expected_audience = "https://other.example.org"
    with pytest.raises(MatrixOpenIDError, match=r"^Matrix OpenID verification failed\.$") as error:
        await verify_matrix_openid(TOKEN, paths)
    assert error.value.status_code == 401
    peer.expected_audience = AUDIENCE
    assert await verify_matrix_openid(TOKEN, paths) == "@alice:example.org"


@pytest.mark.asyncio
async def test_bound_401_logs_the_audience_but_never_the_token(homeserver: tuple[ComputerPeer, RuntimePaths]) -> None:
    """An operator can see which audience the homeserver refused, and an unbound 401 stays quiet."""
    peer, paths = homeserver
    peer.openid_status = 401
    with capture_logs() as logs, pytest.raises(MatrixOpenIDError):
        await verify_matrix_openid(TOKEN, paths)
    assert logs == []
    matrix_openid._audience_support.clear()
    peer.advertise_audience = True
    peer.expected_audience = "https://other.example.org"
    with capture_logs() as logs, pytest.raises(MatrixOpenIDError):
        await verify_matrix_openid(TOKEN, paths)
    assert [(log["log_level"], log["audience"]) for log in logs] == [("warning", AUDIENCE)]
    assert "openid-secret" not in repr(logs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "public_url",
    [
        "",
        "   ",
        "portal.example.org",
        "localhost:8765",
        "ftp://portal.example.org",
        "https://:443",
        "https://portal.example.org:bad",
        "https://[::1",
    ],
)
async def test_bound_verification_requires_a_valid_public_url(
    homeserver: tuple[ComputerPeer, RuntimePaths],
    public_url: str,
) -> None:
    """A binding homeserver never gets an audience guessed from anywhere but MINDROOM_PUBLIC_URL."""
    peer, paths = homeserver
    peer.advertise_audience = True
    with pytest.raises(MatrixOpenIDError) as error:
        await verify_matrix_openid(TOKEN, _with_public_url(paths, public_url))
    assert (error.value.status_code, error.value.detail) == (
        503,
        "Set MINDROOM_PUBLIC_URL to verify bound Matrix OpenID tokens.",
    )
    assert peer.userinfo_audiences == []


@pytest.mark.asyncio
async def test_unbound_verification_does_not_need_a_public_url(homeserver: tuple[ComputerPeer, RuntimePaths]) -> None:
    """A homeserver that cannot bind tokens verifies exactly as before, whatever MINDROOM_PUBLIC_URL holds."""
    peer, paths = homeserver
    for public_url in ("", "not a url"):
        assert await verify_matrix_openid(TOKEN, _with_public_url(paths, public_url)) == "@alice:example.org"
    assert peer.userinfo_audiences == [None, None]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("public_url", "audience"),
    [
        ("https://Portal.Example.org:443", "https://portal.example.org"),
        ("https://portal.example.org/some/path?q=1#f", "https://portal.example.org"),
        ("  http://Portal.Example.org:80/  ", "http://portal.example.org"),
        ("https://portal.example.org:8443", "https://portal.example.org:8443"),
        ("http://portal.example.org:443", "http://portal.example.org:443"),
        ("https://user:pw@portal.example.org", "https://portal.example.org"),
        ("http://[::1]:8765", "http://[::1]:8765"),
    ],
)
async def test_bound_audience_is_the_normalized_browser_origin(
    homeserver: tuple[ComputerPeer, RuntimePaths],
    public_url: str,
    audience: str,
) -> None:
    """The audience matches what JavaScript's `URL.origin` gives Chat for the same address."""
    peer, paths = homeserver
    peer.advertise_audience = True
    await verify_matrix_openid(TOKEN, _with_public_url(paths, public_url))
    assert peer.userinfo_audiences == [audience]


@pytest.mark.asyncio
async def test_capability_is_cached_for_ten_minutes(
    homeserver: tuple[ComputerPeer, RuntimePaths],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two verifications read `/versions` once, and the answer expires after ten minutes."""
    peer, paths = homeserver
    now = 1000.0
    monkeypatch.setattr(matrix_openid, "monotonic", lambda: now)
    peer.advertise_audience = True
    await verify_matrix_openid(TOKEN, paths)
    await verify_matrix_openid(TOKEN, paths)
    assert peer.versions_calls == 1
    now += 599
    peer.advertise_audience = False
    await verify_matrix_openid(TOKEN, paths)
    assert (peer.versions_calls, peer.userinfo_audiences[-1]) == (1, AUDIENCE)
    now += 2
    await verify_matrix_openid(TOKEN, paths)
    assert (peer.versions_calls, peer.userinfo_audiences[-1]) == (2, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 500])
async def test_capability_fetch_failure_falls_back_without_caching(
    homeserver: tuple[ComputerPeer, RuntimePaths],
    status: int,
) -> None:
    """A failing `/versions` verifies unbound and is retried, so a recovered homeserver is picked up."""
    peer, paths = homeserver
    peer.versions_status = status
    assert await verify_matrix_openid(TOKEN, paths) == "@alice:example.org"
    assert peer.userinfo_audiences == [None]
    peer.versions_status = 200
    peer.advertise_audience = True
    await verify_matrix_openid(TOKEN, paths)
    assert (peer.versions_calls, peer.userinfo_audiences) == (2, [None, AUDIENCE])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 500])
async def test_failed_refresh_keeps_the_last_known_capability(
    homeserver: tuple[ComputerPeer, RuntimePaths],
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """A homeserver that binds tokens keeps binding when `/versions` fails after the cache expires."""
    peer, paths = homeserver
    now = 1000.0
    monkeypatch.setattr(matrix_openid, "monotonic", lambda: now)
    peer.advertise_audience = True
    await verify_matrix_openid(TOKEN, paths)
    now += 601
    peer.versions_status = status
    await verify_matrix_openid(TOKEN, paths)
    assert (peer.versions_calls, peer.userinfo_audiences) == (2, [AUDIENCE, AUDIENCE])
    peer.versions_status = 200
    peer.advertise_audience = False
    await verify_matrix_openid(TOKEN, paths)
    assert (peer.versions_calls, peer.userinfo_audiences[-1]) == (3, None)


@pytest.mark.asyncio
async def test_unreachable_versions_endpoint_falls_back(tmp_path: Path) -> None:
    """A homeserver that cannot be reached for `/versions` is unbound and surfaces the userinfo outage."""
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MATRIX_HOMESERVER": "http://127.0.0.1:9", "MATRIX_SERVER_NAME": "example.org"},
    )
    with pytest.raises(MatrixOpenIDError) as error:
        await verify_matrix_openid(TOKEN, paths)
    assert error.value.status_code == 503
    assert matrix_openid._audience_support == {}
