"""Cleanup stale streaming messages left behind by restarts."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import nio
from nio.api import Api, RelationshipType

from mindroom.constants import (
    ORIGINAL_SENDER_KEY,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.client import (
    build_threaded_edit_content,
    edit_message,
    get_joined_rooms,
    resolve_latest_visible_messages,
)
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.identity import MatrixID
from mindroom.streaming import (
    _RESTART_INTERRUPTED_RESPONSE_NOTE,
    build_restart_interrupted_body,
    is_in_progress_message,
)
from mindroom.tool_system.events import _TOOL_TRACE_KEY

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_ROOM_HISTORY_PAGE_SIZE = 100
_MAX_ROOM_HISTORY_PAGES = 2
# Startup cleanup runs before this process starts its Matrix sync loop, so it cannot
# clobber streams created by the same process. The remaining race is another
# concurrently running instance cleaning up a message during a long provider/tool stall
# where no new chunks arrive for a while, so keep a generous recency guard here.
_STALE_STREAM_RECENCY_GUARD_MS = 10_000
_RATE_LIMIT_DELAY_SECONDS = 0.15
_STOP_REACTION_KEYS = frozenset({"🛑", "⏹️"})


@dataclass
class _MessageState:
    """Latest visible state for one original Matrix message."""

    latest_body: str | None = None
    latest_timestamp: int = 0
    latest_event_id: str = ""
    latest_content: dict[str, object] | None = None
    thread_id: str | None = None
    stream_status: str | None = None
    stop_reaction_event_ids: set[str] = field(default_factory=set)


async def cleanup_stale_streaming_messages(
    client: nio.AsyncClient,
    *,
    bot_user_id: str,
    bot_user_ids: set[str] | None = None,
    config: Config,
    runtime_paths: RuntimePaths,
) -> int:
    """Clean stale in-progress bot messages across currently joined rooms."""
    joined_room_ids = await get_joined_rooms(client)
    if not joined_room_ids:
        return 0

    sender_domain = MatrixID.parse(bot_user_id).domain
    exact_bot_user_ids = {bot_user_id} if bot_user_ids is None else set(bot_user_ids)
    cleaned_count = 0

    for room_id in joined_room_ids:
        try:
            cleaned_count += await _cleanup_room_stale_streaming_messages(
                client,
                room_id=room_id,
                bot_user_id=bot_user_id,
                bot_user_ids=exact_bot_user_ids,
                sender_domain=sender_domain,
                config=config,
                runtime_paths=runtime_paths,
            )
        except Exception as exc:
            logger.warning(
                "Failed stale stream cleanup for room",
                room_id=room_id,
                error=str(exc),
            )

    return cleaned_count


async def _cleanup_room_stale_streaming_messages(
    client: nio.AsyncClient,
    *,
    room_id: str,
    bot_user_id: str,
    bot_user_ids: set[str],
    sender_domain: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> int:
    """Clean stale bot messages in one room."""
    message_states = await _scan_room_message_states(
        client,
        room_id=room_id,
        bot_user_id=bot_user_id,
    )
    if not message_states:
        return 0

    cleaned_count = 0
    prior_edit_succeeded = False
    candidate_items = sorted(
        ((event_id, state) for event_id, state in message_states.items() if state.latest_body is not None),
        key=lambda item: (item[1].latest_timestamp, item[0]),
    )

    for target_event_id, state in candidate_items:
        assert state.latest_body is not None  # guaranteed by filter above
        if _is_cleanup_candidate(state):
            if _is_recent_timestamp(state.latest_timestamp):
                continue
            edited = await _cleanup_candidate_message(
                client,
                room_id=room_id,
                target_event_id=target_event_id,
                state=state,
                bot_user_ids=bot_user_ids,
                sender_domain=sender_domain,
                config=config,
                runtime_paths=runtime_paths,
                prior_edit_succeeded=prior_edit_succeeded,
            )
            if not edited:
                continue

            cleaned_count += 1
            prior_edit_succeeded = True
            continue

        if _has_restart_interrupted_note(state.latest_body):
            await _redact_stop_reactions(
                client,
                room_id=room_id,
                target_event_id=target_event_id,
                history_reaction_event_ids=state.stop_reaction_event_ids,
                bot_user_ids=bot_user_ids,
            )

    return cleaned_count


async def _cleanup_one_stale_message(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_event_id: str,
    state: _MessageState,
    bot_user_ids: set[str],
    sender_domain: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Edit one stale message and redact stale stop reactions."""
    assert state.latest_body is not None
    edit_succeeded = await _edit_stale_message(
        client,
        room_id=room_id,
        target_event_id=target_event_id,
        new_text=build_restart_interrupted_body(state.latest_body),
        thread_id=state.thread_id,
        sender_domain=sender_domain,
        config=config,
        runtime_paths=runtime_paths,
        preserved_content=state.latest_content,
    )
    if not edit_succeeded:
        return False

    await _redact_stop_reactions(
        client,
        room_id=room_id,
        target_event_id=target_event_id,
        history_reaction_event_ids=state.stop_reaction_event_ids,
        bot_user_ids=bot_user_ids,
    )
    return True


