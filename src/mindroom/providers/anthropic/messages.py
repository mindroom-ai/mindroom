"""The Claude adapter over the Anthropic Messages API, on the direct API, Vertex AI, and Bedrock Mantle.

One frozen adapter serves all three routes; per-request state travels in ``ModelRequest``. Each request builds
its SDK client and wire body off the event loop (Bedrock credential resolution and attachment encoding block),
streams the reply through the SDK's message stream, and parses the finished message once; the pipeline already
downloaded its URL media. The only state an adapter keeps is the ambient Google credentials a Vertex route
discovers once.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import anthropic
from anthropic import NOT_GIVEN, AsyncAnthropic, AsyncAnthropicVertex
from anthropic.lib.streaming import (
    ParsedBetaContentBlockStopEvent,
    ParsedBetaMessageStopEvent,
    ParsedContentBlockStopEvent,
    ParsedMessageStopEvent,
)
from anthropic.types import RawContentBlockDeltaEvent, ToolUseBlock
from anthropic.types import TextDelta as AnthropicTextDelta
from anthropic.types import ThinkingDelta as AnthropicThinkingDelta
from anthropic.types.beta import (
    BetaMessage,
    BetaRawContentBlockDeltaEvent,
    BetaTextDelta,
    BetaThinkingDelta,
    BetaToolUseBlock,
)

from mindroom.constants import PROVIDER_ENV_KEYS
from mindroom.credentials.environment import get_secret_from_env
from mindroom.llm.errors import ProviderError
from mindroom.llm.provider import (
    STRUCTURED_OUTPUT_TOOL,
    Capabilities,
    ModelIdentity,
    ModelRequest,
    NativeCompactionReplay,
    ReasoningDelta,
    StreamEvent,
    StreamProgress,
    TextDelta,
    ToolCallReady,
    UsageReported,
    hosted_tool_started,
    structured_output_tools,
)
from mindroom.logging_config import get_logger
from mindroom.model_defaults import ANTHROPIC_BASE_URL_DEFAULT, claude_rejects_sampling_controls
from mindroom.native_compaction import native_compaction_route
from mindroom.providers.anthropic.native_compaction import (
    COMPACTION_BETA,
    MIN_COMPACTION_THRESHOLD,
    authored_native_compaction_supported,
    compaction_context_management,
    native_compaction_supported,
    native_replay_messages_for_route,
    record_native_checkpoint_on,
    uses_compaction_edit,
)
from mindroom.providers.anthropic.prompt_cache import native_tool_search_supported, prepare_request_body
from mindroom.providers.anthropic.settings import (
    ENDPOINT_VARIABLES,
    ClaudeRouteKind,
    ClaudeSettings,
    parse_claude_settings,
)
from mindroom.providers.anthropic.wire import (
    assistant_message_from_response,
    checks_thinking_prefix,
    format_claude_messages,
    format_claude_tools,
    parse_claude_usage,
    stop_reason_of,
    supports_forced_tool_use,
    supports_prefill,
    supports_structured_outputs,
    tool_call_from_block,
)
from mindroom.providers.assistant_turn import TurnTimer, stamp_finished_turn, turn_end
from mindroom.providers.claude_wire_blocks import SERVER_TOOL_USE_BLOCK_TYPE, as_dict
from mindroom.providers.credentials import model_api_key
from mindroom.providers.errors import (
    classify_provider_error,
    media_ids,
    missing_api_key_error,
    path_tokens,
    provider_failure,
    retry_after_seconds,
)
from mindroom.providers.extra_kwargs import sdk_request_body, settings_params
from mindroom.providers.google_adc import load_ambient_google_credentials, with_runtime_application_credentials
from mindroom.providers.identity import model_identity
from mindroom.providers.model_http_client import MODEL_HTTP_CLIENT
from mindroom.providers.names import provider_route
from mindroom.runtime_env_policy import AWS_ENV_BY_KEY, VERTEXAI_CLAUDE_ENV_BY_KEY
from mindroom.token_counting.families import counter_for_model
from mindroom.tool_system.dependencies import ensure_optional_deps, optional_dependency_environment

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

    import httpx2
    from anthropic.lib.bedrock import AsyncAnthropicBedrockMantle
    from anthropic.types import Message as AnthropicMessage
    from google.auth.credentials import Credentials as GoogleCredentials
    from pydantic import BaseModel

    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import MediaKind, NativeHistoryRoute, StructuredOutput, TokenCounter

    type _AsyncClaudeClient = AsyncAnthropic | AsyncAnthropicVertex | AsyncAnthropicBedrockMantle

logger = get_logger(__name__)

_STRUCTURED_OUTPUTS_BETA = "structured-outputs-2025-11-13"
# Replayed thinking whose earlier context changed is dropped instead of failing the request. The system prompt carries
# the date and the turn's memory and enrichment, and compaction and replay limits rewrite earlier turns, so a request
# regularly replays thinking produced after a different prefix; the API rejects such a request by default on accounts
# created from 2026-08-31. The default binding goes only on the routes shown to accept the beta, because a platform
# rejects a beta it does not take, so not on Bedrock; a request without a binding leaves out earlier turns' thinking
# instead (``_replays_earlier_thinking``).
_THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"
_THINKING_BINDING_ROUTES: frozenset[ClaudeRouteKind] = frozenset({"anthropic", "vertexai_claude"})
_VERTEX_ENDPOINTS = {
    "global": "https://aiplatform.googleapis.com/v1",
    "us": "https://aiplatform.us.rep.googleapis.com/v1",
    "eu": "https://aiplatform.eu.rep.googleapis.com/v1",
}
_SAMPLING_CONTROL_NAMES = ("temperature", "top_p", "top_k")
# The SDK's ``messages.stream`` arguments that are request options or headers, not body fields. The SDK adds its own
# ``stream`` flag to the body, and its Vertex AI and Bedrock clients rewrite the body for their platform.
_SDK_OPTION_KEYS = ("extra_headers", "extra_query", "extra_body", "timeout", "betas")


def vertex_project_and_region(settings: ClaudeSettings) -> tuple[str | None, str | None]:
    """Return the project and region a Vertex AI Claude client gets from its settings, None for one they leave unset.

    ``client_params`` win over the values the builder filled from the model's settings and the runtime env, as the
    client takes them.
    """
    client_params = settings.client_params or {}
    return client_params.get("project_id") or settings.project_id, client_params.get("region") or settings.region


def _route_endpoint(route: ClaudeRouteKind, settings: ClaudeSettings) -> str | None:
    """Return the endpoint identity of one Claude route, or None on Bedrock, which has none.

    Stored native-compaction checkpoints and the media limits learned from provider errors are keyed by it.

    It reads the route's values, which the builder filled from the runtime env, with ``client_params`` over them,
    as the client does.
    """
    client_params = settings.client_params or {}
    if route == "anthropic":
        return str(client_params.get("base_url") or ANTHROPIC_BASE_URL_DEFAULT).rstrip("/")
    if route == "vertexai_claude":
        project, region = vertex_project_and_region(settings)
        base_url = (
            client_params.get("base_url")
            or settings.base_url
            or _VERTEX_ENDPOINTS.get(region or "", f"https://{region}-aiplatform.googleapis.com/v1")
        )
        return f"{str(base_url).rstrip('/')}|{project}|{region}"
    return None


def _route_client_kwargs(route: ClaudeRouteKind, settings: ClaudeSettings) -> dict[str, Any]:
    """Return the credentials and endpoint of one route, before ``client_params`` and the HTTP client."""
    if route == "anthropic":
        return {"api_key": settings.api_key, "auth_token": settings.auth_token}
    if route == "vertexai_claude":
        # Unset values stay unset so the SDK reads CLOUD_ML_REGION, ANTHROPIC_VERTEX_PROJECT_ID, and
        # ANTHROPIC_VERTEX_BASE_URL itself.
        return {
            "region": NOT_GIVEN if settings.region is None else settings.region,
            "project_id": NOT_GIVEN if settings.project_id is None else settings.project_id,
            "base_url": settings.base_url,
        }
    return _bedrock_credentials(settings)


@dataclass(frozen=True, slots=True)
class _PreparedRequest:
    """One request's SDK client and keyword arguments, built off the event loop."""

    client: _AsyncClaudeClient
    kwargs: dict[str, Any]
    beta: bool
    checkpoint_prefix: bool


