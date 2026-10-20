"""Models send through one runtime-owned HTTP client, so a collected model never touches the event loop."""

from __future__ import annotations

import asyncio
import copy
import gc
import os
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.embeddings.openai_embedder import MindRoomOpenAIEmbedder
from mindroom.knowledge_graph.store import _GraphLoop, run_on_loop
from mindroom.orchestrator import _finish_runtime_shutdown
from mindroom.providers.claude_prompt_cache import aclose_anthropic_async_client
from mindroom.providers.model_http_client import (
    MODEL_HTTP_CLIENT,
    aclose_model_http_client,
    closing_model_connections,
)
from mindroom.providers.model_loading import _SHARED_HTTP_CLIENT_PROVIDERS, get_model_instance
from mindroom.tool_system.tool_hooks import _run_coroutine_from_sync
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.constants import RuntimePaths

# A page that both SDKs' `models.list()` accept.
_MODELS_PAGE = b'{"object": "list", "data": [], "has_more": false, "first_id": null, "last_id": null}'


class _ModelsServer:
    """A keep-alive server that answers every request with an empty model list."""

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
                await reader.readuntil(b"\r\n\r\n")
                headers = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                writer.write(headers % len(_MODELS_PAGE) + _MODELS_PAGE)
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


def _model_config(tmp_path: Path, provider: str, base_url: str) -> tuple[Config, RuntimePaths]:
    runtime_paths = test_runtime_paths(tmp_path)
    client_params = {"base_url": f"{base_url}/v1" if provider == "openai" else base_url, "max_retries": 0}
    extra_kwargs = {"api_key": "test", "client_params": client_params}
    model = ModelConfig(provider=provider, id="test-model", extra_kwargs=extra_kwargs)
    return bind_runtime_paths(Config(models={"default": model}), runtime_paths), runtime_paths


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


async def _list_models(client: Any) -> None:  # noqa: ANN401 - either SDK's client
    await client.models.list()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_collected_model_leaves_its_pooled_connection_alone(
    tmp_path: Path,
    server: _ModelsServer,
    provider: str,
) -> None:
    """Collecting a dropped model schedules no late close, and the next model reuses its connection."""
    config, runtime_paths = _model_config(tmp_path, provider, server.url)
    gc.disable()
    try:
        gc.collect()  # Leave only this test's model to the collection below.
        await get_model_instance(config, runtime_paths).get_async_client().models.list()
        tasks_before = asyncio.all_tasks()
        gc.collect()
        assert asyncio.all_tasks() <= tasks_before
    finally:
        gc.enable()
    await get_model_instance(config, runtime_paths).get_async_client().models.list()
    assert server.connections == 1


@pytest.mark.asyncio
async def test_models_built_in_one_loop_share_one_connection(tmp_path: Path, server: _ModelsServer) -> None:
    """A model built in a worker thread reuses the loop's connection, even after a turn closes its Claude client."""
    config, runtime_paths = _model_config(tmp_path, "anthropic", server.url)
    first = get_model_instance(config, runtime_paths)
    second = await asyncio.to_thread(get_model_instance, config, runtime_paths)
    await first.get_async_client().models.list()
    await aclose_anthropic_async_client(first)
    client = second.get_async_client()
    await client.models.list()
    assert not client.is_closed()
    assert server.connections == 1


@pytest.mark.asyncio
async def test_one_model_sends_from_the_runtime_loop_and_a_finishing_thread_loop(
    tmp_path: Path,
    server: _ModelsServer,
) -> None:
    """Each loop gets its own connection, and a loop that finishes closes its own before it ends."""
    config, runtime_paths = _model_config(tmp_path, "openai", server.url)
    client = get_model_instance(config, runtime_paths).get_async_client()
    await client.models.list()
    await asyncio.to_thread(asyncio.run, closing_model_connections(client.models.list()))
    await asyncio.wait_for(server.closed.wait(), timeout=5)
    await client.models.list()
    assert server.connections == 2


@pytest.mark.asyncio
async def test_sync_tool_hook_bridge_closes_its_loop_pool(tmp_path: Path, server: _ModelsServer) -> None:
    """Hook work run on its own loop from a worker thread closes that loop's model connections when it ends."""
    config, runtime_paths = _model_config(tmp_path, "openai", server.url)
    client = get_model_instance(config, runtime_paths).get_async_client()
    await asyncio.to_thread(_run_coroutine_from_sync, _list_models(client))
    await asyncio.wait_for(server.closed.wait(), timeout=5)


