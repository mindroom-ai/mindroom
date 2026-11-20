"""Models send through one runtime-owned HTTP client, so a collected model never touches the event loop."""

from __future__ import annotations

import asyncio
import gc
import json
import os
import socket
from pathlib import Path
from typing import TYPE_CHECKING

import httpx2
import pytest
import pytest_asyncio

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.embeddings.embedder_health import EmbedderHealthRecorder
from mindroom.embeddings.errors import EmbedderRequestError
from mindroom.embeddings.openai_embedder import OpenAIEmbedder
from mindroom.knowledge_graph.store import _GraphLoop, run_on_loop
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, complete
from mindroom.llm.request_context import bind_request_headers
from mindroom.orchestrator import _finish_runtime_shutdown
from mindroom.providers.model_http_client import (
    MODEL_HTTP_CLIENT,
    aclose_model_http_client,
    closing_model_connections,
)
from mindroom.providers.registry import build_model_provider
from tests.conftest import bind_runtime_paths, test_runtime_paths
from tests.providers.family_support import FAMILIES

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from mindroom.llm.provider import ModelProvider

# A finished streamed reply of each model API, by the end of its request path, and an empty page every other request
# accepts.
_STREAMS = {
    "/chat/completions": FAMILIES["openai_chat"].reply().content,
    "/responses": FAMILIES["openai_responses"].reply().content,
    "/messages": FAMILIES["anthropic"].reply().content,
}
_EMPTY_PAGE = {"object": "list", "data": [], "has_more": False, "first_id": None, "last_id": None}
_REQUEST = ModelRequest(messages=[Message(role="user", content="Hi")])


class _ModelsServer:
    """A keep-alive server that streams a finished reply to every model request and answers anything else empty.

    Each stream is a chunked body whose terminating chunk follows its last event. With ``redirect`` set to a status
    and a URL, it answers each request outside ``/moved`` with that redirect to the URL's ``/moved`` copy of its path.
    ``headers`` holds each request's headers by lowercased name, in order.
    """

    def __init__(self) -> None:
        self.url = ""
        self.connections = 0
        self.peer: tuple[str, int] = ("", 0)
        self.closed = asyncio.Event()
        self.redirect: tuple[int, str] | None = None
        self.headers: list[dict[str, str]] = []

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self.peer = writer.get_extra_info("peername")[:2]
        try:
            while True:
                head = (await reader.readuntil(b"\r\n\r\n")).decode()
                request_line, *header_lines = head.split("\r\n")
                pairs = (line.split(": ", 1) for line in header_lines if ": " in line)
                headers = {name.lower(): value for name, value in pairs}
                self.headers.append(headers)
                await reader.readexactly(int(headers.get("content-length", "0")))
                target = request_line.split(" ")[1]
                path = target.partition("?")[0]
                stream = next((body for end, body in _STREAMS.items() if path.endswith(end)), None)
                if self.redirect is not None and not path.startswith("/moved"):
                    code, url = self.redirect
                    writer.write(
                        b"HTTP/1.1 %d Redirect\r\nLocation: %s/moved%s\r\nContent-Length: 0\r\n\r\n"
                        % (code, url.encode(), target.encode()),
                    )
                elif stream is None:
                    body = json.dumps(_EMPTY_PAGE).encode()
                    status = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                    writer.write(status % len(body) + body)
                else:
                    start = b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n"
                    writer.write(start + b"%x\r\n%s\r\n0\r\n\r\n" % (len(stream), stream))
                await writer.drain()
        except asyncio.IncompleteReadError:
            self.closed.set()
            writer.close()


async def _serving() -> AsyncIterator[_ModelsServer]:
    """Serve models for one test, then close the connections its models pooled."""
    models_server = _ModelsServer()
    listener = await asyncio.start_server(models_server.serve, "127.0.0.1", 0)
    models_server.url = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
    yield models_server
    await aclose_model_http_client()
    listener.close()


server = pytest_asyncio.fixture(_serving, name="server")
# A second server listens on another port, so on another origin than ``server``.
other_server = pytest_asyncio.fixture(_serving, name="other_server")


def _model(tmp_path: Path, provider: str, api: str | None = None, **settings: object) -> ModelProvider:
    """Build the configured model as the runtime does, with a test key and these ``extra_kwargs``."""
    runtime_paths = test_runtime_paths(tmp_path)
    model = ModelConfig(provider=provider, id="test-model", api=api, extra_kwargs={"api_key": "test", **settings})
    config = bind_runtime_paths(Config(models={"default": model}), runtime_paths)
    return build_model_provider(config, runtime_paths)


def _provider(tmp_path: Path, provider: str, base_url: str, api: str | None = None) -> ModelProvider:
    """Build the configured model, sending to ``base_url``."""
    return _model(
        tmp_path,
        provider,
        api,
        client_params={"base_url": f"{base_url}/v1" if provider == "openai" else base_url},
    )