@dataclass(slots=True)
class _AmbientGoogleCredentials:
    """Application Default Credentials a Vertex adapter discovers on first use and shares across its clients.

    The SDK refreshes the shared credentials' token only when it expires, so discovery and token minting
    happen once per adapter instead of on every request of a tool loop.
    """

    credentials: GoogleCredentials | None = None
    project_id: str | None = None

    def load(self) -> tuple[GoogleCredentials, str | None]:
        """Return the discovered credentials and the project they name, discovering them on the first call."""
        if self.credentials is None:
            self.credentials, self.project_id = load_ambient_google_credentials()
        return self.credentials, self.project_id


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeProvider:
    """A Claude model behind one of the Anthropic Messages routes."""

    identity: ModelIdentity
    capabilities: Capabilities
    route: ClaudeRouteKind
    settings: ClaudeSettings
    http_client: httpx2.AsyncClient = MODEL_HTTP_CLIENT
    google_credentials: _AmbientGoogleCredentials = field(default_factory=_AmbientGoogleCredentials, compare=False)

    @property
    def native_history_route(self) -> NativeHistoryRoute:
        """Claude compacts history on the provider side where the route supports it or the model config authored it."""
        return self

    @property
    def model_params(self) -> dict[str, Any]:
        """The route's JSON-safe, non-secret settings."""
        return settings_params(self.settings)

    @property
    def token_counter(self) -> TokenCounter:
        """Claude's tokenizer does not run locally, so the route estimates."""
        return counter_for_model(self.identity)

    def native_compaction_settings(
        self,
        *,
        threshold: int | None,
        history_generation: str,
        allow_authored: bool,
    ) -> NativeCompactionReplay | None:
        """Return the replay settings of a request that wants compaction at ``threshold`` input tokens.

        A threshold under Claude's minimum, or one on a route without MindRoom-managed compaction, falls back
        to the authored provider policy when ``allow_authored`` and the model authored a compaction edit;
        otherwise the request runs without native compaction.
        """
        if threshold is not None and threshold < MIN_COMPACTION_THRESHOLD:
            threshold = None
        if threshold is None or not self.capabilities.native_compaction:
            if not allow_authored or not authored_native_compaction_supported(route=self.route, settings=self.settings):
                return None
            threshold = None
        # Only Bedrock has no endpoint identity, and it compacts neither way.
        assert self.identity.endpoint is not None
        route = native_compaction_route(
            provider=self.identity.provider,
            model_id=self.identity.id,
            endpoint=self.identity.endpoint,
            history_generation=history_generation,
        )
        return NativeCompactionReplay(route=route, threshold=threshold)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Send one request and yield its events; the SDK's message stream accumulates the final message.

        ``message_start`` and ``message_delta`` report the usage so far, as a partial ``UsageReported``, so a stream
        that stops early still settles what Anthropic bills.
        """
        timer = TurnTimer()
        prepared = await asyncio.to_thread(self._prepare, request)
        client, kwargs = prepared.client, prepared.kwargs
        try:
            await request.on_send(sdk_request_body(kwargs, _SDK_OPTION_KEYS))
            manager = client.beta.messages.stream(**kwargs) if prepared.beta else client.messages.stream(**kwargs)
            async with manager as events:
                async for event in events:
                    if event.type in _USAGE_EVENT_TYPES:
                        # The SDK has already added the event's usage to its accumulated message.
                        yield UsageReported(parse_claude_usage(events.current_message_snapshot.usage), final=False)
                    elif isinstance(event, (ParsedMessageStopEvent, ParsedBetaMessageStopEvent)):
                        for ending in self._finished_turn(event.message, request, prepared, timer):
                            yield ending
                    elif (output := _output_event(event)) is not None:
                        yield timer.output(output)
                    elif event.type in _PROVIDER_EVENT_TYPES:
                        # Block starts, tool-call argument pieces, and server-tool blocks carry no output but
                        # show the provider is still working; the SDK's own convenience events repeat them.
                        yield _progress(event)
        except Exception as error:
            failure = _provider_error(error, provider=self.identity.provider)
            failure.rejected_media = _rejected_media(error, request.messages)
            raise failure from error

    def _finished_turn(
        self,
        response: AnthropicMessage | BetaMessage,
        request: ModelRequest,
        prepared: _PreparedRequest,
        timer: TurnTimer,
    ) -> list[StreamEvent]:
        """Return the events that end a finished response, its message stamped with provenance and timings."""
        message = assistant_message_from_response(
            response,
            record_context_management=self.settings.context_management is not None,
        )
        stamp_finished_turn(message, self.identity, timer)
        record_native_checkpoint_on(message, request.native_compaction, checkpoint_prefix=prepared.checkpoint_prefix)
        raw_stop_reason = response.stop_reason
        stop_reason = stop_reason_of(raw_stop_reason)
        if stop_reason == "output_limit":
            logger.warning(
                "claude_output_limit_reached",
                model_id=self.identity.id,
                stop_reason=raw_stop_reason,
                tool_calls=len(message.tool_calls or []),
            )
        elif stop_reason == "refusal":
            logger.warning("claude_safeguard_refusal", model_id=self.identity.id, stop_reason=raw_stop_reason)
        transformations = response.input_transformations if isinstance(response, BetaMessage) else None
        dropped = [entry for entry in transformations or () if entry.type == "thinking_dropped"]
        if dropped:
            # The model answered without that earlier reasoning; ``reasons`` says why: the context before it changed, or
            # another model, organization, or end user produced it.
            logger.info(
                "claude_replayed_thinking_dropped",
                model_id=self.identity.id,
                blocks=len(dropped),
                reasons=sorted({entry.reason for entry in dropped}),
            )
        return turn_end(message, stop_reason)

    def _output_format(self, schema: type[BaseModel] | None) -> dict[str, Any] | None:
        """Return the ``output_config.format`` value, or None on every route but ``native``.

        A ``forced_tool`` route answers through the forced ``structured_output`` call, and a JSON-mode route relies on
        the prompt asking for JSON.
        """
        if schema is None or self.capabilities.structured_output != "native":
            return None
        return {"type": "json_schema", "schema": anthropic.transform_schema(schema.model_json_schema())}

    def _authored_params(self, request: ModelRequest) -> dict[str, Any]:
        """Return the model's authored request parameters with the request's overrides applied."""
        settings, overrides = self.settings, request.overrides
        params: dict[str, Any] = {}
        if settings.max_tokens:
            params["max_tokens"] = settings.max_tokens
        if settings.thinking and not overrides.omit_thinking_setting:
            # A null field is unset, so an authored null ``block_binding`` is no binding on every route.
            params["thinking"] = {key: value for key, value in settings.thinking.items() if value is not None}
        if settings.output_config:
            params["output_config"] = dict(settings.output_config)
        if settings.stop_sequences:
            params["stop_sequences"] = list(settings.stop_sequences)
        temperature = settings.temperature
        if overrides.temperature is not None and not self.capabilities.provider_default_sampling:
            temperature = overrides.temperature
        sampling = {"temperature": temperature, "top_p": settings.top_p, "top_k": settings.top_k}
        params.update({name: value for name, value in sampling.items() if value is not None})
        if settings.context_management is not None:
            params["context_management"] = dict(settings.context_management)
        replay = request.native_compaction
        if replay is not None and replay.threshold is not None:
            params["context_management"] = compaction_context_management(replay.threshold)
        # The raw request overrides merge last, as the SDK merges a raw body.
        params.update(_raw_request_params(settings, omit_thinking_setting=overrides.omit_thinking_setting))
        return params

    def _with_sampling_in_extra_body(self, params: dict[str, Any]) -> dict[str, Any]:
        """Move sampling controls into ``extra_body`` and drop them for the generations that reject them."""
        moved = {name: params.pop(name) for name in _SAMPLING_CONTROL_NAMES if name in params}
        extra_body: dict[str, Any] = {**moved, **(params.get("extra_body") or {})}
        if claude_rejects_sampling_controls(self.identity.id):
            for name in _SAMPLING_CONTROL_NAMES:
                extra_body.pop(name, None)
        if extra_body:
            params["extra_body"] = extra_body
        else:
            params.pop("extra_body", None)
        return params

    def _with_thinking_binding(self, params: dict[str, Any], request: ModelRequest) -> dict[str, Any]:
        """Return the params with stale replayed thinking set to be dropped (``_THINKING_BINDING_BETA``).

        The default ``drop_block`` binding goes only on the routes that take the beta, and rides on adaptive
        thinking, which these models run when the request sends no thinking setting; the API rejects it beside any
        other thinking type. An authored ``block_binding`` is kept, a null one counts as none, and a request that
        omits the thinking setting gets none.
        """
        if (
            self.route not in _THINKING_BINDING_ROUTES
            or request.overrides.omit_thinking_setting
            or not checks_thinking_prefix(self.identity.id)
        ):
            return params
        owner = _thinking_owner(params)
        thinking = dict(owner.get("thinking") or {"type": "adaptive"})
        if thinking.get("type") == "adaptive" and thinking.get("block_binding") is None:
            thinking["block_binding"] = {"prefix_mismatch_behavior": "drop_block"}
            owner["thinking"] = thinking
        return params

    def _replays_earlier_thinking(self, params: dict[str, Any]) -> bool:
        """Return whether earlier turns' thinking replays: always, unless the API checks it and nothing binds it.

        Where no ``block_binding`` is sent (Bedrock, ``between_tools``, disabled thinking), a stale earlier block
        fails the request, so earlier turns replay without thinking, which is always valid. An authored binding,
        ``"error"`` included, is the operator's choice and keeps every block.
        """
        return not checks_thinking_prefix(self.identity.id) or _sends_thinking_binding(params)

    def _request_params(
        self,
        request: ModelRequest,
        output_format: dict[str, Any] | None,
        *,
        strict_tools: bool,
    ) -> dict[str, Any]:
        """Return the request parameters beside ``messages``, ``system``, and ``tools``, with the betas they need.

        Only the direct API gets the structured-outputs beta header; Bedrock sends strict tools without it.
        """
        params = self._with_thinking_binding(self._with_sampling_in_extra_body(self._authored_params(request)), request)
        extra_body = params.get("extra_body") or {}
        compaction_edit = uses_compaction_edit(extra_body.get("context_management", params.get("context_management")))
        extra_headers, header_betas = _split_beta_header(params.pop("extra_headers", None))
        if extra_headers:
            params["extra_headers"] = extra_headers
        # Betas authored in ``request_params`` or in a header map stay beside the ones MindRoom adds, in one header.
        betas = [
            *(params.get("betas") or []),
            *(self.settings.betas or []),
            *header_betas,
            *_split_beta_header(self._authored_default_headers())[1],
        ]
        if output_format is not None or (strict_tools and self.route == "anthropic"):
            betas.append(_STRUCTURED_OUTPUTS_BETA)
        if request.native_compaction is not None or compaction_edit:
            betas.append(COMPACTION_BETA)
        # The API rejects a binding without its beta, so an authored one gets it on every route.
        if _sends_thinking_binding(params):
            betas.append(_THINKING_BINDING_BETA)
        if betas:
            params["betas"] = list(dict.fromkeys(betas))
        if output_format is not None:
            params["output_config"] = {**(params.get("output_config") or {}), "format": output_format}
        return params

    def _prepare(self, request: ModelRequest) -> _PreparedRequest:
        """Build the SDK client and the wire body of one request."""
        settings, overrides = self.settings, request.overrides
        replay = request.native_compaction
        route = replay.route if replay is not None else None
        projected, replays_checkpoint = native_replay_messages_for_route(
            request.messages,
            route=route,
            manual_thinking=(settings.effective_thinking() or {}).get("type") == "enabled",
        )
        output_format = self._output_format(request.output_schema)
        output_tool = structured_output_tools(self.capabilities.structured_output, request.output_schema)
        # Vertex rejects the tool-level strict field.
        strict_allowed = self.route != "vertexai_claude"
        tools = format_claude_tools((*request.tools, *output_tool), strict_allowed=strict_allowed)
        strict_tools = strict_allowed and any(tool.strict for tool in request.tools)
        params = self._request_params(request, output_format, strict_tools=strict_tools)
        authors_thinking = settings.authors_thinking() and not overrides.omit_thinking_setting
        conversation = format_claude_messages(
            projected,
            append_trailing_user_message=not supports_prefill(self.identity.id) or authors_thinking,
            earlier_thinking=self._replays_earlier_thinking(params),
            provider=self.identity.provider,
        )
        body: dict[str, Any] = {"model": self.identity.id, "messages": conversation.messages, **params}
        if tools:
            body["tools"] = tools
        body = prepare_request_body(
            body,
            system=conversation.system,
            route=self.route,
            deferred_tool_names=request.deferred_tools.names,
            tool_choice=request.tool_choice,
            extended_cache_time=settings.extended_cache_time,
            cache_system_prompt=settings.cache_system_prompt and overrides.prompt_cache,
        )
        if output_tool:
            body = {**body, "tool_choice": {"type": "tool", "name": STRUCTURED_OUTPUT_TOOL}}
        return _PreparedRequest(
            client=self._client(),
            kwargs=body,
            # A strict tool goes through the beta endpoint even on Bedrock, which gets no beta header for it.
            beta=bool(body.get("betas")) or "context_management" in body or strict_tools,
            checkpoint_prefix=replays_checkpoint,
        )

    def _authored_default_headers(self) -> Mapping[str, str] | None:
        """Return the client's authored default headers: ``default_headers``, else the ones in ``client_params``."""
        if self.settings.default_headers is not None:
            return self.settings.default_headers
        return (self.settings.client_params or {}).get("default_headers")

    def _client(self) -> _AsyncClaudeClient:
        """Build this request's SDK client over the shared HTTP client.

        The route's own credentials and endpoint come first and ``client_params`` may override any of them;
        ``default_headers``, the shared HTTP client, and no SDK retries then win over ``client_params``, since the
        request pipeline alone retries a failed request.
        """
        settings = self.settings
        kwargs = _route_client_kwargs(self.route, settings)
        if settings.timeout is not None:
            kwargs["timeout"] = settings.timeout
        kwargs.update(settings.client_params or {})
        if (default_headers := self._authored_default_headers()) is not None:
            kwargs["default_headers"] = dict(default_headers)
        kwargs["http_client"] = self.http_client
        kwargs["max_retries"] = 0
        if self.route == "anthropic":
            return AsyncAnthropic(**kwargs)
        if self.route == "vertexai_claude":
            return self._vertex_client(kwargs)
        from anthropic.lib.bedrock import AsyncAnthropicBedrockMantle  # noqa: PLC0415 - optional aws_bedrock extra

        return AsyncAnthropicBedrockMantle(**kwargs)

    def _vertex_client(self, kwargs: dict[str, Any]) -> AsyncAnthropicVertex:
        """Build a Vertex client; without configured credentials it shares the adapter's ambient ones.

        Handing the SDK the credentials keeps it from running Application Default Credentials discovery and
        minting a token for every client; the project they name fills in when nothing else set one, as the
        SDK's own lookup would.
        """
        if "credentials" in kwargs or "access_token" in kwargs:
            return AsyncAnthropicVertex(**kwargs)
        credentials, project_id = self.google_credentials.load()
        client = AsyncAnthropicVertex(credentials=credentials, **kwargs)
        if client.project_id is None:
            client.project_id = project_id
        return client


