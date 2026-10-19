"""A garbage-collected provider SDK client must not unregister descriptors that new sockets reuse."""

from __future__ import annotations

import asyncio
import gc
import os
import socket
from pathlib import Path

import anthropic
import openai
import pytest

from mindroom.providers.agno_compat_sdk_clients import remove_sdk_client_finalizers

# A page that both SDKs' `models.list()` accept.
_MODELS_PAGE = b'{"object": "list", "data": [], "has_more": false, "first_id": null, "last_id": null}'


async def _serve_models(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Answer every request on a keep-alive connection with an empty model list."""
    try:
        while True:
            await reader.readuntil(b"\r\n\r\n")
            headers = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n"
            writer.write(headers % len(_MODELS_PAGE) + _MODELS_PAGE)
            await writer.drain()
    except asyncio.IncompleteReadError:
        writer.close()


async def _drop_client_with_idle_connection(sdk: str, base_url: str) -> None:
    """Make one request so the client pools an idle connection, then leave the client to the garbage collector."""
    client = (
        openai.AsyncOpenAI(base_url=f"{base_url}/v1", api_key="test", max_retries=0)
        if sdk == "openai"
        else anthropic.AsyncAnthropic(base_url=base_url, api_key="test", max_retries=0)
    )
    await client.models.list()


def _is_open(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
    except OSError:
        return False
    return True


def _open_descriptors() -> set[int]:
    # Listing /dev/fd opens a descriptor of its own, which is closed again by the time it is checked.
    return {descriptor for descriptor in (int(path.name) for path in Path("/dev/fd").iterdir()) if _is_open(descriptor)}


@pytest.mark.asyncio
@pytest.mark.parametrize("sdk", ["openai", "anthropic"])
async def test_collected_sdk_client_leaves_reused_descriptor_registered(sdk: str) -> None:
    """A socket that reuses a collected client's descriptor still gets its events."""
    remove_sdk_client_finalizers()
    server = await asyncio.start_server(_serve_models, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    loop = asyncio.get_running_loop()
    gc.disable()
    try:
        await _drop_client_with_idle_connection(sdk, f"http://127.0.0.1:{port}")
        open_before = _open_descriptors()
        tasks_before = asyncio.all_tasks()
        gc.collect()
        freed = open_before - _open_descriptors()
        scheduled_by_collection = asyncio.all_tasks() - tasks_before
        assert freed, "the collection closed no client socket"
        # Fill lower free descriptors so the next socket takes the one the collection freed.
        fillers = [socket.socket()]
        while fillers[-1].fileno() not in freed:
            fillers.append(socket.socket())
        fillers.pop().close()
        reused, peer = socket.socketpair()
        try:
            assert reused.fileno() in freed
            readable = asyncio.Event()
            loop.add_reader(reused, readable.set)
            # Anything the collection scheduled runs while the new socket still waits, as a connect or response would.
            await asyncio.gather(*scheduled_by_collection)
            peer.send(b"x")
            await asyncio.wait_for(readable.wait(), timeout=5)
        finally:
            loop.remove_reader(reused)
            for sock in (reused, peer, *fillers):
                sock.close()
    finally:
        gc.enable()
        server.close()
        await server.wait_closed()