def _keepalive_option(local_address: tuple[str, int], option: int) -> int:
    """Read one keep-alive option from this process's socket bound to the given address."""
    level = socket.SOL_SOCKET if option == socket.SO_KEEPALIVE else socket.IPPROTO_TCP
    for path in Path("/dev/fd").iterdir():
        try:
            sock = socket.socket(fileno=os.dup(int(path.name)))
        except OSError:
            continue
        with sock:
            if sock.family == socket.AF_INET and sock.getsockname() == local_address:
                return sock.getsockopt(level, option)
    msg = f"no socket bound to {local_address}"
    raise AssertionError(msg)


async def _send(provider: ModelProvider) -> None:
    await complete(provider, _REQUEST)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_collected_model_leaves_its_pooled_connection_alone(
    tmp_path: Path,
    server: _ModelsServer,
    provider: str,
) -> None:
    """Collecting a dropped model schedules no late close, and the next model reuses its connection."""
    gc.disable()
    try:
        gc.collect()  # Leave only this test's model to the collection below.
        await _send(_provider(tmp_path, provider, server.url))
        tasks_before = asyncio.all_tasks()
        gc.collect()
        assert asyncio.all_tasks() <= tasks_before
    finally:
        gc.enable()
    await _send(_provider(tmp_path, provider, server.url))
    assert server.connections == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "api"),
    [("openai", "chat_completions"), ("openai", "responses"), ("anthropic", None)],
)
async def test_a_streamed_reply_returns_its_connection_to_the_pool(
    tmp_path: Path,
    server: _ModelsServer,
    provider: str,
    api: str | None,
) -> None:
    """A reply streamed to its end leaves its connection pooled, so the next request opens no new one."""
    model = _provider(tmp_path, provider, server.url, api)
    for _ in range(2):
        turn = await complete(model, _REQUEST)
        assert turn.message.content == "ok"
    assert server.connections == 1


@pytest.mark.asyncio
async def test_models_built_in_one_loop_share_one_connection(tmp_path: Path, server: _ModelsServer) -> None:
    """A model built in a worker thread reuses the loop's connection."""
    first = _provider(tmp_path, "anthropic", server.url)
    second = await asyncio.to_thread(_provider, tmp_path, "anthropic", server.url)
    await _send(first)
    await _send(second)
    assert server.connections == 1


@pytest.mark.asyncio
async def test_one_model_sends_from_the_runtime_loop_and_a_finishing_thread_loop(
    tmp_path: Path,
    server: _ModelsServer,
) -> None:
    """Each loop gets its own connection, and a loop that finishes closes its own before it ends."""
    provider = _provider(tmp_path, "openai", server.url)
    await _send(provider)
    await asyncio.to_thread(asyncio.run, closing_model_connections(_send(provider)))
    await asyncio.wait_for(server.closed.wait(), timeout=5)
    await _send(provider)
    assert server.connections == 2


@pytest.mark.asyncio
async def test_finished_loop_pool_is_dropped(tmp_path: Path, server: _ModelsServer) -> None:
    """A loop that ended without closing its pool loses it at the next request, so its sockets get collected."""
    provider = _provider(tmp_path, "openai", server.url)
    await asyncio.to_thread(asyncio.run, _send(provider))
    await _send(provider)
    gc.collect()
    await asyncio.wait_for(server.closed.wait(), timeout=5)


@pytest.mark.asyncio
async def test_knowledge_graph_loop_closes_its_pool_when_it_stops(tmp_path: Path, server: _ModelsServer) -> None:
    """Stopping the knowledge graph's loop closes the model connections it opened."""
    provider = _provider(tmp_path, "openai", server.url)
    graph = _GraphLoop()
    await run_on_loop(graph.loop, _send(provider))
    await graph.stop()
    await asyncio.wait_for(server.closed.wait(), timeout=5)


@pytest.mark.asyncio
async def test_pooled_connections_probe_idle_tcp_connections(tmp_path: Path, server: _ModelsServer) -> None:
    """Pooled connections send TCP keep-alive probes, as the Anthropic SDK's own client does, to survive NAT timeouts."""
    await _send(_provider(tmp_path, "anthropic", server.url))
    assert _keepalive_option(server.peer, socket.SO_KEEPALIVE)
    # macOS names the idle time TCP_KEEPALIVE.
    idle_option = socket.TCP_KEEPIDLE if hasattr(socket, "TCP_KEEPIDLE") else socket.TCP_KEEPALIVE
    assert _keepalive_option(server.peer, idle_option) == 60


