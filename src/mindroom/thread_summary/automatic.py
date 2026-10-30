"""The automatic pass after a reply: summarize a due thread, and tag it on its first refresh."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mindroom.authorization import ReplyMembershipPendingError
from mindroom.entity_resolution import current_internal_sender_ids
from mindroom.logging_config import get_logger
from mindroom.matrix.conversation_reads import with_delivered_response
from mindroom.matrix.room_history_reads import fetch_thread_messages_from_source
from mindroom.thread_summary import schedule
from mindroom.thread_summary.generation import GeneratedSummary, generate_summary, resolve_thread_summary_model_name
from mindroom.thread_summary.notices import (
    ThreadSummaryWriteError,
    normalize_thread_summary_text,
    send_thread_summary_notice,
    summary_generated_at,
)
from mindroom.thread_summary.state import (
    SummaryState,
    TruncatedThreadHistoryError,
    human_summary_authorizer,
    load_complete_thread_history,
)
from mindroom.thread_tag_vocabulary import (
    format_tag_vocabulary_with_counts,
    load_tag_vocabulary_snapshot,
    maybe_rebuild_tag_vocabulary,
)
from mindroom.thread_tags import RESOLVED_THREAD_TAG, get_thread_tags, set_thread_tags_if_empty

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence

    import nio

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.conversation_reads import ConversationReader, DeliveredResponse
    from mindroom.matrix.thread_history_result import ThreadHistoryResult

logger = get_logger(__name__)


async def _thread_is_resolved(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
) -> bool:
    """Return whether a thread carries the resolved lifecycle tag.

    Fails open, matching ``_refresh_tag_vocabulary`` and the history load in this
    module: this is a best-effort background read, and the cost of being wrong is
    one summary on a resolved thread. Letting a transport error escape instead
    would abort the pass before it advances the baseline, so a persistently
    failing room-state read would re-run the full state and history reads on
    every turn.
    """
    try:
        tags_state = await get_thread_tags(client, room_id, thread_id)
    except Exception as exc:
        # Keep the type and traceback: this catch is broad, and intermittent
        # Matrix failures are otherwise indistinguishable from each other.
        logger.exception(
            "Thread tag read failed; continuing with automatic summary",
            room_id=room_id,
            thread_id=thread_id,
            error_type=type(exc).__name__,
        )
        return False
    return tags_state is not None and RESOLVED_THREAD_TAG in tags_state.tags


async def _summary_delivery_timestamp(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    *,
    trusted_sender_ids: Collection[str],
    authorize_human: Callable[[str], bool],
    projected_state: SummaryState,
) -> datetime | None:
    """Order a new summary after source history, or suppress it for a new pin.

    The gate before generation cannot cover the generation window itself. The
    per-thread lock is process-local, so another runtime can write a manual pin
    while this process is waiting on the model, and the finished automatic
    summary would then land on top of a title the user just fixed.

    Reads from source rather than through the projection. A projected read is
    strict about staleness but still answers from local state, so it cannot
    observe a pin another runtime just wrote — which is the only case this
    guard exists for. Costs one homeserver read per generated summary, so once
    per interval rather than per turn.

    A failed re-read suppresses delivery because a pin may have landed while
    the model ran. The caller records the completed generation attempt so the
    next eligible pass retries at the normal interval without a retry storm.
    """
    try:
        source_history = await fetch_thread_messages_from_source(
            client,
            room_id,
            thread_id,
            trusted_sender_ids=trusted_sender_ids,
        )
    except Exception:
        logger.exception(
            "Pin re-check before summary delivery failed; discarding automatic summary",
            room_id=room_id,
            thread_id=thread_id,
        )
        return None
    try:
        source_state = SummaryState.from_history(
            source_history,
            thread_id=thread_id,
            trusted_sender_ids=trusted_sender_ids,
            authorize_human=authorize_human,
        )
    except ReplyMembershipPendingError:
        # The model already ran, so this deferred delivery advances the same
        # retry baseline as any other unsuccessful generation attempt.
        return None
    if source_state.pinned:
        return None
    return summary_generated_at(projected_state.newest_generated_at, source_state.newest_generated_at)


async def _apply_initial_tags(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    tags: Sequence[str],
) -> bool:
    """Apply generated tags only when no manual or concurrent tags exist."""
    set_by = client.user_id
    if not tags or not set_by:
        return False
    try:
        result = await set_thread_tags_if_empty(
            client,
            room_id,
            thread_id,
            tags,
            set_by=set_by,
        )
    except Exception:
        logger.exception(
            "Failed to apply automatic thread tags",
            room_id=room_id,
            thread_id=thread_id,
        )
        return False
    if result.skipped_due_to_prior_mutation:
        logger.info(
            "Skipping automatic tags because another tag mutation completed first",
            room_id=room_id,
            thread_id=thread_id,
        )
        return True
    if result.had_existing_tags:
        logger.info(
            "Skipping automatic tags because the thread already has tags",
            room_id=room_id,
            thread_id=thread_id,
        )
        return True
    if result.failed_tags:
        logger.warning(
            "Failed to write some automatic thread tags",
            room_id=room_id,
            thread_id=thread_id,
            tags=result.failed_tags,
        )
    if result.applied_tags:
        logger.info(
            "Automatically tagged thread",
            room_id=room_id,
            thread_id=thread_id,
            tags=result.applied_tags,
        )
    return bool(result.applied_tags)


async def _refresh_tag_vocabulary(
    client: nio.AsyncClient,
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Refresh vocabulary and return text when the check already loaded a snapshot."""
    try:
        snapshot = await maybe_rebuild_tag_vocabulary(
            client,
            room_id,
            config,
            runtime_paths,
            now=datetime.now(UTC),
        )
    except Exception:
        logger.exception("Tag vocabulary rebuild failed", room_id=room_id)
        return None
    if snapshot is None:
        return None
    return format_tag_vocabulary_with_counts(snapshot)