# The raw API events that report the request's usage so far.
_USAGE_EVENT_TYPES = frozenset({"message_start", "message_delta"})
# Raw API events the SDK forwards; its convenience events (text, thinking, input_json, ...) repeat them.
_PROVIDER_EVENT_TYPES = frozenset({"content_block_start", "content_block_delta", "content_block_stop"})
# The HTTP status the SDK reports for an ``error`` event that arrives after the stream started.
_COMMITTED_STREAM_STATUS = 200
# How the SDK's ``TypeError`` begins when a direct-API client has no credential at all: no key or token in the config,
# the dashboard, or the variables the SDK reads, and no other credential source it discovers.
_NO_CREDENTIALS_ERROR = '"Could not resolve authentication method.'
# The blocks the model writes itself; every other block is a server tool's call or result.
_MODEL_BLOCK_TYPES = frozenset({"text", "thinking", "redacted_thinking", "tool_use"})


def _progress(event: Any) -> StreamProgress:  # noqa: ANN401 - SDK stream event
    """Return the progress event of a raw stream event, which commits the attempt when a server tool's block opens.

    A server tool is named by its ``server_tool_use`` block, and any other server block, such as a tool search
    result or a compaction, by its type.
    """
    if event.type != "content_block_start" or (block := event.content_block).type in _MODEL_BLOCK_TYPES:
        return StreamProgress()
    return hosted_tool_started(block.name if block.type == SERVER_TOOL_USE_BLOCK_TYPE else block.type)