@pytest.mark.asyncio
async def test_finished_loop_pool_is_dropped(tmp_path: Path, server: _ModelsServer) -> None:
    """A loop that ended without closing its pool loses it at the next request, so its sockets get collected."""
    config, runtime_paths = _model_config(tmp_path, "openai", server.url)
    client = get_model_instance(config, runtime_paths).get_async_client()
    await asyncio.to_thread(asyncio.run, _list_models(client))
    await client.models.list()
    gc.collect()
    await asyncio.wait_for(server.closed.wait(), timeout=5)


@pytest.mark.asyncio
async def test_knowledge_graph_loop_closes_its_pool_when_it_stops(tmp_path: Path, server: _ModelsServer) -> None:
    """Stopping the knowledge graph's loop closes the model connections it opened."""
    config, runtime_paths = _model_config(tmp_path, "openai", server.url)
    graph = _GraphLoop()
    await run_on_loop(graph.loop, _list_models(get_model_instance(config, runtime_paths).get_async_client()))
    await graph.stop()
    await asyncio.wait_for(server.closed.wait(), timeout=5)


@pytest.mark.asyncio
async def test_pooled_connections_probe_idle_tcp_connections(tmp_path: Path, server: _ModelsServer) -> None:
    """Pooled connections send TCP keep-alive probes, as the Anthropic SDK's own client does, to survive NAT timeouts."""
    config, runtime_paths = _model_config(tmp_path, "anthropic", server.url)
    await _list_models(get_model_instance(config, runtime_paths).get_async_client())
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
    config, runtime_paths = _model_config(tmp_path, "openai", "http://models.invalid")
    await _list_models(get_model_instance(config, runtime_paths).get_async_client())
    assert server.connections == 1


@pytest.mark.asyncio
async def test_openai_embedder_reuses_the_model_connection(tmp_path: Path, server: _ModelsServer) -> None:
    """Async embedding requests go through the same pooled connection as the models."""
    config, runtime_paths = _model_config(tmp_path, "openai", server.url)
    await get_model_instance(config, runtime_paths).get_async_client().models.list()
    embedder = MindRoomOpenAIEmbedder(id="test-embedding", api_key="test", base_url=f"{server.url}/v1")
    await embedder.aclient.models.list()
    assert server.connections == 1


@pytest.mark.asyncio
async def test_runtime_shutdown_closes_pooled_model_connections(tmp_path: Path, server: _ModelsServer) -> None:
    """Runtime shutdown closes the idle connections models left in the loop's pool."""
    config, runtime_paths = _model_config(tmp_path, "openai", server.url)
    await get_model_instance(config, runtime_paths).get_async_client().models.list()
    await _finish_runtime_shutdown(
        shutdown_wait_task=None,
        api_task=None,
        orchestrator_task=None,
        auxiliary_tasks=[],
        orchestrator=None,
        stall_detector=None,
    )
    await asyncio.wait_for(server.closed.wait(), timeout=5)


_PROVIDER_SETTINGS = {
    "azure": {"azure_endpoint": "https://example.openai.azure.com", "api_version": "2024-06-01"},
    "bedrock_claude": {"aws_region": "us-east-1", "aws_access_key": "test", "aws_secret_key": "test"},
    "vertexai_claude": {"project_id": "test", "region": "us-east5"},
}


@pytest.mark.parametrize("provider", sorted(_SHARED_HTTP_CLIENT_PROVIDERS))
def test_deep_copied_model_keeps_the_shared_http_client(tmp_path: Path, provider: str) -> None:
    """Agno Learning deep-copies the model every run, and the copy must keep sending through the shared client."""
    runtime_paths = test_runtime_paths(tmp_path)
    extra_kwargs = {"api_key": "test", **_PROVIDER_SETTINGS.get(provider, {})}
    model = ModelConfig(provider=provider, id="gpt-6-astra", extra_kwargs=extra_kwargs)
    config = bind_runtime_paths(Config(models={"default": model}), runtime_paths)
    assert copy.deepcopy(get_model_instance(config, runtime_paths)).http_client is MODEL_HTTP_CLIENT