async def _deliver_generated_summary(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    generated: GeneratedSummary,
    normalized_summary: str,
    model_name: str,
    conversation_reader: ConversationReader,
    *,
    trusted_sender_ids: Collection[str],
    authorize_human: Callable[[str], bool],
    projected_state: SummaryState,
) -> None:
    """Apply initial tags, then independently deliver the generated summary.

    Re-reads pin state first. The gate before generation cannot cover the
    generation window, and the per-thread lock is process-local, so another
    runtime can write a manual pin while this process waits on the model. That
    check belongs here rather than in the caller because it is a delivery-time
    question: a superseded summary should apply neither its tags nor its title.
    """
    try:
        generated_at = await _summary_delivery_timestamp(
            client,
            room_id,
            thread_id,
            trusted_sender_ids=trusted_sender_ids,
            authorize_human=authorize_human,
            projected_state=projected_state,
        )
    except ThreadSummaryWriteError:
        logger.exception("Cannot order automatic thread summary", room_id=room_id, thread_id=thread_id)
        return
    if generated_at is None:
        logger.info(
            "Skipping automatic thread summary delivery after pin re-check",
            room_id=room_id,
            thread_id=thread_id,
            message_count=projected_state.message_count,
        )
        return

    initial_enrichment_complete: bool | None = None
    if generated.tags:
        initial_enrichment_complete = await _apply_initial_tags(client, room_id, thread_id, generated.tags)

    try:
        await send_thread_summary_notice(
            client,
            room_id,
            thread_id,
            normalized_summary,
            projected_state.message_count,
            model_name,
            conversation_reader,
            initial_enrichment_complete=initial_enrichment_complete,
            generated_at=generated_at,
        )
    except Exception:
        logger.exception("Thread summary send failed", room_id=room_id, thread_id=thread_id)


async def _countable_thread_history(
    conversation_reader: ConversationReader,
    room_id: str,
    thread_id: str,
) -> ThreadHistoryResult | None:
    """Return history an automatic pass may count, or nothing.

    An automatic pass moves the durable baseline on every outcome it reaches,
    so it must not run at all on a count it cannot trust. Both reasons it
    cannot are handled the same way -- the history is unavailable, or it is a
    page of a longer thread -- because both leave the pass with no number worth
    recording.
    """
    try:
        return await load_complete_thread_history(conversation_reader, room_id, thread_id)
    except TruncatedThreadHistoryError:
        logger.info(
            "Skipping thread summary for a thread longer than one projected page",
            room_id=room_id,
            thread_id=thread_id,
        )
        return None
    except Exception:
        logger.exception(
            "Authoritative thread history load failed",
            room_id=room_id,
            thread_id=thread_id,
        )
        return None


