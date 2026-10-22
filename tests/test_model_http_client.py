"""Models send through one runtime-owned HTTP client, so a collected model never touches the event loop."""

from __future__ import annotations

import asyncio
import gc
import json
import os
import socket
from pathlib import Path
from typing import TYPE_CHECKING

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
from mindroom.orchestrator import _finish_runtime_shutdown
from mindroom.providers.model_http_client import aclose_model_http_client, closing_model_connections
from mindroom.providers.registry import build_model_provider
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.llm.provider import ModelProvider

# A finished reply of each API, and an empty page every other request accepts.
_CHAT_COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1,
    "model": "test-model",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}
_CLAUDE_MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "test-model",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
_EMPTY_PAGE = {"object": "list", "data": [], "has_more": False, "first_id": None, "last_id": None}
_REQUEST = ModelRequest(messages=[Message(role="user", content="Hi")], stream=False)


class _ModelsServer:
    """A keep-alive server that answers chat and message requests with a finished reply, anything else empty."""

    def __init__(self) -> None:
        self.url = ""
        self.connections = 0
        self.peer: tuple[str, int] = ("", 0)
        self.closed = asyncio.Event()

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self.peer = writer.get_extra_info("peername")[:2]
        try:
            while True:
                head = (await reader.readuntil(b"\r\n\r\n")).decode()
                request_line, *header_lines = head.split("\r\n")
                headers = dict(line.lower().split(": ", 1) for line in header_lines if ": " in line)
                await reader.readexactly(int(headers.get("content-length", "0")))
                path = request_line.split(" ")[1]
                reply = (
                    _CHAT_COMPLETION
                    if path.endswith("/chat/completions")
                    else _CLAUDE_MESSAGE
                    if path.endswith("/messages")
                    else _EMPTY_PAGE
                )
                body = json.dumps(reply).encode()
                status = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                writer.write(status % len(body) + body)
                await writer.drain()
        except asyncio.IncompleteReadError:
            self.closed.set()
            writer.close()


@pytest_asyncio.fixture
async def server() -> AsyncIterator[_ModelsServer]:
    """Serve models for one test, then close the connections its models pooled."""
    models_server = _ModelsServer()
    listener = await asyncio.start_server(models_server.serve, "127.0.0.1", 0)
    models_server.url = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
    yield models_server
    await aclose_model_http_client()
    listener.close()


def _provider(tmp_path: Path, provider: str, base_url: str) -> ModelProvider:
    """Build the configured model as the runtime does, sending to ``base_url``."""
    runtime_paths = test_runtime_paths(tmp_path)
    client_params = {"base_url": f"{base_url}/v1" if provider == "openai" else base_url, "max_retries": 0}
    extra_kwargs = {"api_key": "test", "client_params": client_params}
    model = ModelConfig(provider=provider, id="test-model", extra_kwargs=extra_kwargs)
    config = bind_runtime_paths(Config(models={"default": model}), runtime_paths)
    return build_model_provider(config, runtime_paths)


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