async def _cleanup_candidate_message(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_event_id: str,
    state: _MessageState,
    bot_user_ids: set[str],
    sender_domain: str,
    config: Config,
    runtime_paths: RuntimePaths,
    prior_edit_succeeded: bool,
) -> bool:
    """Best-effort cleanup of one stale candidate message."""
    try:
        if prior_edit_succeeded:
            await asyncio.sleep(_RATE_LIMIT_DELAY_SECONDS)
        return await _cleanup_one_stale_message(
            client,
            room_id=room_id,
            target_event_id=target_event_id,
            state=state,
            bot_user_ids=bot_user_ids,
            sender_domain=sender_domain,
            config=config,
            runtime_paths=runtime_paths,
        )
    except Exception as exc:
        logger.warning(
            "Failed stale message cleanup",
            room_id=room_id,
            event_id=target_event_id,
            error=str(exc),
        )
        return False


async def _scan_room_message_states(
    client: nio.AsyncClient,
    *,
    room_id: str,
    bot_user_id: str,
) -> dict[str, _MessageState]:
    """Scan recent room history and return latest state by original event ID."""
    message_states, message_events = await _collect_room_history_events(
        client,
        room_id=room_id,
        bot_user_id=bot_user_id,
    )

    resolved_messages = await resolve_latest_visible_messages(message_events, client)
    _merge_bot_resolved_message_states(
        message_states,
        resolved_messages,
        bot_user_id=bot_user_id,
    )
    return message_states


async def _collect_room_history_events(
    client: nio.AsyncClient,
    *,
    room_id: str,
    bot_user_id: str,
) -> tuple[dict[str, _MessageState], list[nio.RoomMessageText]]:
    """Return room history text events plus tracked stop reactions."""
    message_states: dict[str, _MessageState] = {}
    message_events: list[nio.RoomMessageText] = []
    from_token: str | None = None

    for _ in range(_MAX_ROOM_HISTORY_PAGES):
        response = await client.room_messages(
            room_id,
            start=from_token,
            limit=_ROOM_HISTORY_PAGE_SIZE,
            direction=nio.MessageDirection.back,
        )
        if not isinstance(response, nio.RoomMessagesResponse):
            logger.warning(
                "Failed to fetch room history during stale stream cleanup",
                room_id=room_id,
                error=str(response),
            )
            return {}, []

        if not response.chunk:
            break

        for event in response.chunk:
            try:
                if isinstance(event, nio.RoomMessageText):
                    message_events.append(event)
                else:
                    _record_stop_reaction(
                        message_states,
                        event=event,
                        bot_user_id=bot_user_id,
                    )
            except Exception as exc:
                event_id = getattr(event, "event_id", None)
                logger.warning(
                    "Failed to inspect room event during stale stream cleanup",
                    room_id=room_id,
                    event_id=event_id,
                    error=str(exc),
                )

        if not response.end:
            break
        from_token = response.end

    return message_states, message_events


def _merge_bot_resolved_message_states(
    message_states: dict[str, _MessageState],
    resolved_messages: dict[str, dict[str, object]],
    *,
    bot_user_id: str,
) -> None:
    """Merge resolved bot-authored messages into cleanup state."""
    for target_event_id, message_data in resolved_messages.items():
        if message_data.get("sender") != bot_user_id:
            continue
        _merge_resolved_message_state(
            message_states,
            target_event_id=target_event_id,
            message_data=message_data,
        )


