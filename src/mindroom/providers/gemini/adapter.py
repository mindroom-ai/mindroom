# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Gemini provider adapter over google-genai, for the Gemini API and Vertex AI (``vertexai: true``).

The provider registry imports this module, and with it the SDK, when it builds a Gemini model.
The Gemini API authenticates with the configured or shared Google key. Vertex AI takes its project, location, and
credentials file from the config, else ``GOOGLE_CLOUD_PROJECT``, ``GOOGLE_CLOUD_LOCATION``, and
``GOOGLE_APPLICATION_CREDENTIALS`` in the runtime env, else the SDK's application default credentials.
"""

from __future__ import annotations

import time
from contextlib import aclosing
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar, cast

from google import genai
from google.genai.errors import APIError
from google.genai.types import HttpOptions
from pydantic import ValidationError

from mindroom.llm.errors import InvalidRequest, ProviderError
from mindroom.llm.provider import Capabilities, ModelIdentity, StreamProgress, TurnCompleted, UsageReported
from mindroom.model_defaults import GOOGLE_PROVIDER_DEFAULT_SAMPLING_MODEL_SUFFIXES
from mindroom.providers.assistant_turn import finished_assistant_message, output_started
from mindroom.providers.credentials import gemini_uses_vertexai, model_api_key
from mindroom.providers.errors import classify_provider_error, provider_failure
from mindroom.providers.extra_kwargs import settings_from_extra_kwargs, settings_params
from mindroom.providers.gemini.files import uploaded_file_uri
from mindroom.providers.gemini.settings import GeminiSettings
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
from mindroom.providers.google_adc import with_runtime_application_credentials
from mindroom.providers.tool_policy import provider_tools_disabled

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    from google.genai import Client
    from google.genai.types import GenerateContentConfig, GenerateContentResponse

    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import ModelRequest, StreamEvent

_PROVIDER = "Google"
_SAMPLING_CONTROL_NAMES = frozenset({"temperature", "top_p", "top_k"})


@dataclass(slots=True, kw_only=True)
class GeminiProvider:
    """One Gemini model; the SDK client is built on the first request and reused."""

    identity: ModelIdentity
    capabilities: Capabilities
    settings: GeminiSettings
    _client: Client | None = field(default=None, init=False, repr=False)
    # Gemini keeps no history of its own, and its replayed history is sized canonically.
    native_history_route: ClassVar[None] = None
    replay_estimator: ClassVar[None] = None

    @property
    def model_params(self) -> dict[str, Any]:
        """The route's JSON-safe, non-secret settings."""
        return settings_params(self.settings)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Send one streamed ``generateContent`` request and yield its events.

        Media uploads and downloads happen first, so their SDK and transport errors are normalized too.
        """
        started = time.monotonic()
        reply = GeminiReply()
        first_output_at: float | None = None
        # Client construction reads credentials from disk; its failures are configuration errors, not transient.
        client = self._sdk_client()
        provider = self.identity.provider_name
        # The client, which ``client_params`` can switch to Vertex AI, names the route; Vertex AI has no Files API.
        upload = None if client.vertexai else partial(uploaded_file_uri, client, provider=provider)
        try:
            contents, system_instruction = await format_contents(request.messages, MediaRoute(provider, upload))
            config = self._request_config(request, system_instruction)
            # The request dicts use google-genai's field names; the SDK validates them into its types.
            call = {"model": self.identity.id, "contents": cast("Any", contents), "config": cast("Any", config or None)}
            chunks = await client.aio.models.generate_content_stream(**call)
            async with aclosing(cast("AsyncGenerator[GenerateContentResponse]", chunks)) as responses:
                async for chunk in responses:
                    # A chunk without output, such as grounding or code-execution progress, is still activity.
                    for event in read_chunk(reply, chunk) or [StreamProgress()]:
                        first_output_at = output_started(first_output_at, event)
                        yield event
        except ProviderError:
            # Media and chunk failures arrive normalized.
            raise
        except Exception as error:
            raise _provider_error(error, provider=provider) from error
        if not reply.finished:
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
        if request.output_schema is not None:
            config["response_mime_type"] = "application/json"
            config["response_schema"] = response_schema(request.output_schema)
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
        client_http_options = (self.settings.client_params or {}).get("http_options")
        if client_http_options is not None and HttpOptions.model_validate(client_http_options).extra_body:
            msg = "Participation decisions cannot safely apply Gemini body overrides"
            raise ValueError(msg)
        return without_tool_selection(config, vertexai=self._sdk_client().vertexai) if config else config

    def _sdk_client(self) -> Client:
        if self._client is None:
            self._client = genai.Client(**self._client_params())
        return self._client

    def _client_params(self) -> dict[str, Any]:
        settings = self.settings
        if settings.vertexai:
            params: dict[str, Any] = {"project": settings.project_id, "location": settings.location}
        else:
            params = {"api_key": settings.api_key}
        params = {key: value for key, value in params.items() if value is not None}
        # The route is always explicit, so google-genai never chooses it from its own environment variables.
        params["vertexai"] = settings.vertexai
        if settings.timeout is not None:
            params["http_options"] = {"timeout": int(settings.timeout * 1000)}
        return {**params, **(settings.client_params or {})}


def _provider_error(error: Exception, *, provider: str) -> ProviderError:
    """Return the normalized error for one failed request; any other exception propagates as it is.

    Vertex AI mints its Google access token inside the request, so its failures arrive here too.
    """
    if (failure := provider_failure(error, provider=provider)) is not None:
        return failure
    if isinstance(error, APIError):
        return classify_provider_error(str(error), provider=provider, status_code=error.code)
    if isinstance(error, ValidationError):
        # The SDK rejected an authored generation setting; the same request cannot succeed.
        return InvalidRequest(str(error), provider=provider)
    raise error


def _vertex_kwargs(authored: dict[str, Any], runtime_paths: RuntimePaths) -> dict[str, Any]:
    """Fill a Vertex AI model's project, location, and Google credentials from the runtime env where it sets none.

    A model in express mode, with an ``api_key`` in ``client_params``, gets none of them, since google-genai refuses a
    key beside a project, a location, or credentials.
    """
    kwargs = dict(authored)
    if (kwargs.get("client_params") or {}).get("api_key"):
        return kwargs
    for key, variable in (("project_id", "GOOGLE_CLOUD_PROJECT"), ("location", "GOOGLE_CLOUD_LOCATION")):
        if not kwargs.get(key) and (value := runtime_paths.env_value(variable)):
            kwargs[key] = value
    return with_runtime_application_credentials(kwargs, runtime_paths)


def build_provider(model_config: ModelConfig, runtime_paths: RuntimePaths) -> GeminiProvider:
    """Build the adapter for a ``google`` or ``gemini`` model config; its settings record the resolved route."""
    vertexai = gemini_uses_vertexai(model_config, runtime_paths)
    # Vertex AI authenticates with Google Cloud credentials, so it never takes the shared key.
    extra_kwargs = {
        **(model_config.extra_kwargs or {}),
        "vertexai": vertexai,
        "api_key": model_api_key(model_config, runtime_paths),
    }
    if vertexai:
        extra_kwargs = _vertex_kwargs(extra_kwargs, runtime_paths)
    settings = settings_from_extra_kwargs(GeminiSettings, extra_kwargs, provider=model_config.provider)
    return GeminiProvider(
        identity=ModelIdentity(id=model_config.id, provider=_PROVIDER, provider_name=_PROVIDER),
        capabilities=Capabilities(
            structured_output="native",
            provider_default_sampling=model_config.id.casefold().endswith(
                GOOGLE_PROVIDER_DEFAULT_SAMPLING_MODEL_SUFFIXES,
            ),
        ),
        settings=settings,
    )


__all__ = ["GeminiProvider", "build_provider"]
