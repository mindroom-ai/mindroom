"""The protocol every model provider adapter implements, and the request and stream values it exchanges.

Adapters are stream-only; ``complete`` drains a stream for callers that want one finished turn.
Per-request state travels in ``ModelRequest`` rather than on the adapter, so adapters stay immutable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from mindroom.llm.errors import ProviderError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

    from pydantic import BaseModel

    from mindroom.llm.messages import Message
    from mindroom.llm.usage import MessageMetrics

type MediaKind = Literal["audio", "image", "file", "video"]
type StopReason = Literal["end", "tool_use", "output_limit", "refusal", "content_filter", "incomplete"]
type ToolChoice = Literal["auto", "none", "required"] | Mapping[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelIdentity:
    """Who answers a request.

    ``id`` is the effective model id, ``provider`` the stored ``model_provider`` string (Agno's ``Model.provider``),
    and ``provider_name`` the metrics provider string (Agno's ``get_provider()``) that usage rows and pricing key by.
    ``endpoint`` is the base URL requests go to, when the route has one; capabilities learned from provider
    errors and native-compaction routes are keyed by it.
    """

    id: str
    provider: str
    provider_name: str
    endpoint: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Capabilities:
    """What a provider route supports, so policy code stops probing adapter classes."""

    structured_output: Literal["native", "json_schema", "json_mode"] = "json_mode"
    omitted_media: frozenset[MediaKind] = frozenset()
    native_tool_search: bool = False
    prompt_cache_breakpoints: bool = False
    native_compaction: bool = False
    approval_receipt_after_response_id: bool = False
    provider_default_sampling: bool = False
    claude_family: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolSpec:
    """A model-facing tool definition; ``parameters`` is the final JSON schema.

    ``strict`` None means the tool never chose strictness, so wires that send the flag omit it.
    The approval flags are not policy here: OpenAI-family wires send them inside each function definition
    exactly as Agno's ``Function.to_dict()`` did, so request bodies and prompt caches stay unchanged.
    """

    name: str
    description: str
    parameters: Mapping[str, Any]
    strict: bool | None = False
    requires_confirmation: bool | None = None
    external_execution: bool | None = None
    approval_type: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RequestOverrides:
    """Per-request knobs that override the adapter's configured defaults; None leaves a default untouched."""

    temperature: float | None = None
    max_output_tokens: int | None = None
    timeout_seconds: float | None = None
    sdk_max_retries: int | None = None
    prompt_cache: bool | None = None
    disable_thinking: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeCompaction:
    """Provider-side compaction for one replay route; ``threshold`` None keeps the authored provider policy."""

    route: str
    threshold: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelRequest:
    """One provider request; ``native_tools`` are hosted tools passed through as the provider's own dicts.

    ``stream=False`` asks for one non-streaming provider call where the route offers one; the events still
    arrive through ``stream()``, after the response completes.
    ``deferred_tool_names`` name tools kept out of the rendered prompt: routes with native tool search defer
    them server-side, and the portable tool-search wrapper hides them behind ``call_tool`` everywhere else.
    ``portable_replay`` replays history explicitly instead of continuing provider-stored state, and
    ``native_compaction`` turns on provider-side compaction for routes that support it.
    """

    messages: Sequence[Message]
    tools: Sequence[ToolSpec] = ()
    tool_choice: ToolChoice | None = None
    response_format: type[BaseModel] | Mapping[str, Any] | None = None
    native_tools: Sequence[Mapping[str, Any]] = ()
    overrides: RequestOverrides = RequestOverrides()
    stream: bool = True
    deferred_tool_names: frozenset[str] = frozenset()
    portable_replay: bool = False
    native_compaction: NativeCompaction | None = None


@dataclass(frozen=True, slots=True)
class TextDelta:
    """A piece of the assistant's visible text."""

    text: str


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    """A piece of the assistant's reasoning text."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolCallReady:
    """A complete tool call in the stored shape ``{"id", "type": "function", "function": {"name", "arguments"}}``."""

    call: dict[str, Any]


@dataclass(frozen=True, slots=True)
class UsageReported:
    """Usage the provider reported; ``final=False`` for a partial report before the turn ends."""

    usage: MessageMetrics
    final: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnCompleted:
    """The last stream event: the finished assistant message with its metrics and provider data."""

    message: Message
    stop_reason: StopReason | None = None


type StreamEvent = TextDelta | ReasoningDelta | ToolCallReady | UsageReported | TurnCompleted


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelTurn:
    """One finished model turn, as ``complete`` returns it."""

    message: Message
    stop_reason: StopReason | None = None

    @property
    def content(self) -> str:
        """The assistant's text."""
        return self.message.get_content_string()

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        """The tool calls the model requested, in the stored shape."""
        return list(self.message.tool_calls or [])

    @property
    def usage(self) -> MessageMetrics:
        """The usage this turn reported."""
        return self.message.metrics


class ModelProvider(Protocol):
    """A model behind one provider route."""

    @property
    def identity(self) -> ModelIdentity:
        """Who answers requests."""
        ...

    @property
    def capabilities(self) -> Capabilities:
        """What this route supports."""
        ...

    def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Send one request and yield its events; ``TurnCompleted`` is the last one."""
        ...

    def format_tool_results(self, messages: Sequence[Message], results: Sequence[Message]) -> list[Message]:
        """Return the messages to append for one batch of tool results, given the conversation so far."""
        ...


async def complete(provider: ModelProvider, request: ModelRequest) -> ModelTurn:
    """Drain one request's stream and return its finished turn."""
    completed: TurnCompleted | None = None
    async for event in provider.stream(request):
        if isinstance(event, TurnCompleted):
            completed = event
    if completed is None:
        msg = "The provider stream ended without completing the turn"
        raise ProviderError(msg, provider=provider.identity.provider_name, code="incomplete_stream")
    return ModelTurn(message=completed.message, stop_reason=completed.stop_reason)


__all__ = [
    "Capabilities",
    "MediaKind",
    "ModelIdentity",
    "ModelProvider",
    "ModelRequest",
    "ModelTurn",
    "NativeCompaction",
    "ReasoningDelta",
    "RequestOverrides",
    "StopReason",
    "StreamEvent",
    "TextDelta",
    "ToolCallReady",
    "ToolChoice",
    "ToolSpec",
    "TurnCompleted",
    "UsageReported",
    "complete",
]