async def maybe_generate_thread_summary(  # noqa: PLR0911
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    conversation_reader: ConversationReader,
    delivered_response: DeliveredResponse,
    entity_name: str,
    membership_index: AgentReplyMembershipIndex,
) -> None:
    """Generate an early summary, then one-shot initial tags on its first refresh."""
    refreshed_tag_vocabulary = await _refresh_tag_vocabulary(client, room_id, config, runtime_paths)
    baselines = schedule.THREAD_SUMMARY_BASELINES
    async with baselines.lock(room_id, thread_id):
        projected = await _countable_thread_history(conversation_reader, room_id, thread_id)
        if projected is None:
            return
        # This pass starts at delivery time, so the projection cannot hold the
        # answer that queued it until sync echoes that event back. Counting and
        # titling the thread without it writes a number one short of the thread
        # into the summary notice, where it becomes the durable baseline every
        # later threshold is measured from, and titles the thread from a
        # question whose answer is missing.
        thread_history = with_delivered_response(
            projected,
            delivered_response,
            thread_id=thread_id,
            sender=client.user_id,
        )
        trusted_sender_ids = current_internal_sender_ids(config, runtime_paths)
        authorize_human = human_summary_authorizer(
            client,
            room_id,
            config,
            runtime_paths,
            entity_name,
            membership_index,
        )
        try:
            state = SummaryState.from_history(
                thread_history,
                thread_id=thread_id,
                trusted_sender_ids=trusted_sender_ids,
                authorize_human=authorize_human,
            )
        except ReplyMembershipPendingError:
            # Unknown authority defers without advancing the baseline, so a
            # pin is never overwritten before membership recovers.
            return
        baselines.record(room_id, thread_id, state.last_summary_count)
        message_count = state.message_count
        if state.pinned:
            logger.debug(
                "Skipping automatic thread summary for a pinned thread",
                room_id=room_id,
                thread_id=thread_id,
                message_count=message_count,
            )
            # Keep the cheap pre-check from loading history on every pinned turn.
            baselines.record(room_id, thread_id, message_count)
            return
        if message_count < baselines.next_threshold(room_id, thread_id, config):
            return
        if await _thread_is_resolved(client, room_id, thread_id):
            logger.debug(
                "Skipping automatic thread summary for a resolved thread",
                room_id=room_id,
                thread_id=thread_id,
                message_count=message_count,
            )
            baselines.record(room_id, thread_id, message_count)
            return

        tag_vocabulary = None
        if state.last_summary_count > 0 and not state.initial_enrichment_complete:
            tag_vocabulary = refreshed_tag_vocabulary or format_tag_vocabulary_with_counts(
                load_tag_vocabulary_snapshot(runtime_paths, room_id),
            )
        try:
            model_name = resolve_thread_summary_model_name(
                config,
                runtime_paths,
                room_id,
                entity_name=entity_name,
            )
            generated = await generate_summary(
                state.ordinary_messages,
                config,
                runtime_paths,
                model_name=model_name,
                tag_vocabulary=tag_vocabulary,
            )
        except Exception:
            logger.exception("Thread summary generation failed", room_id=room_id, thread_id=thread_id)
            # Record current count to prevent retry storms until next threshold
            baselines.record(room_id, thread_id, message_count)
            return

        if generated is None:
            logger.warning("Thread summary generation returned None", room_id=room_id, thread_id=thread_id)
            # Record current count to prevent retry storms until next threshold
            baselines.record(room_id, thread_id, message_count)
            return

        normalized_summary = normalize_thread_summary_text(generated.summary)
        if not normalized_summary:
            logger.warning(
                "Thread summary generation returned no plain-text content",
                room_id=room_id,
                thread_id=thread_id,
            )
            baselines.record(room_id, thread_id, message_count)
            return

        await _deliver_generated_summary(
            client,
            room_id,
            thread_id,
            generated,
            normalized_summary,
            model_name,
            conversation_reader,
            trusted_sender_ids=trusted_sender_ids,
            authorize_human=authorize_human,
            projected_state=state,
        )
        # Record after the delivery attempt so cancellation cannot leave a
        # partially delivered initial enrichment marked complete.
        baselines.record(room_id, thread_id, message_count)
