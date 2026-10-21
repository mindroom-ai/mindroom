"""Tests for shared Matrix OpenID verification."""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web
from structlog.testing import capture_logs

from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.matrix_openid import (
    MatrixOpenIDError,
    MatrixOpenIDToken,
    homeserver_binds_openid_audience,
    verify_matrix_openid,
)
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
    """An unusable MINDROOM_PUBLIC_URL is never replaced by a guess, so no audience is sent."""
    peer, paths = homeserver
    assert await verify_matrix_openid(TOKEN, _with_public_url(paths, public_url)) == "@alice:example.org"
    assert peer.userinfo_audiences == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("env", "audience"),
    [
        ({}, "http://localhost:8765"),
        ({"MINDROOM_PORT": "9000"}, "http://localhost:9000"),
        ({"MINDROOM_PORT": "80"}, "http://localhost"),
    ],
)
async def test_audience_defaults_to_the_local_api_origin(
    homeserver: tuple[ComputerPeer, RuntimePaths],
    env: dict[str, str],
    audience: str,
) -> None:
    """Without MINDROOM_PUBLIC_URL the audience is MindRoom's own local origin, which Chat opens on the same machine."""
    peer, paths = homeserver
    for public_url in ("", "   "):
        local = replace(paths, process_env={**paths.process_env, "MINDROOM_PUBLIC_URL": public_url, **env})
        peer.userinfo_audiences.clear()
        assert await verify_matrix_openid(TOKEN, local) == "@alice:example.org"
        assert peer.userinfo_audiences == [audience]


_VersionsServer = Callable[[web.Response], Awaitable[RuntimePaths]]
_BINDING_CHECK_FAILED = "Could not check whether the homeserver binds OpenID tokens"


def _warnings(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {key: value for key, value in log.items() if key != "log_level"}
        for log in logs
        if log["log_level"] == "warning"
    ]


@pytest_asyncio.fixture
async def versions_server(tmp_path: Path) -> AsyncIterator[_VersionsServer]:
    """Serve one fixed `/_matrix/client/versions` answer over real HTTP."""
    runners: list[web.AppRunner] = []

    async def serve(answer: web.Response) -> RuntimePaths:
        async def versions(_request: web.Request) -> web.Response:
            return answer

        upstream = web.Application()
        upstream.router.add_get("/_matrix/client/versions", versions)
        runner = web.AppRunner(upstream, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        runners.append(runner)
        return resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env={"MATRIX_HOMESERVER": f"http://127.0.0.1:{runner.addresses[0][1]}"},
        )

    try:
        yield serve
    finally:
        for runner in runners:
            await runner.cleanup()


@pytest.mark.asyncio
async def test_binding_probe_true_when_versions_advertises_the_flag(versions_server: _VersionsServer) -> None:
    """The fork advertises binding in `unstable_features` while `mindroom_openid_audience` is on."""
    paths = await versions_server(
        web.json_response({"versions": ["v1.11"], "unstable_features": {"io.mindroom.openid_audience": True}}),
    )
    assert await homeserver_binds_openid_audience(paths) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env",
    [{"MINDROOM_PORT": "tcp://10.0.0.1:8765"}, {"MINDROOM_PUBLIC_URL": "https://portal.example.org:bad"}],
    ids=["kubernetes-service-link-port", "unusable-public-url"],
)
async def test_binding_probe_false_without_an_audience_to_send(
    versions_server: _VersionsServer,
    env: dict[str, str],
) -> None:
    """Without an audience a binding homeserver still accepts unbound tokens, so no audience counts as no binding."""
    paths = await versions_server(
        web.json_response({"versions": ["v1.11"], "unstable_features": {"io.mindroom.openid_audience": True}}),
    )
    assert await homeserver_binds_openid_audience(replace(paths, process_env={**paths.process_env, **env})) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unstable_features",
    [{"io.mindroom.openid_audience": False}, {}, ["io.mindroom.openid_audience"]],
    ids=["flag-false", "flag-absent", "not-an-object"],
)
async def test_binding_probe_false_without_the_flag(
    versions_server: _VersionsServer,
    unstable_features: object,
) -> None:
    """Only an explicit `true` counts as binding; binding switched off is normal and logs no warning."""
    paths = await versions_server(web.json_response({"versions": ["v1.11"], "unstable_features": unstable_features}))
    with capture_logs() as logs:
        assert await homeserver_binds_openid_audience(paths) is False
    assert _warnings(logs) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "why"),
    [
        (lambda: web.Response(status=500), {"status": 500}),
        (
            lambda: web.Response(status=302, headers={"Location": "https://elsewhere.example/_matrix/client/versions"}),
            {"status": 302},
        ),
        (lambda: web.Response(text="not json"), {"error": "JSONDecodeError"}),
        (
            lambda: web.json_response({"unstable_features": {"io.mindroom.openid_audience": True}, "pad": "x" * 20000}),
            {"error": "ByteLimitExceededError"},
        ),
    ],
    ids=["server-error", "redirect", "not-json", "oversized"],
)
async def test_binding_probe_false_on_bad_responses(
    versions_server: _VersionsServer,
    answer: Callable[[], web.Response],
    why: dict[str, object],
) -> None:
    """Any answer that is not a small, direct JSON confirmation means no binding, with a warning saying why."""
    paths = await versions_server(answer())
    with capture_logs() as logs:
        assert await homeserver_binds_openid_audience(paths) is False
    assert _warnings(logs) == [{"event": _BINDING_CHECK_FAILED, **why}]


@pytest.mark.asyncio
async def test_binding_probe_false_when_homeserver_unreachable(tmp_path: Path) -> None:
    """A homeserver that cannot be reached is not assumed to bind tokens."""
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MATRIX_HOMESERVER": "http://127.0.0.1:9"},
    )
    with capture_logs() as logs:
        assert await homeserver_binds_openid_audience(paths) is False
    assert _warnings(logs) == [{"event": _BINDING_CHECK_FAILED, "error": "ClientConnectorError"}]
