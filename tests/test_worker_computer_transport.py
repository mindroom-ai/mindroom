"""Worker HTTP readers keep their byte limits and distinct public failures."""

from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from mindroom.constants import resolve_runtime_paths
from mindroom.worker_computer.auth import MatrixOpenIDToken, verify_openid
from mindroom.worker_computer.client import computer_request
from mindroom.worker_computer.sessions import ComputerError
from mindroom.workers.models import WorkerHandle


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [16384, 16385])
async def test_openid_response_size_boundary(tmp_path: Path, size: int) -> None:
    """Accept a complete exact-limit subject, but classify overflow as invalid OpenID."""

    async def respond(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b'{"sub":"@alice:example.org"}'.ljust(size, b" "))
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get("/_matrix/federation/v1/openid/userinfo", respond)
    async with TestServer(app) as server:
        paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env={"MATRIX_HOMESERVER": str(server.make_url("")), "MATRIX_SERVER_NAME": "example.org"},
        )
        token = MatrixOpenIDToken(
            access_token="openid-test",  # noqa: S106 - isolated test credential
            token_type="Bearer",  # noqa: S106 - isolated test token
            matrix_server_name="example.org",
            expires_in=30,
        )
        if size == 16384:
            assert await verify_openid(token, paths) == "@alice:example.org"
        else:
            with pytest.raises(ComputerError, match=r"^Invalid Matrix OpenID response\.$") as error:
                await verify_openid(token, paths)
            assert error.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected_error"),
    [
        (b'{"state":"stopped","generation":"one","controller_session_id":null}'.ljust(16384, b" "), None),
        (b" " * 16385, "Invalid computer worker response."),
        (b"{", "Computer worker is unavailable."),
    ],
)
async def test_worker_response_size_and_parse_errors(body: bytes, expected_error: str | None) -> None:
    """Overflow stays distinct from malformed JSON while exact-limit status parses."""

    async def respond(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(body)
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get("/computer", respond)
    async with TestServer(app) as server:
        handle = WorkerHandle(
            "worker-id",
            "worker",
            str(server.make_url("/execute")),
            "worker-secret",
            "ready",
            "docker",
            0,
            0,
        )
        if expected_error is None:
            assert await computer_request(handle, "status", "viewer") == {
                "state": "stopped",
                "generation": "one",
                "controller_session_id": None,
            }
        else:
            with pytest.raises(ComputerError) as error:
                await computer_request(handle, "status", "viewer")
            assert error.value.status_code == 503
            assert str(error.value) == expected_error