@pytest.mark.asyncio
async def test_pooled_connections_honor_proxy_environment(
    tmp_path: Path,
    server: _ModelsServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pools send through the proxy that the environment names, as the SDKs' own clients do."""
    for name in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy", "http_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", server.url)
    await _send(_provider(tmp_path, "openai", "http://models.invalid"))
    assert server.connections == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_embedder_clients")
async def test_openai_embedder_reuses_the_model_connection(tmp_path: Path, server: _ModelsServer) -> None:
    """Async embedding requests go through the same pooled connection as the models."""
    await _send(_provider(tmp_path, "openai", server.url))
    embedder = OpenAIEmbedder(
        model="test-embedding",
        api_key="test",
        base_url=f"{server.url}/v1",
        # A recorder from a retired generation, so the failure below leaves process-wide health alone.
        health_recorder=EmbedderHealthRecorder(generation=-1),
    )
    # The server answers with an empty list, which the embedder rejects after the request went out.
    with pytest.raises(EmbedderRequestError, match="0 embeddings for 1 inputs"):
        await embedder.aembed(["hello"], purpose="query")
    assert server.connections == 1


@pytest.mark.asyncio
async def test_runtime_shutdown_closes_pooled_model_connections(tmp_path: Path, server: _ModelsServer) -> None:
    """Runtime shutdown closes the idle connections models left in the loop's pool."""
    await _send(_provider(tmp_path, "openai", server.url))
    await _finish_runtime_shutdown(
        shutdown_wait_task=None,
        api_task=None,
        orchestrator_task=None,
        auxiliary_tasks=[],
        orchestrator=None,
        stall_detector=None,
    )
    await asyncio.wait_for(server.closed.wait(), timeout=5)


# A model request's own headers: the key its SDK sends, a header its config authors, and a header its run binds.
_OWN_HEADERS = {"x-api-key", "api-key", "authorization", "x-config", "x-run"}
_CLAUDE_OWN_HEADERS = {"x-api-key", "x-config", "x-run"}
_BODY_HEADERS = {"content-type", "content-length", "transfer-encoding"}


def _model_with_headers(tmp_path: Path, route: str, url: str) -> ModelProvider:
    """Build a Claude or Azure OpenAI model sending to ``url`` with its key and a header its config authors."""
    if route == "anthropic":
        return _model(tmp_path, route, default_headers={"x-config": "set"}, client_params={"base_url": url})
    return _model(tmp_path, route, azure_endpoint=url, extra_headers={"x-config": "set"})


def _own(headers: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in headers.items() if name in _OWN_HEADERS}


async def _send_with_run_headers(provider: ModelProvider) -> None:
    with bind_request_headers(lambda: {"x-run": "run"}):
        await _send(provider)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [307, 302])
@pytest.mark.parametrize(("route", "key"), [("anthropic", "x-api-key"), ("azure", "api-key")])
async def test_a_redirect_to_another_origin_gets_only_a_fresh_request_s_headers(
    tmp_path: Path,
    server: _ModelsServer,
    other_server: _ModelsServer,
    route: str,
    key: str,
    status: int,
) -> None:
    """A model request redirected to another origin sends it no key and no header its config or run set.

    The other origin gets only the headers any request from the client carries and the ones describing the body.
    """
    server.redirect = (status, other_server.url)
    await _send_with_run_headers(_model_with_headers(tmp_path, route, server.url))
    assert set(_own(server.headers[0])) == {key, "x-config", "x-run"}
    (redirected,) = other_server.headers
    fresh = {**MODEL_HTTP_CLIENT.headers, "host": other_server.url.removeprefix("http://")}
    assert {name: value for name, value in redirected.items() if name not in _BODY_HEADERS} == fresh


@pytest.mark.asyncio
async def test_a_redirect_within_the_origin_keeps_the_request_s_own_headers(
    tmp_path: Path,
    server: _ModelsServer,
) -> None:
    """A model request redirected to another path of its own origin sends there every header it sent first."""
    server.redirect = (307, server.url)
    await _send_with_run_headers(_model_with_headers(tmp_path, "anthropic", server.url))
    first, redirected = (_own(headers) for headers in server.headers)
    assert (set(first), redirected) == (_CLAUDE_OWN_HEADERS, first)


@pytest.mark.asyncio
async def test_an_upgrade_to_https_keeps_the_request_s_own_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redirect from a host's plain HTTP port to its HTTPS port reaches the same server, so it keeps every header."""
    sent: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        if request.url.scheme == "http":
            return httpx2.Response(308, headers={"location": str(request.url.copy_with(scheme="https"))})
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=_STREAMS["/messages"])

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", lambda **_: httpx2.MockTransport(respond))
    try:
        await _send_with_run_headers(_model_with_headers(tmp_path, "anthropic", "http://models.example"))
    finally:
        await aclose_model_http_client()
    (first_scheme, first), (redirected_scheme, redirected) = (
        (request.url.scheme, _own(request.headers)) for request in sent
    )
    assert (first_scheme, redirected_scheme, set(first), redirected) == ("http", "https", _CLAUDE_OWN_HEADERS, first)


@pytest.mark.asyncio
async def test_a_download_follows_a_redirect_to_another_origin(
    server: _ModelsServer,
    other_server: _ModelsServer,
) -> None:
    """Media URLs, which remote media downloads through the models' client, still follow a redirect to another origin."""
    server.redirect = (302, other_server.url)
    async with MODEL_HTTP_CLIENT.stream("GET", f"{server.url}/media.png") as response:
        await response.aread()
    assert (response.status_code, response.url) == (200, f"{other_server.url}/moved/media.png")
