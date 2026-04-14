"""Low-level Matrix room/event/state API tool for agents."""

from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from threading import Lock
from typing import ClassVar

import nio
from agno.tools import Toolkit

from mindroom.custom_tools.attachment_helpers import room_access_allowed
from mindroom.logging_config import get_logger
from mindroom.matrix.client import send_message_result
from mindroom.matrix.event_info import EventInfo
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context

logger = get_logger(__name__)


class MatrixApiTools(Toolkit):
    """Expose a small low-level Matrix API surface to agents."""

    _rate_limit_lock: ClassVar[Lock] = Lock()
    _recent_write_units: ClassVar[dict[tuple[str, str, str], deque[float]]] = defaultdict(deque)
    _RATE_LIMIT_WINDOW_SECONDS: ClassVar[float] = 60.0
    _RATE_LIMIT_MAX_UNITS: ClassVar[int] = 8
    _VALID_ACTIONS: ClassVar[tuple[str, ...]] = (
        "send_event",
        "get_state",
        "put_state",
        "redact",
        "get_event",
    )
    _VALID_ACTIONS_SET: ClassVar[frozenset[str]] = frozenset(_VALID_ACTIONS)
    _WRITE_ACTION_WEIGHTS: ClassVar[dict[str, int]] = {
        "send_event": 1,
        "put_state": 2,
        "redact": 2,
    }
    _HARD_BLOCKED_STATE_TYPES: ClassVar[frozenset[str]] = frozenset({"m.room.create"})
    _DANGEROUS_STATE_TYPES: ClassVar[frozenset[str]] = frozenset(
        {
            "m.room.power_levels",
            "m.room.encryption",
            "m.room.server_acl",
            "m.room.join_rules",
            "m.room.history_visibility",
            "m.room.guest_access",
            "m.room.member",
            "m.room.canonical_alias",
            "m.room.tombstone",
            "m.room.third_party_invite",
        },
    )

    def __init__(self) -> None:
        super().__init__(
            name="matrix_api",
            tools=[self.matrix_api],
        )

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        payload: dict[str, object] = {"status": status, "tool": "matrix_api"}
        payload.update(kwargs)
        return json.dumps(payload, sort_keys=True)

    @classmethod
    def _context_error(cls) -> str:
        return cls._payload(
            "error",
            message="Matrix API tool context is unavailable in this runtime path.",
        )

    @classmethod
    def _error_payload(
        cls,
        *,
        action: str,
        message: str,
        response: object | None = None,
        **kwargs: object,
    ) -> str:
        payload: dict[str, object] = {
            "action": action,
            "message": message,
            **kwargs,
        }
        normalized_response, status_code = cls._normalize_response(response)
        if normalized_response is not None:
            payload["response"] = normalized_response
        if status_code is not None:
            payload["status_code"] = status_code
        return cls._payload("error", **payload)

    @classmethod
    def _normalize_response(
        cls,
        response: object | None,
    ) -> tuple[str | None, str | None]:
        if response is None:
            return None, None
        if isinstance(
            response,
            (
                nio.RoomSendError,
                nio.RoomGetStateEventError,
                nio.RoomPutStateError,
                nio.RoomRedactError,
                nio.RoomGetEventError,
            ),
        ):
            return cls._normalize_matrix_error(response)
        if isinstance(response, Exception):
            detail = str(response)
            return (
                f"{type(response).__name__}: {detail}" if detail else type(response).__name__,
                None,
            )
        return str(response), None

    @staticmethod
    def _normalize_matrix_error(
        response: nio.RoomSendError
        | nio.RoomGetStateEventError
        | nio.RoomPutStateError
        | nio.RoomRedactError
        | nio.RoomGetEventError,
    ) -> tuple[str, str | None]:
        return str(response), response.status_code

    @classmethod
    def _supported_actions_message(cls) -> str:
        return "Unsupported action. Use send_event, get_state, put_state, redact, or get_event."

    @staticmethod
    def _normalize_action(action: str) -> str:
        return action.strip().lower() if isinstance(action, str) else ""

    @staticmethod
    def _resolve_room_id(
        context: ToolRuntimeContext,
        room_id: object | None,
    ) -> tuple[str | None, str | None]:
        if room_id is None:
            return context.room_id, None
        if not isinstance(room_id, str):
            return None, "room_id must be omitted or a non-empty Matrix room ID string."

        normalized_room_id = room_id.strip()
        if not normalized_room_id:
            return None, "room_id must be omitted or a non-empty Matrix room ID string."
        if not normalized_room_id.startswith("!") or ":" not in normalized_room_id:
            return None, "room_id must be a Matrix room ID in !room:server form."
        return normalized_room_id, None

    @staticmethod
    def _validate_bool(
        value: object,
        *,
        field_name: str,
    ) -> tuple[bool | None, str | None]:
        if isinstance(value, bool):
            return value, None
        return None, f"{field_name} must be a boolean."

    @staticmethod
    def _validate_non_empty_string(
        value: str | None,
        *,
        field_name: str,
    ) -> tuple[str | None, str | None]:
        if not isinstance(value, str):
            return None, f"{field_name} is required and must be a non-empty string."
        normalized_value = value.strip()
        if not normalized_value:
            return None, f"{field_name} is required and must be a non-empty string."
        return normalized_value, None

    @staticmethod
    def _resolve_state_key(
        state_key: str | None,
    ) -> tuple[str, str | None]:
        if state_key is None:
            return "", None
        if not isinstance(state_key, str):
            return "", "state_key must be a string."
        return state_key, None

    @staticmethod
    def _validate_content(
        content: dict[str, object] | None,
    ) -> tuple[dict[str, object] | None, str | None]:
        if not isinstance(content, dict):
            return None, "content must be a JSON object (dict)."
        try:
            json.dumps(content, sort_keys=True)
        except (TypeError, ValueError) as exc:
            return None, f"content must be JSON-serializable: {exc}"
        return content, None

    @classmethod
    def _content_summary(
        cls,
        content: dict[str, object] | None,
    ) -> dict[str, object] | None:
        if content is None:
            return None
        serialized = json.dumps(content, sort_keys=True)
        return {
            "content_keys": sorted(str(key) for key in content),
            "content_bytes": len(serialized.encode("utf-8")),
        }

    @classmethod
    def _check_rate_limit(
        cls,
        context: ToolRuntimeContext,
        room_id: str,
        *,
        action: str,
    ) -> str | None:
        weight = cls._WRITE_ACTION_WEIGHTS[action]
        key = (context.agent_name, context.requester_id, room_id)
        now = time.monotonic()
        cutoff = now - cls._RATE_LIMIT_WINDOW_SECONDS

        with cls._rate_limit_lock:
            history = cls._recent_write_units[key]
            while history and history[0] < cutoff:
                history.popleft()
            if len(history) + weight > cls._RATE_LIMIT_MAX_UNITS:
                return (
                    "Rate limit exceeded for matrix_api writes "
                    f"({cls._RATE_LIMIT_MAX_UNITS} units per {int(cls._RATE_LIMIT_WINDOW_SECONDS)}s)."
                )
            history.extend(now for _ in range(weight))

            stale_keys: list[tuple[str, str, str]] = []
            for other_key, other_history in cls._recent_write_units.items():
                if other_key == key:
                    continue
                while other_history and other_history[0] < cutoff:
                    other_history.popleft()
                if not other_history:
                    stale_keys.append(other_key)
            for stale_key in stale_keys:
                del cls._recent_write_units[stale_key]

        return None

    @classmethod
    def _audit_write(
        cls,
        *,
        context: ToolRuntimeContext,
        room_id: str,
        action: str,
        status: str,
        event_type: str | None = None,
        state_key: str | None = None,
        target_event_id: str | None = None,
        reason: str | None = None,
        dangerous: bool | None = None,
        content: dict[str, object] | None = None,
        response: object | None = None,
    ) -> None:
        audit_payload: dict[str, object] = {
            "agent_name": context.agent_name,
            "requester_id": context.requester_id,
            "room_id": room_id,
            "action": action,
            "status": status,
        }
        if event_type is not None:
            audit_payload["event_type"] = event_type
        if state_key is not None:
            audit_payload["state_key"] = state_key
        if target_event_id is not None:
            audit_payload["target_event_id"] = target_event_id
        if reason is not None:
            audit_payload["reason"] = reason
        if dangerous is not None:
            audit_payload["dangerous"] = dangerous
        content_summary = cls._content_summary(content)
        if content_summary is not None:
            audit_payload.update(content_summary)
        normalized_response, status_code = cls._normalize_response(response)
        if normalized_response is not None:
            audit_payload["response"] = normalized_response
        if status_code is not None:
            audit_payload["status_code"] = status_code
        logger.warning(
            "matrix_api_write_audit",
            agent=context.agent_name,
            user_id=context.requester_id,
            room_id=room_id,
            action=action,
            status=status,
            event_type=event_type,
            event_id=target_event_id,
            state_key=state_key,
            reason=reason,
            dangerous=dangerous,
            **(content_summary or {}),
            response=normalized_response,
            status_code=status_code,
        )

    @classmethod
    def _state_write_policy_error(
        cls,
        *,
        action: str,
        room_id: str,
        event_type: str,
        state_key: str,
        allow_dangerous: bool,
    ) -> tuple[str | None, bool]:
        if event_type in cls._HARD_BLOCKED_STATE_TYPES:
            return (
                cls._error_payload(
                    action=action,
                    room_id=room_id,
                    event_type=event_type,
                    state_key=state_key,
                    message=f"State event type '{event_type}' is blocked by matrix_api.",
                ),
                False,
            )
        dangerous = event_type in cls._DANGEROUS_STATE_TYPES
        if dangerous and not allow_dangerous:
            return (
                cls._error_payload(
                    action=action,
                    room_id=room_id,
                    event_type=event_type,
                    state_key=state_key,
                    dangerous=True,
                    message=(
                        f"State event type '{event_type}' is dangerous. "
                        "Re-run with allow_dangerous=true only when you intentionally want to change critical room state."
                    ),
                ),
                True,
            )
        return None, dangerous

    @classmethod
    def _send_event_policy_error(
        cls,
        *,
        room_id: str,
        event_type: str,
    ) -> str | None:
        if event_type == "m.room.redaction":
            return cls._error_payload(
                action="send_event",
                room_id=room_id,
                event_type=event_type,
                message="Event type 'm.room.redaction' must use redact instead of send_event.",
            )
        if event_type in cls._HARD_BLOCKED_STATE_TYPES:
            return cls._error_payload(
                action="send_event",
                room_id=room_id,
                event_type=event_type,
                message=f"Event type '{event_type}' is blocked by matrix_api.",
            )
        if event_type in cls._DANGEROUS_STATE_TYPES:
            return cls._error_payload(
                action="send_event",
                room_id=room_id,
                event_type=event_type,
                dangerous=True,
                message=(
                    f"Event type '{event_type}' is dangerous room state and cannot be sent with send_event. "
                    "Use put_state instead."
                ),
            )
        return None

    @staticmethod
    async def _requires_conversation_cache_write(
        context: ToolRuntimeContext,
        *,
        room_id: str,
        event_type: str,
        content: dict[str, object],
    ) -> bool:
        """Return whether one send_event payload must update threaded conversation cache state."""
        if event_type != "m.room.message":
            return False
        event_info = EventInfo.from_event({"type": event_type, "content": content})
        if isinstance(event_info.thread_id, str) or isinstance(event_info.thread_id_from_edit, str):
            return True
        if not event_info.is_edit or not isinstance(event_info.original_event_id, str):
            return False
        try:
            return isinstance(
                await context.event_cache.get_thread_id_for_event(room_id, event_info.original_event_id),
                str,
            )
        except Exception as exc:
            logger.warning(
                "Failed to resolve edit target thread mapping for matrix_api send_event",
                room_id=room_id,
                original_event_id=event_info.original_event_id,
                error=str(exc),
            )
            return True

    @staticmethod
    async def _redaction_requires_conversation_cache_write(
        context: ToolRuntimeContext,
        *,
        room_id: str,
        event_id: str,
    ) -> bool:
        """Return whether one redact payload must update threaded conversation cache state."""
        try:
            return isinstance(await context.event_cache.get_thread_id_for_event(room_id, event_id), str)
        except Exception as exc:
            logger.warning(
                "Failed to resolve redaction target thread mapping for matrix_api redact",
                room_id=room_id,
                target_event_id=event_id,
                error=str(exc),
            )
            return True

    @staticmethod
    async def _record_send_event_outbound_cache_write(
        context: ToolRuntimeContext,
        *,
        room_id: str,
        event_type: str,
        event_id: str,
        content: dict[str, object],
    ) -> None:
        """Record a successful threaded room-message send in the local conversation cache."""
        if event_type != "m.room.message" or context.conversation_cache is None:
            return
        await context.conversation_cache.record_outbound_message(
            room_id,
            event_id,
            content,
        )

    async def _send_event(  # noqa: C901,PLR0911
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
        event_type: str | None,
        content: dict[str, object] | None,
        dry_run: bool,
    ) -> str:
        normalized_event_type, event_type_error = self._validate_non_empty_string(
            event_type,
            field_name="event_type",
        )
        if event_type_error is not None:
            return self._error_payload(
                action="send_event",
                room_id=room_id,
                message=event_type_error,
            )
        normalized_content, content_error = self._validate_content(content)
        if content_error is not None:
            return self._error_payload(
                action="send_event",
                room_id=room_id,
                event_type=normalized_event_type,
                message=content_error,
            )

        assert normalized_event_type is not None
        assert normalized_content is not None

        if (
            policy_error := self._send_event_policy_error(room_id=room_id, event_type=normalized_event_type)
        ) is not None:
            return policy_error
        requires_conversation_cache_write = await self._requires_conversation_cache_write(
            context,
            room_id=room_id,
            event_type=normalized_event_type,
            content=normalized_content,
        )
        if requires_conversation_cache_write and context.conversation_cache is None:
            return self._error_payload(
                action="send_event",
                room_id=room_id,
                event_type=normalized_event_type,
                message="Conversation cache is required for threaded Matrix message sends.",
            )

        if dry_run:
            return self._payload(
                "ok",
                action="send_event",
                room_id=room_id,
                event_type=normalized_event_type,
                dry_run=True,
                would_send={
                    "event_type": normalized_event_type,
                    "content": normalized_content,
                },
            )

        if (limit_error := self._check_rate_limit(context, room_id, action="send_event")) is not None:
            return self._error_payload(
                action="send_event",
                room_id=room_id,
                event_type=normalized_event_type,
                message=limit_error,
            )

        try:
            if normalized_event_type == "m.room.message":
                delivered = await send_message_result(
                    context.client,
                    room_id,
                    dict(normalized_content),
                )
                if delivered is None:
                    response: object = None
                else:
                    normalized_content = delivered.content_sent
                    response = nio.RoomSendResponse(
                        event_id=delivered.event_id,
                        room_id=room_id,
                    )
            else:
                response = await context.client.room_send(
                    room_id=room_id,
                    message_type=normalized_event_type,
                    content=normalized_content,
                )
        except Exception as exc:
            self._audit_write(
                context=context,
                room_id=room_id,
                action="send_event",
                status="error",
                event_type=normalized_event_type,
                content=normalized_content,
                response=exc,
            )
            return self._error_payload(
                action="send_event",
                room_id=room_id,
                event_type=normalized_event_type,
                message="Failed to send Matrix event.",
                response=exc,
            )

        if isinstance(response, nio.RoomSendResponse):
            await self._record_send_event_outbound_cache_write(
                context,
                room_id=room_id,
                event_type=normalized_event_type,
                event_id=response.event_id,
                content=normalized_content,
            )
            self._audit_write(
                context=context,
                room_id=room_id,
                action="send_event",
                status="ok",
                event_type=normalized_event_type,
                content=normalized_content,
            )
            return self._payload(
                "ok",
                action="send_event",
                room_id=room_id,
                event_type=normalized_event_type,
                event_id=response.event_id,
            )

        self._audit_write(
            context=context,
            room_id=room_id,
            action="send_event",
            status="error",
            event_type=normalized_event_type,
            content=normalized_content,
            response=response,
        )
        return self._error_payload(
            action="send_event",
            room_id=room_id,
            event_type=normalized_event_type,
            message="Failed to send Matrix event.",
            response=response,
        )

    async def _get_state(  # noqa: PLR0911
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
        event_type: str | None,
        state_key: str | None,
    ) -> str:
        normalized_event_type, event_type_error = self._validate_non_empty_string(
            event_type,
            field_name="event_type",
        )
        if event_type_error is not None:
            return self._error_payload(
                action="get_state",
                room_id=room_id,
                message=event_type_error,
            )
        resolved_state_key, state_key_error = self._resolve_state_key(state_key)
        if state_key_error is not None:
            return self._error_payload(
                action="get_state",
                room_id=room_id,
                event_type=normalized_event_type,
                message=state_key_error,
            )

        assert normalized_event_type is not None

        try:
            response = await context.client.room_get_state_event(
                room_id=room_id,
                event_type=normalized_event_type,
                state_key=resolved_state_key,
            )
        except Exception as exc:
            return self._error_payload(
                action="get_state",
                room_id=room_id,
                event_type=normalized_event_type,
                state_key=resolved_state_key,
                message="Failed to fetch Matrix state event.",
                response=exc,
            )

        if isinstance(response, nio.RoomGetStateEventError) and response.status_code == "M_NOT_FOUND":
            return self._payload(
                "ok",
                action="get_state",
                room_id=room_id,
                event_type=normalized_event_type,
                state_key=resolved_state_key,
                found=False,
            )
        if isinstance(response, nio.RoomGetStateEventResponse):
            if not isinstance(response.content, dict):
                return self._error_payload(
                    action="get_state",
                    room_id=room_id,
                    event_type=normalized_event_type,
                    state_key=resolved_state_key,
                    message="Matrix returned malformed state content.",
                    response=response,
                )
            return self._payload(
                "ok",
                action="get_state",
                room_id=room_id,
                event_type=normalized_event_type,
                state_key=resolved_state_key,
                found=True,
                content=response.content,
            )
        return self._error_payload(
            action="get_state",
            room_id=room_id,
            event_type=normalized_event_type,
            state_key=resolved_state_key,
            message="Failed to fetch Matrix state event.",
            response=response,
        )

    async def _put_state(  # noqa: PLR0911
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
        event_type: str | None,
        state_key: str | None,
        content: dict[str, object] | None,
        dry_run: bool,
        allow_dangerous: bool,
    ) -> str:
        normalized_event_type, event_type_error = self._validate_non_empty_string(
            event_type,
            field_name="event_type",
        )
        if event_type_error is not None:
            return self._error_payload(
                action="put_state",
                room_id=room_id,
                message=event_type_error,
            )
        resolved_state_key, state_key_error = self._resolve_state_key(state_key)
        if state_key_error is not None:
            return self._error_payload(
                action="put_state",
                room_id=room_id,
                event_type=normalized_event_type,
                message=state_key_error,
            )
        normalized_content, content_error = self._validate_content(content)
        if content_error is not None:
            return self._error_payload(
                action="put_state",
                room_id=room_id,
                event_type=normalized_event_type,
                state_key=resolved_state_key,
                message=content_error,
            )

        assert normalized_event_type is not None
        assert normalized_content is not None

        policy_error, dangerous = self._state_write_policy_error(
            action="put_state",
            room_id=room_id,
            event_type=normalized_event_type,
            state_key=resolved_state_key,
            allow_dangerous=allow_dangerous,
        )
        if policy_error is not None:
            return policy_error

        if dry_run:
            return self._payload(
                "ok",
                action="put_state",
                room_id=room_id,
                event_type=normalized_event_type,
                state_key=resolved_state_key,
                dry_run=True,
                dangerous=dangerous,
                would_put={
                    "event_type": normalized_event_type,
                    "state_key": resolved_state_key,
                    "content": normalized_content,
                },
            )

        if (limit_error := self._check_rate_limit(context, room_id, action="put_state")) is not None:
            return self._error_payload(
                action="put_state",
                room_id=room_id,
                event_type=normalized_event_type,
                state_key=resolved_state_key,
                message=limit_error,
            )

        try:
            response = await context.client.room_put_state(
                room_id=room_id,
                event_type=normalized_event_type,
                state_key=resolved_state_key,
                content=normalized_content,
            )
        except Exception as exc:
            self._audit_write(
                context=context,
                room_id=room_id,
                action="put_state",
                status="error",
                event_type=normalized_event_type,
                state_key=resolved_state_key,
                dangerous=dangerous,
                content=normalized_content,
                response=exc,
            )
            return self._error_payload(
                action="put_state",
                room_id=room_id,
                event_type=normalized_event_type,
                state_key=resolved_state_key,
                message="Failed to write Matrix state event.",
                response=exc,
            )

        if isinstance(response, nio.RoomPutStateResponse):
            self._audit_write(
                context=context,
                room_id=room_id,
                action="put_state",
                status="ok",
                event_type=normalized_event_type,
                state_key=resolved_state_key,
                dangerous=dangerous,
                content=normalized_content,
            )
            return self._payload(
                "ok",
                action="put_state",
                room_id=room_id,
                event_type=normalized_event_type,
                state_key=resolved_state_key,
                event_id=response.event_id,
            )

        self._audit_write(
            context=context,
            room_id=room_id,
            action="put_state",
            status="error",
            event_type=normalized_event_type,
            state_key=resolved_state_key,
            dangerous=dangerous,
            content=normalized_content,
            response=response,
        )
        return self._error_payload(
            action="put_state",
            room_id=room_id,
            event_type=normalized_event_type,
            state_key=resolved_state_key,
            message="Failed to write Matrix state event.",
            response=response,
        )

    async def _redact(  # noqa: PLR0911
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
        event_id: str | None,
        reason: str | None,
        dry_run: bool,
    ) -> str:
        normalized_event_id, event_id_error = self._validate_non_empty_string(
            event_id,
            field_name="event_id",
        )
        normalized_reason = reason.strip() if isinstance(reason, str) and reason.strip() else None
        error_message = event_id_error if event_id_error is not None else None
        if error_message is not None:
            return self._error_payload(
                action="redact",
                room_id=room_id,
                message=error_message,
            )

        assert normalized_event_id is not None

        requires_conversation_cache_write = await self._redaction_requires_conversation_cache_write(
            context,
            room_id=room_id,
            event_id=normalized_event_id,
        )
        if requires_conversation_cache_write and context.conversation_cache is None:
            error_message = "Conversation cache is required for threaded Matrix message redactions."

        if dry_run:
            if error_message is not None:
                return self._error_payload(
                    action="redact",
                    room_id=room_id,
                    target_event_id=normalized_event_id,
                    message=error_message,
                )
            return self._payload(
                "ok",
                action="redact",
                room_id=room_id,
                target_event_id=normalized_event_id,
                reason=normalized_reason,
                dry_run=True,
                would_redact={
                    "event_id": normalized_event_id,
                    "reason": normalized_reason,
                },
            )

        if (limit_error := self._check_rate_limit(context, room_id, action="redact")) is not None:
            error_message = limit_error

        if error_message is not None:
            return self._error_payload(
                action="redact",
                room_id=room_id,
                target_event_id=normalized_event_id,
                message=error_message,
            )

        try:
            response = await context.client.room_redact(
                room_id=room_id,
                event_id=normalized_event_id,
                reason=normalized_reason,
            )
        except Exception as exc:
            self._audit_write(
                context=context,
                room_id=room_id,
                action="redact",
                status="error",
                target_event_id=normalized_event_id,
                reason=normalized_reason,
                response=exc,
            )
            return self._error_payload(
                action="redact",
                room_id=room_id,
                target_event_id=normalized_event_id,
                message="Failed to redact Matrix event.",
                response=exc,
            )

        if isinstance(response, nio.RoomRedactResponse):
            if context.conversation_cache is not None:
                await context.conversation_cache.record_outbound_redaction(
                    room_id,
                    normalized_event_id,
                )
            self._audit_write(
                context=context,
                room_id=room_id,
                action="redact",
                status="ok",
                target_event_id=normalized_event_id,
                reason=normalized_reason,
            )
            return self._payload(
                "ok",
                action="redact",
                room_id=room_id,
                target_event_id=normalized_event_id,
                reason=normalized_reason,
                redaction_event_id=response.event_id,
            )

        self._audit_write(
            context=context,
            room_id=room_id,
            action="redact",
            status="error",
            target_event_id=normalized_event_id,
            reason=normalized_reason,
            response=response,
        )
        return self._error_payload(
            action="redact",
            room_id=room_id,
            target_event_id=normalized_event_id,
            message="Failed to redact Matrix event.",
            response=response,
        )

    async def _get_event(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
        event_id: str | None,
    ) -> str:
        normalized_event_id, event_id_error = self._validate_non_empty_string(
            event_id,
            field_name="event_id",
        )
        if event_id_error is not None:
            return self._error_payload(
                action="get_event",
                room_id=room_id,
                message=event_id_error,
            )

        assert normalized_event_id is not None

        try:
            response = await context.client.room_get_event(room_id, normalized_event_id)
        except Exception as exc:
            return self._error_payload(
                action="get_event",
                room_id=room_id,
                event_id=normalized_event_id,
                message="Failed to fetch Matrix event.",
                response=exc,
            )

        if isinstance(response, nio.RoomGetEventError) and response.status_code == "M_NOT_FOUND":
            return self._payload(
                "ok",
                action="get_event",
                room_id=room_id,
                event_id=normalized_event_id,
                found=False,
            )
        if isinstance(response, nio.RoomGetEventResponse):
            raw_event = response.event.source
            if not isinstance(raw_event, dict):
                return self._error_payload(
                    action="get_event",
                    room_id=room_id,
                    event_id=normalized_event_id,
                    message="Matrix returned malformed event data.",
                    response=response,
                )
            payload: dict[str, object] = {
                "action": "get_event",
                "room_id": room_id,
                "event_id": normalized_event_id,
                "found": True,
                "event": raw_event,
            }
            if "type" in raw_event:
                payload["event_type"] = raw_event["type"]
            if "sender" in raw_event:
                payload["sender"] = raw_event["sender"]
            if "origin_server_ts" in raw_event:
                payload["origin_server_ts"] = raw_event["origin_server_ts"]
            return self._payload("ok", **payload)
        return self._error_payload(
            action="get_event",
            room_id=room_id,
            event_id=normalized_event_id,
            message="Failed to fetch Matrix event.",
            response=response,
        )

    async def matrix_api(  # noqa: C901, PLR0911
        self,
        action: str = "send_event",
        room_id: str | None = None,
        event_type: str | None = None,
        content: dict[str, object] | None = None,
        state_key: str | None = None,
        event_id: str | None = None,
        reason: str | None = None,
        dry_run: bool = False,
        allow_dangerous: bool = False,
    ) -> str:
        """Access a small low-level Matrix API surface with room context defaults.

        Actions:
        - send_event: Send an arbitrary room event with `event_type` and `content`.
        - get_state: Read one state event by `event_type` and optional `state_key`.
        - put_state: Write one state event by `event_type`, optional `state_key`, and `content`.
        - redact: Redact an event by `event_id`.
        - get_event: Fetch one event by `event_id`.

        `room_id` defaults to the current Matrix tool runtime context room.
        `dry_run` is supported for send_event, put_state, and redact.
        `allow_dangerous` only affects put_state for a small set of high-risk room-state event types.
        """
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()

        normalized_action = self._normalize_action(action)
        if normalized_action not in self._VALID_ACTIONS_SET:
            return self._error_payload(
                action=normalized_action or str(action),
                message=self._supported_actions_message(),
            )

        normalized_dry_run, dry_run_error = self._validate_bool(dry_run, field_name="dry_run")
        if dry_run_error is not None:
            return self._error_payload(
                action=normalized_action,
                message=dry_run_error,
            )

        normalized_allow_dangerous, allow_dangerous_error = self._validate_bool(
            allow_dangerous,
            field_name="allow_dangerous",
        )
        if allow_dangerous_error is not None:
            return self._error_payload(
                action=normalized_action,
                message=allow_dangerous_error,
            )

        resolved_room_id, room_id_error = self._resolve_room_id(context, room_id)
        if room_id_error is not None:
            room_id_payload: dict[str, object] = {}
            if isinstance(room_id, str):
                room_id_payload["room_id"] = room_id.strip()
            return self._error_payload(
                action=normalized_action,
                message=room_id_error,
                **room_id_payload,
            )

        assert normalized_dry_run is not None
        assert normalized_allow_dangerous is not None
        assert resolved_room_id is not None

        if not room_access_allowed(context, resolved_room_id):
            return self._error_payload(
                action=normalized_action,
                room_id=resolved_room_id,
                message="Not authorized to access the target room.",
            )

        if normalized_action == "send_event":
            return await self._send_event(
                context,
                room_id=resolved_room_id,
                event_type=event_type,
                content=content,
                dry_run=normalized_dry_run,
            )
        if normalized_action == "get_state":
            return await self._get_state(
                context,
                room_id=resolved_room_id,
                event_type=event_type,
                state_key=state_key,
            )
        if normalized_action == "put_state":
            return await self._put_state(
                context,
                room_id=resolved_room_id,
                event_type=event_type,
                state_key=state_key,
                content=content,
                dry_run=normalized_dry_run,
                allow_dangerous=normalized_allow_dangerous,
            )
        if normalized_action == "redact":
            return await self._redact(
                context,
                room_id=resolved_room_id,
                event_id=event_id,
                reason=reason,
                dry_run=normalized_dry_run,
            )
        return await self._get_event(
            context,
            room_id=resolved_room_id,
            event_id=event_id,
        )
