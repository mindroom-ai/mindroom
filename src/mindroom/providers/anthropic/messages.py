"""The Claude adapter over the Anthropic Messages API, on the direct API, Vertex AI, and Bedrock Mantle.

One frozen adapter serves all three routes; per-request state travels in ``ModelRequest``. Each request
fetches its URL-backed media, then builds its SDK client and wire body off the event loop (Bedrock credential
resolution and attachment encoding block), streams the reply through the SDK's message stream, and parses the
finished message once. A request with ``stream=False`` is sent as one non-streaming call instead. The only
state an adapter keeps is the ambient Google credentials a Vertex route discovers once.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, NoReturn, cast

import anthropic
import httpx
from anthropic import NOT_GIVEN, AsyncAnthropic, AsyncAnthropicVertex
from anthropic.lib.streaming import (
    ParsedBetaContentBlockStopEvent,
    ParsedBetaMessageStopEvent,
    ParsedContentBlockStopEvent,
    ParsedMessageStopEvent,
)
from anthropic.types import RawContentBlockDeltaEvent, RawMessageStartEvent, ToolUseBlock
from anthropic.types import TextDelta as AnthropicTextDelta
from anthropic.types import ThinkingDelta as AnthropicThinkingDelta
from anthropic.types.beta import (
    BetaRawContentBlockDeltaEvent,
    BetaRawMessageStartEvent,
    BetaTextDelta,
    BetaThinkingDelta,
    BetaToolUseBlock,
)
from pydantic import BaseModel

from mindroom.constants import runtime_env_path
from mindroom.credentials.sync import get_api_key_for_provider, get_secret_from_env
from mindroom.llm.errors import INCOMPLETE_STREAM, ProviderError
from mindroom.llm.provider import (
    Capabilities,
    ModelIdentity,
    ModelRequest,
    NativeCompactionReplay,
    ReasoningDelta,
    StreamEvent,
    StreamProgress,
    TextDelta,
    ToolCallReady,
    TurnCompleted,
    UsageReported,
)
from mindroom.logging_config import get_logger
from mindroom.model_defaults import CLAUDE_PROVIDER_DEFAULT_SAMPLING_MODEL_SUFFIXES
from mindroom.native_compaction import native_compaction_route
from mindroom.providers.anthropic.native_compaction import (
    COMPACTION_BETA,
    MIN_COMPACTION_THRESHOLD,
    authored_native_compaction_supported,
    compaction_context_management,
    native_compaction_supported,
    native_replay_messages_for_route,
    record_native_checkpoint_on,
    reject_paused_compaction,
    request_uses_native_checkpoint,
    uses_compaction_edit,
)
from mindroom.providers.anthropic.prompt_cache import native_tool_search_supported, prepare_request_body
from mindroom.providers.anthropic.settings import ClaudeRouteKind, ClaudeSettings, parse_claude_settings
from mindroom.providers.anthropic.wire import (
    OUTPUT_LIMIT_STOP_REASONS,
    assistant_message_from_response,
    build_system_blocks,
    format_claude_messages,
    format_claude_tools,
    inline_url_media,
    parse_claude_usage,
    stop_reason_of,
    supports_prefill,
    supports_structured_outputs,
    tool_call_from_block,
)
from mindroom.providers.assistant_turn import one_message_per_result, stamp_finished_turn
from mindroom.providers.errors import classify_provider_error, transport_error
from mindroom.providers.google_adc import load_ambient_google_credentials, load_google_application_credentials
from mindroom.providers.model_http_client import MODEL_HTTP_CLIENT
from mindroom.providers.model_loading import canonical_provider
from mindroom.runtime_env_policy import AWS_BEDROCK_CLAUDE_ENV_BY_KEY, VERTEXAI_CLAUDE_ENV_BY_KEY
from mindroom.tool_system.dependencies import ensure_optional_deps, optional_dependency_environment

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

    from anthropic.lib.bedrock import AsyncAnthropicBedrockMantle
    from anthropic.types import Message as AnthropicMessage
    from anthropic.types.beta import BetaMessage
    from google.auth.credentials import Credentials as GoogleCredentials

    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import StopReason

    type _AsyncClaudeClient = AsyncAnthropic | AsyncAnthropicVertex | AsyncAnthropicBedrockMantle

logger = get_logger(__name__)

PROVIDER_NAMES: dict[ClaudeRouteKind, str] = {
    "anthropic": "Anthropic",
    "vertexai_claude": "VertexAI",
    "bedrock_claude": "AwsBedrock",
}
STRUCTURED_OUTPUTS_BETA = "structured-outputs-2025-11-13"
_DEFAULT_ANTHROPIC_ENDPOINT = "https://api.anthropic.com"
_VERTEX_ENDPOINTS = {
    "global": "https://aiplatform.googleapis.com/v1",
    "us": "https://aiplatform.us.rep.googleapis.com/v1",
    "eu": "https://aiplatform.eu.rep.googleapis.com/v1",
}
_SAMPLING_CONTROL_NAMES = ("temperature", "top_p", "top_k")


def _canonical_route(provider: str) -> ClaudeRouteKind:
    canonical = canonical_provider(provider)
    if canonical not in PROVIDER_NAMES:
        msg = f"Provider {provider!r} is not a Claude route; expected one of {', '.join(sorted(PROVIDER_NAMES))}"
        raise ValueError(msg)
    return cast("ClaudeRouteKind", canonical)


def native_compaction_endpoint(route: ClaudeRouteKind, settings: ClaudeSettings) -> str:
    """Return the endpoint identity that binds stored checkpoints to one route, or "" when it has none.

    Unset Vertex values fall back to the process environment the SDK reads, so the identity matches the
    client the SDK would build.
    """
    client_params = settings.client_params or {}
    if route == "anthropic":
        base_url = client_params.get("base_url") or os.environ.get("ANTHROPIC_BASE_URL") or _DEFAULT_ANTHROPIC_ENDPOINT
        return str(base_url).rstrip("/")
    if route == "vertexai_claude":
        region = settings.region or os.environ.get(VERTEXAI_CLAUDE_ENV_BY_KEY["region"])
        project = settings.project_id or os.environ.get(VERTEXAI_CLAUDE_ENV_BY_KEY["project_id"])
        base_url = (
            settings.base_url
            or client_params.get("base_url")
            or os.environ.get("ANTHROPIC_VERTEX_BASE_URL")
            or _VERTEX_ENDPOINTS.get(region or "", f"https://{region}-aiplatform.googleapis.com/v1")
        )
        return f"{str(base_url).rstrip('/')}|{project}|{region}"
    return ""


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
    http_client: httpx.AsyncClient = MODEL_HTTP_CLIENT
    google_credentials: _AmbientGoogleCredentials = field(default_factory=_AmbientGoogleCredentials, compare=False)

    def format_tool_results(self, messages: Sequence[Message], results: Sequence[Message]) -> list[Message]:
        """Append each result as its own tool message; the wire formatter merges them into one user turn."""
        return one_message_per_result(messages, results)

    def native_compaction_settings(
        self,
        *,
        threshold: int | None,
        history_generation: str = "",
        allow_authored: bool = False,
    ) -> NativeCompactionReplay | None:
        """Return the replay settings of a request that wants compaction at ``threshold`` input tokens.

        A threshold under Claude's minimum, or one on a route without MindRoom-managed compaction, falls back
        to the authored provider policy when ``allow_authored`` and the model authored a compaction edit;
        otherwise the request runs without native compaction.
        """
        if threshold is not None and threshold < MIN_COMPACTION_THRESHOLD:
            threshold = None
        if threshold is None or not self.capabilities.native_compaction:
            if not allow_authored or not authored_native_compaction_supported(
                provider=self.identity.provider,
                settings=self.settings,
            ):
                return None
            threshold = None
        endpoint = native_compaction_endpoint(self.route, self.settings)
        if not endpoint:
            return None
        route = native_compaction_route(
            provider=self.identity.provider,
            model_id=self.identity.id,
            endpoint=endpoint,
            history_generation=history_generation,
        )
        return NativeCompactionReplay(route=route, threshold=threshold)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Send one request and yield its events; the SDK's message stream accumulates the final message.

        ``message_start`` reports the input and cache usage as a partial ``UsageReported`` so a stream that
        stops early still settles what Anthropic bills. A request with ``stream=False`` is sent without
        streaming and yields its usage and finished turn once the response arrives.
        """
        started = time.monotonic()
        messages = await inline_url_media(request.messages, self.http_client)
        if messages is not request.messages:
            request = replace(request, messages=messages)
        prepared = await asyncio.to_thread(self._prepare, request)
        client, kwargs = prepared.client, prepared.kwargs
        first_output_at: float | None = None
        server_tool_started = False
        try:
            if not request.stream:
                response = await (
                    client.beta.messages.create(**kwargs) if prepared.beta else client.messages.create(**kwargs)
                )
                message, stop_reason = self._finish(response, request, prepared, started, first_output_at)
                yield UsageReported(message.metrics)
                yield TurnCompleted(message=message, stop_reason=stop_reason)
                return
            manager = client.beta.messages.stream(**kwargs) if prepared.beta else client.messages.stream(**kwargs)
            async with manager as events:
                async for event in events:
                    if isinstance(event, (RawMessageStartEvent, BetaRawMessageStartEvent)):
                        yield UsageReported(parse_claude_usage(event.message.usage), final=False)
                    elif isinstance(event, (ParsedMessageStopEvent, ParsedBetaMessageStopEvent)):
                        message, stop_reason = self._finish(event.message, request, prepared, started, first_output_at)
                        yield UsageReported(message.metrics)
                        yield TurnCompleted(message=message, stop_reason=stop_reason)
                    elif (output := _output_event(event)) is not None:
                        first_output_at = first_output_at or time.monotonic()
                        yield output
                    elif event.type in _PROVIDER_EVENT_TYPES:
                        # Block starts, tool-call argument pieces, and server-tool blocks carry no output but
                        # show the provider is still working; the SDK's own convenience events repeat them.
                        server_tool_started = server_tool_started or _starts_server_tool(event)
                        yield StreamProgress()
        except (anthropic.APIError, httpx.TransportError) as error:
            self._raise_provider_error(error, server_tool_started=server_tool_started)

    def _finish(
        self,
        response: AnthropicMessage | BetaMessage,
        request: ModelRequest,
        prepared: _PreparedRequest,
        started: float,
        first_output_at: float | None,
    ) -> tuple[Message, StopReason | None]:
        """Build the finished assistant message with MindRoom's provenance keys and timings."""
        message = assistant_message_from_response(
            response,
            record_context_management=self.settings.context_management is not None,
        )
        stamp_finished_turn(message, self.identity, started=started, first_output_at=first_output_at)
        record_native_checkpoint_on(message, request.native_compaction, checkpoint_prefix=prepared.checkpoint_prefix)
        raw_stop_reason = response.stop_reason
        if raw_stop_reason in OUTPUT_LIMIT_STOP_REASONS:
            logger.warning(
                "claude_output_limit_reached",
                model_id=self.identity.id,
                stop_reason=raw_stop_reason,
                tool_calls=len(message.tool_calls or []),
            )
        elif raw_stop_reason == "refusal":
            logger.warning("claude_safeguard_refusal", model_id=self.identity.id, stop_reason=raw_stop_reason)
        return message, stop_reason_of(raw_stop_reason)

    def _output_format(self, response_format: type[BaseModel] | Mapping[str, Any] | None) -> dict[str, Any] | None:
        """Return the ``output_config.format`` value, or None when the route answers in JSON mode instead."""
        if response_format is None or self.capabilities.structured_output != "native":
            return None
        if isinstance(response_format, type) and issubclass(response_format, BaseModel):
            return {"type": "json_schema", "schema": anthropic.transform_schema(response_format.model_json_schema())}
        # Claude has json_schema output only; a json_object request relies on the prompt asking for JSON.
        return None if response_format.get("type") == "json_object" else dict(response_format)

    def _authored_params(self, request: ModelRequest) -> dict[str, Any]:
        """Return the model's authored request parameters with the request's overrides applied."""
        settings, overrides = self.settings, request.overrides
        params: dict[str, Any] = {}
        max_tokens = settings.max_tokens if overrides.max_output_tokens is None else overrides.max_output_tokens
        if max_tokens:
            params["max_tokens"] = max_tokens
        if settings.thinking and not overrides.disable_thinking:
            params["thinking"] = dict(settings.thinking)
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
        params.update(_raw_request_params(settings, disable_thinking=overrides.disable_thinking))
        if overrides.timeout_seconds is not None:
            params["timeout"] = overrides.timeout_seconds
        return params

    def _with_sampling_in_extra_body(self, params: dict[str, Any]) -> dict[str, Any]:
        """Move sampling controls into ``extra_body`` and drop them for the generations that reject them."""
        moved = {name: params.pop(name) for name in _SAMPLING_CONTROL_NAMES if name in params}
        extra_body: dict[str, Any] = {**moved, **(params.get("extra_body") or {})}
        if self.identity.id.casefold().endswith(CLAUDE_PROVIDER_DEFAULT_SAMPLING_MODEL_SUFFIXES):
            for name in _SAMPLING_CONTROL_NAMES:
                extra_body.pop(name, None)
        if extra_body:
            params["extra_body"] = extra_body
        else:
            params.pop("extra_body", None)
        return params

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
        params = self._with_sampling_in_extra_body(self._authored_params(request))
        extra_body = params.get("extra_body") or {}
        context_management = extra_body.get("context_management", params.get("context_management")) or {}
        reject_paused_compaction(context_management)
        betas = list(self.settings.betas or [])
        if output_format is not None or (strict_tools and self.route == "anthropic"):
            betas.append(STRUCTURED_OUTPUTS_BETA)
        if request.native_compaction is not None or uses_compaction_edit(context_management):
            betas.append(COMPACTION_BETA)
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
        projected = native_replay_messages_for_route(
            request.messages,
            route=route,
            manual_thinking=(settings.effective_thinking() or {}).get("type") == "enabled",
        )
        output_format = self._output_format(request.response_format)
        thinking_enabled = settings.thinking_enabled() and not overrides.disable_thinking
        conversation = format_claude_messages(
            projected,
            append_trailing_user_message=not supports_prefill(self.identity.id) or thinking_enabled,
            enable_citations=output_format is None,
        )
        # Vertex rejects the tool-level strict field.
        strict_allowed = self.route != "vertexai_claude"
        tools = [
            *format_claude_tools(request.tools, strict_allowed=strict_allowed),
            *(dict(tool) for tool in request.native_tools),
        ]
        strict_tools = strict_allowed and any(tool.strict for tool in request.tools)
        body: dict[str, Any] = {
            "model": self.identity.id,
            "messages": conversation.messages,
            **self._request_params(request, output_format, strict_tools=strict_tools),
        }
        cache_system_prompt = settings.cache_system_prompt and overrides.prompt_cache is not False
        if conversation.system:
            body["system"] = build_system_blocks(
                conversation.system,
                cache_system_prompt=cache_system_prompt,
                extended_cache_time=settings.extended_cache_time,
            )
        if tools:
            body["tools"] = tools
        body = prepare_request_body(
            body,
            route=self.route,
            deferred_tool_names=request.deferred_tool_names,
            tool_choice=request.tool_choice,
            extended_cache_time=settings.extended_cache_time,
            cache_system_prompt=cache_system_prompt,
        )
        return _PreparedRequest(
            client=self._client(overrides.timeout_seconds, overrides.sdk_max_retries),
            kwargs=body,
            # A strict tool goes through the beta endpoint even on Bedrock, which gets no beta header for it.
            beta=bool(body.get("betas")) or "context_management" in body or strict_tools,
            checkpoint_prefix=request_uses_native_checkpoint(projected, route),
        )

    def _client(self, timeout_seconds: float | None, max_retries: int | None) -> _AsyncClaudeClient:
        """Build this request's SDK client over the shared HTTP client.

        The route's own credentials and endpoint come first and ``client_params`` may override any of them;
        ``default_headers`` and the shared HTTP client then win over ``client_params``, and a caller's
        per-request deadline and retry budget win over everything the model authored.
        """
        settings = self.settings
        kwargs = self._route_client_kwargs()
        if settings.timeout is not None:
            kwargs["timeout"] = settings.timeout
        kwargs.update(settings.client_params or {})
        if settings.default_headers is not None:
            kwargs["default_headers"] = dict(settings.default_headers)
        kwargs["http_client"] = self.http_client
        if timeout_seconds is not None:
            kwargs["timeout"] = timeout_seconds
        if max_retries is not None:
            kwargs["max_retries"] = max_retries
        if self.route == "anthropic":
            return AsyncAnthropic(**kwargs)
        if self.route == "vertexai_claude":
            return self._vertex_client(kwargs)
        return _bedrock_client(kwargs)

    def _route_client_kwargs(self) -> dict[str, Any]:
        """Return the credentials and endpoint of this route, before ``client_params`` and the HTTP client."""
        settings = self.settings
        if self.route == "anthropic":
            return {"api_key": settings.api_key, "auth_token": settings.auth_token}
        if self.route == "vertexai_claude":
            # Unset values stay unset so the SDK reads CLOUD_ML_REGION, ANTHROPIC_VERTEX_PROJECT_ID, and
            # ANTHROPIC_VERTEX_BASE_URL itself.
            return {
                "region": NOT_GIVEN if settings.region is None else settings.region,
                "project_id": NOT_GIVEN if settings.project_id is None else settings.project_id,
                "base_url": settings.base_url,
            }
        return _bedrock_credentials(settings)

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

    def _raise_provider_error(self, error: Exception, *, server_tool_started: bool) -> NoReturn:
        """Translate an SDK or transport failure into the shared provider errors.

        Once a server tool (web search, code execution, tool search) started, a retry would run it again, so the
        failure is final and carries the code that keeps the media fallback from resending the request.
        """
        provider = self.identity.provider_name
        if isinstance(error, anthropic.APIConnectionError | httpx.TransportError):
            mapped: ProviderError = transport_error(error, provider=provider)
        elif isinstance(error, anthropic.APIStatusError):
            mapped = classify_provider_error(error.message, provider=provider, status_code=error.status_code)
        else:
            mapped = ProviderError(str(error), provider=provider)
        if server_tool_started and mapped.retryable:
            msg = f"Claude stream failed after a server tool started: {mapped}"
            raise ProviderError(
                msg,
                provider=provider,
                status_code=mapped.status_code,
                code=INCOMPLETE_STREAM,
            ) from error
        raise mapped from error


