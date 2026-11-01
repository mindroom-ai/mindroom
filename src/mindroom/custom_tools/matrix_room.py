"""Native Matrix room introspection, agent discovery, and history actions."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from dataclasses import asdict, replace
from threading import Lock
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

import nio
from aiohttp import ClientError

from mindroom.custom_tools.attachment_helpers import resolve_requested_room_id, room_access_allowed
from mindroom.custom_tools.matrix_agent_discovery import available_room_agents
from mindroom.custom_tools.matrix_helpers import check_rate_limit
from mindroom.custom_tools.tool_payloads import custom_tool_payload, tool_context_or_error
from mindroom.logging_config import get_logger
from mindroom.matrix.client_visible_messages import (
    message_preview,
    thread_root_body_preview,
    trusted_visible_sender_ids,
)
from mindroom.matrix.room_history_reads import RoomThreadsPageError, get_room_threads_page
from mindroom.thread_summary.state import current_thread_summary
from mindroom.tool_system.authoring import Toolkit

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)


class MatrixRoomTools(Toolkit):
    """Native Matrix room introspection actions."""

    _rate_limit_lock: ClassVar[Lock] = Lock()
    _recent_actions: ClassVar[dict[tuple[str, str, str], deque[float]]] = defaultdict(deque)
    _RATE_LIMIT_WINDOW_SECONDS: ClassVar[float] = 30.0
    _RATE_LIMIT_MAX_ACTIONS: ClassVar[int] = 20
    _DEFAULT_THREAD_LIMIT: ClassVar[int] = 20
    _MAX_THREAD_LIMIT: ClassVar[int] = 50
    # Each summary reads a whole thread, which is a homeserver walk the first time; a few at once keep a full page fast.
    _SUMMARY_READ_CONCURRENCY: ClassVar[int] = 6
    _MAX_STATE_EVENTS: ClassVar[int] = 100

    def __init__(self) -> None:
        super().__init__(
            name="matrix_room",
            tools=[self.matrix_room],
        )

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        return custom_tool_payload("matrix_room", status, **kwargs)

    @classmethod
    def _thread_limit(cls, limit: int | None) -> int:
        """Clamp thread limit to [1, 50], defaulting to 20."""
        if limit is None:
            return cls._DEFAULT_THREAD_LIMIT
        return max(1, min(limit, cls._MAX_THREAD_LIMIT))

    @staticmethod
    def _thread_metadata(source: dict[str, Any]) -> dict[str, Any]:
        """Return the event's bundled `m.thread` aggregation, empty when any level of it is malformed."""
        unsigned = source.get("unsigned")
        relations = unsigned.get("m.relations") if isinstance(unsigned, dict) else None
        thread_metadata = relations.get("m.thread") if isinstance(relations, dict) else None
        return thread_metadata if isinstance(thread_metadata, dict) else {}

    async def _serialize_thread_root(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
        event: nio.Event,
        trusted_sender_ids: frozenset[str],
    ) -> dict[str, object] | None:
        event_id = event.event_id
        sender = event.sender
        timestamp = event.server_timestamp
        source = event.source
        if (
            not isinstance(event_id, str)
            or not isinstance(sender, str)
            or not isinstance(timestamp, int)
            or isinstance(timestamp, bool)
            or not isinstance(source, dict)
        ):
            logger.warning(
                "Skipping malformed room thread root",
                room_id=room_id,
                event_type=type(event).__name__,
            )
            return None
        body_preview = await thread_root_body_preview(
            event,
            client=context.client,
            config=context.config,
            runtime_paths=context.runtime_paths,
            trusted_sender_ids=trusted_sender_ids,
        )

        thread_metadata = self._thread_metadata(source)
        reply_count = thread_metadata.get("count")
        latest_event = thread_metadata.get("latest_event")
        latest_activity_ts = latest_event.get("origin_server_ts") if isinstance(latest_event, dict) else None
        payload: dict[str, object] = {
            "thread_id": event_id,
            "sender": sender,
            "timestamp": timestamp,
            "body_preview": body_preview,
            "reply_count": reply_count if isinstance(reply_count, int) and not isinstance(reply_count, bool) else 0,
        }
        if isinstance(latest_activity_ts, int) and not isinstance(latest_activity_ts, bool):
            payload["latest_activity_ts"] = latest_activity_ts
        return payload

    async def _add_current_summaries(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
        threads: list[dict[str, object]],
        trusted_sender_ids: frozenset[str],
    ) -> None:
        membership_index = context.require_agent_reply_memberships()
        reads = asyncio.Semaphore(self._SUMMARY_READ_CONCURRENCY)

        async def add_current_summary(thread: dict[str, object]) -> None:
            async with reads:
                current_summary = await current_thread_summary(
                    context.client,
                    room_id,
                    cast("str", thread["thread_id"]),
                    config=context.config,
                    runtime_paths=context.runtime_paths,
                    conversation_reader=context.conversation_reader,
                    entity_name=context.agent_name,
                    membership_index=membership_index,
                    trusted_sender_ids=trusted_sender_ids,
                )
            if current_summary is not None:
                thread["summary"] = current_summary.summary
                thread["summary_pinned"] = current_summary.pinned

        await asyncio.gather(*(add_current_summary(thread) for thread in threads))

    @classmethod
    def _check_rate_limit(
        cls,
        context: ToolRuntimeContext,
        room_id: str,
    ) -> str | None:
        return check_rate_limit(
            lock=cls._rate_limit_lock,
            recent_actions=cls._recent_actions,
            window_seconds=cls._RATE_LIMIT_WINDOW_SECONDS,
            max_actions=cls._RATE_LIMIT_MAX_ACTIONS,
            tool_name="matrix_room",
            context=context,
            room_id=room_id,
        )

    @staticmethod
    def _transport_error_message(exc: TimeoutError | ClientError) -> str:
        detail = str(exc).strip()
        suffix = f": {detail}" if detail else ""
        return f"Matrix request failed ({type(exc).__name__}){suffix}."

    async def _dispatch_action(
        self,
        context: ToolRuntimeContext,
        *,
        action: str,
        room_id: str,
        limit: int | None,
        event_type: str | None,
        state_key: str | None,
        page_token: str | None,
        include_summaries: bool,
    ) -> str:
        if action == "room-info":
            return await self._room_info(context, room_id=room_id)
        if action == "members":
            return await self._members(context, room_id=room_id)
        if action == "agents":
            agents = await available_room_agents(context, room_id)
            return self._payload(
                "ok",
                action="agents",
                room_id=room_id,
                agents=[asdict(agent) for agent in agents],
            )
        if action == "threads":
            return await self._threads(
                context,
                room_id=room_id,
                limit=self._thread_limit(limit),
                page_token=(page_token or "").strip() or None,
                include_summaries=include_summaries,
            )
        return await self._state(
            context,
            room_id=room_id,
            event_type=(event_type or "").strip() or None,
            state_key=(state_key or "").strip(),
        )

    async def _room_info(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
    ) -> str:
        cached_room: nio.MatrixRoom | None = context.client.rooms.get(room_id)
        if cached_room is None:
            return self._payload(
                "error",
                action="room-info",
                room_id=room_id,
                message="Room not found in client state.",
            )

        power_levels = cached_room.power_levels
        power_summary: dict[str, Any] = {}
        if power_levels is not None:
            power_summary = {
                "ban": power_levels.defaults.ban,
                "invite": power_levels.defaults.invite,
                "kick": power_levels.defaults.kick,
                "redact": power_levels.defaults.redact,
                "state_default": power_levels.defaults.state_default,
                "events_default": power_levels.defaults.events_default,
                "users_default": power_levels.defaults.users_default,
            }

        creator: str | None = None
        try:
            create_response = await context.client.room_get_state_event(room_id, "m.room.create")
        except (ClientError, TimeoutError):
            create_response = None
        if isinstance(create_response, nio.RoomGetStateEventResponse):
            content = create_response.content
            creator = content.get("creator") if isinstance(content, dict) else None

        return self._payload(
            "ok",
            action="room-info",
            room_id=room_id,
            thread_id=context.resolved_thread_id if room_id == context.room_id else None,
            reply_to_event_id=context.reply_to_event_id if room_id == context.room_id else None,
            requester_id=context.requester_id,
            agent_name=context.agent_name,
            name=cached_room.name,
            topic=cached_room.topic,
            member_count=cached_room.member_count,
            encrypted=cached_room.encrypted,
            join_rule=cached_room.join_rule,
            canonical_alias=cached_room.canonical_alias,
            room_version=cached_room.room_version,
            guest_access=cached_room.guest_access,
            power_levels_summary=power_summary,
            creator=creator,
        )

    async def _members(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
    ) -> str:
        try:
            response = await context.client.joined_members(room_id)
        except (ClientError, TimeoutError) as exc:
            return self._payload(
                "error",
                action="members",
                room_id=room_id,
                message=self._transport_error_message(exc),
            )
        if not isinstance(response, nio.JoinedMembersResponse):
            return self._payload(
                "error",
                action="members",
                room_id=room_id,
                message=f"Failed to fetch members: {response}",
            )

        cached_room: nio.MatrixRoom | None = context.client.rooms.get(room_id)
        power_levels = cached_room.power_levels if cached_room is not None else None

        members_list: list[dict[str, Any]] = []
        for member in response.members:
            member_info: dict[str, Any] = {
                "user_id": member.user_id,
                "display_name": member.display_name,
                "avatar_url": member.avatar_url,
            }
            if power_levels is not None:
                member_info["power_level"] = power_levels.get_user_level(member.user_id)
            members_list.append(member_info)

        return self._payload(
            "ok",
            action="members",
            room_id=room_id,
            count=len(members_list),
            members=members_list,
        )

    async def _threads(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
        limit: int,
        page_token: str | None,
        include_summaries: bool,
    ) -> str:
        try:
            thread_roots, next_token = await get_room_threads_page(
                context.client,
                room_id,
                limit=limit,
                page_token=page_token,
            )
        except RoomThreadsPageError as exc:
            error_payload: dict[str, object] = {
                "action": "threads",
                "response": exc.response,
                "room_id": room_id,
            }
            if exc.errcode is not None:
                error_payload["errcode"] = exc.errcode
            if exc.retry_after_ms is not None:
                error_payload["retry_after_ms"] = exc.retry_after_ms
            return self._payload("error", **error_payload)

        threads_list: list[dict[str, object]] = []
        trusted_sender_ids = trusted_visible_sender_ids(context.config, context.runtime_paths)
        for event in thread_roots:
            thread_info = await self._serialize_thread_root(
                context,
                room_id=room_id,
                event=event,
                trusted_sender_ids=trusted_sender_ids,
            )
            if thread_info is not None:
                threads_list.append(thread_info)
        if include_summaries:
            await self._add_current_summaries(
                context,
                room_id=room_id,
                threads=threads_list,
                trusted_sender_ids=trusted_sender_ids,
            )

        return self._payload(
            "ok",
            action="threads",
            room_id=room_id,
            count=len(threads_list),
            threads=threads_list,
            next_token=next_token,
            has_more=next_token is not None,
        )

    async def _state(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
        event_type: str | None,
        state_key: str,
    ) -> str:
        if event_type is not None:
            try:
                response = await context.client.room_get_state_event(
                    room_id,
                    event_type,
                    state_key,
                )
            except (ClientError, TimeoutError) as exc:
                return self._payload(
                    "error",
                    action="state",
                    room_id=room_id,
                    event_type=event_type,
                    state_key=state_key,
                    message=self._transport_error_message(exc),
                )
            if isinstance(response, nio.RoomGetStateEventResponse):
                return self._payload(
                    "ok",
                    action="state",
                    room_id=room_id,
                    event_type=event_type,
                    state_key=state_key,
                    content=response.content,
                )
            return self._payload(
                "error",
                action="state",
                room_id=room_id,
                event_type=event_type,
                state_key=state_key,
                message=f"Failed to fetch state event: {response}",
            )

        try:
            response = await context.client.room_get_state(room_id)
        except (ClientError, TimeoutError) as exc:
            return self._payload(
                "error",
                action="state",
                room_id=room_id,
                message=self._transport_error_message(exc),
            )
        if not isinstance(response, nio.RoomGetStateResponse):
            return self._payload(
                "error",
                action="state",
                room_id=room_id,
                message=f"Failed to fetch room state: {response}",
            )

        state_events: list[dict[str, Any]] = []
        type_counts: dict[str, int] = defaultdict(int)
        for event_dict in response.events:
            etype = event_dict.get("type", "")
            type_counts[etype] += 1
            if etype == "m.room.member":
                continue
            if len(state_events) >= self._MAX_STATE_EVENTS:
                continue
            content = event_dict.get("content", {})
            state_events.append(
                {
                    "type": etype,
                    "state_key": event_dict.get("state_key", ""),
                    "content_preview": message_preview(json.dumps(content)),
                },
            )

        state_summary = dict(sorted(type_counts.items()))

        return self._payload(
            "ok",
            action="state",
            room_id=room_id,
            count=len(state_events),
            state_summary=state_summary,
            events=state_events,
        )

    async def matrix_room(
        self,
        action: Literal["room-info", "members", "agents", "threads", "state"] = "room-info",
        room_id: str | None = None,
        limit: int | None = None,
        event_type: str | None = None,
        state_key: str | None = None,
        page_token: str | None = None,
        include_summaries: bool = False,
    ) -> str:
        """Inspect Matrix room metadata, available agents, members, threads, and state.

        Actions:
        - room-info: Room metadata and current thread_id, reply_to_event_id, requester_id, and agent_name.
        - members: List joined members with display names and power levels.
        - agents: List agents and teams available to answer this requester in the room, including yourself.
          Each row has name, matrix_user_id, description, and thread_mode (thread or room).
          To start a conversation, use matrix_message(recipient=name, message="...").
          This lists conversation targets; run_subagent separately lists your allowed subagents.
        - threads: List thread roots with preview, sender, timestamp, reply count, and latest activity when available.
          With include_summaries=True, each row also has summary (the current thread title, or null) and
          summary_pinned (true when the thread's title is pinned, so automatic summaries leave it alone); both are omitted
          when the thread's full history is unavailable. This reads every listed thread, so pass it only when needed.
          Use page_token from a previous response's next_token to paginate.
        - state: Read room state. If event_type is given, return that specific state event.
          If omitted, return a summary of all state events (m.room.member events are elided).

        room_id defaults to the current room. limit (default 20, max 50) and include_summaries apply to threads.
        """
        if isinstance(context := tool_context_or_error(self._payload, "Matrix room"), str):
            return context

        context = replace(context, config=context.current_config, config_provider=None)
        resolved_room_id, room_error = await resolve_requested_room_id(context, room_id)
        if room_error is not None or resolved_room_id is None:
            return self._payload("error", action=action, message=room_error)
        if not await room_access_allowed(context, resolved_room_id, contact_room_ok=True):
            return self._payload(
                "error",
                action=action,
                room_id=resolved_room_id,
                message="Not authorized to access the target room.",
            )

        if (limit_error := self._check_rate_limit(context, resolved_room_id)) is not None:
            return self._payload(
                "error",
                action=action,
                room_id=resolved_room_id,
                message=limit_error,
            )

        return await self._dispatch_action(
            context,
            action=action,
            room_id=resolved_room_id,
            limit=limit,
            event_type=event_type,
            state_key=state_key,
            page_token=page_token,
            include_summaries=include_summaries,
        )
