# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Gemini provider adapter over google-genai, for the Gemini API and Vertex AI (``vertexai: true``).

The SDK is imported on first use, so importing this module stays cheap.
Credentials resolve as the Agno-backed model resolved them: the configured or shared Google key, then
``GOOGLE_API_KEY``, and on Vertex AI ``GOOGLE_CLOUD_PROJECT``, ``GOOGLE_CLOUD_LOCATION``, and the SDK's
application default credentials from the process environment.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast

import httpx

from mindroom.credentials.sync import get_api_key_for_provider
from mindroom.llm.messages import Message
from mindroom.llm.provider import Capabilities, ModelIdentity, TextDelta, TurnCompleted, UsageReported
from mindroom.llm.usage import MessageMetrics
from mindroom.model_defaults import GOOGLE_PROVIDER_DEFAULT_SAMPLING_MODEL_SUFFIXES
from mindroom.providers.extra_kwargs import settings_from_extra_kwargs
from mindroom.providers.gemini.wire import (
    GeminiReply,
    format_contents,
    function_declarations,
    read_chunk,
    response_schema,
    stop_reason,
    tool_config,
)
from mindroom.providers.status_errors import provider_error_for_status, provider_unavailable
from mindroom.providers.tool_policy import provider_tools_disabled

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence

    from google.genai import Client
    from google.genai.types import GenerateContentConfig, GenerateContentResponse

    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import ModelRequest, StreamEvent

_PROVIDER = "Google"
_SAMPLING_CONTROL_NAMES = frozenset({"temperature", "top_p", "top_k"})


@dataclass(frozen=True, slots=True, kw_only=True)
class GeminiSettings:
    """The ``extra_kwargs`` a Gemini model accepts, named as Agno's ``Gemini`` fields."""

    api_key: str | None = None
    vertexai: bool = False
    project_id: str | None = None
    location: str | None = None
    # A google.auth credentials object; only programmatic callers can supply one.
    credentials: object | None = None
    client_params: Mapping[str, Any] | None = None
    timeout: float | None = None
    generation_config: Mapping[str, Any] | None = None
    generative_model_kwargs: Mapping[str, Any] | None = None
    safety_settings: Sequence[Any] | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    max_output_tokens: int | None = None
    stop_sequences: Sequence[str] | None = None
    logprobs: bool | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    seed: int | None = None
    response_modalities: Sequence[str] | None = None
    speech_config: Mapping[str, Any] | None = None
    cached_content: str | None = None
    thinking_budget: int | None = None
    include_thoughts: bool | None = None
    thinking_level: str | None = None