# Raw API events the SDK forwards; its convenience events (text, thinking, input_json, ...) repeat them.
_PROVIDER_EVENT_TYPES = frozenset({"content_block_start", "content_block_delta", "content_block_stop", "message_delta"})
# Blocks the model produces itself; any other block type is a server tool at work.
_MODEL_BLOCK_TYPES = frozenset({"text", "thinking", "redacted_thinking", "tool_use"})


def _starts_server_tool(event: Any) -> bool:  # noqa: ANN401 - SDK stream event
    """Return whether a ``content_block_start`` opens a block a server-side tool produces."""
    return event.type == "content_block_start" and event.content_block.type not in _MODEL_BLOCK_TYPES


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


def _raw_request_params(settings: ClaudeSettings, *, disable_thinking: bool) -> dict[str, Any]:
    """Return a copy of the authored raw request overrides, without thinking when the request disables it."""
    request_params = {
        key: dict(value) if isinstance(value, dict) else value for key, value in (settings.request_params or {}).items()
    }
    if disable_thinking:
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


def _bedrock_client(kwargs: dict[str, Any]) -> AsyncAnthropicBedrockMantle:
    from anthropic.lib.bedrock import AsyncAnthropicBedrockMantle  # noqa: PLC0415 - optional aws_bedrock extra

    return AsyncAnthropicBedrockMantle(**kwargs)


