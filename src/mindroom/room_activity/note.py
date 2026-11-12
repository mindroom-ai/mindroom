"""List, in a thread reply's turn context, the room's other threads with messages since the agent's last reply there.

The note is ``persist=False`` enrichment, so it rides after the prompt in the turn context, which never takes a cache
breakpoint and replays where it was sent: later requests still extend this one, and the system prompt never changes.
It is read from the agent's own projection of the room, so it names only threads its Matrix account received there,
and it carries no message text beyond each thread's title.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from mindroom.config.resolution import room_activity
from mindroom.constants import THREAD_SUMMARY_CONTENT_KEY
from mindroom.hooks import EnrichmentItem
from mindroom.matrix.client_visible_messages import message_preview, trusted_visible_sender_ids
from mindroom.matrix.conversation_reads import projected_visible_message
from mindroom.thread_summary.state import human_summary_authorizer, summary_state
from mindroom.tool_system.agent_tools import room_turn_offers

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping, Sequence

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.config.main import Config
    from mindroom.config.room_activity import RoomActivityConfig
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal.reads import ThreadActivity
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.conversation_reads import ConversationReader
    from mindroom.response_turn import ResponseTurnContext

_ROOM_ACTIVITY_ENRICHMENT_KEY = "room_activity"
_MINUTE_MS = 60_000
# The newest messages of the window one note counts; a busier window leaves its oldest uncounted.
_ROW_LIMIT = 1_000
# How many of a thread's newest messages are searched for its current summary title.
_SUMMARY_TAIL = 50
_MAX_NAMES = 3
_TITLE_LENGTH = 80
_NAME_LENGTH = 40
_READ_TOOL = "matrix_message"
_FOOTER = f'Read one with {_READ_TOOL}(action="read", thread_id="...") if it bears on this request.'


def _room_activity_item(text: str) -> EnrichmentItem:
    """Return the note as this turn's transient context, which is never stored as conversation."""
    return EnrichmentItem(key=_ROOM_ACTIVITY_ENRICHMENT_KEY, text=text, cache_policy="volatile", persist=False)


def _offers_read_tool(ctx: ResponseTurnContext, config: Config) -> bool:
    """Return whether this turn can read another thread, without which the note only costs tokens."""
    return room_turn_offers(config, ctx.entity_label, _READ_TOOL) and (
        ctx.tool_names is None or _READ_TOOL in ctx.tool_names
    )


async def with_room_activity_note(
    ctx: ResponseTurnContext,
    *,
    runtime: BotRuntimeView,
    runtime_paths: RuntimePaths,
    reader: ConversationReader,
    agent_user_id: str,
) -> tuple[EnrichmentItem, ...]:
    """Add the note to an agent's thread reply when its ``room_activity`` setting is on and other threads changed."""
    items = tuple(ctx.transient_enrichment_items)
    config = runtime.config
    settings = room_activity(config, ctx.entity_label)
    client = runtime.client
    # A minimal turn shows its model turn context only as a context document, so the note would go unread there.
    if (
        not settings.enabled
        or ctx.agent_mode == "minimal"
        or ctx.room_id is None
        or ctx.thread_id is None
        or client is None
        or not _offers_read_tool(ctx, config)
    ):
        return items
    note = await _room_activity_note(
        reader,
        settings,
        room_id=ctx.room_id,
        thread_id=ctx.thread_id,
        agent_user_id=agent_user_id,
        thread_history=ctx.thread_history or (),
        member_display_names=ctx.member_display_names,
        trusted_sender_ids=trusted_visible_sender_ids(config, runtime_paths),
        authorize_human=human_summary_authorizer(
            client,
            ctx.room_id,
            config,
            runtime_paths,
            ctx.entity_label,
            runtime.agent_reply_memberships,
        ),
        now_ms=int(time.time() * 1000),
    )
    return items if note is None else (*items, _room_activity_item(note))