def _provider_error(error: Exception, *, provider: str) -> ProviderError:
    """Return the shared error for an SDK, transport, or Google token failure; any other exception propagates.

    The Vertex client mints its Google access token inside the request. An ``error`` event inside the stream arrives
    with the stream's committed 200 status, so it is classified without a status. The body's error type and
    ``details.error_code`` name the failure for the classifier, and the error keeps the SDK's text, which repeats the
    whole body. The SDK finds a client's credentials only when it sends, so a client without any fails then, as the
    missing key it is.
    """
    if isinstance(error, TypeError) and str(error).startswith(_NO_CREDENTIALS_ERROR):
        return missing_api_key_error(PROVIDER_ENV_KEYS["anthropic"], provider=provider)
    if (failure := provider_failure(error, provider=provider)) is not None:
        return failure
    if isinstance(error, anthropic.APIStatusError):
        body = _ErrorBody.of(error.body)
        return classify_provider_error(
            error.message,
            provider=provider,
            status_code=None if error.status_code == _COMMITTED_STREAM_STATUS else error.status_code,
            code=body.code,
            error_type=body.type,
            retry_after_seconds=retry_after_seconds(error.response.headers),
        )
    if isinstance(error, anthropic.APIError):
        return ProviderError(str(error), provider=provider)
    raise error