def claude_capabilities(route: ClaudeRouteKind, model_id: str, settings: ClaudeSettings) -> Capabilities:
    """Return what one Claude route offers for this model."""
    provider = PROVIDER_NAMES[route]
    endpoint = native_compaction_endpoint(route, settings)
    return Capabilities(
        structured_output="native" if route == "anthropic" and supports_structured_outputs(model_id) else "json_mode",
        omitted_media=frozenset({"audio", "video"}),
        native_tool_search=native_tool_search_supported(route, model_id),
        prompt_cache_breakpoints=settings.cache_system_prompt,
        native_compaction=native_compaction_supported(
            provider=provider,
            model_id=model_id,
            settings=settings,
            endpoint=endpoint,
        ),
        # Vertex summaries always keep the provider's sampling default; the newest Claude generations reject
        # non-default sampling on every route.
        provider_default_sampling=route == "vertexai_claude"
        or model_id.casefold().endswith(CLAUDE_PROVIDER_DEFAULT_SAMPLING_MODEL_SUFFIXES),
        claude_family=True,
    )


def _vertex_kwargs(authored: dict[str, Any], runtime_paths: RuntimePaths) -> dict[str, Any]:
    """Fill Vertex project, region, endpoint, and Google credentials from the runtime env; a key is ignored."""
    kwargs = {key: value for key, value in authored.items() if key != "api_key"}
    if "project_id" not in kwargs and (
        project_id := get_secret_from_env(VERTEXAI_CLAUDE_ENV_BY_KEY["project_id"], runtime_paths=runtime_paths)
    ):
        kwargs["project_id"] = project_id
    if "region" not in kwargs and (region := runtime_paths.env_value(VERTEXAI_CLAUDE_ENV_BY_KEY["region"])):
        kwargs["region"] = region
    if "base_url" not in kwargs and (base_url := runtime_paths.env_value("ANTHROPIC_VERTEX_BASE_URL")):
        kwargs["base_url"] = base_url
    client_params = dict(kwargs.get("client_params") or {})
    if "credentials" not in client_params and (
        credentials_path := runtime_env_path(runtime_paths, "GOOGLE_APPLICATION_CREDENTIALS")
    ):
        client_params["credentials"] = load_google_application_credentials(str(credentials_path))
    if client_params:
        kwargs["client_params"] = client_params
    return kwargs


