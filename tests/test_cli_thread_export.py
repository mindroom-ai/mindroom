"""The export CLI uses the selected runtime API and never owns Matrix state."""

import json
from pathlib import Path

import httpx
import pytest
import typer

from mindroom import constants
from mindroom.cli import thread_export
from mindroom.cli.main import _threads_export


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 401, 409, 503])
async def test_export_cli_http_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """Pass auth and installation identity, and surface runtime failures without fallback."""
    paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_URL": "http://localhost:9001", "MINDROOM_API_KEY": "test-key"},
    )
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert str(request.url) == "http://localhost:9001/api/threads/export"
        assert request.headers["Authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body == {
            "config_path": str(paths.config_path),
            "storage_root": str(paths.storage_root),
            "output_dir": str(tmp_path / "exports"),
            "room_filter": "lobby",
            "max_thread_roots": 17,
            "include_invited_rooms": False,
        }
        return httpx.Response(
            status,
            json={"output_dir": str(tmp_path / "exports")} if status == 200 else {"detail": "unavailable"},
        )

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        thread_export.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    request = thread_export.request_thread_export(
        runtime_paths=paths,
        url=None,
        output_dir=tmp_path / "exports",
        room_filter="lobby",
        max_thread_roots=17,
        include_invited_rooms=False,
    )
    if status == 200:
        assert (await request).output_dir == tmp_path / "exports"
    else:
        with pytest.raises(RuntimeError, match=str(status)):
            await request
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_export_cli_unreachable_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An offline install gets actionable guidance without opening any stores."""
    paths = constants.resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})

    def refuse(request: httpx.Request) -> httpx.Response:
        message = "offline"
        raise httpx.ConnectError(message, request=request)

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        thread_export.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(refuse), **kwargs),
    )
    with pytest.raises(RuntimeError, match="start MindRoom with its API enabled"):
        await thread_export.request_thread_export(
            runtime_paths=paths,
            url="http://localhost:9002",
            output_dir=None,
            room_filter=None,
            max_thread_roots=1,
            include_invited_rooms=True,
        )
    assert not paths.storage_root.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "trust_env"),
    [
        ("http://10.0.0.1", None),
        ("http://remote.test", None),
        ("http://[::2]", None),
        ("http://127.0.0.1:8765", False),
        ("http://localhost:8765", False),
        ("http://[::1]:8765", False),
        ("https://remote.test", True),
    ],
)
async def test_export_cli_sends_the_key_only_over_loopback_http_or_https(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    trust_env: bool | None,
) -> None:
    """A watched export refuses remote plaintext HTTP before any request, and plaintext keys skip environment proxies."""
    monkeypatch.setenv("MINDROOM_API_KEY", "test-secret")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    clients: list[dict[str, object]] = []
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"output_dir": str(tmp_path / "exports")})

    client_type = httpx.AsyncClient

    def client(**kwargs: object) -> httpx.AsyncClient:
        clients.append(kwargs)
        return client_type(transport=httpx.MockTransport(respond), **kwargs)

    async def stop_watching(_seconds: int) -> None:
        raise typer.Exit(0)

    monkeypatch.setattr(thread_export.httpx, "AsyncClient", client)
    monkeypatch.setattr("mindroom.cli.main.asyncio.sleep", stop_watching)
    with pytest.raises(typer.Exit) as exit_info:
        await _threads_export(
            config_path=tmp_path / "config.yaml",
            url=url,
            storage_path=tmp_path / "storage",
            output=None,
            room=None,
            watch=True,
            interval=1,
            max_thread_roots=1,
            include_invited_rooms=True,
        )

    if trust_env is None:
        assert exit_info.value.exit_code == 1
        assert clients == sent == []
    else:
        assert exit_info.value.exit_code == 0
        assert [request.headers["Authorization"] for request in sent] == ["Bearer test-secret"]
        assert clients[0]["trust_env"] is trust_env
        assert clients[0]["follow_redirects"] is False
