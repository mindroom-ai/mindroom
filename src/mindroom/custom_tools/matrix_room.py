"""Native Matrix room introspection toolkit for room-info/members/threads/state actions."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from threading import Lock
from typing import Any, ClassVar

import nio
from agno.tools import Toolkit

from mindroom.custom_tools.attachment_helpers import room_access_allowed
from mindroom.custom_tools.matrix_helpers import check_rate_limit, message_preview
from mindroom.matrix.client import RoomThreadsPageError, get_room_threads_page
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context


class MatrixRoomTools(Toolkit):
    """Native Matrix room introspection actions."""

    _rate_limit_lock: ClassVar[Lock] = Lock()
    _recent_actions: ClassVar[dict[tuple[str, str, str], deque[float]]] = defaultdict(deque)
    _RATE_LIMIT_WINDOW_SECONDS: ClassVar[float] = 30.0
    _RATE_LIMIT_MAX_ACTIONS: ClassVar[int] = 20
    _DEFAULT_THREAD_LIMIT: ClassVar[int] = 20
    _MAX_THREAD_LIMIT: ClassVar[int] = 50
    _MAX_STATE_EVENTS: ClassVar[int] = 100
    _VALID_ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {"room-info", "members", "threads", "state"},
    )

    def __init__(self) -> None:
        super().__init__(
            name="matrix_room",
            tools=[self.matrix_room],
        )

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        payload: dict[str, object] = {"status": status, "tool": "matrix_room"}
        payload.update(kwargs)
        return json.dumps(payload, sort_keys=True)

    @classmethod
    def _context_error(cls) -> str:
        return cls._payload(
            "error",
            message="Matrix room tool context is unavailable in this runtime path.",
        )

    @classmethod
    def _thread_limit(cls, limit: int | None) -> int:
        """Clamp thread limit to [1, 50], defaulting to 20."""
        if limit is None:
            return cls._DEFAULT_THREAD_LIMIT
        return max(1, min(limit, cls._MAX_THREAD_LIMIT))

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
        create_response = await context.client.room_get_state_event(room_id, "m.room.create")
        if isinstance(create_response, nio.RoomGetStateEventResponse):
            creator = create_response.content.get("creator")

        return self._payload(
            "ok",
            action="room-info",
            room_id=room_id,
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
        response = await context.client.joined_members(room_id)
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

        threads_list: list[dict[str, Any]] = []
        for event in thread_roots:
            thread_info: dict[str, Any] = {
                "thread_id": event.event_id,
                "sender": event.sender,
                "timestamp": event.server_timestamp,
            }
            if isinstance(event, nio.MegolmEvent):
                thread_info["body_preview"] = "[encrypted]"
            elif hasattr(event, "body"):
                thread_info["body_preview"] = message_preview(event.body)
            else:
                thread_info["body_preview"] = ""

            relations = (event.source or {}).get("unsigned", {}).get("m.relations", {})
            thread_rel = relations.get("m.thread", {})
            thread_info["reply_count"] = thread_rel.get("count", 0)

            threads_list.append(thread_info)

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
        state_key: str | None,
    ) -> str:
        if event_type is not None:
            response = await context.client.room_get_state_event(
                room_id,
                event_type,
                state_key or "",
            )
            if isinstance(response, nio.RoomGetStateEventResponse):
                return self._payload(
                    "ok",
                    action="state",
                    room_id=room_id,
                    event_type=event_type,
                    state_key=state_key or "",
                    content=response.content,
                )
            return self._payload(
                "error",
                action="state",
                room_id=room_id,
                event_type=event_type,
                state_key=state_key or "",
                message=f"Failed to fetch state event: {response}",
            )

        response = await context.client.room_get_state(room_id)
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

    async def matrix_room(  # noqa: PLR0911
        self,
        action: str = "room-info",
        room_id: str | None = None,
        limit: int | None = None,
        event_type: str | None = None,
        state_key: str | None = None,
        page_token: str | None = None,
    ) -> str:
        """Inspect Matrix room metadata, members, threads, and state.

        Actions:
        - room-info: Room metadata (name, topic, encryption, member count, power levels, join rule).
        - members: List joined members with display names and power levels.
        - threads: List thread roots with preview, sender, timestamp, reply count.
          Use page_token from a previous response's next_token to paginate.
        - state: Read room state. If event_type is given, return that specific state event.
          If omitted, return a summary of all state events (m.room.member events are elided).

        room_id defaults to the current room. limit applies to threads (default 20, max 50).
        """
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()

        normalized_action = action.strip().lower()
        if normalized_action not in self._VALID_ACTIONS:
            return self._payload(
                "error",
                action=normalized_action,
                message="Unsupported action. Use room-info, members, threads, or state.",
            )

        resolved_room_id = room_id or context.room_id
        if not room_access_allowed(context, resolved_room_id):
            return self._payload(
                "error",
                action=normalized_action,
                room_id=resolved_room_id,
                message="Not authorized to access the target room.",
            )

        if (limit_error := self._check_rate_limit(context, resolved_room_id)) is not None:
            return self._payload(
                "error",
                action=normalized_action,
                room_id=resolved_room_id,
                message=limit_error,
            )

        if normalized_action == "room-info":
            return await self._room_info(context, room_id=resolved_room_id)
        if normalized_action == "members":
            return await self._members(context, room_id=resolved_room_id)
        if normalized_action == "threads":
            return await self._threads(
                context,
                room_id=resolved_room_id,
                limit=self._thread_limit(limit),
                page_token=page_token,
            )
        if normalized_action == "state":
            return await self._state(
                context,
                room_id=resolved_room_id,
                event_type=event_type,
                state_key=state_key,
            )
        return self._payload(
            "error",
            action=normalized_action,
            message="Unsupported action. Use room-info, members, threads, or state.",
        )
