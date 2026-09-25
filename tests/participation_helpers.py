"""Deterministic provider boundary for participation tests."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

import httpx
from agno.models.response import ModelResponse
from google import genai
from google.genai.types import HttpOptions, HttpRetryOptions
from google.oauth2.credentials import Credentials

from mindroom.synthetic_model import SyntheticModel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from agno.models.message import Message


class ParticipationModel(SyntheticModel):
    """Provider boundary double; real Agno response/tool loop remains in use."""

    def __init__(self, decision: ModelResponse | BaseException) -> None:
        super().__init__(id="test", name="test", provider="test")
        self.decision = decision
        self.requests: list[dict[str, Any]] = []

    async def ainvoke(
        self,
        messages: list[Message],
        **kwargs: object,
    ) -> ModelResponse:
        """Capture provider inputs and return the next deterministic response."""
        self.requests.append(
            {
                "messages": deepcopy(messages),
                **{
                    key: deepcopy(kwargs.get(key))
                    for key in ("tools", "tool_choice", "response_format", "compress_tool_results")
                },
            },
        )
        if len(self.requests) == 1:
            if isinstance(self.decision, BaseException):
                raise self.decision
            return self.decision
        return ModelResponse(content="Useful answer")

    async def ainvoke_stream(
        self,
        messages: list[Message],
        **kwargs: object,
    ) -> AsyncIterator[ModelResponse]:
        """Stream the next deterministic response through the normal model loop."""
        yield await self.ainvoke(messages, **kwargs)


def gemini_client(respond: Callable[[httpx.Request], httpx.Response], *, vertexai: bool) -> genai.Client:
    """Route one Gemini API or Vertex AI SDK client to a local HTTP handler."""
    transport = httpx.MockTransport(respond)
    http_options = HttpOptions(
        httpx_client=httpx.Client(transport=transport),
        httpx_async_client=httpx.AsyncClient(transport=transport),
        retry_options=HttpRetryOptions(attempts=1),
    )
    if vertexai:
        return genai.Client(
            vertexai=True,
            project="test-project",
            location="us-central1",
            credentials=Credentials(token="test-token"),  # noqa: S106 - synthetic SDK credential
            http_options=http_options,
        )
    return genai.Client(api_key="test-key", http_options=http_options)


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
