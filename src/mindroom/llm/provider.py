"""The protocol every model provider adapter implements, and the request and stream values it exchanges.

Adapters are stream-only; ``complete`` drains a stream for callers that want one finished turn.
Per-request state travels in ``ModelRequest`` rather than on the adapter, so adapters stay immutable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from mindroom.llm.errors import incomplete_stream_error
from mindroom.llm.usage import add_usage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

    from pydantic import BaseModel

    from mindroom.llm.messages import Message
    from mindroom.llm.usage import MessageMetrics

type MediaKind = Literal["audio", "image", "file", "video"]
type StopReason = Literal["end", "tool_use", "output_limit", "refusal", "content_filter", "incomplete"]
type ToolChoice = Literal["none"]


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
    """What a provider route supports, so policy code stops probing adapter classes.

    ``streams_only`` marks a route whose endpoint only streams, so a non-streaming request is streamed too and gets
    the stream retry and idle limit.
    """

    structured_output: Literal["native", "json_schema", "json_mode"] = "json_mode"
    omitted_media: frozenset[MediaKind] = frozenset()
    native_tool_search: bool = False
    native_compaction: bool = False
    approval_receipt_after_response_id: bool = False
    provider_default_sampling: bool = False
    claude_family: bool = False
    streams_only: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolSpec:
    """A model-facing tool definition; ``parameters`` is the final JSON schema.

    ``description`` and the flags keep Agno's tri-state: None means the tool never set the value, so wires that send
    the field inside each function definition omit it, as Agno's ``Function.to_dict()`` did.
    The approval flags are not policy here: OpenAI-family wires send them inside each function definition exactly as
    Agno did, so request bodies and prompt caches stay unchanged.
    """

    name: str
    description: str | None
    parameters: Mapping[str, Any]
    strict: bool | None = None
    requires_confirmation: bool | None = None
    external_execution: bool | None = None
    approval_type: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RequestOverrides:
    """Per-request knobs that override the adapter's configured defaults; None leaves a default untouched."""

    temperature: float | None = None
    sdk_max_retries: int | None = None
    prompt_cache: bool | None = None
    disable_thinking: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeCompactionReplay:
    """The provider-managed compaction route one request replays.

    ``route`` identifies the model, endpoint, and history generation whose stored checkpoints may be
    replayed; ``threshold`` is the input-token trigger MindRoom asks for, or None to keep the authored
    provider policy while still replaying that route's checkpoints.
    """

    route: str
    threshold: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelRequest:
    """One provider request.

    ``stream=False`` asks for one non-streaming provider call where the route offers one; its usage and finished
    turn still arrive through ``stream()`` after the response completes, without deltas, so callers read its output
    from the finished turn. A route that only streams streams the request, deltas included.
    ``deferred_tool_names`` name tools kept out of the rendered prompt: routes with native tool search defer
    them server-side, and the portable tool-search wrapper hides them behind ``call_tool`` everywhere else.
    ``portable_replay`` replays history explicitly instead of continuing provider-stored state, and
    ``native_compaction`` turns on provider-side compaction for routes that support it.
    """

    messages: Sequence[Message]
    tools: Sequence[ToolSpec] = ()
    tool_choice: ToolChoice | None = None
    response_format: type[BaseModel] | Mapping[str, Any] | None = None
    overrides: RequestOverrides = RequestOverrides()
    stream: bool = True
    deferred_tool_names: frozenset[str] = frozenset()
    portable_replay: bool = False
    native_compaction: NativeCompactionReplay | None = None


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


@dataclass(frozen=True, slots=True)
class StreamProgress:
    """Provider stream data that carried no other event, such as tool-call arguments or hosted-tool progress.

    It shows that the provider is still sending: idle limits count it as activity, and output consumers skip it.
    ``commits`` marks the start of a hosted tool whose rerun would repeat work or cost (``hosted_tool_started``):
    once an attempt yields it, a retry could run the tool twice, so any later failure of the attempt is final.
    """

    commits: bool = False


# Hosted tools whose rerun repeats no work: a tool search only looks up deferred tool definitions, and a compaction
# only summarizes the request's own history. Every route names them with these prefixes, such as Claude's
# ``tool_search_tool_regex`` and ``compaction`` blocks and Responses' ``tool_search_call`` and ``compaction`` items.
_READ_ONLY_HOSTED_TOOL_PREFIXES = ("tool_search", "compaction")