@dataclass(frozen=True, slots=True)
class _ErrorBody:
    """The names and message of an error response's or a stream ``error`` event's ``error`` object.

    The body is ``{"type": "error", "error": {...}}``, and ``code`` is the ``details.error_code`` that tells apart
    failures sharing a type.
    """

    type: str | None
    code: str | None
    message: str | None

    @classmethod
    def of(cls, body: object) -> _ErrorBody:
        error = as_dict((as_dict(body) or {}).get("error")) or {}
        details = as_dict(error.get("details")) or {}
        error_type, code, message = error.get("type"), details.get("error_code"), error.get("message")
        return cls(
            type=error_type if isinstance(error_type, str) else None,
            code=code if isinstance(code, str) else None,
            message=message if isinstance(message, str) else None,
        )


# The media kind of each content block a rejection's body path names: Claude names a document by its source's type.
_BLOCK_KINDS: dict[str, MediaKind] = {"image": "image", "document": "file", "pdf": "file"}
# A reported rejection of an image Claude could not read, which names no path.
_UNPROCESSED_IMAGE = "could not process image"


def _rejected_media(error: Exception, messages: Sequence[Message]) -> frozenset[str]:
    """Return the ids of the attachments in ``messages`` of the kind a rejection names, or none when it names none.

    Claude starts the message with the body path of the block it rejects, as reported in
    ``messages.2.content.0.image.source.base64: image exceeds 5 MB maximum`` and
    ``messages.8.content.0.pdf.source.base64.data: The PDF specified was not valid.``; the block's name is its kind.
    The path indexes the request body rather than the conversation, so only the block's kind is read.
    """
    if not isinstance(error, anthropic.APIStatusError):
        return frozenset()
    text = _ErrorBody.of(error.body).message or ""
    path, separator, _detail = text.partition(": ")
    steps = path_tokens(path) if separator else []
    blocks = [
        name
        for step, index, name in zip(steps, steps[1:], steps[2:], strict=False)
        if step == "content" and isinstance(index, int) and isinstance(name, str)
    ]
    kind = _BLOCK_KINDS.get(blocks[-1]) if blocks else None
    if kind is None and _UNPROCESSED_IMAGE in text.casefold():
        kind = "image"
    return frozenset() if kind is None else media_ids(messages, kind)