async def _room_activity_note(
    reader: ConversationReader,
    settings: RoomActivityConfig,
    *,
    room_id: str,
    thread_id: str,
    agent_user_id: str,
    thread_history: Sequence[ResolvedVisibleMessage],
    member_display_names: Mapping[str, str],
    trusted_sender_ids: Collection[str],
    authorize_human: Callable[[str], bool],
    now_ms: int,
) -> str | None:
    """Return the note for a reply in ``thread_id``, or None when no other thread of the room had new messages."""
    window_start = now_ms - settings.window_minutes * _MINUTE_MS
    # The agent's own summary notice of this thread is no reply, and the activity since its reply stays new.
    last_reply = max(
        (
            message.timestamp
            for message in thread_history
            if message.sender == agent_user_id and THREAD_SUMMARY_CONTENT_KEY not in message.content
        ),
        default=0,
    )
    activity = await reader.room_thread_activity(
        room_id=room_id,
        since_ts=max(window_start, last_reply),
        until_ts=now_ms,
        exclude_thread_id=thread_id,
        thread_limit=settings.max_threads,
        row_limit=_ROW_LIMIT,
        summary_tail=_SUMMARY_TAIL,
        summary_key=THREAD_SUMMARY_CONTENT_KEY,
    )
    if not activity:
        return None
    lines = [
        _thread_line(
            thread,
            agent_user_id=agent_user_id,
            member_display_names=member_display_names,
            trusted_sender_ids=trusted_sender_ids,
            authorize_human=authorize_human,
            now_ms=now_ms,
        )
        for thread in activity
    ]
    since = (
        "since your last reply here" if last_reply > window_start else f"in the last {settings.window_minutes} minutes"
    )
    return "\n".join([f"Other threads in this room with activity {since}:", *lines, _FOOTER])


def _thread_line(
    thread: ThreadActivity,
    *,
    agent_user_id: str,
    member_display_names: Mapping[str, str],
    trusted_sender_ids: Collection[str],
    authorize_human: Callable[[str], bool],
    now_ms: int,
) -> str:
    """Describe one thread's new messages in one line."""
    new = thread.recent
    names: list[str] = []
    own_reply: int | None = None
    for message in new:
        if message.sender == agent_user_id:
            own_reply = own_reply or message.created_ts
            continue
        name = message_preview(
            member_display_names.get(message.sender) or message.sender.removeprefix("@").partition(":")[0],
            _NAME_LENGTH,
        )
        if name not in names:
            names.append(name)
    parts = [_people(names)] if names else []
    parts.append(f"{len(new)} new message{'' if len(new) == 1 else 's'}, last {_ago(new[0].created_ts, now_ms)}")
    if own_reply is not None:
        parts.append(f"you replied there {_ago(own_reply, now_ms)}")
    state = summary_state(
        [projected_visible_message(message) for message in thread.summaries],
        thread_id=thread.thread_id,
        trusted_sender_ids=trusted_sender_ids,
        authorize_human=authorize_human,
    )
    title = state.summary if state is not None and state.summary else _first_line(thread)
    label = f'"{message_preview(title, _TITLE_LENGTH)}" ' if title else ""
    return f"- {label}(thread {thread.thread_id}): {'; '.join(parts)}"


def _first_line(thread: ThreadActivity) -> str:
    """Return the first non-blank line of the thread's root, or nothing when its root cannot be read."""
    if thread.root is None:
        return ""
    body = projected_visible_message(thread.root).body
    return next((line for line in body.splitlines() if line.strip()), "")


def _people(names: list[str]) -> str:
    """Name at most ``_MAX_NAMES`` writers, the most recent first, and count the rest."""
    rest = len(names) - _MAX_NAMES
    if rest <= 0:
        return ", ".join(names)
    return f"{', '.join(names[:_MAX_NAMES])} and {rest} other{'' if rest == 1 else 's'}"


def _ago(timestamp_ms: int, now_ms: int) -> str:
    return f"{max(1, (now_ms - timestamp_ms) // _MINUTE_MS)} min ago"
