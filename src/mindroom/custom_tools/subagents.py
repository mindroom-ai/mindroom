"""Standalone sub-agent session orchestration toolkit."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agno.tools import Toolkit

from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.matrix.client import fetch_thread_history, send_message
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.session_tools_context import SessionToolsContext, get_session_tools_context
from mindroom.thread_utils import create_session_id

if TYPE_CHECKING:
    from pathlib import Path


_REGISTRY_LOCK = Lock()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())


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
        message="Session tools context is unavailable in this runtime path.",
    )


def _registry_path(context: SessionToolsContext) -> Path:
    # Intentionally separate from `openclaw/` registry state.
    return context.storage_path / "subagents" / "session_registry.json"


def _empty_registry() -> dict[str, dict[str, Any]]:
    return {"sessions": {}, "runs": {}}


def _load_registry(context: SessionToolsContext) -> dict[str, Any]:
    path = _registry_path(context)
    if not path.is_file():
        return _empty_registry()

    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return _empty_registry()

    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        return _empty_registry()

    sessions = loaded.get("sessions")
    runs = loaded.get("runs")
    if not isinstance(sessions, dict):
        sessions = {}
    if not isinstance(runs, dict):
        runs = {}
    return {"sessions": sessions, "runs": runs}


def _save_registry(context: SessionToolsContext, registry: dict[str, Any]) -> None:
    path = _registry_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(registry, sort_keys=True, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _session_key_to_room_thread(session_key: str) -> tuple[str, str | None]:
    marker = ":$"
    if marker in session_key:
        room_id, thread_suffix = session_key.rsplit(marker, 1)
        return room_id, f"${thread_suffix}"
    return session_key, None


def _session_in_scope(session: dict[str, Any], context: SessionToolsContext) -> bool:
    # Scope by requester and thread to prevent cross-user/session leakage.
    scope = session.get("scope")
    if not isinstance(scope, dict):
        return False
    return (
        scope.get("agent_name") == context.agent_name
        and scope.get("room_id") == context.room_id
        and scope.get("thread_id") == context.thread_id
        and scope.get("requester_id") == context.requester_id
    )


def _run_in_scope(run: dict[str, Any], context: SessionToolsContext) -> bool:
    # Scope by requester and thread to prevent cross-user/session leakage.
    scope = run.get("scope")
    if not isinstance(scope, dict):
        return False
    return (
        scope.get("agent_name") == context.agent_name
        and scope.get("room_id") == context.room_id
        and scope.get("thread_id") == context.thread_id
        and scope.get("requester_id") == context.requester_id
    )


def _touch_session(
    context: SessionToolsContext,
    *,
    session_key: str,
    kind: str,
    label: str | None = None,
    parent_session_key: str | None = None,
    target_agent: str | None = None,
    status: str = "active",
) -> None:
    now_iso = _now_iso()
    now_epoch = _now_epoch()
    room_id, thread_id = _session_key_to_room_thread(session_key)
    with _REGISTRY_LOCK:
        registry = _load_registry(context)
        existing = registry["sessions"].get(session_key)
        run_scope = {
            "agent_name": context.agent_name,
            "room_id": context.room_id,
            "thread_id": context.thread_id,
            "requester_id": context.requester_id,
        }
        base_scope = (
            dict(existing.get("scope", {}))
            if isinstance(existing, dict) and isinstance(existing.get("scope"), dict)
            else {}
        )
        if base_scope.get("thread_id") in {"", "null"}:
            base_scope["thread_id"] = None
        if base_scope.get("requester_id") in {"", "null"}:
            base_scope["requester_id"] = None
        for key, value in run_scope.items():
            if value is None:
                continue
            if key not in base_scope or base_scope.get(key) in {None, ""}:
                base_scope[key] = value
        if "thread_id" not in base_scope:
            base_scope["thread_id"] = None
        if "requester_id" not in base_scope:
            base_scope["requester_id"] = context.requester_id
        entry = {
            "session_key": session_key,
            "kind": kind,
            "label": label,
            "status": status,
            "agent_name": context.agent_name,
            "room_id": room_id,
            "thread_id": thread_id,
            "requester_id": context.requester_id,
            "parent_session_key": parent_session_key,
            "target_agent": target_agent,
            "updated_at": now_iso,
            "updated_at_epoch": now_epoch,
            "scope": base_scope,
        }
        if isinstance(existing, dict):
            created_at = existing.get("created_at", now_iso)
            created_at_epoch = existing.get("created_at_epoch", now_epoch)
        else:
            created_at = now_iso
            created_at_epoch = now_epoch
        entry["created_at"] = created_at
        entry["created_at_epoch"] = created_at_epoch
        registry["sessions"][session_key] = entry
        _save_registry(context, registry)


def _track_run(
    context: SessionToolsContext,
    *,
    run_id: str,
    session_key: str,
    task: str,
    target_agent: str,
    status: str,
    event_id: str | None = None,
) -> dict[str, Any]:
    now_iso = _now_iso()
    now_epoch = _now_epoch()
    room_id, thread_id = _session_key_to_room_thread(session_key)
    with _REGISTRY_LOCK:
        registry = _load_registry(context)
        run_info = {
            "run_id": run_id,
            "session_key": session_key,
            "task": task,
            "target_agent": target_agent,
            "status": status,
            "event_id": event_id,
            "agent_name": context.agent_name,
            "room_id": room_id,
            "thread_id": thread_id,
            "requester_id": context.requester_id,
            "created_at": now_iso,
            "updated_at": now_iso,
            "created_at_epoch": now_epoch,
            "updated_at_epoch": now_epoch,
            "scope": {
                "agent_name": context.agent_name,
                "room_id": context.room_id,
                "thread_id": context.thread_id,
                "requester_id": context.requester_id,
            },
        }
        registry["runs"][run_id] = run_info
        _save_registry(context, registry)
        return run_info


def _update_run_status(
    context: SessionToolsContext,
    run_id: str,
    status: str,
) -> dict[str, Any] | None:
    with _REGISTRY_LOCK:
        registry = _load_registry(context)
        run = registry["runs"].get(run_id)
        if not isinstance(run, dict):
            return None
        if not _run_in_scope(run, context):
            return None
        run["status"] = status
        run["updated_at"] = _now_iso()
        run["updated_at_epoch"] = _now_epoch()
        registry["runs"][run_id] = run
        _save_registry(context, registry)
        return run


def _update_all_runs_status(context: SessionToolsContext, status: str) -> list[str]:
    with _REGISTRY_LOCK:
        registry = _load_registry(context)
        updated: list[str] = []
        now_iso = _now_iso()
        now_epoch = _now_epoch()
        for run_id, run in registry["runs"].items():
            if not isinstance(run, dict):
                continue
            if not _run_in_scope(run, context):
                continue
            run["status"] = status
            run["updated_at"] = now_iso
            run["updated_at_epoch"] = now_epoch
            updated.append(run_id)
        _save_registry(context, registry)
        return updated


def _list_runs(context: SessionToolsContext) -> list[dict[str, Any]]:
    with _REGISTRY_LOCK:
        registry = _load_registry(context)
        runs = registry.get("runs", {})
        if not isinstance(runs, dict):
            return []
        values = [run for run in runs.values() if isinstance(run, dict) and _run_in_scope(run, context)]
        values.sort(key=lambda run: int(run.get("updated_at_epoch", 0)), reverse=True)
        return values


def _decode_runs(raw_runs: str | None) -> list[dict[str, Any]]:
    if raw_runs is None:
        return []
    try:
        decoded: Any = json.loads(raw_runs)
    except json.JSONDecodeError:
        return []
    if isinstance(decoded, str):
        try:
            decoded = json.loads(decoded)
        except json.JSONDecodeError:
            return []
    if isinstance(decoded, list):
        return [run for run in decoded if isinstance(run, dict)]
    return []


def _agent_sessions_query(agent_name: str) -> str:
    table_name = _table_name(agent_name)
    # Table names are derived from internal agent ids and escaped in `_table_name`.
    return f'SELECT session_id, session_type, user_id, created_at, updated_at, runs FROM "{table_name}" ORDER BY updated_at DESC'  # noqa: S608


def _session_runs_query(agent_name: str) -> str:
    table_name = _table_name(agent_name)
    # Table names are derived from internal agent ids and escaped in `_table_name`.
    return f'SELECT runs FROM "{table_name}" WHERE session_id = ? LIMIT 1'  # noqa: S608


def _table_name(agent_name: str) -> str:
    # Agent/team names are validated to be alphanumeric + underscore in
    # config.py. Escaping remains defense-in-depth.
    escaped_agent_name = agent_name.replace('"', '""')
    return f"{escaped_agent_name}_sessions"


def _requested_limit(limit: int | None, default: int, maximum: int) -> int:
    return default if limit is None else max(1, min(limit, maximum))


def _minutes_cutoff(minutes: int | None) -> int | None:
    if minutes is None:
        return None
    now = datetime.now(UTC)
    cutoff = now - timedelta(minutes=max(0, minutes))
    return int(cutoff.timestamp())


def _coerce_epoch(value: object) -> float:
    """Normalize mixed timestamp formats into unix seconds."""
    if value is None or isinstance(value, bool):
        return 0.0

    numeric: float | None = None
    if isinstance(value, int | float):
        numeric = float(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped:
            try:
                numeric = float(stripped)
            except ValueError:
                iso_value = f"{stripped[:-1]}+00:00" if stripped.endswith("Z") else stripped
                try:
                    parsed = datetime.fromisoformat(iso_value)
                except ValueError:
                    numeric = None
                else:
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    return parsed.timestamp()

    if numeric is None:
        return 0.0

    # Matrix timestamps are often milliseconds; convert for ordering.
    return numeric / 1000.0 if numeric > 1_000_000_000_000 else numeric


def _registry_sessions(context: SessionToolsContext) -> list[dict[str, Any]]:
    with _REGISTRY_LOCK:
        registry = _load_registry(context)
    sessions = registry.get("sessions")
    if not isinstance(sessions, dict):
        return []
    session_values = [
        session for session in sessions.values() if isinstance(session, dict) and _session_in_scope(session, context)
    ]
    session_values.sort(key=lambda session: int(session.get("updated_at_epoch", 0)), reverse=True)
    return session_values


def _dedupe_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for session in sessions:
        session_key = session.get("session_key")
        if not isinstance(session_key, str):
            continue
        previous = deduped.get(session_key)
        if previous is None or int(session.get("updated_at_epoch", 0)) >= int(previous.get("updated_at_epoch", 0)):
            deduped[session_key] = session
    return list(deduped.values())


def _filter_by_kind(sessions: list[dict[str, Any]], kinds: list[str] | None) -> list[dict[str, Any]]:
    if not kinds:
        return sessions
    allowed = {kind.strip().lower() for kind in kinds if kind and kind.strip()}
    if not allowed:
        return sessions
    return [session for session in sessions if str(session.get("kind", "")).lower() in allowed]


def _filter_by_activity(sessions: list[dict[str, Any]], cutoff_epoch: int | None) -> list[dict[str, Any]]:
    if cutoff_epoch is None:
        return sessions
    return [session for session in sessions if int(session.get("updated_at_epoch", 0)) >= cutoff_epoch]


def _apply_message_limit(sessions: list[dict[str, Any]], message_limit: int | None) -> list[dict[str, Any]]:
    if message_limit is None:
        return sessions
    preview_limit = max(0, message_limit)
    return [
        {
            **session,
            "last_content_preview": str(session.get("last_content_preview", ""))[:preview_limit],
        }
        for session in sessions
    ]


def _agent_thread_mode(context: SessionToolsContext, agent_name: str) -> str:
    resolver = getattr(context.config, "get_entity_thread_mode", None)
    if not callable(resolver):
        return "thread"

    mode = resolver(agent_name)
    return "room" if mode == "room" else "thread"


def _resolve_labeled_session_key(
    context: SessionToolsContext,
    *,
    label: str,
    fallback_session_key: str,
) -> str:
    with _REGISTRY_LOCK:
        registry = _load_registry(context)
        sessions = registry.get("sessions")
        if not isinstance(sessions, dict):
            return fallback_session_key

        candidates = [
            tracked_session
            for tracked_session in sessions.values()
            if isinstance(tracked_session, dict)
            and tracked_session.get("label") == label
            and _session_in_scope(tracked_session, context)
        ]
        candidates.sort(
            key=lambda tracked_session: _coerce_epoch(tracked_session.get("updated_at_epoch")),
            reverse=True,
        )
        for tracked_session in candidates:
            candidate = tracked_session.get("session_key")
            if isinstance(candidate, str):
                return candidate
    return fallback_session_key


def _resolve_tracked_target_agent(
    context: SessionToolsContext,
    *,
    session_key: str,
) -> str | None:
    with _REGISTRY_LOCK:
        registry = _load_registry(context)
        sessions = registry.get("sessions")
        if not isinstance(sessions, dict):
            return None
        session = sessions.get(session_key)
        if not isinstance(session, dict):
            return None
        resolved = session.get("target_agent")
        if isinstance(resolved, str) and resolved:
            return resolved
        return None


def _threaded_dispatch_error(
    context: SessionToolsContext,
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


def _read_agent_sessions(context: SessionToolsContext) -> list[dict[str, Any]]:
    db_path = context.storage_path / "sessions" / f"{context.agent_name}.db"
    if not db_path.is_file():
        return []

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(_agent_sessions_query(context.agent_name)).fetchall()
        except sqlite3.OperationalError:
            return []

    sessions: list[dict[str, Any]] = []
    for row in rows:
        runs = _decode_runs(row["runs"])
        last_run = runs[-1] if runs else {}
        sessions.append(
            {
                "session_key": str(row["session_id"]),
                "kind": str(row["session_type"]),
                "user_id": row["user_id"],
                "created_at_epoch": int(row["created_at"] or 0),
                "updated_at_epoch": int(row["updated_at"] or row["created_at"] or 0),
                "run_count": len(runs),
                "last_status": str(last_run.get("status", "unknown")),
                "last_content_preview": str(last_run.get("content", ""))[:240],
            },
        )
    return sessions


async def _send_matrix_text(
    context: SessionToolsContext,
    *,
    room_id: str,
    text: str,
    thread_id: str | None,
    original_sender: str | None = None,
) -> str | None:
    content = format_message_with_mentions(
        context.config,
        text,
        sender_domain=context.config.domain,
        thread_event_id=thread_id,
    )
    if original_sender:
        content[ORIGINAL_SENDER_KEY] = original_sender
    return await send_message(context.client, room_id, content)


def _load_tracked_session(context: SessionToolsContext, session_key: str) -> dict[str, Any] | None:
    with _REGISTRY_LOCK:
        registry = _load_registry(context)
        tracked = registry["sessions"].get(session_key)
        return tracked if isinstance(tracked, dict) else None


def _read_session_history(
    context: SessionToolsContext,
    *,
    session_key: str,
    include_tools: bool,
) -> list[dict[str, Any]]:
    sessions = _read_agent_sessions(context)
    matching = [session for session in sessions if session.get("session_key") == session_key]
    if not matching:
        return []

    db_path = context.storage_path / "sessions" / f"{context.agent_name}.db"
    with sqlite3.connect(db_path) as conn:
        try:
            row = conn.execute(_session_runs_query(context.agent_name), (session_key,)).fetchone()
        except sqlite3.OperationalError:
            row = None

    runs = _decode_runs(row[0] if row else None)
    history: list[dict[str, Any]] = []
    for run in runs:
        content_type = str(run.get("content_type", ""))
        if not include_tools and "tool" in content_type.lower():
            continue
        history.append(
            {
                "source": "agent_db",
                "run_id": run.get("run_id"),
                "status": run.get("status"),
                "created_at": run.get("created_at"),
                "content_type": content_type,
                "content": run.get("content"),
                "input": run.get("input"),
            },
        )
    return history


async def _sessions_send(
    context: SessionToolsContext,
    *,
    message: str,
    session_key: str | None = None,
    label: str | None = None,
    agent_id: str | None = None,
    timeout_seconds: int | None = None,
) -> str:
    if not message.strip():
        return _payload("sessions_send", "error", message="Message cannot be empty.")

    target_session = session_key or create_session_id(context.room_id, context.thread_id)
    if label:
        target_session = await asyncio.to_thread(
            _resolve_labeled_session_key,
            context,
            label=label,
            fallback_session_key=target_session,
        )

    target_room_id, target_thread_id = _session_key_to_room_thread(target_session)
    target_agent = agent_id or await asyncio.to_thread(
        _resolve_tracked_target_agent,
        context,
        session_key=target_session,
    )
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
        _touch_session,
        context,
        session_key=target_session,
        kind="thread" if target_thread_id else "room",
        label=label,
        target_agent=agent_id,
        status="active",
    )

    return _payload(
        "sessions_send",
        "ok",
        session_key=target_session,
        room_id=target_room_id,
        thread_id=target_thread_id,
        event_id=event_id,
        timeout_seconds=timeout_seconds,
    )


async def _sessions_spawn(
    context: SessionToolsContext,
    *,
    task: str,
    label: str | None = None,
    agent_id: str | None = None,
    model: str | None = None,
    run_timeout_seconds: int | None = None,
    timeout_seconds: int | None = None,
    cleanup: str | None = None,
) -> str:
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
    parent_session_key = create_session_id(context.room_id, context.thread_id)
    run_id = str(uuid4())

    await asyncio.to_thread(
        _touch_session,
        context,
        session_key=spawned_session_key,
        kind="spawn",
        label=label,
        parent_session_key=parent_session_key,
        target_agent=target_agent,
        status="accepted",
    )
    run_info = await asyncio.to_thread(
        _track_run,
        context,
        run_id=run_id,
        session_key=spawned_session_key,
        task=task.strip(),
        target_agent=target_agent,
        status="accepted",
        event_id=event_id,
    )

    return _payload(
        "sessions_spawn",
        "ok",
        run_id=run_id,
        session_key=spawned_session_key,
        parent_session_key=parent_session_key,
        event_id=event_id,
        target_agent=target_agent,
        model=model,
        run_timeout_seconds=run_timeout_seconds,
        timeout_seconds=timeout_seconds,
        cleanup=cleanup,
        run=run_info,
    )


async def _subagents_list_payload(context: SessionToolsContext, recent_minutes: int | None) -> str:
    runs = await asyncio.to_thread(_list_runs, context)
    cutoff_epoch = _minutes_cutoff(recent_minutes)
    if cutoff_epoch is not None:
        runs = [run for run in runs if int(run.get("updated_at_epoch", 0)) >= cutoff_epoch]
    return _payload("subagents", "ok", action="list", runs=runs)


async def _subagents_kill_payload(context: SessionToolsContext, target: str | None) -> str:
    if target is None:
        return _payload("subagents", "error", action="kill", message="Target run_id is required.")

    if target == "all":
        updated = await asyncio.to_thread(_update_all_runs_status, context, "killed")
        return _payload("subagents", "ok", action="kill", updated=updated)

    updated_run = await asyncio.to_thread(_update_run_status, context, target, "killed")
    if updated_run is None:
        return _payload("subagents", "error", action="kill", message=f"Unknown run_id: {target}")
    return _payload("subagents", "ok", action="kill", run=updated_run)


async def _subagents_steer_payload(
    context: SessionToolsContext,
    *,
    target: str | None,
    message: str | None,
) -> str:
    if target is None or message is None or not message.strip():
        return _payload(
            "subagents",
            "error",
            action="steer",
            message="Both target run_id and non-empty message are required.",
        )

    runs = {
        run["run_id"]: run for run in await asyncio.to_thread(_list_runs, context) if isinstance(run.get("run_id"), str)
    }
    run = runs.get(target)
    if run is None:
        return _payload("subagents", "error", action="steer", message=f"Unknown run_id: {target}")

    result = await _sessions_send(
        context,
        message=message.strip(),
        session_key=str(run.get("session_key")),
        agent_id=str(run.get("target_agent") or context.agent_name),
    )
    dispatch = json.loads(result)
    if dispatch.get("status") != "ok":
        return _payload(
            "subagents",
            "error",
            action="steer",
            run_id=target,
            dispatch=dispatch,
            message="Failed to steer run.",
        )

    await asyncio.to_thread(_update_run_status, context, target, "steered")
    return _payload(
        "subagents",
        "ok",
        action="steer",
        run_id=target,
        dispatch=dispatch,
    )


class SubAgentsTools(Toolkit):
    """Session and sub-agent orchestration tools for any MindRoom agent."""

    def __init__(self) -> None:
        super().__init__(
            name="subagents",
            tools=[
                self.agents_list,
                self.session_status,
                self.sessions_list,
                self.sessions_history,
                self.sessions_send,
                self.sessions_spawn,
                self.subagents,
            ],
        )

    async def agents_list(self) -> str:
        """List agent ids available for `sessions_spawn` targeting."""
        context = get_session_tools_context()
        if context is None:
            return _context_error("agents_list")

        return _payload(
            "agents_list",
            "ok",
            agents=sorted(context.config.agents.keys()),
            current_agent=context.agent_name,
        )

    async def session_status(
        self,
        session_key: str | None = None,
        model: str | None = None,
    ) -> str:
        """Show status information for a session and optional model override."""
        context = get_session_tools_context()
        if context is None:
            return _context_error("session_status")

        effective_session_key = session_key or create_session_id(context.room_id, context.thread_id)
        db_sessions = {entry["session_key"]: entry for entry in await asyncio.to_thread(_read_agent_sessions, context)}
        tracked_session = await asyncio.to_thread(_load_tracked_session, context, effective_session_key)

        return _payload(
            "session_status",
            "ok",
            session_key=effective_session_key,
            model=model,
            has_db_session=effective_session_key in db_sessions,
            db_session=db_sessions.get(effective_session_key),
            tracked_session=tracked_session,
            current_session=create_session_id(context.room_id, context.thread_id),
        )

    async def sessions_list(
        self,
        kinds: list[str] | None = None,
        limit: int | None = None,
        active_minutes: int | None = None,
        message_limit: int | None = None,
    ) -> str:
        """List sessions with optional filters and message previews."""
        context = get_session_tools_context()
        if context is None:
            return _context_error("sessions_list")

        requested_limit = _requested_limit(limit, default=20, maximum=200)
        db_sessions, registry_sessions = await asyncio.gather(
            asyncio.to_thread(_read_agent_sessions, context),
            asyncio.to_thread(_registry_sessions, context),
        )
        all_sessions = [*db_sessions, *registry_sessions]
        deduped = _dedupe_sessions(all_sessions)
        filtered = _filter_by_kind(deduped, kinds)
        filtered = _filter_by_activity(filtered, _minutes_cutoff(active_minutes))
        filtered.sort(key=lambda session: int(session.get("updated_at_epoch", 0)), reverse=True)
        limited = _apply_message_limit(filtered[:requested_limit], message_limit)

        return _payload(
            "sessions_list",
            "ok",
            sessions=limited,
            total=len(filtered),
            limit=requested_limit,
        )

    async def sessions_history(
        self,
        session_key: str,
        limit: int | None = None,
        include_tools: bool = False,
    ) -> str:
        """Fetch transcript history for one session."""
        context = get_session_tools_context()
        if context is None:
            return _context_error("sessions_history")

        requested_limit = _requested_limit(limit, default=50, maximum=500)

        db_history = await asyncio.to_thread(
            _read_session_history,
            context,
            session_key=session_key,
            include_tools=include_tools,
        )

        room_id, thread_id = _session_key_to_room_thread(session_key)
        matrix_history: list[dict[str, Any]] = []
        if thread_id is not None:
            thread_messages = await fetch_thread_history(context.client, room_id, thread_id)
            matrix_history.extend(
                {
                    "source": "matrix_thread",
                    "event_id": message.get("event_id"),
                    "sender": message.get("sender"),
                    "timestamp": message.get("timestamp"),
                    "body": message.get("body"),
                }
                for message in thread_messages[-requested_limit:]
            )

        combined_history = [*db_history, *matrix_history]
        combined_history.sort(
            key=lambda entry: (
                _coerce_epoch(entry.get("timestamp"))
                if entry.get("timestamp") is not None
                else _coerce_epoch(entry.get("created_at"))
            ),
        )
        combined_history = combined_history[-requested_limit:]

        return _payload(
            "sessions_history",
            "ok",
            session_key=session_key,
            history=combined_history,
            include_tools=include_tools,
        )

    async def sessions_send(
        self,
        message: str,
        session_key: str | None = None,
        label: str | None = None,
        agent_id: str | None = None,
        timeout_seconds: int | None = None,
    ) -> str:
        """Send a message to another session."""
        context = get_session_tools_context()
        if context is None:
            return _context_error("sessions_send")

        return await _sessions_send(
            context,
            message=message,
            session_key=session_key,
            label=label,
            agent_id=agent_id,
            timeout_seconds=timeout_seconds,
        )

    async def sessions_spawn(
        self,
        task: str,
        label: str | None = None,
        agent_id: str | None = None,
        model: str | None = None,
        run_timeout_seconds: int | None = None,
        timeout_seconds: int | None = None,
        cleanup: str | None = None,
    ) -> str:
        """Spawn an isolated background session."""
        context = get_session_tools_context()
        if context is None:
            return _context_error("sessions_spawn")

        return await _sessions_spawn(
            context,
            task=task,
            label=label,
            agent_id=agent_id,
            model=model,
            run_timeout_seconds=run_timeout_seconds,
            timeout_seconds=timeout_seconds,
            cleanup=cleanup,
        )

    async def subagents(
        self,
        action: str = "list",
        target: str | None = None,
        message: str | None = None,
        recent_minutes: int | None = None,
    ) -> str:
        """Inspect or control spawned sub-agent runs."""
        context = get_session_tools_context()
        if context is None:
            return _context_error("subagents")

        normalized_action = action.strip().lower()
        if normalized_action == "list":
            return await _subagents_list_payload(context, recent_minutes)
        if normalized_action == "kill":
            return await _subagents_kill_payload(context, target)
        if normalized_action == "steer":
            return await _subagents_steer_payload(context, target=target, message=message)

        return _payload(
            "subagents",
            "error",
            action=action,
            message="Unsupported action. Use list, kill, or steer.",
        )
