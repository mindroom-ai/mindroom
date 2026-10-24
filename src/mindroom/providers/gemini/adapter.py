# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Gemini provider adapter over google-genai, for the Gemini API and Vertex AI (``vertexai: true``).

The SDK is imported on first use, so importing this module stays cheap.
Credentials resolve to the configured or shared Google key, then ``GOOGLE_API_KEY``, and on Vertex AI
``GOOGLE_CLOUD_PROJECT``, ``GOOGLE_CLOUD_LOCATION``, and the SDK's application default credentials from the
process environment.
"""

from __future__ import annotations

import os
import time
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar, cast

from pydantic import ValidationError

from mindroom.credentials.sync import get_api_key_for_provider
from mindroom.llm.errors import InvalidRequest, ProviderError
from mindroom.llm.provider import Capabilities, ModelIdentity, StreamProgress, TurnCompleted, UsageReported
from mindroom.model_defaults import GOOGLE_PROVIDER_DEFAULT_SAMPLING_MODEL_SUFFIXES
from mindroom.providers.assistant_turn import finished_assistant_message, output_started
from mindroom.providers.errors import classify_provider_error, provider_failure
from mindroom.providers.extra_kwargs import SECRET, settings_from_extra_kwargs, settings_params
from mindroom.providers.gemini.files import uploaded_file_uri
from mindroom.providers.gemini.wire import (
    GeminiReply,
    MediaRoute,
    format_contents,
    function_declarations,
    malformed_tool_call_error,
    read_chunk,
    response_schema,
    stop_reason,
    tool_config,
    without_tool_selection,
)
from mindroom.providers.model_loading import uses_alternative_auth
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
    """The ``extra_kwargs`` a Gemini model accepts."""

    api_key: str | None = field(default=None, metadata=SECRET)
    vertexai: bool = False
    project_id: str | None = None
    location: str | None = None
    # A google.auth credentials object; only programmatic callers can supply one.
    credentials: object | None = field(default=None, metadata=SECRET)
    client_params: Mapping[str, Any] | None = field(default=None, metadata=SECRET)
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
    cached_content: str | None = None
    thinking_budget: int | None = None
    include_thoughts: bool | None = None
    thinking_level: str | None = None
    # Google Search grounding, sent in place of the request's function tools, so a grounded model calls no
    # function; kept as main behaves.
    search: bool = False


@dataclass(slots=True, kw_only=True)
class GeminiProvider:
    """One Gemini model; the SDK client is built on the first request and reused."""

    identity: ModelIdentity
    capabilities: Capabilities
    settings: GeminiSettings
    vertexai: bool
    _client: Client | None = field(default=None, init=False, repr=False)
    # Gemini keeps no history of its own, and its replayed history is sized canonically.
    native_history_route: ClassVar[None] = None
    replay_estimator: ClassVar[None] = None

    @property
    def model_params(self) -> dict[str, Any]:
        """The route's JSON-safe, non-secret settings."""
        return settings_params(self.settings)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Send one ``generateContent`` request, streamed unless ``request.stream`` is False, and yield its events.

        Media uploads and downloads happen first, so their SDK and transport errors are normalized too.
        """
        started = time.monotonic()
        reply = GeminiReply()
        first_output_at: float | None = None
        # Client construction reads credentials from disk; its failures are configuration errors, not transient.
        client = self._sdk_client()
        try:
            contents, system_instruction = await format_contents(request.messages, self._media_route())
            config = self._request_config(request, system_instruction)
            # The request dicts use google-genai's field names; the SDK validates them into its types.
            call = {"model": self.identity.id, "contents": cast("Any", contents), "config": cast("Any", config or None)}
            if request.stream:
                chunks = await client.aio.models.generate_content_stream(**call)
                async with aclosing(cast("AsyncGenerator[GenerateContentResponse]", chunks)) as responses:
                    async for chunk in responses:
                        # A chunk without output, such as grounding or code-execution progress, is still activity.
                        for event in read_chunk(reply, chunk) or [StreamProgress()]:
                            first_output_at = output_started(first_output_at, event)
                            yield event
            else:
                whole = read_chunk(reply, await client.aio.models.generate_content(**call), whole_response=True)
                first_output_at = time.monotonic()
                # A reply that arrives whole yields no deltas; its usage still comes first, so a malformed call
                # settles it.
                for event in whole:
                    if isinstance(event, UsageReported):
                        yield event
        except ProviderError:
            # Media and chunk failures arrive normalized.
            raise
        except Exception as error:
            raise _provider_error(error, provider=self.identity.provider_name) from error
        if request.stream and not reply.finished:
            # The stream ended before Gemini ended the reply, so the turn did not finish.
            return
        for event in self._finished_turn(reply, started=started, first_output_at=first_output_at):
            yield event

    def _finished_turn(self, reply: GeminiReply, *, started: float, first_output_at: float | None) -> list[StreamEvent]:
        """Return the final usage report and the finished turn of a reply Gemini ended, or raise its malformed call."""
        if (malformed := malformed_tool_call_error(reply, provider=self.identity.provider_name)) is not None:
            raise malformed
        message = finished_assistant_message(
            self.identity,
            content=reply.content or None,
            reasoning=reply.reasoning or None,
            tool_calls=reply.tool_calls,
            provider_data={} if reply.thought_signature is None else {"thought_signature": reply.thought_signature},
            usage=reply.usage,
            started=started,
            first_output_at=first_output_at,
        )
        usage = [] if reply.usage is None else [UsageReported(message.metrics)]
        return [*usage, TurnCompleted(message=message, stop_reason=stop_reason(reply))]

    def _request_config(
        self,
        request: ModelRequest,
        system_instruction: str | list[Any] | None,
    ) -> dict[str, Any] | GenerateContentConfig:
        """Return the generation config of one request."""
        settings = self.settings
        overrides = request.overrides
        # TODO(simplify): an authored ``generation_config`` key that has a dedicated field, such as temperature, is
        # dropped when that field is unset. Let a dedicated field replace the key only when it is set. Operators and
        # models would notice.
        config: dict[str, Any] = {
            **(settings.generation_config or {}),
            **(settings.generative_model_kwargs or {}),
            "safety_settings": settings.safety_settings,
            "temperature": settings.temperature if overrides.temperature is None else overrides.temperature,
            "top_p": settings.top_p,
            "top_k": settings.top_k,
            "max_output_tokens": settings.max_output_tokens,
            "stop_sequences": settings.stop_sequences,
            "logprobs": settings.logprobs,
            "presence_penalty": settings.presence_penalty,
            "frequency_penalty": settings.frequency_penalty,
            "seed": settings.seed,
            "cached_content": settings.cached_content,
        }
        if system_instruction is not None:
            config["system_instruction"] = system_instruction
        if (schema := response_schema(request.response_format)) is not None:
            config["response_mime_type"] = "application/json"
            config["response_schema"] = schema
        thinking = {
            "thinking_budget": settings.thinking_budget,
            "include_thoughts": settings.include_thoughts,
            "thinking_level": settings.thinking_level,
        }
        # ``overrides.disable_thinking`` leaves the authored thinking config alone: Gemini 2.5 and 3 think by
        # default, so dropping a ``thinking_budget: 0`` would turn thinking on, and the Pro models cannot turn it off.
        if thinking := {key: value for key, value in thinking.items() if value is not None}:
            config["thinking_config"] = thinking
        if settings.search:
            config["tools"] = [{"google_search": {}}]
        elif (declarations := function_declarations(request.tools)) is not None:
            config["tools"] = [declarations]
        if (function_calling := tool_config(request.tool_choice)) is not None:
            config["tool_config"] = function_calling
        config = {key: value for key, value in config.items() if value is not None}
        if self.capabilities.provider_default_sampling:
            config = {key: value for key, value in config.items() if key not in _SAMPLING_CONTROL_NAMES}
        if overrides.caller_retries:
            # The SDK retries only as ``http_options.retry_options`` authors it; one attempt overrides that here.
            config["http_options"] = {**(config.get("http_options") or {}), "retry_options": {"attempts": 1}}
        if provider_tools_disabled():
            return self._decision_config(config)
        return config

    def _decision_config(self, config: dict[str, Any]) -> dict[str, Any] | GenerateContentConfig:
        """Keep function schemas but disable function calling and native tools for a decision request."""
        from google.genai.types import HttpOptions  # noqa: PLC0415 - provider SDK loads on first request

        client_http_options = (self.settings.client_params or {}).get("http_options")
        if client_http_options is not None and HttpOptions.model_validate(client_http_options).extra_body:
            msg = "Participation decisions cannot safely apply Gemini body overrides"
            raise ValueError(msg)
        return without_tool_selection(config, vertexai=self.vertexai) if config else config

    def _media_route(self) -> MediaRoute:
        provider = self.identity.provider_name
        if self.vertexai:
            return MediaRoute(provider=provider)
        return MediaRoute(provider=provider, upload=partial(uploaded_file_uri, self._sdk_client(), provider=provider))

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


def _provider_error(error: Exception, *, provider: str) -> ProviderError:
    """Return the normalized error for one failed request; any other exception propagates as it is.

    Vertex AI mints its Google access token inside the request, so its failures arrive here too.
    """
    from google.genai.errors import APIError  # noqa: PLC0415 - provider SDK loads on first request

    if (failure := provider_failure(error, provider=provider)) is not None:
        return failure
    if isinstance(error, APIError):
        return classify_provider_error(str(error), provider=provider, status_code=error.code)
    if isinstance(error, ValidationError):
        # The SDK rejected an authored generation setting; the same request cannot succeed.
        return InvalidRequest(str(error), provider=provider)
    raise error


def build_provider(model_config: ModelConfig, runtime_paths: RuntimePaths) -> GeminiProvider:
    """Build the adapter for a ``google`` or ``gemini`` model config."""
    extra_kwargs = dict(model_config.extra_kwargs or {})
    if model_config.api_key is not None:
        extra_kwargs["api_key"] = model_config.api_key
    settings = settings_from_extra_kwargs(GeminiSettings, extra_kwargs, provider=model_config.provider)
    # Vertex AI authenticates with Google Cloud credentials, so it never takes the shared key.
    if settings.api_key is None and not uses_alternative_auth(model_config):
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
