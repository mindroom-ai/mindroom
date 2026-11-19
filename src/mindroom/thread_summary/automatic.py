"""Automatic prompt titles and post-reply summaries, with tags on the first refresh."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from html import unescape
from typing import TYPE_CHECKING

from mindroom.entity_resolution import current_internal_sender_ids
from mindroom.logging_config import get_logger
from mindroom.matrix.conversation_reads import refetched_thread_history, with_delivered_response
from mindroom.matrix.thread_forks import fork_parent_thread_id
from mindroom.requester_identity import is_managed_entity_id
from mindroom.thread_summary import schedule
from mindroom.thread_summary.generation import (
    GeneratedSummary,
    clamp_generated_title,
    generate_summary,
    resolve_thread_summary_model_name,
    room_summary_instructions,
)
from mindroom.thread_summary.notices import (
    ThreadSummaryWriteError,
    normalize_thread_summary_text,
    send_thread_summary_notice,
    summary_generated_at,
)
from mindroom.thread_summary.state import (
    SummaryState,
    TruncatedThreadHistoryError,
    current_thread_summary,
    human_summary_authorizer,
    load_complete_thread_history,
    summary_state,
)
from mindroom.thread_tag_vocabulary import (
    format_tag_vocabulary_with_counts,
    load_tag_vocabulary_snapshot,
    maybe_rebuild_tag_vocabulary,
)
from mindroom.thread_tags import (
    RESOLVED_THREAD_TAG,
    ThreadTagsPermissionError,
    get_thread_tag_content,
    parse_thread_tag_record,
    set_thread_tags_if_empty,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Sequence

    import nio

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.conversation_reads import ConversationReader, DeliveredResponse
    from mindroom.matrix.thread_history_result import ThreadHistoryResult

logger = get_logger(__name__)

# Rooms already warned that their power levels refuse the agent's automatic tags.
_rooms_refusing_automatic_tags: set[str] = set()
# Ends every automatic title of a forked thread.
_FORKED_TITLE_SUFFIX = " (forked)"


def _keepable_title(title: str) -> bool:
    """Return whether a title already obeys the short-title rule, so a refresh may keep it instead of rewriting it."""
    base = title.removesuffix(_FORKED_TITLE_SUFFIX)
    return clamp_generated_title(base) == base


def _title_key(title: str) -> str:
    """Return a title without the differences a reader would not call a new title: case, spacing, emoji style, end."""
    return " ".join(title.replace("\ufe0f", "").casefold().split()).rstrip(".!?…。\uff01\uff1f ")


def _repeats_title(
    generated: GeneratedSummary,
    current: str,
    history: Sequence[ResolvedVisibleMessage],
    thread_id: str,
) -> bool:
    """Return whether the model kept the current title, by its answer or by an echo that differs only cosmetically."""
    # The model read the title HTML-escaped, so an echo may come back either way.
    echoes = {
        _title_key(_publishable_title(text, history, thread_id))
        for text in (generated.summary, unescape(generated.summary))
    }
    return generated.keep_current or _title_key(current) in echoes


def _record_kept_title(
    runtime_paths: RuntimePaths,
    room_id: str,
    thread_id: str,
    message_count: int,
    initial_enrichment_complete: bool,
) -> None:
    """Keep the current title: post nothing, but write down the pass as its notice would have."""
    logger.debug("Keeping the current thread title", room_id=room_id, thread_id=thread_id)
    try:
        schedule.record_kept_title(
            runtime_paths,
            room_id,
            thread_id,
            message_count,
            initial_enrichment_complete=initial_enrichment_complete,
        )
    except Exception:
        logger.exception("Kept thread title record failed", room_id=room_id, thread_id=thread_id)


def _publishable_title(title: str, history: Sequence[ResolvedVisibleMessage], thread_id: str) -> str:
    """Return a new title normalized and cut to the short-title limits, marked once as a fork's when it is one."""
    base = normalize_thread_summary_text(title)
    if fork_parent_thread_id(history, thread_id) is None:
        return clamp_generated_title(base)
    # The marker is outside the limits, and a fork of a fork stays "X (forked)".
    return f"{clamp_generated_title(base.removesuffix(_FORKED_TITLE_SUFFIX))}{_FORKED_TITLE_SUFFIX}"