def _merge_resolved_message_state(
    message_states: dict[str, _MessageState],
    *,
    target_event_id: str,
    message_data: dict[str, object],
) -> None:
    """Store one resolved message if it has the fields cleanup needs."""
    body = message_data.get("body")
    timestamp = message_data.get("timestamp")
    latest_event_id = message_data.get("latest_event_id", message_data.get("event_id", ""))
    if not isinstance(body, str) or not isinstance(timestamp, int) or not isinstance(latest_event_id, str):
        return

    thread_id = message_data.get("thread_id")
    if thread_id is not None and not isinstance(thread_id, str):
        thread_id = None

    stream_status = message_data.get("stream_status")
    if stream_status is not None and not isinstance(stream_status, str):
        stream_status = None

    latest_content = message_data.get("content")
    normalized_latest_content: dict[str, object] | None = None
    if isinstance(latest_content, dict):
        normalized_latest_content = {}
        for key, value in latest_content.items():
            if isinstance(key, str):
                normalized_latest_content[key] = value

    state = message_states.setdefault(target_event_id, _MessageState())
    state.latest_body = body
    state.latest_timestamp = timestamp
    state.latest_event_id = latest_event_id
    state.latest_content = normalized_latest_content
    state.thread_id = thread_id
    state.stream_status = stream_status


def _record_stop_reaction(
    message_states: dict[str, _MessageState],
    *,
    event: object,
    bot_user_id: str,
) -> None:
    """Track self-authored stop reactions by their target message ID."""
    event_sender = getattr(event, "sender", None)
    if event_sender != bot_user_id:
        return

    event_source = getattr(event, "source", None)
    if not isinstance(event_source, dict):
        return

    event_info = EventInfo.from_event(event_source)
    if not event_info.is_reaction or event_info.reaction_key not in _STOP_REACTION_KEYS:
        return

    target_event_id = event_info.reaction_target_event_id
    reaction_event_id = getattr(event, "event_id", None)
    if not isinstance(target_event_id, str) or not isinstance(reaction_event_id, str):
        return

    message_states.setdefault(target_event_id, _MessageState()).stop_reaction_event_ids.add(reaction_event_id)


async def _edit_stale_message(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_event_id: str,
    new_text: str,
    thread_id: str | None,
    sender_domain: str,
    config: Config,
    runtime_paths: RuntimePaths,
    preserved_content: dict[str, object] | None,
) -> bool:
    """Edit a stale message while preserving thread context when present."""
    content = await build_threaded_edit_content(
        client,
        room_id=room_id,
        event_id=target_event_id,
        new_text=new_text,
        thread_id=thread_id,
        config=config,
        runtime_paths=runtime_paths,
        sender_domain=sender_domain,
        extra_content=_preserved_cleanup_content(preserved_content),
    )

    response_event_id = await edit_message(client, room_id, target_event_id, content, new_text)
    if response_event_id:
        return True

    logger.warning(
        "Failed to edit stale streaming message",
        room_id=room_id,
        event_id=target_event_id,
    )
    return False


def _preserved_cleanup_content(content: dict[str, object] | None) -> dict[str, object] | None:
    """Return the metadata fields that should survive a restart cleanup edit."""
    if content is None:
        return None

    preserved: dict[str, object] = {}
    for key in (STREAM_STATUS_KEY, _TOOL_TRACE_KEY, ORIGINAL_SENDER_KEY, "m.mentions"):
        value = content.get(key)
        if value is not None:
            preserved[key] = value

    return preserved or None


async def _redact_stop_reactions(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_event_id: str,
    history_reaction_event_ids: Iterable[str],
    bot_user_ids: set[str],
) -> None:
    """Best-effort removal of stale bot-authored stop reactions."""
    reaction_event_ids = set(history_reaction_event_ids)
    try:
        reaction_event_ids.update(
            await _get_stop_reaction_event_ids_from_relations(
                client,
                room_id=room_id,
                target_event_id=target_event_id,
                bot_user_ids=bot_user_ids,
            ),
        )
    except Exception as exc:
        logger.warning(
            "Failed to fetch stop reactions from relations API, falling back to history scan",
            room_id=room_id,
            event_id=target_event_id,
            error=str(exc),
        )

    for reaction_event_id in sorted(reaction_event_ids):
        try:
            response = await client.room_redact(
                room_id=room_id,
                event_id=reaction_event_id,
                reason="Response interrupted by service restart",
            )
            if isinstance(response, nio.RoomRedactError):
                logger.warning(
                    "Failed to redact stale stop reaction",
                    room_id=room_id,
                    event_id=target_event_id,
                    reaction_event_id=reaction_event_id,
                    error=str(response),
                )
        except Exception as exc:
            logger.warning(
                "Failed to redact stale stop reaction",
                room_id=room_id,
                event_id=target_event_id,
                reaction_event_id=reaction_event_id,
                error=str(exc),
            )


