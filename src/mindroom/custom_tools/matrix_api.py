"""Low-level Matrix room/event/state API tool for agents."""

from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock
from typing import Any, ClassVar

import nio
from agno.tools import Toolkit

from mindroom.custom_tools.attachment_helpers import room_access_allowed
from mindroom.logging_config import get_logger
from mindroom.matrix.thread_bookkeeping import (
    MutationThreadImpactState,
    resolve_event_thread_impact_for_client,
    resolve_redaction_thread_impact_for_client,
)
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context

logger = get_logger(__name__)


class MatrixSearchError(nio.ErrorResponse):
    """Matrix search request failed or returned malformed data."""


@dataclass
class MatrixSearchResponse(nio.Response):
    """Parsed subset of Matrix room-event search results."""

    count: int
    next_batch: str | None
    results: list[dict[str, object]]

    @staticmethod
    def _malformed_response_error() -> MatrixSearchError:
        return MatrixSearchError("Malformed Matrix search response.")

    @staticmethod
    def _matrix_error_from_dict(parsed_dict: dict[Any, Any]) -> MatrixSearchError:
        error_response = nio.ErrorResponse.from_dict(parsed_dict)
        return MatrixSearchError(
            error_response.message,
            error_response.status_code,
            error_response.retry_after_ms,
            error_response.soft_logout,
        )

    @classmethod
    def from_dict(
        cls,
        parsed_dict: dict[Any, Any],
    ) -> MatrixSearchResponse | MatrixSearchError:
        """Parse one Matrix search payload or normalize one Matrix error payload."""
        if not isinstance(parsed_dict, dict):
            return cls._malformed_response_error()

        search_categories = parsed_dict.get("search_categories")
        if search_categories is None:
            return cls._matrix_error_from_dict(parsed_dict)
        if not isinstance(search_categories, dict):
            return cls._malformed_response_error()

        room_events = search_categories.get("room_events")
        if not isinstance(room_events, dict):
            return cls._malformed_response_error()

        count = room_events.get("count")
        next_batch = room_events.get("next_batch")
        results = room_events.get("results", [])
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or (next_batch is not None and not isinstance(next_batch, str))
            or not isinstance(results, list)
            or any(not isinstance(result, dict) for result in results)
        ):
            return cls._malformed_response_error()

        return cls(
            count=count,
            next_batch=next_batch,
            results=results,
        )


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
        "search",
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
    _SEARCH_ALLOWED_KEYS: ClassVar[tuple[str, ...]] = (
        "content.body",
        "content.name",
        "content.topic",
    )
    _SEARCH_ALLOWED_ORDER_BY: ClassVar[frozenset[str]] = frozenset({"rank", "recent"})
    _SEARCH_LIMIT_CAP: ClassVar[int] = 50
    _SEARCH_SNIPPET_MAX_CHARS: ClassVar[int] = 200

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
            nio.ErrorResponse,
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
        response: nio.ErrorResponse,
    ) -> tuple[str, str | None]:
        return str(response), response.status_code

    @classmethod
    def _supported_actions_message(cls) -> str:
        return "Unsupported action. Use send_event, get_state, put_state, redact, get_event, or search."

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
        value: object,
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

    @staticmethod
    def _copy_string_keyed_dict(value: object) -> dict[str, object] | None:
        if not isinstance(value, dict):
            return None

        normalized_value: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                return None
            normalized_value[key] = item
        return normalized_value

    @staticmethod
    def _validate_string_list(
        value: object,
        *,
        field_name: str,
    ) -> list[str]:
        error_message = f"{field_name} must be a list of non-empty strings."
        if not isinstance(value, list):
            raise ValueError(error_message)  # noqa: TRY004

        normalized_items: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(error_message)
            normalized_items.append(item.strip())
        return normalized_items

    @classmethod
    def _validate_search_order_by(cls, order_by: object) -> str:
        error_message = "order_by must be one of: rank, recent."
        normalized_order_by = order_by.strip().lower() if isinstance(order_by, str) else ""
        if normalized_order_by not in cls._SEARCH_ALLOWED_ORDER_BY:
            raise ValueError(error_message)
        return normalized_order_by

    @classmethod
    def _validate_search_keys(cls, keys: object) -> list[str]:
        normalized_keys = cls._validate_string_list(keys, field_name="keys")
        invalid_keys = [key for key in normalized_keys if key not in cls._SEARCH_ALLOWED_KEYS]
        if invalid_keys:
            allowed_keys = ", ".join(cls._SEARCH_ALLOWED_KEYS)
            error_message = f"keys entries must be one of: {allowed_keys}."
            raise ValueError(error_message)
        return normalized_keys

    @classmethod
    def _validate_search_limit(cls, limit: object) -> int:
        error_message = f"limit must be an integer between 1 and {cls._SEARCH_LIMIT_CAP}."
        if not isinstance(limit, int) or isinstance(limit, bool) or not 0 < limit <= cls._SEARCH_LIMIT_CAP:
            raise ValueError(error_message)
        return limit

    @staticmethod
    def _validate_optional_dict(
        value: object,
        *,
        field_name: str,
    ) -> dict[str, object] | None:
        if value is None:
            return None
        normalized_value = MatrixApiTools._copy_string_keyed_dict(value)
        if normalized_value is None:
            error_message = f"{field_name} must be a JSON object (dict) when provided."
            raise ValueError(error_message)
        return normalized_value

    @staticmethod
    def _validate_optional_string(
        value: object,
        *,
        field_name: str,
    ) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            error_message = f"{field_name} must be omitted or a non-empty string."
            raise ValueError(error_message)
        return value.strip()

    @classmethod
    def _truncate_snippet(cls, text: object) -> str:
        if not isinstance(text, str):
            return ""
        snippet = " ".join(text.split())
        if len(snippet) <= cls._SEARCH_SNIPPET_MAX_CHARS:
            return snippet
        return f"{snippet[: cls._SEARCH_SNIPPET_MAX_CHARS - 3].rstrip()}..."

    @classmethod
    def _search_snippet_text(cls, content: dict[str, object]) -> str:
        for key in ("body", "name", "topic"):
            value = content.get(key)
            if isinstance(value, str):
                return cls._truncate_snippet(value)
        return ""

    @classmethod
    def _normalize_search_event_payload(
        cls,
        raw_event: object,
    ) -> dict[str, object]:
        raw_event_dict = cls._copy_string_keyed_dict(raw_event) or {}
        event_id = raw_event_dict.get("event_id")
        event_room_id = raw_event_dict.get("room_id")
        sender = raw_event_dict.get("sender")
        event_type = raw_event_dict.get("type")
        content = raw_event_dict.get("content")
        normalized_content = cls._copy_string_keyed_dict(content) or {}
        origin_server_ts = raw_event_dict.get("origin_server_ts")
        return {
            "event_id": event_id if isinstance(event_id, str) else "",
            "room_id": event_room_id if isinstance(event_room_id, str) else "",
            "sender": sender if isinstance(sender, str) else "",
            "origin_server_ts": origin_server_ts if isinstance(origin_server_ts, int) else 0,
            "type": event_type if isinstance(event_type, str) else "",
            "snippet": cls._search_snippet_text(normalized_content),
        }

    @classmethod
    def _normalize_search_context_payload(
        cls,
        raw_context: object,
    ) -> dict[str, object] | None:
        context_dict = cls._copy_string_keyed_dict(raw_context)
        if context_dict is None:
            return None

        events_before = context_dict.get("events_before")
        events_after = context_dict.get("events_after")
        normalized_context: dict[str, object] = {
            "events_before": (
                [cls._normalize_search_event_payload(event) for event in events_before]
                if isinstance(events_before, list)
                else []
            ),
            "events_after": (
                [cls._normalize_search_event_payload(event) for event in events_after]
                if isinstance(events_after, list)
                else []
            ),
        }
        if isinstance(context_dict.get("start"), str):
            normalized_context["start"] = context_dict["start"]
        if isinstance(context_dict.get("end"), str):
            normalized_context["end"] = context_dict["end"]
        profile_info = cls._copy_string_keyed_dict(context_dict.get("profile_info"))
        if profile_info is not None:
            normalized_context["profile_info"] = {
                user_id: normalized_profile
                for user_id, raw_profile in profile_info.items()
                if (normalized_profile := cls._copy_string_keyed_dict(raw_profile)) is not None
            }
        return normalized_context

    @classmethod
    def _build_search_filter(
        cls,
        *,
        room_id: str,
        raw_filter: dict[str, object] | None,
        limit: int,
    ) -> dict[str, object]:
        if raw_filter is None:
            return {"rooms": [room_id], "limit": limit}

        filter_payload = dict(raw_filter)
        rooms = filter_payload.get("rooms")
        if rooms is None:
            filter_payload["rooms"] = [room_id]
        elif (
            not isinstance(rooms, list)
            or any(not isinstance(entry, str) for entry in rooms)
            or any(entry != room_id for entry in rooms)
        ):
            error_message = "filter.rooms must be omitted or contain only the target room_id."
            raise ValueError(error_message)

        if "limit" in filter_payload:
            error_message = "filter.limit is not supported; use the top-level limit parameter."
            raise ValueError(error_message)
        filter_payload["limit"] = limit
        return filter_payload

    @classmethod
    def _build_search_request_body(
        cls,
        *,
        room_id: str,
        search_term: object,
        keys: object,
        order_by: object,
        limit: object,
        search_filter: object,
        event_context: object,
    ) -> dict[str, object]:
        normalized_search_term, search_term_error = cls._validate_non_empty_string(
            search_term,
            field_name="search_term",
        )
        if search_term_error is not None:
            raise ValueError(search_term_error)

        resolved_order_by = cls._validate_search_order_by(order_by)
        resolved_limit = cls._validate_search_limit(limit)
        resolved_filter = cls._validate_optional_dict(search_filter, field_name="filter")
        resolved_event_context = cls._validate_optional_dict(event_context, field_name="event_context")

        room_events: dict[str, object] = {
            "search_term": normalized_search_term,
            "filter": cls._build_search_filter(room_id=room_id, raw_filter=resolved_filter, limit=resolved_limit),
            "order_by": resolved_order_by,
        }
        if keys is not None:
            room_events["keys"] = cls._validate_search_keys(keys)
        if resolved_event_context is not None:
            room_events["event_context"] = resolved_event_context
        return {"search_categories": {"room_events": room_events}}

    @classmethod
    def _build_search_path(cls, next_batch: object) -> str:
        resolved_next_batch = cls._validate_optional_string(next_batch, field_name="next_batch")
        if resolved_next_batch is None:
            return nio.Api._build_path(["search"])
        return nio.Api._build_path(["search"], {"next_batch": resolved_next_batch})

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
    async def _record_send_event_outbound_cache_write(
        context: ToolRuntimeContext,
        *,
        room_id: str,
        event_type: str,
        event_id: str,
        content: dict[str, object],
        requires_conversation_cache_write: bool,
    ) -> None:
        """Record a successful threaded room-message send in the local conversation cache."""
        if event_type != "m.room.message" or not requires_conversation_cache_write:
            return
        context.conversation_cache.notify_outbound_message(
            room_id,
            event_id,
            content,
        )

    @staticmethod
    async def _resolve_redaction_cache_write_requirement(
        context: ToolRuntimeContext,
        *,
        room_id: str,
        event_id: str,
    ) -> tuple[bool, str | None]:
        """Return redaction bookkeeping intent plus an optional fail-closed error."""
        try:
            thread_impact = await resolve_redaction_thread_impact_for_client(
                context.client,
                room_id=room_id,
                event_id=event_id,
                conversation_cache=context.conversation_cache,
            )
        except Exception as exc:
            logger.warning(
                "Failed to resolve redaction target thread mapping for matrix_api redact",
                room_id=room_id,
                target_event_id=event_id,
                error=str(exc),
            )
            return False, "Failed to resolve redaction target thread mapping."

        if thread_impact.state is MutationThreadImpactState.UNKNOWN:
            logger.warning(
                "Failed to resolve redaction target thread mapping for matrix_api redact",
                room_id=room_id,
                target_event_id=event_id,
                error="thread impact unknown",
            )
            return False, "Failed to resolve redaction target thread mapping."

        return thread_impact.state is MutationThreadImpactState.THREADED, None

    async def _send_event(  # noqa: PLR0911
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

        try:
            thread_impact = await resolve_event_thread_impact_for_client(
                context.client,
                room_id=room_id,
                event_type=normalized_event_type,
                content=normalized_content,
                conversation_cache=context.conversation_cache,
            )
            if thread_impact.state is MutationThreadImpactState.UNKNOWN:
                return self._error_payload(
                    action="send_event",
                    room_id=room_id,
                    event_type=normalized_event_type,
                    message="Failed to resolve threaded Matrix message send target.",
                )
            requires_conversation_cache_write = thread_impact.state is MutationThreadImpactState.THREADED
        except Exception as exc:
            logger.warning(
                "Failed to resolve threaded send_event target for matrix_api",
                room_id=room_id,
                event_type=normalized_event_type,
                error=str(exc),
            )
            return self._error_payload(
                action="send_event",
                room_id=room_id,
                event_type=normalized_event_type,
                message="Failed to resolve threaded Matrix message send target.",
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
                requires_conversation_cache_write=requires_conversation_cache_write,
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

        (
            requires_conversation_cache_write,
            thread_resolution_error,
        ) = await self._resolve_redaction_cache_write_requirement(
            context,
            room_id=room_id,
            event_id=normalized_event_id,
        )
        if thread_resolution_error is not None:
            error_message = thread_resolution_error

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
            if requires_conversation_cache_write:
                context.conversation_cache.notify_outbound_redaction(
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

    async def _search(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
        search_term: object,
        keys: object,
        order_by: object,
        limit: object,
        next_batch: object,
        search_filter: object,
        event_context: object,
    ) -> str:
        try:
            request_body = self._build_search_request_body(
                room_id=room_id,
                search_term=search_term,
                keys=keys,
                order_by=order_by,
                limit=limit,
                search_filter=search_filter,
                event_context=event_context,
            )
            path = self._build_search_path(next_batch)
        except ValueError as exc:
            return self._error_payload(
                action="search",
                room_id=room_id,
                message=str(exc),
            )

        method = "POST"

        try:
            response = await context.client._send(
                MatrixSearchResponse,
                method,
                path,
                nio.Api.to_json(request_body),
            )
        except Exception as exc:
            return self._error_payload(
                action="search",
                room_id=room_id,
                message="Failed to search Matrix room events.",
                response=exc,
            )

        if isinstance(response, MatrixSearchResponse):
            normalized_results: list[dict[str, object]] = []
            include_context = event_context is not None
            for raw_result in response.results:
                event_payload = self._normalize_search_event_payload(raw_result.get("result"))
                rank = raw_result.get("rank")
                normalized_result: dict[str, object] = {
                    "rank": float(rank) if isinstance(rank, (int, float)) else 0.0,
                    **event_payload,
                }
                if include_context:
                    context_payload = self._normalize_search_context_payload(raw_result.get("context"))
                    if context_payload is not None:
                        normalized_result["context"] = context_payload
                normalized_results.append(normalized_result)

            return self._payload(
                "ok",
                action="search",
                room_id=room_id,
                count=response.count,
                next_batch=response.next_batch,
                results=normalized_results,
            )

        return self._error_payload(
            action="search",
            room_id=room_id,
            message="Failed to search Matrix room events.",
            response=response,
        )

    async def matrix_api(  # noqa: C901, PLR0911, PLR0912
        self,
        action: str = "send_event",
        room_id: str | None = None,
        event_type: str | None = None,
        content: dict[str, object] | None = None,
        state_key: str | None = None,
        event_id: str | None = None,
        reason: str | None = None,
        search_term: str | None = None,
        keys: list[str] | None = None,
        order_by: str = "rank",
        limit: int = 10,
        next_batch: str | None = None,
        filter: dict[str, object] | None = None,  # noqa: A002
        event_context: dict[str, object] | None = None,
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
        - search: Full-text search one room's events with `search_term`, optional `keys`, pagination, and context.

        `room_id` defaults to the current Matrix tool runtime context room.
        `search` enforces a single-room scope via `room_id`; if `filter.rooms` is supplied it must match that room.
        `search` always uses the top-level `limit`; `filter.limit` is rejected to avoid conflicting inputs.
        `dry_run` is supported for send_event, put_state, and redact.
        `allow_dangerous` only affects put_state for a small set of high-risk room-state event types.
        `search` rejects `dry_run` and `allow_dangerous` because it is read-only.
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

        if normalized_action == "search" and (normalized_dry_run or normalized_allow_dangerous):
            return self._error_payload(
                action="search",
                room_id=resolved_room_id,
                message="dry_run/allow_dangerous not applicable to read-only search action",
            )

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
        if normalized_action == "get_event":
            return await self._get_event(
                context,
                room_id=resolved_room_id,
                event_id=event_id,
            )
        return await self._search(
            context,
            room_id=resolved_room_id,
            search_term=search_term,
            keys=keys,
            order_by=order_by,
            limit=limit,
            next_batch=next_batch,
            search_filter=filter,
            event_context=event_context,
        )