def _output_event(event: object) -> TextDelta | ReasoningDelta | ToolCallReady | None:
    """Return the output a stream event carries: a text or thinking delta, or a tool call whose block closed.

    The SDK builds a delta or block of a type it does not know as the first model of its union, keeping the
    unknown ``type``, so the type decides.
    """
    if isinstance(event, (RawContentBlockDeltaEvent, BetaRawContentBlockDeltaEvent)):
        delta = event.delta
        if delta.type == "text_delta" and isinstance(delta, (AnthropicTextDelta, BetaTextDelta)):
            return TextDelta(delta.text)
        if delta.type == "thinking_delta" and isinstance(delta, (AnthropicThinkingDelta, BetaThinkingDelta)):
            return ReasoningDelta(delta.thinking)
    elif isinstance(event, (ParsedContentBlockStopEvent, ParsedBetaContentBlockStopEvent)):
        block = event.content_block
        if block.type == "tool_use" and isinstance(block, (ToolUseBlock, BetaToolUseBlock)):
            return ToolCallReady(tool_call_from_block(block))
    return None


def _thinking_owner(params: dict[str, Any]) -> dict[str, Any]:
    """Return the dict whose ``thinking`` the request sends: the SDK sends ``extra_body`` over the body's own fields."""
    extra_body = params.get("extra_body") or {}
    return extra_body if "thinking" in extra_body else params


