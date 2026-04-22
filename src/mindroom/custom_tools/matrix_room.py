"""Native Matrix room introspection toolkit for room-info/members/threads/state actions."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock
from typing import Any, ClassVar, cast

import nio
from agno.tools import Toolkit
from aiohttp import ClientError

from mindroom.custom_tools.attachment_helpers import room_access_allowed
from mindroom.custom_tools.matrix_helpers import (
    check_rate_limit,
    message_preview,
)
from mindroom.matrix.client_thread_history import RoomThreadsPageError, get_room_threads_page
from mindroom.matrix.client_visible_messages import thread_root_body_preview
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    get_tool_runtime_context,
)


@dataclass(frozen=True)
class _MatrixRoomRequest:
    action: str
    room_id: str | None
    limit: int | None
    event_type: str | None
    state_key: str | None
    page_token: str | None


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

    @staticmethod
    def _thread_reply_count(event: nio.Event) -> int:
        source = event.source
        if not isinstance(source, dict):
            return 0
        unsigned = source.get("unsigned", {})
        if not isinstance(unsigned, dict):
            return 0
        relations = unsigned.get("m.relations", {})
        if not isinstance(relations, dict):
            return 0
        thread_metadata = relations.get("m.thread", {})
        if not isinstance(thread_metadata, dict):
            return 0
        count = thread_metadata.get("count")
        return count if isinstance(count, int) and not isinstance(count, bool) else 0

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

    @classmethod
    def _input_error(cls, action: str, message: str) -> str:
        return cls._payload("error", action=action, message=message)

    @classmethod
    def _normalize_optional_str_fields(
        cls,
        action: str,
        *,
        room_id: object,
        event_type: object,
        state_key: object,
        page_token: object,
    ) -> tuple[str | None, str | None, str | None, str | None] | str:
        normalized: dict[str, str | None] = {}
        for name, value in {
            "room_id": room_id,
            "event_type": event_type,
            "state_key": state_key,
            "page_token": page_token,
        }.items():
            if value is None:
                normalized[name] = None
                continue
            if not isinstance(value, str):
                return cls._input_error(action, f"{name} must be a string when provided.")
            normalized[name] = value.strip()
        return (
            normalized["room_id"],
            normalized["event_type"],
            normalized["state_key"],
            normalized["page_token"],
        )

    @classmethod
    def _normalize_request(
        cls,
        *,
        action: object,
        room_id: object,
        limit: object,
        event_type: object,
        state_key: object,
        page_token: object,
    ) -> _MatrixRoomRequest | str:
        if not isinstance(action, str):
            return cls._input_error("invalid", "action must be a string.")
        normalized_action = action.strip().lower()
        normalized_fields = cls._normalize_optional_str_fields(
            normalized_action,
            room_id=room_id,
            event_type=event_type,
            state_key=state_key,
            page_token=page_token,
        )
        if isinstance(normalized_fields, str):
            return normalized_fields
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool)):
            return cls._input_error(normalized_action, "limit must be an integer when provided.")
        normalized_room_id, normalized_event_type, normalized_state_key, normalized_page_token = normalized_fields
        return _MatrixRoomRequest(
            action=normalized_action,
            room_id=normalized_room_id,
            limit=cast("int | None", limit),
            event_type=normalized_event_type,
            state_key=normalized_state_key,
            page_token=normalized_page_token,
        )

    async def _dispatch_action(
        self,
        context: ToolRuntimeContext,
        *,
        request: _MatrixRoomRequest,
        resolved_room_id: str,
    ) -> str:
        if request.action == "room-info":
            return await self._room_info(context, room_id=resolved_room_id)
        if request.action == "members":
            return await self._members(context, room_id=resolved_room_id)
        if request.action == "threads":
            return await self._threads(
                context,
                room_id=resolved_room_id,
                limit=self._thread_limit(request.limit),
                page_token=request.page_token or None,
            )
        return await self._state(
            context,
            room_id=resolved_room_id,
            event_type=request.event_type or None,
            state_key=request.state_key,
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
        except (ClientError, TimeoutError) as exc:
            return self._payload(
                "error",
                action="threads",
                room_id=room_id,
                message=self._transport_error_message(exc),
            )

        threads_list: list[dict[str, Any]] = []
        for event in thread_roots:
            thread_info: dict[str, Any] = {
                "thread_id": event.event_id,
                "sender": event.sender,
                "timestamp": event.server_timestamp,
            }
            thread_info["body_preview"] = await thread_root_body_preview(
                event,
                client=context.client,
                config=context.config,
                runtime_paths=context.runtime_paths,
            )
            thread_info["reply_count"] = self._thread_reply_count(event)

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
            try:
                response = await context.client.room_get_state_event(
                    room_id,
                    event_type,
                    state_key or "",
                )
            except (ClientError, TimeoutError) as exc:
                return self._payload(
                    "error",
                    action="state",
                    room_id=room_id,
                    event_type=event_type,
                    state_key=state_key or "",
                    message=self._transport_error_message(exc),
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

        request = self._normalize_request(
            action=action,
            room_id=room_id,
            limit=limit,
            event_type=event_type,
            state_key=state_key,
            page_token=page_token,
        )
        if isinstance(request, str):
            return request
        if request.action not in self._VALID_ACTIONS:
            return self._payload(
                "error",
                action=request.action,
                message="Unsupported action. Use room-info, members, threads, or state.",
            )

        resolved_room_id = request.room_id or context.room_id
        if not room_access_allowed(context, resolved_room_id):
            return self._payload(
                "error",
                action=request.action,
                room_id=resolved_room_id,
                message="Not authorized to access the target room.",
            )

        if (limit_error := self._check_rate_limit(context, resolved_room_id)) is not None:
            return self._payload(
                "error",
                action=request.action,
                room_id=resolved_room_id,
                message=limit_error,
            )

        return await self._dispatch_action(
            context,
            request=request,
            resolved_room_id=resolved_room_id,
        )