async def _provisional_title(
    prompt_message: ResolvedVisibleMessage | None,
    history: Sequence[ResolvedVisibleMessage],
    thread_id: str,
    *,
    parent_summary: Callable[[str], Awaitable[SummaryState | None]],
    config: Config,
    runtime_paths: RuntimePaths,
    model_name: str,
    room_instructions: Callable[[], Awaitable[str | None]],
) -> str | None:
    """Return a new thread's first title: a fork's parent's current title without a model, else its prompt's, if any."""
    parent_thread_id = fork_parent_thread_id(history, thread_id)
    parent_state = None if parent_thread_id is None else await parent_summary(parent_thread_id)
    title = None if parent_state is None else parent_state.summary
    if title is None:
        if prompt_message is None:
            return None
        generated = await generate_summary(
            [prompt_message],
            config,
            runtime_paths,
            model_name=model_name,
            tag_vocabulary=None,
            room_instructions=await room_instructions(),
        )
        if generated is None:
            return None
        title = generated.summary
    return _publishable_title(title, history, thread_id) or None


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
    failing tag read would re-run the tag and history reads on every turn.
    """
    try:
        # One state event, not the room's whole state, which a busy room makes large.
        content = await get_thread_tag_content(client, room_id, thread_id, RESOLVED_THREAD_TAG)
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
    return parse_thread_tag_record(RESOLVED_THREAD_TAG, content) is not None


async def _summary_delivery_timestamp(
    conversation_reader: ConversationReader,
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

    Walks the thread on the server again before reading the projection. A
    projected read alone answers from what sync has delivered, so it cannot
    observe a pin another runtime just wrote — which is the only case this
    guard exists for. Costs one thread walk per generated summary, so once per
    interval rather than per turn.

    A failed re-read, or one a bound stopped before the end of the thread,
    suppresses delivery because a pin may have landed while the model ran. The
    caller records the completed generation attempt so the next eligible pass
    retries at the normal interval without a retry storm. An event this bot
    cannot decrypt is read as absent, as the gate before generation reads it:
    counting it as missing would discard the summary after the model ran, and
    again on every later pass, because that gate keeps letting the thread through.
    """
    try:
        source_history = await refetched_thread_history(
            conversation_reader,
            room_id,
            thread_id,
            # A pin can be an edit of any earlier summary, and a recursive walk
            # may page a reply's edits out, so each summary's edits are asked for.
            edits_of=projected_state.summary_event_ids,
            unreadable_is_missing=False,
        )
    except Exception:
        logger.exception(
            "Pin re-check before summary delivery failed; discarding automatic summary",
            room_id=room_id,
            thread_id=thread_id,
        )
        return None
    if not source_history.is_full_history:
        # A pin can be an edit of any earlier summary, so a walk that stopped
        # before the end of the thread cannot rule one out.
        logger.info(
            "Pin re-check before summary delivery could not read the whole thread; discarding automatic summary",
            room_id=room_id,
            thread_id=thread_id,
        )
        return None
    source_state = summary_state(
        source_history,
        thread_id=thread_id,
        trusted_sender_ids=trusted_sender_ids,
        authorize_human=authorize_human,
    )
    if source_state is None:
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
    if not set_by:
        return False
    try:
        result = await set_thread_tags_if_empty(
            client,
            room_id,
            thread_id,
            tags,
            set_by=set_by,
        )
    except ThreadTagsPermissionError as exc:
        # A room setting, not a fault: warn once per room, and a later summary tags once the power levels allow it.
        if room_id not in _rooms_refusing_automatic_tags:
            _rooms_refusing_automatic_tags.add(room_id)
            logger.warning("Automatic thread tags need a higher power level", room_id=room_id, reason=str(exc))
        return False
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
    kept: Callable[[bool], None] | None = None,
) -> None:
    """Apply initial tags, then independently deliver the generated summary.

    Re-reads pin state first (``_summary_delivery_timestamp``): a superseded
    summary should apply neither its tags nor its title. ``kept`` keeps the
    current title instead: it records the pass, and whether the tags are done, in place of a notice.
    """
    if kept is not None and not generated.tags:
        kept(False)
        return
    try:
        generated_at = await _summary_delivery_timestamp(
            conversation_reader,
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
    if kept is not None:
        kept(initial_enrichment_complete is True)
        return

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

    An automatic pass records the counted size as its baseline on every outcome
    it reaches, so it must not run at all on a count it cannot trust. Both reasons it
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


async def _untitled_prompt(
    history: ThreadHistoryResult,
    *,
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    prompt_event_ids: Collection[str],
    config: Config,
    runtime_paths: RuntimePaths,
    trusted_sender_ids: Collection[str],
    authorize_human: Callable[[str], bool],
    sole: bool,
) -> ResolvedVisibleMessage | None:
    """Return the turn's first message in an untitled, unresolved thread; when ``sole``, only while it is the only turn.

    Attachments and the text sent with them are one turn, so ``sole`` means the
    thread's human messages are exactly ``prompt_event_ids``, all unedited.
    """
    state = summary_state(
        history,
        thread_id=thread_id,
        trusted_sender_ids=trusted_sender_ids,
        authorize_human=authorize_human,
    )
    if state is None or state.summary_event_ids:
        return None
    # The router's voice echo and routing relay, and a responder's reply
    # placeholder, can land before a read; none is a second prompt.
    ordinary = [
        message
        for message in state.ordinary_messages
        if message.event_id in prompt_event_ids or not is_managed_entity_id(message.sender, config, runtime_paths)
    ]
    prompt_message = next((message for message in ordinary if message.event_id in prompt_event_ids), None)
    if prompt_message is None:
        return None
    # An edit keeps a message's event ID but changes its text, so the prompt
    # this title would be made from is no longer what the thread shows.
    if sole and (
        {message.event_id for message in ordinary} != set(prompt_event_ids)
        or any(message.latest_event_id != message.event_id for message in ordinary)
    ):
        return None
    # Resolution lives in room state, which a history read cannot see.
    if await _thread_is_resolved(client, room_id, thread_id):
        return None
    return prompt_message


async def maybe_generate_prompt_summary(  # noqa: PLR0911
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    prompt_event_ids: Collection[str],
    prompt: str | None,
    conversation_reader: ConversationReader,
    entity_name: str,
    membership_index: AgentReplyMembershipIndex,
) -> None:
    """Title a new thread from its first turn's messages without advancing the post-reply schedule.

    ``prompt`` is None for a batch of messages starting a fork: only the parent's title can title it then.
    """
    baselines = schedule.THREAD_SUMMARY_BASELINES
    if (prompt is not None and not prompt.strip()) or not baselines.claim_provisional_title(room_id, thread_id):
        return
    try:
        async with baselines.lock(room_id, thread_id):
            # A baseline means this process already summarized the thread, maybe
            # before that notice reached any history read. A prompt title only
            # covers the wait for the first reply, so a delivered reply makes it stale.
            if baselines.count(room_id, thread_id) > 0 or baselines.provisional_title_superseded(room_id, thread_id):
                return
            # The claim stops this process's other bots. Another runtime's title,
            # or a pin from Chat, may not have reached this client's sync yet, so
            # read the server's history before claiming absence.
            history = await refetched_thread_history(
                conversation_reader,
                room_id,
                thread_id,
                edits_of=prompt_event_ids,
                unreadable_is_missing=True,
            )
            if not history.is_full_history:
                return
            trusted_sender_ids = current_internal_sender_ids(config, runtime_paths)
            authorize_human = human_summary_authorizer(
                client,
                room_id,
                config,
                runtime_paths,
                entity_name,
                membership_index,
            )
            untitled_prompt = partial(
                _untitled_prompt,
                client=client,
                room_id=room_id,
                thread_id=thread_id,
                prompt_event_ids=prompt_event_ids,
                config=config,
                runtime_paths=runtime_paths,
                trusted_sender_ids=trusted_sender_ids,
                authorize_human=authorize_human,
                sole=prompt is not None,
            )
            if (prompt_message := await untitled_prompt(history)) is None:
                return
            model_name = resolve_thread_summary_model_name(config, runtime_paths, room_id, entity_name=entity_name)
            title = await _provisional_title(
                None if prompt is None else replace(prompt_message, body=prompt),
                history,
                thread_id,
                parent_summary=partial(
                    current_thread_summary,
                    client,
                    room_id,
                    config=config,
                    runtime_paths=runtime_paths,
                    conversation_reader=conversation_reader,
                    entity_name=entity_name,
                    membership_index=membership_index,
                    trusted_sender_ids=trusted_sender_ids,
                ),
                config=config,
                runtime_paths=runtime_paths,
                model_name=model_name,
                room_instructions=partial(room_summary_instructions, client, room_id),
            )
            if title is None:
                return
            # A different runtime can title or pin the thread while the model
            # runs. Keep that title even though this runtime holds its lock.
            history = await refetched_thread_history(
                conversation_reader,
                room_id,
                thread_id,
                edits_of=prompt_event_ids,
                unreadable_is_missing=True,
            )
            if not history.is_full_history:
                return
            # Everything that made the thread eligible can change while the model
            # runs: another title or pin, a resolution, or a deleted or edited prompt.
            if await untitled_prompt(history) is None:
                return
            # The reply can be delivered while the model runs; its post-reply pass titles the thread instead.
            if baselines.provisional_title_superseded(room_id, thread_id):
                return
            await send_thread_summary_notice(
                client,
                room_id,
                thread_id,
                title,
                0,
                model_name,
                conversation_reader,
            )
    except Exception:
        logger.exception("Prompt thread summary failed", room_id=room_id, thread_id=thread_id)


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
    retitle: bool = False,
) -> None:
    """Generate an early summary, then one-shot initial tags on its first refresh.

    ``retitle`` summarizes now, whatever the thread's count, for a message that changes what the thread is about,
    such as a call's notes. A pinned or resolved thread keeps its title all the same.
    """
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
        state = summary_state(
            thread_history,
            thread_id=thread_id,
            trusted_sender_ids=trusted_sender_ids,
            authorize_human=authorize_human,
        )
        if state is None:
            # Unknown authority defers without advancing the baseline, so a
            # pin is never overwritten before membership recovers.
            return
        # A refresh that kept its title wrote these down in place of a notice.
        kept_count, kept_enriched = schedule.kept_title_record(runtime_paths, room_id, thread_id)
        baselines.record(room_id, thread_id, max(state.last_summary_count, kept_count))
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
        if not retitle and message_count < baselines.next_threshold(room_id, thread_id, config):
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
        if max(state.last_summary_count, kept_count) > 0 and not (state.initial_enrichment_complete or kept_enriched):
            tag_vocabulary = refreshed_tag_vocabulary or format_tag_vocabulary_with_counts(
                load_tag_vocabulary_snapshot(runtime_paths, room_id),
            )
        # A long legacy title is rewritten once; a retitle asks for a new title outright.
        current_title = state.summary if state.summary and not retitle and _keepable_title(state.summary) else None
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
                room_instructions=await room_summary_instructions(client, room_id),
                current_title=current_title,
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

        keep = current_title is not None and _repeats_title(generated, current_title, thread_history, thread_id)
        if not keep and not normalize_thread_summary_text(generated.summary):
            logger.warning(
                "Thread summary generation returned no plain-text content",
                room_id=room_id,
                thread_id=thread_id,
            )
            baselines.record(room_id, thread_id, message_count)
            return
        normalized_summary = _publishable_title(generated.summary, thread_history, thread_id)

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
            kept=partial(_record_kept_title, runtime_paths, room_id, thread_id, message_count) if keep else None,
        )
        # Record after the delivery attempt so cancellation cannot leave a
        # partially delivered initial enrichment marked complete.
        baselines.record(room_id, thread_id, message_count)
