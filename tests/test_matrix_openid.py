"""Tests for shared Matrix OpenID verification."""

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web

from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.matrix_openid import MatrixOpenIDError, MatrixOpenIDToken, verify_matrix_openid
from tests.computer_helpers import ComputerPeer

AUDIENCE = "https://portal.example.org"
TOKEN = MatrixOpenIDToken(
    access_token="openid-secret",  # noqa: S106 - test token
    token_type="Bearer",  # noqa: S106 - test token
    matrix_server_name="example.org",
    expires_in=3600,
)


@pytest_asyncio.fixture
async def homeserver(tmp_path: Path) -> AsyncIterator[tuple[ComputerPeer, RuntimePaths]]:
    """Serve the fake homeserver's userinfo over real HTTP."""
    peer = ComputerPeer()
    upstream = web.Application()
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
async def test_audience_mismatch_from_userinfo_is_unauthorized(homeserver: tuple[ComputerPeer, RuntimePaths]) -> None:
    """A homeserver refusing the audience is a 401, and the right audience is accepted."""
    peer, paths = homeserver
    peer.expected_audience = "https://other.example.org"
    with pytest.raises(MatrixOpenIDError, match=r"^Matrix OpenID verification failed\.$") as error:
        await verify_matrix_openid(TOKEN, paths)
    assert error.value.status_code == 401
    peer.expected_audience = AUDIENCE
    assert await verify_matrix_openid(TOKEN, paths) == "@alice:example.org"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("public_url", "audience"),
    [
        ("https://Portal.Example.org:443", "https://portal.example.org"),
        ("https://portal.example.org/some/path?q=1#f", "https://portal.example.org"),
        ("  http://Portal.Example.org:80/  ", "http://portal.example.org"),
        ("https://portal.example.org:8443", "https://portal.example.org:8443"),
        ("http://portal.example.org:443", "http://portal.example.org:443"),
        ("http://[::1]:8765", "http://[::1]:8765"),
    ],
)
async def test_audience_is_the_normalized_browser_origin(
    homeserver: tuple[ComputerPeer, RuntimePaths],
    public_url: str,
    audience: str,
) -> None:
    """The audience matches what JavaScript's `URL.origin` gives Chat for the same address."""
    peer, paths = homeserver
    await verify_matrix_openid(TOKEN, _with_public_url(paths, public_url))
    assert peer.userinfo_audiences == [audience]


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
async def test_no_audience_without_a_usable_public_url(
    homeserver: tuple[ComputerPeer, RuntimePaths],
    public_url: str,
) -> None:
    """The audience is never guessed from anywhere but MINDROOM_PUBLIC_URL, so none is sent without one."""
    peer, paths = homeserver
    assert await verify_matrix_openid(TOKEN, _with_public_url(paths, public_url)) == "@alice:example.org"
    assert peer.userinfo_audiences == [None]