def _bedrock_static_credentials(kwargs: dict[str, Any], runtime_paths: RuntimePaths) -> None:
    for config_key, env_key in (
        ("aws_access_key", AWS_BEDROCK_CLAUDE_ENV_BY_KEY["access_key"]),
        ("aws_secret_key", AWS_BEDROCK_CLAUDE_ENV_BY_KEY["secret_key"]),
        ("aws_session_token", AWS_BEDROCK_CLAUDE_ENV_BY_KEY["session_token"]),
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
    if "aws_region" not in kwargs:
        aws_region = get_secret_from_env(
            AWS_BEDROCK_CLAUDE_ENV_BY_KEY["region"],
            runtime_paths=runtime_paths,
        ) or get_secret_from_env(AWS_BEDROCK_CLAUDE_ENV_BY_KEY["default_region"], runtime_paths=runtime_paths)
        if aws_region:
            kwargs["aws_region"] = aws_region
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
    _bedrock_session(kwargs, get_secret_from_env(AWS_BEDROCK_CLAUDE_ENV_BY_KEY["profile"], runtime_paths=runtime_paths))
    return kwargs


def build_provider(model_config: ModelConfig, runtime_paths: RuntimePaths) -> ClaudeProvider:
    """Build the adapter for one ``anthropic``, ``vertexai_claude``, or ``bedrock_claude`` model config.

    The direct route takes the model's own key, else the shared Anthropic key, unless the model authenticates
    with ``auth_token``; Vertex and Bedrock resolve their project, region, and credentials from the runtime
    env. A key the dashboard saved for the model arrives as ``model_config.api_key``.
    """
    route = _canonical_route(model_config.provider)
    authored = dict(model_config.extra_kwargs or {})
    if route == "anthropic":
        api_key = model_config.configured_api_key()
        if api_key is None and not authored.get("auth_token"):
            api_key = get_api_key_for_provider("anthropic", runtime_paths=runtime_paths)
        if api_key:
            authored["api_key"] = api_key
    elif route == "vertexai_claude":
        authored = _vertex_kwargs(authored, runtime_paths)
    else:
        authored = _bedrock_kwargs(authored, runtime_paths)
    settings = parse_claude_settings(route, authored)
    provider_name = PROVIDER_NAMES[route]
    identity = ModelIdentity(id=model_config.id, provider=provider_name, provider_name=provider_name)
    capabilities = claude_capabilities(route, model_config.id, settings)
    return ClaudeProvider(identity=identity, capabilities=capabilities, route=route, settings=settings)


__all__ = [
    "PROVIDER_NAMES",
    "STRUCTURED_OUTPUTS_BETA",
    "ClaudeProvider",
    "build_provider",
    "claude_capabilities",
    "native_compaction_endpoint",
]
