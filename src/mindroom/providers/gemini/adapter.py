# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Gemini provider adapter over google-genai, for the Gemini API and Vertex AI (``vertexai: true``).

The provider registry imports this module, and with it the SDK, when it builds a Gemini model.
The Gemini API authenticates with the configured or shared Google key. Vertex AI takes its project, location, and
credentials file from the config, else ``GOOGLE_CLOUD_PROJECT``, ``GOOGLE_CLOUD_LOCATION``, and
``GOOGLE_APPLICATION_CREDENTIALS`` in the runtime env, else the SDK's application default credentials.
"""

from __future__ import annotations

import re
from contextlib import aclosing
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar, cast

from google import genai
from google.genai.errors import APIError
from google.genai.types import HttpOptions
from pydantic import ValidationError

from mindroom.constants import PROVIDER_ENV_KEYS
from mindroom.credentials.environment import get_secret_from_env
from mindroom.llm.errors import QUOTA_EXHAUSTED, InvalidRequest, ProviderError
from mindroom.llm.provider import Capabilities, ModelIdentity
from mindroom.model_defaults import GOOGLE_PROVIDER_DEFAULT_SAMPLING_MODEL_SUFFIXES
from mindroom.providers.assistant_turn import TurnTimer, finished_turn
from mindroom.providers.credentials import gemini_uses_vertexai, model_api_key
from mindroom.providers.errors import classify_provider_error, missing_api_key_error, provider_failure
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
    rejected_media,
    reply_provider_data,
    stop_reason,
    without_tool_selection,
)
from mindroom.providers.google_adc import with_runtime_application_credentials
from mindroom.providers.identity import model_identity
from mindroom.providers.tool_policy import disables_tools
from mindroom.token_counting.families import counter_for_model

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Mapping

    from google.genai import Client
    from google.genai.types import GenerateContentConfig, GenerateContentResponse

    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import ModelRequest, StreamEvent, TokenCounter

_SAMPLING_CONTROL_NAMES = frozenset({"temperature", "top_p", "top_k"})
# How the SDK's ``ValueError`` begins when a Gemini API client has no API key at all.
_NO_API_KEY_ERROR = "No API key was provided."
# The generation a Gemini model id names, as in ``gemini-3.1-pro-preview`` or ``models/gemini-4-pro``; an alias
# such as ``gemini-flash-latest`` names none.
_GEMINI_GENERATION = re.compile(r"gemini-(\d+)(?:[.-]|$)")


@dataclass(slots=True, kw_only=True)
class GeminiProvider:
    """One Gemini model; the SDK client is built on the first request and reused."""

    identity: ModelIdentity
    capabilities: Capabilities
    settings: GeminiSettings
    _client: Client | None = field(default=None, init=False, repr=False)
    # Gemini keeps no history of its own, and its replayed history is sized canonically.
    native_history_route: ClassVar[None] = None

    @property
    def model_params(self) -> dict[str, Any]:
        """The route's JSON-safe, non-secret settings."""
        return settings_params(self.settings)

    @property
    def token_counter(self) -> TokenCounter:
        """Gemini's tokenizer does not run locally, so the route estimates; a tuned model's id need not name Gemini."""
        return counter_for_model(self.identity, family="gemini")

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Send one streamed ``generateContent`` request and yield its events.

        Media uploads happen first, so their SDK and transport errors are normalized too.
        """
        timer = TurnTimer()
        reply = GeminiReply()
        provider = self.identity.provider
        # Client construction reads credentials from disk; its failures are configuration errors, not transient.
        try:
            client = self._sdk_client()
        except ValueError as error:
            # A Gemini API client finds no key in the config, the dashboard, or the variables the SDK reads.
            if not str(error).startswith(_NO_API_KEY_ERROR):
                raise
            raise missing_api_key_error(PROVIDER_ENV_KEYS["google"], provider=provider) from error
        # The client, which ``client_params`` can switch to Vertex AI, names the route; Vertex AI has no Files API.
        upload = None if client.vertexai else partial(uploaded_file_uri, client, provider=provider)
        try:
            contents, system_instruction = await format_contents(
                request.messages,
                MediaRoute(provider, upload),
                # A decision request asks for no server-side tool parts (``without_tool_selection``).
                server_tool_parts=self._sends_search_beside_tools(request) and not disables_tools(request.tool_choice),
            )
            config = self._request_config(request, system_instruction)
            # The request dicts use google-genai's field names; the SDK validates them into its types.
            call = {"model": self.identity.id, "contents": cast("Any", contents), "config": cast("Any", config or None)}
            await request.on_send(_sent_body(call))
            chunks = await client.aio.models.generate_content_stream(**call)
            async with aclosing(cast("AsyncGenerator[GenerateContentResponse]", chunks)) as responses:
                async for chunk in responses:
                    for event in timer.chunk(read_chunk(reply, chunk)):
                        yield event
        except ProviderError:
            # Media and chunk failures arrive normalized.
            raise
        except Exception as error:
            failure = _provider_error(error, provider=provider)
            if isinstance(error, APIError):
                failure.rejected_media = rejected_media(error.message, request.messages)
            raise failure from error
        if not reply.finished:
            # The stream ended before Gemini ended the reply, so the turn did not finish.
            return
        for event in self._finished_turn(reply, timer):
            yield event

    def _finished_turn(self, reply: GeminiReply, timer: TurnTimer) -> list[StreamEvent]:
        """Return the events that end a reply Gemini ended, or raise its malformed call."""
        if (malformed := malformed_tool_call_error(reply, provider=self.identity.provider)) is not None:
            raise malformed
        return finished_turn(
            self.identity,
            timer,
            content=reply.content or None,
            reasoning=reply.reasoning or None,
            tool_calls=reply.tool_calls,
            provider_data=reply_provider_data(reply),
            usage=reply.usage,
            stop_reason=stop_reason(reply),
        )

    def _request_config(
        self,
        request: ModelRequest,
        system_instruction: str | list[Any] | None,
    ) -> dict[str, Any] | GenerateContentConfig:
        """Return the generation config of one request."""
        settings = self.settings
        overrides = request.overrides
        dedicated = {
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
        # A dedicated setting replaces the authored ``generation_config`` key of the same name only when it is set.
        config: dict[str, Any] = {
            **(settings.generation_config or {}),
            **(settings.generative_model_kwargs or {}),
            **{key: value for key, value in dedicated.items() if value is not None},
        }
        if system_instruction is not None:
            config["system_instruction"] = system_instruction
        if request.output_schema is not None:
            config["response_mime_type"] = "application/json"
            # The output schema goes as authored, as JSON Schema.
            config["response_json_schema"] = request.output_schema.model_json_schema()
        thinking = {
            "thinking_budget": settings.thinking_budget,
            "include_thoughts": settings.include_thoughts,
            "thinking_level": settings.thinking_level,
        }
        # Gemini ignores ``overrides.omit_thinking_setting`` and keeps the authored thinking config: Gemini 2.5 and 3
        # think by default, so dropping a ``thinking_budget: 0`` would turn thinking on, and the Pro models cannot turn
        # it off.
        if thinking := {key: value for key, value in thinking.items() if value is not None}:
            config["thinking_config"] = thinking
        config.update(self._tool_settings(request, authored_tool_config=config.get("tool_config")))
        config = {key: value for key, value in config.items() if value is not None}
        if self.capabilities.provider_default_sampling:
            config = {key: value for key, value in config.items() if key not in _SAMPLING_CONTROL_NAMES}
        # The request pipeline retries a failed request, so one attempt also overrides SDK retries that the generation
        # config's ``http_options`` authors.
        config["http_options"] = {**(config.get("http_options") or {}), "retry_options": {"attempts": 1}}
        if disables_tools(request.tool_choice):
            return self._decision_config(config)
        return config

    def _sends_search_beside_tools(self, request: ModelRequest) -> bool:
        """Return whether ``request`` sends Google Search beside its function tools, as Gemini 3 and later take it.

        Such a request asks for the server-side search parts, as the Gemini API requires for that combination, which it
        supports on Gemini 3 and later models only (https://ai.google.dev/gemini-api/docs/generate-content/tool-combination).
        A model whose id names no such generation, an older model or an alias that may point at one, sends search in
        place of the function tools, which every model accepts. Vertex AI takes no search tool beside function calling
        (https://docs.cloud.google.com/vertex-ai/generative-ai/docs/grounding/grounding-with-google-search), so there
        search replaces the function tools too.
        """
        if not self.settings.search or not request.tools or self._sdk_client().vertexai:
            return False
        generation = _GEMINI_GENERATION.match(self.identity.id.rsplit("/", 1)[-1].casefold())
        return generation is not None and int(generation[1]) >= 3

    def _tool_settings(
        self,
        request: ModelRequest,
        *,
        authored_tool_config: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Return the request's ``tools`` and ``tool_config``; one it leaves out keeps the authored generation config's.

        Search beside the function tools adds its flag to the authored ``tool_config``, so authored settings such as
        the search's ``retrieval_config`` stay.
        """
        chosen: dict[str, Any] = {}
        declarations = function_declarations(request.tools)
        if self._sends_search_beside_tools(request):
            chosen["tools"] = [{"google_search": {}}, declarations]
            chosen["tool_config"] = {**(authored_tool_config or {}), "include_server_side_tool_invocations": True}
        elif self.settings.search:
            chosen["tools"] = [{"google_search": {}}]
        elif declarations is not None:
            chosen["tools"] = [declarations]
        return chosen

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
        params = {**params, **(settings.client_params or {})}
        # The request pipeline retries a failed request, so one attempt overrides any SDK retries ``client_params``
        # authors, for the Files API calls of an attachment too large to inline as for generation. google-genai still
        # retries an upload's transfer a fixed number of times, which no option turns off.
        params["http_options"] = {**(params.get("http_options") or {}), "retry_options": {"attempts": 1}}
        return params


def _provider_error(error: Exception, *, provider: str) -> ProviderError:
    """Return the normalized error for one failed request; any other exception propagates as it is.

    Vertex AI mints its Google access token inside the request, so its failures arrive here too. A request the
    account's billing stops is a final error that keeps Google's message alone, which says what to change.
    """
    if (failure := provider_failure(error, provider=provider)) is not None:
        return failure
    if isinstance(error, APIError):
        failure = classify_provider_error(str(error), provider=provider, status_code=error.code)
        if failure.code == QUOTA_EXHAUSTED and error.message:
            return ProviderError(error.message, provider=provider, status_code=error.code, code=QUOTA_EXHAUSTED)
        return failure
    if isinstance(error, ValidationError):
        # The SDK rejected an authored generation setting; the same request cannot succeed.
        return InvalidRequest(str(error), provider=provider)
    raise error


def _sent_body(call: Mapping[str, Any]) -> dict[str, Any]:
    """Return the request as the adapter hands it to the SDK, in google-genai's field names.

    The config's ``http_options`` are request options, so they stay out. The SDK builds the REST body from the rest:
    camelCase names, with the system instruction, tools, and tool config at its top level and the other settings
    under ``generationConfig``.
    """
    config = call["config"] or {}
    if not isinstance(config, dict):
        config = cast("GenerateContentConfig", config).model_dump(mode="json", exclude_none=True)
    return {**call, "config": {key: value for key, value in config.items() if key != "http_options"}}


def _vertex_kwargs(authored: dict[str, Any], runtime_paths: RuntimePaths) -> dict[str, Any]:
    """Fill a Vertex AI model's project, location, and Google credentials from the runtime env where it sets none.

    A model in express mode, with an ``api_key`` in ``client_params``, gets none of them, since google-genai refuses a
    key beside a project, a location, or credentials.
    """
    kwargs = dict(authored)
    if (kwargs.get("client_params") or {}).get("api_key"):
        return kwargs
    for key, variable in (("project_id", "GOOGLE_CLOUD_PROJECT"), ("location", "GOOGLE_CLOUD_LOCATION")):
        if not kwargs.get(key) and (value := get_secret_from_env(variable, runtime_paths=runtime_paths)):
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
        identity=model_identity(model_config),
        capabilities=Capabilities(
            structured_output="native",
            # Gemini fetches Cloud Storage and HTTPS files and video links itself.
            url_media=frozenset({"file", "video"}),
            provider_default_sampling=model_config.id.casefold().endswith(
                GOOGLE_PROVIDER_DEFAULT_SAMPLING_MODEL_SUFFIXES,
            ),
        ),
        settings=settings,
    )


__all__ = ["GeminiProvider", "build_provider"]