def _split_beta_header(headers: Mapping[str, str] | None) -> tuple[dict[str, str], list[str]]:
    """Return ``headers`` without ``anthropic-beta`` (in any case) and the betas it names, in order.

    The SDK sends a request's ``betas`` as one ``anthropic-beta`` header, which a request's ``extra_headers`` replace
    and which replaces the client's default one, so an authored header joins ``betas`` instead; a request then always
    sends the client default's betas itself.
    """
    kept: dict[str, str] = {}
    betas: list[str] = []
    for name, value in (headers or {}).items():
        if name.lower() == "anthropic-beta":
            betas.extend(beta.strip() for beta in str(value).split(",") if beta.strip())
        else:
            kept[name] = value
    return kept, betas


def _sends_thinking_binding(params: dict[str, Any]) -> bool:
    """Return whether the thinking the request sends carries a ``block_binding``, which needs the binding beta."""
    thinking = _thinking_owner(params).get("thinking")
    return isinstance(thinking, dict) and thinking.get("block_binding") is not None


def _raw_request_params(settings: ClaudeSettings, *, omit_thinking_setting: bool) -> dict[str, Any]:
    """Return a copy of the authored raw request overrides, without thinking when the request omits the setting."""
    request_params = {
        key: dict(value) if isinstance(value, dict) else value for key, value in (settings.request_params or {}).items()
    }
    if omit_thinking_setting:
        request_params.pop("thinking", None)
        if isinstance(request_params.get("extra_body"), dict):
            request_params["extra_body"].pop("thinking", None)
    return request_params


def _bedrock_credentials(settings: ClaudeSettings) -> dict[str, Any]:
    """Return this request's AWS credentials; a boto3 session resolves rotating credentials every time."""
    if settings.session is None:
        return {
            "aws_access_key": settings.aws_access_key,
            "aws_secret_key": settings.aws_secret_key,
            "aws_session_token": settings.aws_session_token,
            "aws_region": settings.aws_region,
        }
    resolved = settings.session.get_credentials()
    if resolved is None:
        msg = (
            "boto3 session has no credentials. Check your AWS configuration "
            "(environment variables, config files, IAM role, etc.)."
        )
        raise ValueError(msg)
    credentials = resolved.get_frozen_credentials()
    return {
        "aws_access_key": credentials.access_key,
        "aws_secret_key": credentials.secret_key,
        "aws_session_token": credentials.token,
        "aws_region": settings.aws_region or settings.session.region_name,
    }


def _structured_output(route: ClaudeRouteKind, model_id: str, settings: ClaudeSettings) -> StructuredOutput:
    """Return how the route answers an output schema.

    The direct API answers natively where the model offers it; otherwise a forced tool call answers where the model
    accepts one, and JSON mode where it does not.
    """
    if route == "anthropic" and supports_structured_outputs(model_id):
        return "native"
    manual_thinking = (settings.effective_thinking() or {}).get("type") == "enabled"
    return "forced_tool" if supports_forced_tool_use(model_id, manual_thinking=manual_thinking) else "json_mode"


def _claude_capabilities(
    route: ClaudeRouteKind,
    model_id: str,
    settings: ClaudeSettings,
    endpoint: str | None,
) -> Capabilities:
    """Return what one Claude route offers for this model."""
    return Capabilities(
        structured_output=_structured_output(route, model_id, settings),
        omitted_media=frozenset({"audio", "video"}),
        native_tool_search=native_tool_search_supported(route, model_id),
        native_compaction=native_compaction_supported(
            route=route,
            model_id=model_id,
            settings=settings,
            endpoint=endpoint,
        ),
        # Vertex summaries always keep the provider's sampling default; the newest Claude generations reject
        # non-default sampling on every route.
        provider_default_sampling=route == "vertexai_claude" or claude_rejects_sampling_controls(model_id),
    )


def _anthropic_kwargs(authored: dict[str, Any], api_key: str | None, runtime_paths: RuntimePaths) -> dict[str, Any]:
    """Fill the direct route's key and, unless ``client_params`` sets one, its endpoint from the runtime env."""
    kwargs: dict[str, Any] = {**authored, "api_key": api_key} if api_key else dict(authored)
    client_params = kwargs.get("client_params") or {}
    if "base_url" not in client_params and (
        base_url := get_secret_from_env(ENDPOINT_VARIABLES["anthropic"], runtime_paths=runtime_paths)
    ):
        kwargs["client_params"] = {**client_params, "base_url": base_url}
    return kwargs


