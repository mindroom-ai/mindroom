"""Google SDK error evidence must survive the provider retry boundary."""

from __future__ import annotations

import json
import sys

import httpx
import pytest
from agno.exceptions import ModelProviderError
from agno.models.message import Message
from google import genai
from google.genai.types import HttpOptions, HttpRetryOptions
from google.oauth2.credentials import Credentials
from openai import APIStatusError

from mindroom import provider_stream_retry
from mindroom.google_gemini import MindRoomGoogleGemini
from mindroom.provider_error_compat import is_transient_stream_error
from mindroom.provider_stream_retry import install_provider_stream_retry_hook


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_error", [False, True], ids=["http", "sse"])
@pytest.mark.parametrize("status", [502, 400])
async def test_google_sdk_status_controls_stream_retry(
    status: int,
    *,
    stream_error: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry typed gateway failures while preserving permanent SDK failures."""
    requests: list[dict[str, object]] = []
    delays: list[float] = []

    async def wait(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(provider_stream_retry.asyncio, "sleep", wait)

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            error = {"error": {"code": status, "message": "Provider request failed"}}
            if stream_error:
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=f"data: {json.dumps(error)}\n\n",
                )
            return httpx.Response(status, json=error)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content='data: {"candidates":[{"content":{"role":"model","parts":[{"text":"Recovered"}]},'
            '"finishReason":"STOP"}]}\n\n',
        )

    transport = httpx.MockTransport(respond)
    client = genai.Client(
        vertexai=True,
        project="test-project",
        location="us-central1",
        credentials=Credentials(token="test-token"),  # noqa: S106 - synthetic SDK credential
        http_options=HttpOptions(
            httpx_client=httpx.Client(transport=transport),
            httpx_async_client=httpx.AsyncClient(transport=transport),
            retry_options=HttpRetryOptions(attempts=1),
        ),
    )
    model = MindRoomGoogleGemini(id="test-model", client=client, vertexai=True)
    install_provider_stream_retry_hook(model)
    try:
        stream = model.ainvoke_stream([Message(role="user", content="Hello")], Message(role="assistant"))
        if status == 400:
            with pytest.raises(ModelProviderError):
                _ = [chunk async for chunk in stream]
            assert len(requests) == 1
            assert not delays
        else:
            chunks = [chunk async for chunk in stream]
            assert "".join(chunk.content or "" for chunk in chunks) == "Recovered"
            assert len(requests) == 2
            assert requests[0] == requests[1]
            assert len(delays) == 1
            assert 1 <= delays[0] <= 1.25
    finally:
        await client.aio.aclose()
        client.close()


@pytest.mark.parametrize("typed_status", [False, True])
def test_classification_without_google_sdk(typed_status: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Other providers must not require importing the optional Google SDK."""
    monkeypatch.setitem(sys.modules, "google.genai.errors", None)
    cause = (
        APIStatusError(
            "Gateway failed",
            response=httpx.Response(502, request=httpx.Request("POST", "https://example.org")),
            body=None,
        )
        if typed_status
        else ValueError("Unclassified failure")
    )
    error = ModelProviderError("Provider failed")
    error.__cause__ = cause

    assert is_transient_stream_error(error) is typed_status
