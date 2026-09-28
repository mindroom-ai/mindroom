"""Local HTTP boundary for Gemini API and Vertex AI SDK clients."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx
from google import genai
from google.genai.types import HttpOptions, HttpRetryOptions
from google.oauth2.credentials import Credentials

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable


@asynccontextmanager
async def gemini_client(
    respond: Callable[[httpx.Request], httpx.Response],
    *,
    vertexai: bool,
) -> AsyncIterator[genai.Client]:
    """Yield one SDK client whose requests reach ``respond``, closing it afterwards."""
    transport = httpx.MockTransport(respond)
    http_options = HttpOptions(
        httpx_client=httpx.Client(transport=transport),
        httpx_async_client=httpx.AsyncClient(transport=transport),
        retry_options=HttpRetryOptions(attempts=1),
    )
    if vertexai:
        client = genai.Client(
            vertexai=True,
            project="test-project",
            location="us-central1",
            credentials=Credentials(token="test-token"),  # noqa: S106 - synthetic SDK credential
            http_options=http_options,
        )
    else:
        client = genai.Client(api_key="test-key", http_options=http_options)
    try:
        yield client
    finally:
        await client.aio.aclose()
        client.close()


def gemini_response(*parts: dict[str, Any]) -> httpx.Response:
    """Return one finished Gemini candidate with the given content parts."""
    return httpx.Response(
        200,
        json={"candidates": [{"content": {"role": "model", "parts": list(parts)}, "finishReason": "STOP"}]},
    )


def gemini_decision_response(payload: dict[str, Any], decision: str, leaked_call: dict[str, Any]) -> httpx.Response:
    """Answer a decision like live Gemini: function calls escape mode NONE unless JSON output is required."""
    if payload.get("generationConfig", {}).get("responseMimeType") == "application/json":
        return gemini_response({"text": decision})
    return gemini_response({"functionCall": leaked_call})
