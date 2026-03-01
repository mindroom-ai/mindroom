"""Standalone sub-agent session orchestration toolkit."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from threading import Lock
from typing import TYPE_CHECKING, Any, cast

from agno.tools import Toolkit

from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.matrix.client import send_message
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.thread_utils import create_session_id
from mindroom.tool_runtime_context import ToolRuntimeContext, get_tool_runtime_context

if TYPE_CHECKING:
    from pathlib import Path


_REGISTRY_LOCK = Lock()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _now_epoch() -> float:
    return datetime.now(UTC).timestamp()


def _payload(tool_name: str, status: str, **kwargs: object) -> str:
    payload: dict[str, object] = {
        "status": status,
        "tool": tool_name,
    }
    payload.update(kwargs)
    return json.dumps(payload, sort_keys=True)


def _context_error(tool_name: str) -> str:
    return _payload(
        tool_name,
        "error",
        message="Tool runtime context is unavailable in this runtime path.",
    )


def _get_context() -> ToolRuntimeContext | None:
    context = get_tool_runtime_context()
    if context is None or context.storage_path is None:
        return None
    return context


def _registry_path(context: ToolRuntimeContext) -> Path:
    assert context.storage_path is not None
    return context.storage_path / "subagents" / "session_registry.json"


def _normalize_registry(loaded: object) -> dict[str, Any]:
    if not isinstance(loaded, dict):
        return {}
    return cast("dict[str, Any]", loaded)


def _load_registry(context: ToolRuntimeContext) -> dict[str, Any]:
    path = _registry_path(context)
    if not path.is_file():
        return {}

    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}

    loaded = json.loads(raw)
    return _normalize_registry(loaded)


def _save_registry(context: ToolRuntimeContext, registry: dict[str, Any]) -> None:
    path = _registry_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(registry, sort_keys=True, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _coerce_epoch(value: object) -> float:
    epoch = 0.0
    if value is None or isinstance(value, bool):
        return epoch
    if isinstance(value, int | float):
        return float(value)

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return epoch
        try:
            return float(text)
        except ValueError:
            iso_value = f"{text[:-1]}+00:00" if text.endswith("Z") else text
            try:
                parsed = datetime.fromisoformat(iso_value)
            except ValueError:
                return epoch
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            epoch = parsed.timestamp()

    return epoch


def _entry_recency(entry: dict[str, Any]) -> float:
    return max(
        _coerce_epoch(entry.get("updated_at_epoch")),
        _coerce_epoch(entry.get("updated_at")),
        _coerce_epoch(entry.get("created_at_epoch")),
        _coerce_epoch(entry.get("created_at")),
    )


def _bounded_limit(limit: int | None, *, default: int = 50, maximum: int = 200) -> int:
    if limit is None:
        return default
    return max(1, min(limit, maximum))


def _bounded_offset(offset: int | None) -> int:
    if offset is None:
        return 0
    return max(0, offset)


def _session_key_to_room_thread(session_key: str) -> tuple[str, str | None]:
    marker = ":$"
    if marker in session_key:
        room_id, thread_suffix = session_key.rsplit(marker, 1)
        return room_id, f"${thread_suffix}"
    return session_key, None


def _agent_thread_mode(context: ToolRuntimeContext, agent_name: str) -> str:
    resolver = getattr(context.config, "get_entity_thread_mode", None)
    if not callable(resolver):
        return "thread"

    mode = resolver(agent_name)
    return "room" if mode == "room" else "thread"


def _threaded_dispatch_error(
    context: ToolRuntimeContext,
    *,
    session_key: str,
    thread_id: str | None,
    target_agent: str,
) -> str | None:
    if thread_id is None:
        return None
    if _agent_thread_mode(context, target_agent) != "room":
        return None
    return _payload(
        "sessions_send",
        "error",
        session_key=session_key,
        message=(
            f"Threaded session dispatch is not supported for agent '{target_agent}' because it uses thread_mode=room."
        ),
    )


async def _send_matrix_text(
    context: ToolRuntimeContext,
    *,
    room_id: str,
    text: str,
    thread_id: str | None,
    original_sender: str | None = None,
) -> str | None:
    """Send a formatted text message to a Matrix room, optionally in a thread."""
    content = format_message_with_mentions(
        context.config,
        text,
        sender_domain=context.config.domain,
        thread_event_id=thread_id,
    )
    if original_sender:
        content[ORIGINAL_SENDER_KEY] = original_sender
    return await send_message(context.client, room_id, content)


def _record_session(
    context: ToolRuntimeContext,
    *,
    session_key: str,
    label: str | None = None,
    target_agent: str | None = None,
) -> None:
    room_id, thread_id = _session_key_to_room_thread(session_key)
    now_iso = _now_iso()
    now_epoch = _now_epoch()

    with _REGISTRY_LOCK:
        registry = _load_registry(context)
        existing = registry.get(session_key)
        if isinstance(existing, dict):
            if not _in_scope(existing, context):
                return

            existing["agent_name"] = context.agent_name
            existing["room_id"] = room_id
            existing["thread_id"] = thread_id
            existing["requester_id"] = context.requester_id
            existing.setdefault("created_at", now_iso)
            existing.setdefault("created_at_epoch", now_epoch)
            if label is not None:
                existing["label"] = label
            if target_agent is not None:
                existing["target_agent"] = target_agent
            existing["updated_at"] = now_iso
            existing["updated_at_epoch"] = now_epoch
        else:
            registry[session_key] = {
                "label": label,
                "target_agent": target_agent or context.agent_name,
                "agent_name": context.agent_name,
                "room_id": room_id,
                "thread_id": thread_id,
                "requester_id": context.requester_id,
                "created_at": now_iso,
                "created_at_epoch": now_epoch,
                "updated_at": now_iso,
                "updated_at_epoch": now_epoch,
            }
        _save_registry(context, registry)


def _in_scope(entry: dict[str, Any], context: ToolRuntimeContext) -> bool:
    """Check whether a registry entry belongs to the active context scope."""
    return (
        entry.get("agent_name") == context.agent_name
        and entry.get("room_id") == context.room_id
        and entry.get("requester_id") == context.requester_id
    )


def _resolve_by_label(context: ToolRuntimeContext, label: str) -> str | None:
    with _REGISTRY_LOCK:
        registry = _load_registry(context)

    candidates = [
        (key, entry)
        for key, entry in registry.items()
        if isinstance(entry, dict) and entry.get("label") == label and _in_scope(entry, context)
    ]
    if not candidates:
        return None

    candidates.sort(key=lambda item: (_entry_recency(item[1]), item[0]), reverse=True)
    return candidates[0][0]


def _lookup_target_agent(context: ToolRuntimeContext, session_key: str) -> str | None:
    with _REGISTRY_LOCK:
        registry = _load_registry(context)
    entry = registry.get(session_key)
    if isinstance(entry, dict) and _in_scope(entry, context):
        agent = entry.get("target_agent")
        if isinstance(agent, str) and agent:
            return agent
    return None


class SubAgentsTools(Toolkit):
    """Session and sub-agent orchestration tools for any MindRoom agent."""

    def __init__(self) -> None:
        super().__init__(
            name="subagents",
            tools=[
                self.agents_list,
                self.sessions_send,
                self.sessions_spawn,
                self.list_sessions,
            ],
        )

    async def agents_list(self) -> str:
        """List agent ids available for `sessions_spawn` targeting."""
        context = _get_context()
        if context is None:
            return _context_error("agents_list")

        return _payload(
            "agents_list",
            "ok",
            agents=sorted(context.config.agents.keys()),
            current_agent=context.agent_name,
        )

    async def sessions_send(
        self,
        message: str,
        session_key: str | None = None,
        label: str | None = None,
        agent_id: str | None = None,
    ) -> str:
        """Send a message to another session."""
        context = _get_context()
        if context is None:
            return _context_error("sessions_send")

        if not message.strip():
            return _payload("sessions_send", "error", message="Message cannot be empty.")

        target_session = session_key or create_session_id(context.room_id, context.thread_id)
        if label and not session_key:
            resolved = await asyncio.to_thread(_resolve_by_label, context, label)
            if resolved:
                target_session = resolved

        target_room_id, target_thread_id = _session_key_to_room_thread(target_session)
        target_agent = agent_id or await asyncio.to_thread(_lookup_target_agent, context, target_session)
        target_agent = target_agent or context.agent_name

        thread_dispatch_error = _threaded_dispatch_error(
            context,
            session_key=target_session,
            thread_id=target_thread_id,
            target_agent=target_agent,
        )
        if thread_dispatch_error is not None:
            return thread_dispatch_error

        outgoing = message.strip()
        if agent_id:
            outgoing = f"@mindroom_{agent_id} {outgoing}"

        event_id = await _send_matrix_text(
            context,
            room_id=target_room_id,
            text=outgoing,
            thread_id=target_thread_id,
            original_sender=context.requester_id,
        )

        if event_id is None:
            return _payload(
                "sessions_send",
                "error",
                session_key=target_session,
                message="Failed to send message to Matrix.",
            )

        await asyncio.to_thread(
            _record_session,
            context,
            session_key=target_session,
            label=label,
            target_agent=agent_id,
        )

        return _payload(
            "sessions_send",
            "ok",
            session_key=target_session,
            room_id=target_room_id,
            thread_id=target_thread_id,
            event_id=event_id,
        )

    async def sessions_spawn(
        self,
        task: str,
        label: str | None = None,
        agent_id: str | None = None,
    ) -> str:
        """Spawn an isolated background session."""
        context = _get_context()
        if context is None:
            return _context_error("sessions_spawn")

        if not task.strip():
            return _payload("sessions_spawn", "error", message="Task cannot be empty.")

        target_agent = agent_id or context.agent_name
        if _agent_thread_mode(context, target_agent) == "room":
            return _payload(
                "sessions_spawn",
                "error",
                message=(
                    f"Isolated spawn sessions are not supported for agent '{target_agent}' "
                    "because it uses thread_mode=room."
                ),
            )

        spawn_message = f"@mindroom_{target_agent} {task.strip()}"
        event_id = await _send_matrix_text(
            context,
            room_id=context.room_id,
            text=spawn_message,
            thread_id=None,
            original_sender=context.requester_id,
        )

        if event_id is None:
            return _payload(
                "sessions_spawn",
                "error",
                message="Failed to send spawn message to Matrix.",
            )

        spawned_session_key = create_session_id(context.room_id, event_id)

        await asyncio.to_thread(
            _record_session,
            context,
            session_key=spawned_session_key,
            label=label,
            target_agent=target_agent,
        )

        return _payload(
            "sessions_spawn",
            "ok",
            session_key=spawned_session_key,
            event_id=event_id,
            target_agent=target_agent,
        )

    async def list_sessions(
        self,
        limit: int | None = None,
        offset: int | None = None,
    ) -> str:
        """List tracked sub-agent sessions."""
        context = _get_context()
        if context is None:
            return _context_error("list_sessions")

        requested_limit = _bounded_limit(limit)
        requested_offset = _bounded_offset(offset)

        registry = await asyncio.to_thread(_load_registry, context)
        sessions = [
            {"session_key": key, **entry}
            for key, entry in registry.items()
            if isinstance(entry, dict) and _in_scope(entry, context)
        ]
        sessions.sort(
            key=lambda session: (_entry_recency(session), str(session.get("session_key", ""))),
            reverse=True,
        )

        total = len(sessions)
        paged_sessions = sessions[requested_offset : requested_offset + requested_limit]

        return _payload(
            "list_sessions",
            "ok",
            sessions=paged_sessions,
            total=total,
            limit=requested_limit,
            offset=requested_offset,
        )
