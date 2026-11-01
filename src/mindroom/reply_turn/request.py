"""The request one reply turn runs, and the helpers every owner of its locked lifecycle shares."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.constants import ATTACHMENT_IDS_KEY
from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.matrix.typing import typing_indicator
from mindroom.response_sources import ResponseSources

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping, Sequence

    import nio

    from mindroom.config.main import Config
    from mindroom.config.participation import ParticipationConfig
    from mindroom.dispatch_source import ScheduledHistoryBudget
    from mindroom.hooks import EnrichmentItem, MessageEnvelope
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.media_inputs import MediaInputs
    from mindroom.participation import ParticipationGate
    from mindroom.response_payload_preparation import ResponsePayloadPreparation
    from mindroom.skill_learning.capture import SkillReviewCapture
    from mindroom.timing import DispatchPipelineTiming
    from mindroom.tool_jobs.conversation import ConversationKey
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.turn_store import SourcePreparation


def merge_response_extra_content(
    extra_content: dict[str, Any] | None,
    attachment_ids: Sequence[str] | None,
) -> dict[str, Any] | None:
    """Merge optional attachment IDs into response metadata."""
    if not attachment_ids:
        return extra_content
    merged = extra_content if extra_content is not None else {}
    merged[ATTACHMENT_IDS_KEY] = list(attachment_ids)
    return merged


def reply_authorization_entity_names(
    config: Config,
    owner_entity_name: str,
    team_member_names: Sequence[str],
) -> tuple[str, ...]:
    """Return the policy entities governing one agent or team execution."""
    if owner_entity_name in config.teams:
        return (owner_entity_name,)
    return tuple(dict.fromkeys((owner_entity_name, *team_member_names)))


@dataclass(frozen=True)
class ResponseRequest:
    """Typed carrier for one response lifecycle request."""

    thread_history: Sequence[ResolvedVisibleMessage]
    prompt: str
    response_envelope: MessageEnvelope
    sources: ResponseSources
    participation: ParticipationConfig | None = None
    member_display_names: Mapping[str, str] = field(default_factory=dict)
    model_prompt: str | None = None
    # The event this response was dispatched to write over: the answer an edit
    # regenerates or an approval resumes, or a selection's acknowledgement.
    # Once claimed, the span's reply names the event instead.
    existing_event_id: str | None = None
    # Set when an edit regenerates the reply at ``existing_event_id``.
    edit_regeneration: bool = False
    # The span an interactive selection's acknowledgement created, which this answer adopts.
    interactive_span_id: str | None = None
    # Set for a follow-up: the conversation whose ended background jobs it reports in a new reply.
    follow_up_key: ConversationKey | None = None
    user_id: str | None = None
    media: MediaInputs | None = None
    attachment_ids: tuple[str, ...] | None = None
    correlation_id: str | None = None
    matrix_run_metadata: Mapping[str, Any] | None = None
    participating_agent_names: tuple[str, ...] = ()
    transient_enrichment_items: tuple[EnrichmentItem, ...] = ()
    system_enrichment_items: tuple[EnrichmentItem, ...] = ()
    requires_model_history_refresh: bool = False
    scheduled_history_budget: ScheduledHistoryBudget | None = None
    history_boundary_event_id: str | None = None
    scheduled_model: str | None = None
    payload_preparation: ResponsePayloadPreparation | None = None
    current_timestamp_ms: float | None = None
    current_prompt_is_structured: bool = False
    # Runs once the response holds its conversation's lock, so its dispatcher knows the turn left the lane.
    on_lifecycle_lock_acquired: Callable[[], None] | None = None
    # Which sources became terminal while the request waited for its lock: deleted, or answered elsewhere.
    prepare_source_turn: Callable[[Sequence[ResolvedVisibleMessage]], Coroutine[Any, Any, SourcePreparation]] | None = (
        None
    )
    # Runs once the turn claimed its reply, before the response is prepared: an edit prunes the history it
    # replaces only then, so a Stop that refuses the claim leaves that history whole.
    on_reply_claimed: Callable[[], Awaitable[None]] | None = None
    pipeline_timing: DispatchPipelineTiming | None = None

    def __post_init__(self) -> None:
        """Require the envelope to name the source driving this response."""
        if not isinstance(self.sources, ResponseSources):
            message = "ResponseRequest requires ResponseSources"
            raise TypeError(message)
        if self.sources.pending_event_ids[0] != self.response_envelope.source_event_id:
            message = "ResponseRequest first pending event must equal the envelope source event"
            raise ValueError(message)

    @property
    def room_id(self) -> str:
        """Return the canonical response room."""
        return self.response_envelope.target.room_id

    @property
    def reply_to_event_id(self) -> str | None:
        """Return the canonical event this response answers."""
        return self.response_envelope.target.reply_to_event_id

    @property
    def thread_id(self) -> str | None:
        """Return the canonical resolved response thread root."""
        return self.response_envelope.target.resolved_thread_id


def is_silent_schedule_response(request: ResponseRequest) -> bool:
    """Return whether one response answers a silent schedule, which shows only its findings."""
    return request.response_envelope.source_kind == SILENT_SCHEDULE_SOURCE_KIND


def correlation_id_for_request(request: ResponseRequest) -> str:
    """Resolve the correlation id for one request."""
    return request.correlation_id or request.reply_to_event_id or request.response_envelope.source_event_id


def response_typing_log_context(
    request: ResponseRequest,
    *,
    response_run_id: str | None,
) -> dict[str, object]:
    """Return the stable response attribution shared by one typing lease."""
    target = request.response_envelope.target
    return {
        **target.log_context,
        "agent_id": request.response_envelope.agent_name,
        "requester_id": request.response_envelope.requester_id,
        "session_id": target.session_id,
        "reply_to_event_id": target.reply_to_event_id,
        "correlation_id": correlation_id_for_request(request),
        "response_run_id": response_run_id,
    }


@asynccontextmanager
async def response_typing_indicator(
    client: nio.AsyncClient,
    request: ResponseRequest,
    *,
    response_run_id: str | None,
    quiet: bool,
    participation: ParticipationGate | None = None,
) -> AsyncIterator[None]:
    """Expose typing only for a response whose progress may be visible, which a ``quiet`` one's never is."""
    if quiet:
        yield
        return

    @asynccontextmanager
    async def show_typing() -> AsyncIterator[None]:
        async with typing_indicator(
            client,
            request.room_id,
            log_context=response_typing_log_context(request, response_run_id=response_run_id),
        ):
            yield

    if participation is None:
        async with show_typing():
            yield
        return
    finished = asyncio.Event()

    async def approved_typing() -> None:
        if await participation.wait_for_approval():
            async with show_typing():
                await finished.wait()

    task = asyncio.create_task(approved_typing())
    try:
        yield
    finally:
        finished.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@dataclass(frozen=True)
class PreparedResponseRuntime:
    """Resolved runtime context shared by streaming and non-streaming responses."""

    media_inputs: MediaInputs
    model_prompt: str
    active_model_name: str
    show_tool_calls: bool
    tool_context: ToolRuntimeContext
    participation: ParticipationGate | None = None
    skill_review_capture: SkillReviewCapture | None = None