@dataclass(slots=True, kw_only=True)
class GeminiProvider:
    """One Gemini model; the SDK client is built on the first request and reused."""

    identity: ModelIdentity
    capabilities: Capabilities
    settings: GeminiSettings
    vertexai: bool
    _client: Client | None = field(default=None, init=False, repr=False)

    def format_tool_results(self, messages: Sequence[Message], results: Sequence[Message]) -> list[Message]:
        """Append one stored message per tool result; the wire format merges them into one user turn."""
        del messages
        return list(results)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Send one streamed ``generateContent`` request and yield its events."""
        contents, system_instruction = await asyncio.to_thread(format_contents, request.messages)
        config = self._request_config(request, system_instruction)
        from google.genai.errors import APIError  # noqa: PLC0415 - provider SDK loads on first request

        reply = GeminiReply()
        started = time.perf_counter()
        first_text_at: float | None = None
        try:
            chunks = await self._sdk_client().aio.models.generate_content_stream(
                model=self.identity.id,
                # The request dicts use google-genai's field names; the SDK validates them into its types.
                contents=cast("Any", contents),
                config=cast("Any", config or None),
            )
            async with aclosing(cast("AsyncGenerator[GenerateContentResponse]", chunks)) as responses:
                async for chunk in responses:
                    for event in read_chunk(reply, chunk, provider=self.identity.provider_name):
                        if first_text_at is None and isinstance(event, TextDelta):
                            first_text_at = time.perf_counter()
                        yield event
        except APIError as error:
            raise provider_error_for_status(
                str(error),
                provider=self.identity.provider_name,
                status_code=error.code,
            ) from error
        except (httpx.TransportError, OSError) as error:
            raise provider_unavailable(error, provider=self.identity.provider_name) from error
        if reply.usage is not None:
            yield UsageReported(reply.usage)
        metrics = replace(reply.usage) if reply.usage is not None else MessageMetrics()
        metrics.duration = time.perf_counter() - started
        metrics.time_to_first_token = None if first_text_at is None else first_text_at - started
        provider_data: dict[str, Any] = {"mindroom_model": {"id": self.identity.id, "provider": _PROVIDER}}
        if reply.thought_signature is not None:
            provider_data["thought_signature"] = reply.thought_signature
        message = Message(
            role="assistant",
            content=reply.content or None,
            reasoning_content=reply.reasoning or None,
            tool_calls=reply.tool_calls or None,
            provider_data=provider_data,
            metrics=metrics,
        )
        yield TurnCompleted(message=message, stop_reason=stop_reason(reply))

    def _request_config(
        self,
        request: ModelRequest,
        system_instruction: str | list[Any] | None,
    ) -> dict[str, Any] | GenerateContentConfig:
        """Return the generation config of one request, in the order Agno's Gemini built it."""
        settings = self.settings
        overrides = request.overrides
        # Agno let the dedicated fields replace authored generation_config keys, even when unset.
        config: dict[str, Any] = {
            **(settings.generation_config or {}),
            **(settings.generative_model_kwargs or {}),
            "safety_settings": settings.safety_settings,
            "temperature": settings.temperature if overrides.temperature is None else overrides.temperature,
            "top_p": settings.top_p,
            "top_k": settings.top_k,
            "max_output_tokens": (
                settings.max_output_tokens if overrides.max_output_tokens is None else overrides.max_output_tokens
            ),
            "stop_sequences": settings.stop_sequences,
            "logprobs": settings.logprobs,
            "presence_penalty": settings.presence_penalty,
            "frequency_penalty": settings.frequency_penalty,
            "seed": settings.seed,
            "response_modalities": settings.response_modalities,
            "speech_config": settings.speech_config,
            "cached_content": settings.cached_content,
        }
        if system_instruction is not None:
            config["system_instruction"] = system_instruction
        if (schema := response_schema(request.response_format)) is not None:
            config["response_mime_type"] = "application/json"
            config["response_schema"] = schema
        thinking = {
            key: value
            for key, value in (
                ("thinking_budget", settings.thinking_budget),
                ("include_thoughts", settings.include_thoughts),
                ("thinking_level", settings.thinking_level),
            )
            if value is not None
        }
        if thinking:
            config["thinking_config"] = thinking
        if overrides.disable_thinking:
            config.pop("thinking_config", None)
        declarations = function_declarations(request.tools)
        tools = [*([declarations] if declarations is not None else []), *request.native_tools]
        if tools:
            config["tools"] = tools
        if (function_calling := tool_config(request.tool_choice)) is not None:
            config["tool_config"] = function_calling
        if overrides.timeout_seconds is not None:
            config["http_options"] = {"timeout": int(overrides.timeout_seconds * 1000)}
        config = {key: value for key, value in config.items() if value is not None}
        if self.capabilities.provider_default_sampling:
            config = {key: value for key, value in config.items() if key not in _SAMPLING_CONTROL_NAMES}
        if provider_tools_disabled():
            return self._decision_config(config)
        return config

    def _decision_config(self, config: dict[str, Any]) -> dict[str, Any] | GenerateContentConfig:
        """Keep function schemas but disable function calling and native tools for a decision request."""
        from google.genai.types import HttpOptions  # noqa: PLC0415 - provider SDK loads on first request

        # The policy lives with the Agno-backed Gemini model until that model is removed; it imports Agno.
        from mindroom.providers.google_gemini import _without_tool_selection  # noqa: PLC0415  # privata: ignore

        client_http_options = (self.settings.client_params or {}).get("http_options")
        if client_http_options is not None and HttpOptions.model_validate(client_http_options).extra_body:
            msg = "Participation decisions cannot safely apply Gemini body overrides"
            raise ValueError(msg)
        return _without_tool_selection(config, vertexai=self.vertexai) if config else config

    def _sdk_client(self) -> Client:
        if self._client is None:
            from google import genai  # noqa: PLC0415 - provider SDK loads on first request

            self._client = genai.Client(**self._client_params())
        return self._client

    def _client_params(self) -> dict[str, Any]:
        settings = self.settings
        if self.vertexai:
            params: dict[str, Any] = {
                "vertexai": True,
                "project": settings.project_id or os.getenv("GOOGLE_CLOUD_PROJECT"),
                "location": settings.location or os.getenv("GOOGLE_CLOUD_LOCATION"),
                "credentials": settings.credentials,
            }
        else:
            params = {"api_key": settings.api_key or os.getenv("GOOGLE_API_KEY")}
        params = {key: value for key, value in params.items() if value is not None}
        if settings.timeout is not None:
            params["http_options"] = {"timeout": int(settings.timeout * 1000)}
        return {**params, **(settings.client_params or {})}


def build_provider(model_config: ModelConfig, runtime_paths: RuntimePaths) -> GeminiProvider:
    """Build the adapter for a ``google`` or ``gemini`` model config."""
    extra_kwargs = dict(model_config.extra_kwargs or {})
    if model_config.api_key is not None:
        extra_kwargs["api_key"] = model_config.api_key
    settings = settings_from_extra_kwargs(GeminiSettings, extra_kwargs, provider=model_config.provider)
    # Vertex AI authenticates with Google Cloud credentials, so it never takes the shared key.
    if settings.api_key is None and not settings.vertexai:
        settings = replace(settings, api_key=get_api_key_for_provider("google", runtime_paths=runtime_paths))
    return GeminiProvider(
        identity=ModelIdentity(id=model_config.id, provider=_PROVIDER, provider_name=_PROVIDER),
        capabilities=Capabilities(
            structured_output="native",
            provider_default_sampling=model_config.id.casefold().endswith(
                GOOGLE_PROVIDER_DEFAULT_SAMPLING_MODEL_SUFFIXES,
            ),
        ),
        settings=settings,
        vertexai=settings.vertexai or os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "false").lower() == "true",
    )


__all__ = ["GeminiProvider", "GeminiSettings", "build_provider"]