def _vertex_kwargs(authored: dict[str, Any], runtime_paths: RuntimePaths) -> dict[str, Any]:
    """Fill Vertex project, region, endpoint, and Google credentials from the runtime env; a key is ignored."""
    kwargs = {key: value for key, value in authored.items() if key != "api_key"}
    for config_key, env_key in (
        ("project_id", VERTEXAI_CLAUDE_ENV_BY_KEY["project_id"]),
        ("region", VERTEXAI_CLAUDE_ENV_BY_KEY["region"]),
        ("base_url", ENDPOINT_VARIABLES["vertexai_claude"]),
    ):
        if config_key not in kwargs and (value := get_secret_from_env(env_key, runtime_paths=runtime_paths)):
            kwargs[config_key] = value
    return with_runtime_application_credentials(kwargs, runtime_paths)


def _bedrock_static_credentials(kwargs: dict[str, Any], runtime_paths: RuntimePaths) -> None:
    for config_key, env_key in (
        ("aws_access_key", AWS_ENV_BY_KEY["access_key"]),
        ("aws_secret_key", AWS_ENV_BY_KEY["secret_key"]),
        ("aws_session_token", AWS_ENV_BY_KEY["session_token"]),
    ):
        if config_key not in kwargs and (value := get_secret_from_env(env_key, runtime_paths=runtime_paths)):
            kwargs[config_key] = value


def _bedrock_session(kwargs: dict[str, Any], aws_profile: str | None) -> None:
    """Attach a boto3 session for profile or ambient IAM-role credential resolution."""
    with optional_dependency_environment():
        import boto3.session  # noqa: PLC0415 - optional aws_bedrock extra

    session_kwargs: dict[str, str] = {}
    if aws_profile:
        session_kwargs["profile_name"] = aws_profile
    if aws_region := kwargs.get("aws_region"):
        session_kwargs["region_name"] = str(aws_region)
    kwargs["session"] = boto3.session.Session(**session_kwargs)


def _bedrock_kwargs(authored: dict[str, Any], runtime_paths: RuntimePaths) -> dict[str, Any]:
    """Resolve Bedrock credentials: an explicit session, static keys, a profile, or the ambient AWS chain."""
    ensure_optional_deps(
        ["boto3"],
        "aws_bedrock",
        runtime_paths,
        missing_message="Missing AWS Bedrock dependencies. Install with: pip install 'mindroom[aws_bedrock]'",
    )
    kwargs = {key: value for key, value in authored.items() if key not in {"api_key", "aws_profile"}}
    explicit_profile = authored.get("aws_profile")
    if "session" in kwargs:
        return kwargs
    if "aws_region" not in kwargs and (
        found := runtime_paths.first_env_value((AWS_ENV_BY_KEY["region"], AWS_ENV_BY_KEY["default_region"]))
    ):
        kwargs["aws_region"] = found[0]
    if "aws_access_key" in kwargs or "aws_secret_key" in kwargs:
        _bedrock_static_credentials(kwargs, runtime_paths)
        if explicit_profile:
            logger.debug("bedrock_claude_profile_ignored_with_static_credentials")
        return kwargs
    if explicit_profile:
        _bedrock_session(kwargs, str(explicit_profile))
        return kwargs
    _bedrock_static_credentials(kwargs, runtime_paths)
    if kwargs.get("aws_access_key") or kwargs.get("aws_secret_key"):
        return kwargs
    _bedrock_session(kwargs, get_secret_from_env(AWS_ENV_BY_KEY["profile"], runtime_paths=runtime_paths))
    return kwargs


def build_provider(model_config: ModelConfig, runtime_paths: RuntimePaths) -> ClaudeProvider:
    """Build the adapter for one ``anthropic``, ``vertexai_claude``, or ``bedrock_claude`` model config.

    The direct route takes the model's own key, else the shared Anthropic key, unless the model authenticates
    with ``auth_token``; Vertex and Bedrock resolve their project, region, and credentials from the runtime
    env. A key the dashboard saved for the model arrives as ``model_config.api_key``.
    """
    route = cast("ClaudeRouteKind", provider_route(model_config.provider))
    authored = dict(model_config.extra_kwargs or {})
    if route == "anthropic":
        authored = _anthropic_kwargs(authored, model_api_key(model_config, runtime_paths), runtime_paths)
    elif route == "vertexai_claude":
        authored = _vertex_kwargs(authored, runtime_paths)
    else:
        authored = _bedrock_kwargs(authored, runtime_paths)
    settings = parse_claude_settings(route, model_config.id, authored)
    endpoint = _route_endpoint(route, settings)
    identity = model_identity(model_config, endpoint=endpoint)
    capabilities = _claude_capabilities(route, model_config.id, settings, endpoint)
    return ClaudeProvider(identity=identity, capabilities=capabilities, route=route, settings=settings)


__all__ = ["ClaudeProvider", "build_provider", "vertex_project_and_region"]