async def _get_stop_reaction_event_ids_from_relations(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_event_id: str,
    bot_user_ids: set[str],
) -> set[str]:
    """Return bot-authored stop reactions for the original target event."""
    reaction_event_ids: set[str] = set()
    async for related_event in _iter_reaction_relation_events(
        client,
        room_id=room_id,
        target_event_id=target_event_id,
    ):
        related_event_id = getattr(related_event, "event_id", None)
        related_sender = getattr(related_event, "sender", None)
        if not isinstance(related_event_id, str) or related_sender not in bot_user_ids:
            continue

        event_source = getattr(related_event, "source", None)
        if not isinstance(event_source, dict):
            continue

        event_info = EventInfo.from_event(event_source)
        if not event_info.is_reaction or event_info.reaction_target_event_id != target_event_id:
            continue
        if event_info.reaction_key not in _STOP_REACTION_KEYS:
            continue

        reaction_event_ids.add(related_event_id)

    return reaction_event_ids


async def _iter_reaction_relation_events(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_event_id: str,
) -> AsyncIterator[object]:
    """Yield reaction relation events from nio or direct Matrix HTTP."""
    relation_iterator = getattr(client, "room_get_event_relations", None)
    if callable(relation_iterator):
        async for related_event in relation_iterator(
            room_id,
            target_event_id,
            RelationshipType.annotation,
            "m.reaction",
        ):
            yield related_event
        return

    async for related_event in _iter_reaction_relation_events_via_http(
        client,
        room_id=room_id,
        target_event_id=target_event_id,
    ):
        yield related_event


async def _iter_reaction_relation_events_via_http(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_event_id: str,
) -> AsyncIterator[object]:
    """Yield reaction relation events via the raw Matrix relations endpoint."""
    next_batch: str | None = None

    while True:
        query_parameters: dict[str, str] = {"dir": nio.MessageDirection.back.value}
        if next_batch is not None:
            query_parameters["from"] = next_batch
        path = Api._build_path(
            [
                "rooms",
                room_id,
                "relations",
                target_event_id,
                RelationshipType.annotation.value,
                "m.reaction",
            ],
            query_parameters,
            "/_matrix/client/v1",
        )
        headers = {"Content-Type": "application/json"}
        if client.access_token:
            headers["Authorization"] = f"Bearer {client.access_token}"

        response = await client.send("GET", path, headers=headers)
        try:
            if response.status >= 400:
                body_text = await response.text()
                msg = f"HTTP {response.status} from relations API: {body_text}"
                raise ValueError(msg)
            response_body = await response.json()
        finally:
            response.release()

        chunk = response_body.get("chunk")
        if not isinstance(chunk, list):
            msg = "Invalid relations API response: missing chunk list"
            raise TypeError(msg)

        for raw_event in chunk:
            if isinstance(raw_event, dict):
                yield nio.Event.parse_event(raw_event)

        next_batch = response_body.get("next_batch")
        if not isinstance(next_batch, str) or not next_batch:
            return


def _has_restart_interrupted_note(body: str) -> bool:
    """Return whether the body already contains the restart interruption note."""
    return body.rstrip().endswith(_RESTART_INTERRUPTED_RESPONSE_NOTE)


def _is_cleanup_candidate(state: _MessageState) -> bool:
    """Return whether the latest visible state represents stale in-progress output."""
    assert state.latest_body is not None
    if _has_restart_interrupted_note(state.latest_body):
        return False
    if state.stream_status == STREAM_STATUS_COMPLETED:
        return False
    if state.stream_status in {STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING}:
        return True
    return is_in_progress_message(state.latest_body)


def _is_recent_timestamp(timestamp_ms: int, *, now_ms: int | None = None) -> bool:
    """Return whether a timestamp is still within the startup recency guard."""
    current_time_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return current_time_ms - timestamp_ms < _STALE_STREAM_RECENCY_GUARD_MS
