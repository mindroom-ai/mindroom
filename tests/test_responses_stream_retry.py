"""Native Responses failures retain retry policy through the real SDK and model factory."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import pytest
from agno.exceptions import ContextWindowExceededError, ModelProviderError
from agno.models.message import Message
from openai import OpenAI

from mindroom import provider_stream_retry
from mindroom.error_handling import IncompleteResponsesStreamError
from tests.test_openai_responses_stream import _created, _event, _response
from tests.test_provider_stream_retry import _model, _Provider
from tests.test_provider_stream_retry import retry_delays as retry_delays  # noqa: PLC0414 - expose shared fixture

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path


def _failure(kind: str, code: str | None = "server_error") -> str:
    message = "Provider could not finish the request"
    if kind == "error":
        return _event("error", code=code, message=message, param=None)
    return _event(
        "response.failed",
        response={**_response("resp_failed", "failed"), "error": {"code": code, "message": message}},
    )


def _answer(content: str, *, complete: bool = True) -> str:
    stream = _created("resp_answer") + _event(
        "response.output_text.delta",
        item_id="msg_answer",
        output_index=0,
        content_index=0,
        delta=content,
    )
    if complete:
        stream += _event("response.completed", response=_response("resp_answer", "completed"))
    return stream


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["error", "response.failed"])
@pytest.mark.parametrize("code", ["server_error", "rate_limit_exceeded", "vector_store_timeout"])
@pytest.mark.parametrize("created", [False, True])
async def test_native_transient_failure_retries_before_output(
    kind: str,
    code: str,
    *,
    created: bool,
    tmp_path: Path,
    retry_delays: list[float],
) -> None:
    """Typed native failures must reach the retry owner even after response.created."""
    failure = (_created() if created else "") + _failure(kind, code)
    provider = _Provider([failure, _answer("Recovered")])
    async with _model(provider, tmp_path, api="responses") as model:
        chunks = [chunk async for chunk in model.ainvoke_stream([], Message(role="assistant"))]

    assert "".join(chunk.content or "" for chunk in chunks) == "Recovered"
    assert len(provider.requests) == 2
    assert provider.requests[0] == provider.requests[1]
    assert len(retry_delays) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["error", "response.failed"])
async def test_native_transient_failure_exhausts_one_retry_budget(
    kind: str,
    tmp_path: Path,
    retry_delays: list[float],
) -> None:
    """A native terminal error must not escape the same bounded retry policy."""
    provider = _Provider([_created() + _failure(kind) for _ in range(6)])
    async with _model(provider, tmp_path, api="responses") as model:
        with pytest.raises(ModelProviderError) as raised:
            _ = [chunk async for chunk in model.ainvoke_stream([], Message(role="assistant"))]

    assert raised.value.status_code == 500
    assert len(provider.requests) == 5
    assert len(retry_delays) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["error", "response.failed"])
@pytest.mark.parametrize("code", ["invalid_prompt", "context_length_exceeded", "unknown_error", None])
async def test_native_permanent_or_unclassified_failure_does_not_retry(
    kind: str,
    code: str | None,
    tmp_path: Path,
    retry_delays: list[float],
) -> None:
    """Terminal errors retain context-limit typing without replaying unknown failures."""
    provider = _Provider([_created() + _failure(kind, code), _answer("Must not run")])
    async with _model(provider, tmp_path, api="responses") as model:
        with pytest.raises(ModelProviderError) as raised:
            _ = [chunk async for chunk in model.ainvoke_stream([], Message(role="assistant"))]

    assert raised.value.status_code == 400
    assert isinstance(raised.value, ContextWindowExceededError) == (code == "context_length_exceeded")
    assert len(provider.requests) == 1
    assert not retry_delays


@dataclass
class _UnreadBody(httpx.SyncByteStream, httpx.AsyncByteStream):
    data: str
    closed: bool = False

    def __iter__(self) -> Iterator[bytes]:
        yield self.data.encode()
        yield b"\n"

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self:
            yield chunk

    def close(self) -> None:
        self.closed = True

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("finish", ["retry", "partial", "close", "cancel"])
async def test_native_stream_closes_unread_transport(  # noqa: C901, PLR0912
    finish: str,
    *,
    sync: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retry_delays: list[float],
) -> None:
    """Closing the Agno iterator alone must not strand the SDK HTTP response."""
    data = "" if finish == "retry" else _answer("Partial", complete=False)
    if finish in {"retry", "partial"}:
        data += _failure("error")
    body = _UnreadBody(data)
    provider = _Provider(
        [
            httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body),
            _answer("Recovered"),
        ],
    )

    original_respond = provider.respond

    def respond(request: httpx.Request) -> httpx.Response:
        if provider.requests:
            assert body.closed, "Failed HTTP stream must close before its retry"
        return original_respond(request)

    monkeypatch.setattr(provider, "respond", respond)
    monkeypatch.setattr(provider_stream_retry.time, "sleep", retry_delays.append)
    async with _model(provider, tmp_path, api="responses") as model:
        # Both clients use the same transport assertion at the next request.
        with OpenAI(
            api_key="test-key",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        ) as client:
            model.client = client
            if finish == "retry":
                chunks = (
                    list(model.invoke_stream([], Message(role="assistant")))
                    if sync
                    else [chunk async for chunk in model.ainvoke_stream([], Message(role="assistant"))]
                )
                assert "".join(chunk.content or "" for chunk in chunks) == "Recovered"
            elif finish == "partial":
                with pytest.raises(IncompleteResponsesStreamError):  # noqa: PT012 - exercise sync and async owners
                    if sync:
                        list(model.invoke_stream([], Message(role="assistant")))
                    else:
                        _ = [chunk async for chunk in model.ainvoke_stream([], Message(role="assistant"))]
            elif sync:
                stream = model.invoke_stream([], Message(role="assistant"))
                while not next(stream).content:
                    pass
                if finish == "cancel":
                    with pytest.raises(asyncio.CancelledError):
                        stream.throw(asyncio.CancelledError())
                else:
                    stream.close()
            else:
                stream = model.ainvoke_stream([], Message(role="assistant"))
                while not (await anext(stream)).content:
                    pass
                if finish == "cancel":
                    with pytest.raises(asyncio.CancelledError):
                        await stream.athrow(asyncio.CancelledError())
                else:
                    await stream.aclose()
    assert body.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["error", "response.failed"])
async def test_native_failure_after_output_cannot_replay(
    kind: str,
    tmp_path: Path,
    retry_delays: list[float],
) -> None:
    """Normalizing a terminal failure must not authorize replay of published output."""
    provider = _Provider([_answer("Partial", complete=False) + _failure(kind), _answer("Must not run")])
    chunks = []
    async with _model(provider, tmp_path, api="responses") as model:
        model.retries = 1
        with pytest.raises(IncompleteResponsesStreamError):  # noqa: PT012 - retain chunks yielded before failure
            async for chunk in model._ainvoke_stream_with_retry(
                messages=[],
                assistant_message=Message(role="assistant"),
            ):
                chunks.append(chunk)  # noqa: PERF401 - retain chunks when the iterator raises

    assert "".join(chunk.content or "" for chunk in chunks) == "Partial"
    assert len(provider.requests) == 1
    assert not retry_delays
