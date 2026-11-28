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
from mindroom.matrix.thread_fork_access import authorize_cross_room_fork_access
from mindroom.matrix.thread_forks import (
    CrossRoomForkDecision,
    CrossRoomForkHop,
    fork_parent_thread_id,
    fork_root_message,
    thread_fork_reference,
)
from mindroom.requester_identity import is_managed_entity_id
from mindroom.thread_summary import schedule
from mindroom.thread_summary.generation import (
    GeneratedSummary,
    generate_summary,
    resolve_thread_summary_model_name,
    room_title_settings,
    title_error,
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
    current_thread_summary_from_history,
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


def _keepable_title(title: str | None, *, retitle: bool) -> str | None:
    """Return the title a check may keep: one within the short-title rule, never for a retitle."""
    base = (title or "").removesuffix(_FORKED_TITLE_SUFFIX)
    return title if base and not retitle and title_error(base) is None else None


def _title_key(title: str) -> str:
    """Return a title without what a reader would not call a new title: case, spacing, emoji style, end, fork marker."""
    key = " ".join(normalize_thread_summary_text(title).replace("\ufe0f", "").casefold().split())
    return key.rstrip(".!?…。\uff01\uff1f ").removesuffix(_FORKED_TITLE_SUFFIX)


def _title_to_deliver(
    generated: GeneratedSummary,
    current: str | None,
    history: Sequence[ResolvedVisibleMessage],
    thread_id: str,
) -> str:
    """Return ``current`` when the model kept it, by its answer or an echo escaped or not, else the new title."""
    echoes = {_title_key(generated.summary), _title_key(unescape(generated.summary))}
    if current is not None and (generated.keep_current or _title_key(current) in echoes):
        return current
    return _publishable_title(generated.summary, history, thread_id)


def _publishable_title(title: str, history: Sequence[ResolvedVisibleMessage], thread_id: str) -> str:
    """Return a new title normalized, marked once as a fork's when it is one."""
    base = normalize_thread_summary_text(title)
    if not base or fork_parent_thread_id(history, thread_id) is None:
        return base
    # The marker is outside the limits, and a fork of a fork stays "X (forked)".
    return f"{base.removesuffix(_FORKED_TITLE_SUFFIX)}{_FORKED_TITLE_SUFFIX}"


async def _read_authorized_parent_summary(
    history: Sequence[ResolvedVisibleMessage],
    *,
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    conversation_reader: ConversationReader,
    entity_name: str,
    membership_index: AgentReplyMembershipIndex,
    trusted_sender_ids: Collection[str],
    requester_id: str | None,
) -> SummaryState | None:
    """Read the immediate fork parent's summary, enforcing cross-room fork authorization."""
    root = fork_root_message(history, thread_id, room_id=room_id)
    reference = None if root is None else thread_fork_reference(root.content)
    if root is None or reference is None:
        return None
    parent_room_id = reference.resolved_room_id(room_id)
    allow_segment = None
    if parent_room_id != room_id:
        auth = await authorize_cross_room_fork_access(
            client=client,
            config=config,
            runtime_paths=runtime_paths,
            hop=CrossRoomForkHop(
                target_room_id=room_id,
                child_room_id=room_id,
                parent_room_id=parent_room_id,
                fork_root=root,
                fork_point_event_id=reference.event_id,
            ),
            requester_id=requester_id,
            nonblocking=False,
        )
        if auth.decision is not CrossRoomForkDecision.ALLOWED:
            return None
        allow_segment = auth.allow_segment
    if allow_segment is not None:
        parent_history = await _countable_thread_history(conversation_reader, parent_room_id, reference.thread_id)
        if parent_history is None or await allow_segment(parent_history) is not CrossRoomForkDecision.ALLOWED:
            return None
        return current_thread_summary_from_history(
            client,
            parent_room_id,
            reference.thread_id,
            parent_history,
            config=config,
            runtime_paths=runtime_paths,
            entity_name=entity_name,
            membership_index=membership_index,
            trusted_sender_ids=trusted_sender_ids,
        )
    return await current_thread_summary(
        client,
        parent_room_id,
        reference.thread_id,
        config=config,
        runtime_paths=runtime_paths,
        conversation_reader=conversation_reader,
        entity_name=entity_name,
        membership_index=membership_index,
        trusted_sender_ids=trusted_sender_ids,
    )


async def _provisional_title(
    prompt_message: ResolvedVisibleMessage | None,
    history: Sequence[ResolvedVisibleMessage],
    thread_id: str,
    *,
    parent_summary: Callable[[], Awaitable[SummaryState | None]],
    config: Config,
    runtime_paths: RuntimePaths,
    model_name: str,
    room_settings: Callable[[], Awaitable[tuple[str | None, str | None]]],
) -> tuple[str, str] | None:
    """Return a new thread's first title and its model: a fork's parent's short title, else its prompt's, if any.

    A parent's title is copied without a model or a read of ``room_settings``, the room's title model and rules; a
    prompt's is written by the room's model, else ``model_name``.
    """
    parent_state = await parent_summary()
    # A parent title over the short-title limits, such as a long pin, is not copied: the fork titles its prompt.
    title = None if parent_state is None else _keepable_title(parent_state.summary, retitle=False)
    if title is None:
        if prompt_message is None:
            return None
        room_model, room_instructions = await room_settings()
        model_name = room_model or model_name
        generated = await generate_summary(
            [prompt_message],
            config,
            runtime_paths,
            model_name=model_name,
            tag_vocabulary=None,
            room_instructions=room_instructions,
        )
        if generated is None:
            return None
        title = generated.summary
    title = _publishable_title(title, history, thread_id)
    return (title, model_name) if title else None


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
) -> None:
    """Best-effort apply generated tags only when no manual or concurrent tags exist."""
    set_by = client.user_id
    if not set_by:
        return
    try:
        result = await set_thread_tags_if_empty(
            client,
            room_id,
            thread_id,
            tags,
            set_by=set_by,
        )
    except ThreadTagsPermissionError as exc:
        # A room setting, not a fault: warn once per room; automatic tags are best-effort on Pass 2.
        if room_id not in _rooms_refusing_automatic_tags:
            _rooms_refusing_automatic_tags.add(room_id)
            logger.warning("Automatic thread tags need a higher power level", room_id=room_id, reason=str(exc))
        return
    except Exception:
        logger.exception(
            "Failed to apply automatic thread tags",
            room_id=room_id,
            thread_id=thread_id,
        )
        return
    if result.skipped_due_to_prior_mutation:
        logger.info(
            "Skipping automatic tags because another tag mutation completed first",
            room_id=room_id,
            thread_id=thread_id,
        )
        return
    if result.had_existing_tags:
        logger.info(
            "Skipping automatic tags because the thread already has tags",
            room_id=room_id,
            thread_id=thread_id,
        )
        return
    if result.applied_tags:
        logger.info(
            "Automatically tagged thread",
            room_id=room_id,
            thread_id=thread_id,
            tags=result.applied_tags,
        )


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
    is_enrichment_pass: bool,
) -> bool:
    """Apply initial tags, then independently deliver the generated summary.

    Re-reads pin state first (``_summary_delivery_timestamp``): a superseded
    summary should apply neither its tags nor its title. Returns whether this
    delivery durably recorded ``initial_enrichment_complete=True``.
    """
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
        return False
    if generated_at is None:
        logger.info(
            "Skipping automatic thread summary delivery after pin re-check",
            room_id=room_id,
            thread_id=thread_id,
            message_count=projected_state.message_count,
        )
        return False

    if is_enrichment_pass and generated.tags:
        await _apply_initial_tags(client, room_id, thread_id, generated.tags)
    # Pass 2 always finalizes title stability regardless of best-effort tag-write outcome.
    initial_enrichment_complete: bool | None = True if is_enrichment_pass else None

    try:
        event_id = await send_thread_summary_notice(
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
        return False
    return event_id is not None and is_enrichment_pass


async def _countable_thread_history(
    conversation_reader: ConversationReader,
    room_id: str,
    thread_id: str,
) -> ThreadHistoryResult | None:
    """Return history an automatic pass may count, or nothing.

    An automatic pass records the counted size as its baseline on every outcome
    except an untitled thread's failed generation pass (which sets a short retry
    cooldown instead), so it must not run at all on a count it cannot trust. Both reasons it
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
    # A voice transcript echo and an old routing relay, and a responder's reply
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
    prompt_as_shown: bool = False,
    conversation_reader: ConversationReader,
    entity_name: str,
    membership_index: AgentReplyMembershipIndex,
    requester_id: str | None = None,
) -> None:
    """Title a new thread from its first turn's messages without advancing the post-reply schedule.

    ``prompt`` is None for a batch of messages starting a fork: only the parent's title can title it then.
    It is None too with ``prompt_as_shown``, for a routed prompt whose turn holds no text: the thread's own shows it.
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
                sole=prompt is not None or prompt_as_shown,
            )
            if (prompt_message := await untitled_prompt(history)) is None:
                return
            # A routed turn holds no text: it is titled from each of its messages as the thread shows it.
            shown = "\n".join(message.body for message in history if message.event_id in prompt_event_ids)
            body = shown if prompt_as_shown else prompt
            titled = await _provisional_title(
                replace(prompt_message, body=body) if body is not None else None,
                history,
                thread_id,
                parent_summary=partial(
                    _read_authorized_parent_summary,
                    history,
                    client=client,
                    room_id=room_id,
                    thread_id=thread_id,
                    config=config,
                    runtime_paths=runtime_paths,
                    conversation_reader=conversation_reader,
                    entity_name=entity_name,
                    membership_index=membership_index,
                    trusted_sender_ids=trusted_sender_ids,
                    requester_id=requester_id,
                ),
                config=config,
                runtime_paths=runtime_paths,
                model_name=resolve_thread_summary_model_name(config, runtime_paths, room_id, entity_name=entity_name),
                room_settings=partial(room_title_settings, client, config, runtime_paths, room_id),
            )
            if titled is None:
                return
            title, model_name = titled
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