def hosted_tool_started(name: str) -> StreamProgress:
    """Return the progress event of a hosted tool that started, named as its route names the tool or its output.

    One rule decides for every route: a tool that only reads keeps the attempt retryable, and any other, such as web
    search, code execution, file search, image generation, or Gemini's search grounding, commits it.
    """
    return StreamProgress(commits=not name.startswith(_READ_ONLY_HOSTED_TOOL_PREFIXES))


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnCompleted:
    """The last stream event: the finished assistant message with its metrics and provider data."""

    message: Message
    stop_reason: StopReason | None = None


type StreamEvent = TextDelta | ReasoningDelta | ToolCallReady | UsageReported | StreamProgress | TurnCompleted


@dataclass(slots=True)
class ReportedUsage:
    """The usage one request's stream reported so far: its final reports summed, and a partial report none replaced yet.

    A partial report stands in for its attempt until a final one replaces it, so ``received`` is what the provider
    billed for a stream cut off now, and a retrying stage settles a failed attempt's partial report before retrying.
    """

    settled: MessageMetrics | None = None
    pending: MessageMetrics | None = None

    def observe(self, event: StreamEvent) -> None:
        """Track one event of the stream."""
        if not isinstance(event, UsageReported):
            return
        if event.final:
            self.settled = add_usage(self.settled, event.usage)
            self.pending = None
        else:
            self.pending = event.usage

    @property
    def received(self) -> MessageMetrics | None:
        """The settled reports plus the partial report none replaced, or None when nothing was reported."""
        return add_usage(self.settled, self.pending)

    def settle(self) -> UsageReported | None:
        """Count the pending partial report as settled, returning it as a final report to forward before a retry.

        The retry's own reports would otherwise replace it, and the consumer would lose usage the provider billed.
        """
        if self.pending is None:
            return None
        report = UsageReported(self.pending)
        self.settled = add_usage(self.settled, self.pending)
        self.pending = None
        return report


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


class NativeHistoryRoute(Protocol):
    """A route whose provider manages history of its own, by continuing state it stored or by compacting it."""

    def native_compaction_settings(
        self,
        *,
        threshold: int | None,
        history_generation: str,
        allow_authored: bool,
    ) -> NativeCompactionReplay | None:
        """Return the replay settings of a request that wants compaction at ``threshold`` input tokens.

        None runs the request without native compaction. ``history_generation`` binds the replayed checkpoints to the
        history the requests carry, and ``allow_authored`` lets a route without MindRoom-managed compaction keep the
        compaction policy its model config authored.
        """
        ...


class PortableReplayEstimator(Protocol):
    """A route that sizes explicitly replayed history the way its requests send it."""

    def portable_replay_uses_visual_tokens(self) -> bool:
        """Return whether the estimate prices images by their visual cost instead of their transport bytes."""
        ...

    def estimate_portable_replay_tokens(self, messages: list[Message]) -> int | None:
        """Return the route's estimate of ``messages`` as one explicit replay, or None to size them canonically."""
        ...


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

    @property
    def native_history_route(self) -> NativeHistoryRoute | None:
        """The provider's native-history route, or None when its requests carry no native history settings.

        Having one does not imply ``capabilities.native_compaction``: a route whose provider keeps history without
        compacting it still plans its replay through this route.
        """
        ...

    @property
    def replay_estimator(self) -> PortableReplayEstimator | None:
        """How the route sizes explicitly replayed history, or None to size it canonically."""
        ...

    @property
    def model_params(self) -> dict[str, Any]:
        """The route's JSON-safe, non-secret settings, which run metadata and the request log record."""
        ...

    def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Send one request and yield its events; ``TurnCompleted`` is the last one.

        A stream cut off before the provider finished the turn ends without it.
        """
        ...


async def complete(provider: ModelProvider, request: ModelRequest) -> ModelTurn:
    """Drain one request's stream and return its finished turn.

    The request pipeline already turns a stream that ends without its turn into ``incomplete_stream_error``; a bare
    adapter's stream raises it here.
    """
    completed: TurnCompleted | None = None
    async for event in provider.stream(request):
        if isinstance(event, TurnCompleted):
            completed = event
    if completed is None:
        raise incomplete_stream_error(provider=provider.identity.provider_name)
    return ModelTurn(message=completed.message, stop_reason=completed.stop_reason)


__all__ = [
    "Capabilities",
    "MediaKind",
    "ModelIdentity",
    "ModelProvider",
    "ModelRequest",
    "ModelTurn",
    "NativeCompactionReplay",
    "NativeHistoryRoute",
    "PortableReplayEstimator",
    "ReasoningDelta",
    "ReportedUsage",
    "RequestOverrides",
    "StopReason",
    "StreamEvent",
    "StreamProgress",
    "TextDelta",
    "ToolCallReady",
    "ToolChoice",
    "ToolSpec",
    "TurnCompleted",
    "UsageReported",
    "complete",
    "hosted_tool_started",
]