def _record_generation_failure(room_id: str, thread_id: str, state: SummaryState) -> None:
    """Cool down an untitled thread's failed generation pass, or advance an already-titled thread's baseline."""
    baselines = schedule.THREAD_SUMMARY_BASELINES
    if state.summary is None:
        baselines.record_failure_cooldown(room_id, thread_id)
    else:
        baselines.record(room_id, thread_id, state.message_count)


async def maybe_generate_thread_summary(  # noqa: C901, PLR0911
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
    """Generate an early summary, then one final title check with one-shot initial tags, then stop.

    ``retitle`` summarizes now, whatever the thread's count, for a message that changes what the thread is about,
    such as a call's notes. A pinned or resolved thread keeps its title all the same.
    """
    refreshed_tag_vocabulary = await _refresh_tag_vocabulary(client, room_id, config, runtime_paths)
    baselines = schedule.THREAD_SUMMARY_BASELINES
    async with baselines.lock(room_id, thread_id):
        if not retitle and (
            baselines.is_finalized(room_id, thread_id) or baselines.in_failure_cooldown(room_id, thread_id)
        ):
            return
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
        if not retitle and state.initial_enrichment_complete:
            baselines.mark_finalized(room_id, thread_id)
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

        is_enrichment_pass = not retitle and state.last_summary_count > 0 and not state.initial_enrichment_complete
        tag_vocabulary = None
        if is_enrichment_pass:
            tag_vocabulary = refreshed_tag_vocabulary or format_tag_vocabulary_with_counts(
                load_tag_vocabulary_snapshot(runtime_paths, room_id),
            )
        # Pass 1 (last_summary_count == 0) writes fresh from prompt + full reply without keeping a provisional title.
        # Pass 2 offers the current title to keep unless it exceeds the short-title rule or this is a retitle.
        current_title = _keepable_title(state.summary, retitle=retitle) if state.last_summary_count > 0 else None
        try:
            room_model, room_instructions = await room_title_settings(client, config, runtime_paths, room_id)
            model_name = room_model or resolve_thread_summary_model_name(
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
                room_instructions=room_instructions,
                current_title=current_title,
                allow_fallback=state.summary is None,
            )
        except Exception:
            logger.exception("Thread summary generation failed", room_id=room_id, thread_id=thread_id)
            _record_generation_failure(room_id, thread_id, state)
            return

        if generated is None:
            logger.warning("Thread summary generation returned None", room_id=room_id, thread_id=thread_id)
            _record_generation_failure(room_id, thread_id, state)
            return

        # Both Pass 1 (advancing count from 0) and Pass 2 (recording initial_enrichment_complete) always post.
        normalized_summary = _title_to_deliver(generated, current_title, thread_history, thread_id)
        if not normalized_summary:
            logger.warning(
                "Thread summary generation returned no plain-text content",
                room_id=room_id,
                thread_id=thread_id,
            )
            _record_generation_failure(room_id, thread_id, state)
            return

        finalized = await _deliver_generated_summary(
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
            is_enrichment_pass=is_enrichment_pass,
        )
        if finalized:
            baselines.mark_finalized(room_id, thread_id)
        # Record after the delivery attempt so cancellation cannot leave a
        # partially delivered initial enrichment marked complete.
        baselines.record(room_id, thread_id, message_count)
