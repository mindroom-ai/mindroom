"""OpenClaw-compatible toolkit surface for incremental parity work."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shlex
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import nio
from agno.tools import Toolkit
from agno.tools.duckduckgo import DuckDuckGoTools
from agno.tools.website import WebsiteTools

from mindroom.custom_tools.coding import CodingTools
from mindroom.custom_tools.scheduler import SchedulerTools
from mindroom.logging_config import get_logger
from mindroom.matrix.client import fetch_thread_history, send_message
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_content import extract_and_resolve_message
from mindroom.openclaw_context import OpenClawToolContext, get_openclaw_tool_context
from mindroom.thread_utils import create_session_id
from mindroom.tools_metadata import get_tool_by_name

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


class OpenClawCompatTools(Toolkit):
    """OpenClaw-style tool names exposed as a single toolkit."""

    _registry_lock: Lock = Lock()
    _shell_path_lock: Lock = Lock()
    _login_shell_path: str | None = None
    _login_shell_path_loaded = False
    _login_shell_path_applied = False
    _LOGIN_SHELL_TIMEOUT_SECONDS = 15
    _CODING_ERROR_PREFIXES = (
        "Error:",
        "Error reading file:",
        "Error writing file:",
        "Error listing directory:",
        "Error running grep",
    )

    def __init__(self) -> None:
        """Initialize the OpenClaw compatibility toolkit."""
        super().__init__(
            name="openclaw_compat",
            tools=[
                self.agents_list,
                self.session_status,
                self.sessions_list,
                self.sessions_history,
                self.sessions_send,
                self.sessions_spawn,
                self.subagents,
                self.message,
                self.cron,
                self.web_search,
                self.web_fetch,
                self.browser,
                self.exec,
                self.process,
                self.read_file,
                self.edit_file,
                self.write_file,
                self.grep,
                self.find_files,
                self.ls,
            ],
        )
        self._scheduler = SchedulerTools()
        self._duckduckgo = DuckDuckGoTools()
        self._website = WebsiteTools()
        self._shell = get_tool_by_name("shell")
        self._browser_tool: Toolkit | None = None
        self._coding = CodingTools()

    @staticmethod
    def _payload(tool_name: str, status: str, **kwargs: object) -> str:
        """Return a structured JSON payload."""
        payload: dict[str, object] = {
            "status": status,
            "tool": tool_name,
        }
        payload.update(kwargs)
        return json.dumps(payload, sort_keys=True)

    @staticmethod
    def _context_error(tool_name: str) -> str:
        """Return a structured context error payload."""
        return OpenClawCompatTools._payload(
            tool_name,
            "error",
            message="OpenClaw tool context is unavailable in this runtime path.",
        )

    @classmethod
    def _coding_status(cls, result: str) -> str:
        """Map CodingTools string results to stable status values."""
        return "error" if result.startswith(cls._CODING_ERROR_PREFIXES) else "ok"

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _now_epoch() -> int:
        return int(datetime.now(UTC).timestamp())

    @staticmethod
    def _merge_paths(existing_path: str, shell_path: str) -> str:
        """Prepend login-shell PATH entries while keeping order and deduplicating."""
        merged_parts: list[str] = []
        seen: set[str] = set()
        for part in [*shell_path.split(os.pathsep), *existing_path.split(os.pathsep)]:
            normalized = part.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged_parts.append(normalized)
        return os.pathsep.join(merged_parts)

    @classmethod
    def _read_login_shell_path(cls) -> str | None:
        """Read PATH from the user's login shell."""
        if os.name == "nt":
            return None

        shell = os.environ.get("SHELL", "").strip() or "/bin/sh"
        try:
            result = subprocess.run(
                [shell, "-l", "-c", "env -0"],
                capture_output=True,
                check=False,
                timeout=cls._LOGIN_SHELL_TIMEOUT_SECONDS,
                env=os.environ.copy(),
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug(f"Login shell PATH probe failed: {exc}")
            return None

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="ignore").strip()
            message = f"Login shell PATH probe exited with {result.returncode}"
            if stderr:
                message = f"{message}: {stderr}"
            logger.debug(message)
            return None

        for env_entry in result.stdout.decode("utf-8", errors="ignore").split("\0"):
            key, sep, value = env_entry.partition("=")
            if sep and key == "PATH":
                resolved_path = value.strip()
                return resolved_path or None
        return None

    @classmethod
    def _ensure_login_shell_path(cls) -> None:
        """Apply login-shell PATH to this process once for OpenClaw shell aliases."""
        if os.name == "nt":
            return

        with cls._shell_path_lock:
            if cls._login_shell_path_applied:
                return

            if not cls._login_shell_path_loaded:
                shell_path = cls._read_login_shell_path()
                if not shell_path:
                    return
                cls._login_shell_path = shell_path
                cls._login_shell_path_loaded = True

            shell_path = cls._login_shell_path
            if not shell_path:
                cls._login_shell_path_loaded = False
                return

            merged = cls._merge_paths(os.environ.get("PATH", ""), shell_path)
            if merged:
                os.environ["PATH"] = merged
            cls._login_shell_path_applied = True

    @staticmethod
    def _registry_path(context: OpenClawToolContext) -> Path:
        return context.storage_path / "openclaw" / "session_registry.json"

    @classmethod
    def _load_registry(cls, context: OpenClawToolContext) -> dict[str, Any]:
        path = cls._registry_path(context)
        if not path.is_file():
            return {"sessions": {}, "runs": {}}

        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return {"sessions": {}, "runs": {}}

        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            return {"sessions": {}, "runs": {}}

        sessions = loaded.get("sessions")
        runs = loaded.get("runs")
        if not isinstance(sessions, dict):
            sessions = {}
        if not isinstance(runs, dict):
            runs = {}
        return {"sessions": sessions, "runs": runs}

    @classmethod
    def _save_registry(cls, context: OpenClawToolContext, registry: dict[str, Any]) -> None:
        path = cls._registry_path(context)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(registry, sort_keys=True, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    @classmethod
    def _touch_session(
        cls,
        context: OpenClawToolContext,
        *,
        session_key: str,
        kind: str,
        label: str | None = None,
        parent_session_key: str | None = None,
        target_agent: str | None = None,
        status: str = "active",
    ) -> dict[str, Any]:
        """Create or update a tracked session entry."""
        room_id, thread_id = cls._session_key_to_room_thread(session_key)
        with cls._registry_lock:
            registry = cls._load_registry(context)
            sessions = registry["sessions"]
            session = sessions.get(session_key)
            now_iso = cls._now_iso()
            now_epoch = cls._now_epoch()

            if not isinstance(session, dict):
                session = {
                    "session_key": session_key,
                    "kind": kind,
                    "agent_name": context.agent_name,
                    "room_id": room_id,
                    "thread_id": thread_id,
                    "label": label,
                    "parent_session_key": parent_session_key,
                    "target_agent": target_agent,
                    "requester_id": context.requester_id,
                    "status": status,
                    "created_at": now_iso,
                    "created_at_epoch": now_epoch,
                    "updated_at": now_iso,
                    "updated_at_epoch": now_epoch,
                }
            else:
                session["kind"] = kind
                if label is not None:
                    session["label"] = label
                if parent_session_key is not None:
                    session["parent_session_key"] = parent_session_key
                if target_agent is not None:
                    session["target_agent"] = target_agent
                session["agent_name"] = context.agent_name
                session["room_id"] = room_id
                session["thread_id"] = thread_id
                session["requester_id"] = context.requester_id
                session["status"] = status
                session["updated_at"] = now_iso
                session["updated_at_epoch"] = now_epoch

            sessions[session_key] = session
            cls._save_registry(context, registry)
            return session

    @classmethod
    def _track_run(
        cls,
        context: OpenClawToolContext,
        *,
        run_id: str,
        session_key: str,
        task: str,
        target_agent: str,
        status: str,
        event_id: str | None,
    ) -> dict[str, Any]:
        room_id, thread_id = cls._session_key_to_room_thread(session_key)
        with cls._registry_lock:
            registry = cls._load_registry(context)
            runs = registry["runs"]
            now_iso = cls._now_iso()
            now_epoch = cls._now_epoch()

            run_payload = {
                "run_id": run_id,
                "session_key": session_key,
                "task": task,
                "agent_name": context.agent_name,
                "target_agent": target_agent,
                "room_id": room_id,
                "thread_id": thread_id,
                "requester_id": context.requester_id,
                "status": status,
                "event_id": event_id,
                "created_at": now_iso,
                "created_at_epoch": now_epoch,
                "updated_at": now_iso,
                "updated_at_epoch": now_epoch,
            }
            runs[run_id] = run_payload
            cls._save_registry(context, registry)
            return run_payload

    @classmethod
    def _update_run_status(cls, context: OpenClawToolContext, run_id: str, status: str) -> dict[str, Any] | None:
        with cls._registry_lock:
            registry = cls._load_registry(context)
            run = registry["runs"].get(run_id)
            if not isinstance(run, dict):
                return None
            if not cls._run_in_scope(run, context):
                return None
            run["status"] = status
            run["updated_at"] = cls._now_iso()
            run["updated_at_epoch"] = cls._now_epoch()
            cls._save_registry(context, registry)
            return run

    @classmethod
    def _update_all_runs_status(cls, context: OpenClawToolContext, status: str) -> list[str]:
        with cls._registry_lock:
            registry = cls._load_registry(context)
            runs = registry.get("runs")
            if not isinstance(runs, dict):
                return []

            now_iso = cls._now_iso()
            now_epoch = cls._now_epoch()
            updated: list[str] = []
            for run in runs.values():
                if not isinstance(run, dict):
                    continue
                run_id = run.get("run_id")
                if not isinstance(run_id, str):
                    continue
                if not cls._run_in_scope(run, context):
                    continue
                run["status"] = status
                run["updated_at"] = now_iso
                run["updated_at_epoch"] = now_epoch
                updated.append(run_id)

            cls._save_registry(context, registry)
            return updated

    @classmethod
    def _list_runs(cls, context: OpenClawToolContext) -> list[dict[str, Any]]:
        with cls._registry_lock:
            registry = cls._load_registry(context)
            runs = registry.get("runs", {})
            if not isinstance(runs, dict):
                return []
            values = [run for run in runs.values() if isinstance(run, dict) and cls._run_in_scope(run, context)]
            return sorted(values, key=lambda run: int(run.get("updated_at_epoch", 0)), reverse=True)

    @staticmethod
    def _decode_runs(raw_runs: str | None) -> list[dict[str, Any]]:
        if not raw_runs:
            return []

        parsed: Any = json.loads(raw_runs)
        if isinstance(parsed, str):
            parsed = json.loads(parsed)
        if not isinstance(parsed, list):
            return []
        return [entry for entry in parsed if isinstance(entry, dict)]

    @staticmethod
    def _table_name(agent_name: str) -> str:
        # Agent/team names are validated to be alphanumeric + underscore in
        # config.py.  Escaping is kept as defense-in-depth.
        return f"{agent_name.replace('"', '""')}_sessions"

    @classmethod
    def _agent_sessions_query(cls, agent_name: str) -> str:
        table_name = cls._table_name(agent_name)
        # Table names are derived from internal agent ids and escaped in `_table_name`.
        return f'SELECT session_id, session_type, user_id, created_at, updated_at, runs FROM "{table_name}" ORDER BY updated_at DESC'  # noqa: S608

    @classmethod
    def _session_runs_query(cls, agent_name: str) -> str:
        table_name = cls._table_name(agent_name)
        # Table names are derived from internal agent ids and escaped in `_table_name`.
        return f'SELECT runs FROM "{table_name}" WHERE session_id = ? LIMIT 1'  # noqa: S608

    @staticmethod
    def _requested_limit(limit: int | None, default: int, maximum: int) -> int:
        return default if limit is None else max(1, min(limit, maximum))

    @staticmethod
    def _minutes_cutoff(minutes: int | None) -> int | None:
        if minutes is None:
            return None
        return int((datetime.now(UTC) - timedelta(minutes=max(0, minutes))).timestamp())

    @staticmethod
    def _coerce_epoch(value: object) -> float:
        """Normalize mixed timestamp formats into unix seconds."""
        if value is None or isinstance(value, bool):
            return 0.0

        numeric: float | None = None
        if isinstance(value, int | float):
            numeric = float(value)
        elif isinstance(value, str):
            text = value.strip()
            if text:
                try:
                    numeric = float(text)
                except ValueError:
                    iso_value = f"{text[:-1]}+00:00" if text.endswith("Z") else text
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

    @staticmethod
    def _session_in_scope(session: dict[str, Any], context: OpenClawToolContext) -> bool:
        return (
            session.get("agent_name") == context.agent_name
            and session.get("room_id") == context.room_id
            and session.get("requester_id") == context.requester_id
        )

    @staticmethod
    def _run_in_scope(run: dict[str, Any], context: OpenClawToolContext) -> bool:
        return (
            run.get("agent_name") == context.agent_name
            and run.get("room_id") == context.room_id
            and run.get("requester_id") == context.requester_id
        )

    @classmethod
    def _registry_sessions(cls, context: OpenClawToolContext) -> list[dict[str, Any]]:
        with cls._registry_lock:
            registry = cls._load_registry(context)
        sessions = registry.get("sessions", {})
        if not isinstance(sessions, dict):
            return []
        return [
            session
            for session in sessions.values()
            if isinstance(session, dict) and cls._session_in_scope(session, context)
        ]

    @staticmethod
    def _dedupe_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: dict[str, dict[str, Any]] = {}
        for session in sessions:
            session_key = session.get("session_key")
            if not isinstance(session_key, str):
                continue
            existing = deduped.get(session_key)
            if existing is None or int(session.get("updated_at_epoch", 0)) >= int(existing.get("updated_at_epoch", 0)):
                deduped[session_key] = session
        return list(deduped.values())

    @staticmethod
    def _filter_by_kind(sessions: list[dict[str, Any]], kinds: list[str] | None) -> list[dict[str, Any]]:
        if not kinds:
            return sessions
        kind_set = {kind.strip().lower() for kind in kinds if kind.strip()}
        if not kind_set:
            return sessions
        return [
            session
            for session in sessions
            if str(session.get("kind", session.get("session_type", ""))).lower() in kind_set
        ]

    @staticmethod
    def _filter_by_activity(sessions: list[dict[str, Any]], cutoff_epoch: int | None) -> list[dict[str, Any]]:
        if cutoff_epoch is None:
            return sessions
        return [session for session in sessions if int(session.get("updated_at_epoch", 0)) >= cutoff_epoch]

    @staticmethod
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

    def _read_agent_sessions(self, context: OpenClawToolContext) -> list[dict[str, Any]]:
        db_path = context.storage_path / "sessions" / f"{context.agent_name}.db"
        if not db_path.is_file():
            return []

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(self._agent_sessions_query(context.agent_name)).fetchall()
            except sqlite3.OperationalError:
                return []

        sessions: list[dict[str, Any]] = []
        for row in rows:
            runs = self._decode_runs(row["runs"])
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

    @staticmethod
    def _session_key_to_room_thread(session_key: str) -> tuple[str, str | None]:
        marker = ":$"
        if marker in session_key:
            room_id, thread_suffix = session_key.rsplit(marker, 1)
            return room_id, f"${thread_suffix}"
        return session_key, None

    async def _send_matrix_text(
        self,
        context: OpenClawToolContext,
        *,
        room_id: str,
        text: str,
        thread_id: str | None,
    ) -> str | None:
        content = format_message_with_mentions(
            context.config,
            text,
            sender_domain=context.config.domain,
            thread_event_id=thread_id,
        )
        return await send_message(context.client, room_id, content)

    async def agents_list(self) -> str:
        """List agent ids available for `sessions_spawn` targeting."""
        context = get_openclaw_tool_context()
        if context is None:
            return self._context_error("agents_list")

        return self._payload(
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
        context = get_openclaw_tool_context()
        if context is None:
            return self._context_error("session_status")

        effective_session_key = session_key or create_session_id(context.room_id, context.thread_id)
        db_sessions = {
            entry["session_key"]: entry for entry in await asyncio.to_thread(self._read_agent_sessions, context)
        }

        def _load_tracked() -> dict[str, Any] | None:
            with self._registry_lock:
                registry = self._load_registry(context)
                return registry["sessions"].get(effective_session_key)

        tracked_session = await asyncio.to_thread(_load_tracked)

        return self._payload(
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
        context = get_openclaw_tool_context()
        if context is None:
            return self._context_error("sessions_list")

        requested_limit = self._requested_limit(limit, default=20, maximum=200)
        db_sessions, registry_sessions = await asyncio.gather(
            asyncio.to_thread(self._read_agent_sessions, context),
            asyncio.to_thread(self._registry_sessions, context),
        )
        all_sessions = [*db_sessions, *registry_sessions]
        deduped = self._dedupe_sessions(all_sessions)
        filtered = self._filter_by_kind(deduped, kinds)
        filtered = self._filter_by_activity(filtered, self._minutes_cutoff(active_minutes))
        filtered.sort(key=lambda session: int(session.get("updated_at_epoch", 0)), reverse=True)
        limited = self._apply_message_limit(filtered[:requested_limit], message_limit)

        return self._payload(
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
        context = get_openclaw_tool_context()
        if context is None:
            return self._context_error("sessions_history")

        requested_limit = self._requested_limit(limit, default=50, maximum=500)

        def _read_session_history() -> list[dict[str, Any]]:
            sessions = self._read_agent_sessions(context)
            matching = [s for s in sessions if s.get("session_key") == session_key]
            if not matching:
                return []

            db_path = context.storage_path / "sessions" / f"{context.agent_name}.db"
            with sqlite3.connect(db_path) as conn:
                try:
                    row = conn.execute(self._session_runs_query(context.agent_name), (session_key,)).fetchone()
                except sqlite3.OperationalError:
                    row = None
            runs = self._decode_runs(row[0] if row else None)
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

        db_history = await asyncio.to_thread(_read_session_history)

        room_id, thread_id = self._session_key_to_room_thread(session_key)
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
                self._coerce_epoch(entry.get("timestamp"))
                if entry.get("timestamp") is not None
                else self._coerce_epoch(entry.get("created_at"))
            ),
        )
        combined_history = combined_history[-requested_limit:]

        return self._payload(
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
        context = get_openclaw_tool_context()
        if context is None:
            return self._context_error("sessions_send")
        active_context: OpenClawToolContext = context

        if not message.strip():
            return self._payload("sessions_send", "error", message="Message cannot be empty.")

        target_session = session_key or create_session_id(active_context.room_id, active_context.thread_id)
        if label:

            def _find_labeled() -> str:
                with self._registry_lock:
                    registry = self._load_registry(active_context)
                    sessions = registry.get("sessions")
                    if not isinstance(sessions, dict):
                        return target_session

                    candidates = [
                        tracked_session
                        for tracked_session in sessions.values()
                        if isinstance(tracked_session, dict)
                        and tracked_session.get("label") == label
                        and self._session_in_scope(tracked_session, active_context)
                    ]
                    candidates.sort(
                        key=lambda tracked_session: self._coerce_epoch(tracked_session.get("updated_at_epoch")),
                        reverse=True,
                    )
                    for tracked_session in candidates:
                        candidate = tracked_session.get("session_key")
                        if isinstance(candidate, str):
                            return candidate
                return target_session

            target_session = await asyncio.to_thread(_find_labeled)

        target_room_id, target_thread_id = self._session_key_to_room_thread(target_session)
        outgoing = message.strip()
        if agent_id:
            outgoing = f"@mindroom_{agent_id} {outgoing}"

        event_id = await self._send_matrix_text(
            active_context,
            room_id=target_room_id,
            text=outgoing,
            thread_id=target_thread_id,
        )

        if event_id is None:
            return self._payload(
                "sessions_send",
                "error",
                session_key=target_session,
                message="Failed to send message to Matrix.",
            )

        await asyncio.to_thread(
            self._touch_session,
            active_context,
            session_key=target_session,
            kind="thread" if target_thread_id else "room",
            label=label,
            target_agent=agent_id,
            status="active",
        )

        return self._payload(
            "sessions_send",
            "ok",
            session_key=target_session,
            room_id=target_room_id,
            thread_id=target_thread_id,
            event_id=event_id,
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
        context = get_openclaw_tool_context()
        if context is None:
            return self._context_error("sessions_spawn")

        if not task.strip():
            return self._payload("sessions_spawn", "error", message="Task cannot be empty.")

        target_agent = agent_id or context.agent_name
        spawn_message = f"@mindroom_{target_agent} {task.strip()}"
        event_id = await self._send_matrix_text(
            context,
            room_id=context.room_id,
            text=spawn_message,
            thread_id=None,
        )

        if event_id is None:
            return self._payload(
                "sessions_spawn",
                "error",
                message="Failed to send spawn message to Matrix.",
            )

        spawned_session_key = create_session_id(context.room_id, event_id)
        parent_session_key = create_session_id(context.room_id, context.thread_id)
        run_id = str(uuid4())

        await asyncio.to_thread(
            self._touch_session,
            context,
            session_key=spawned_session_key,
            kind="spawn",
            label=label,
            parent_session_key=parent_session_key,
            target_agent=target_agent,
            status="accepted",
        )
        run_info = await asyncio.to_thread(
            self._track_run,
            context,
            run_id=run_id,
            session_key=spawned_session_key,
            task=task.strip(),
            target_agent=target_agent,
            status="accepted",
            event_id=event_id,
        )

        return self._payload(
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

    async def _subagents_list_payload(self, context: OpenClawToolContext, recent_minutes: int | None) -> str:
        runs = await asyncio.to_thread(self._list_runs, context)
        cutoff_epoch = self._minutes_cutoff(recent_minutes)
        if cutoff_epoch is not None:
            runs = [run for run in runs if int(run.get("updated_at_epoch", 0)) >= cutoff_epoch]
        return self._payload("subagents", "ok", action="list", runs=runs)

    async def _subagents_kill_payload(self, context: OpenClawToolContext, target: str | None) -> str:
        if target is None:
            return self._payload("subagents", "error", action="kill", message="Target run_id is required.")

        if target == "all":
            updated = await asyncio.to_thread(self._update_all_runs_status, context, "killed")
            return self._payload("subagents", "ok", action="kill", updated=updated)

        updated_run = await asyncio.to_thread(self._update_run_status, context, target, "killed")
        if updated_run is None:
            return self._payload("subagents", "error", action="kill", message=f"Unknown run_id: {target}")
        return self._payload("subagents", "ok", action="kill", run=updated_run)

    async def _subagents_steer_payload(
        self,
        context: OpenClawToolContext,
        target: str | None,
        message: str | None,
    ) -> str:
        if target is None or message is None or not message.strip():
            return self._payload(
                "subagents",
                "error",
                action="steer",
                message="Both target run_id and non-empty message are required.",
            )

        runs = {
            run["run_id"]: run
            for run in await asyncio.to_thread(self._list_runs, context)
            if isinstance(run.get("run_id"), str)
        }
        run = runs.get(target)
        if run is None:
            return self._payload("subagents", "error", action="steer", message=f"Unknown run_id: {target}")

        result = await self.sessions_send(
            message=message.strip(),
            session_key=str(run.get("session_key")),
            agent_id=str(run.get("target_agent") or context.agent_name),
        )
        dispatch = json.loads(result)
        if dispatch.get("status") != "ok":
            return self._payload(
                "subagents",
                "error",
                action="steer",
                run_id=target,
                dispatch=dispatch,
                message="Failed to steer run.",
            )

        await asyncio.to_thread(self._update_run_status, context, target, "steered")
        return self._payload(
            "subagents",
            "ok",
            action="steer",
            run_id=target,
            dispatch=dispatch,
        )

    async def subagents(
        self,
        action: str = "list",
        target: str | None = None,
        message: str | None = None,
        recent_minutes: int | None = None,
    ) -> str:
        """Inspect or control spawned sub-agent runs."""
        context = get_openclaw_tool_context()
        if context is None:
            return self._context_error("subagents")

        normalized_action = action.strip().lower()
        if normalized_action == "list":
            return await self._subagents_list_payload(context, recent_minutes)
        if normalized_action == "kill":
            return await self._subagents_kill_payload(context, target)
        if normalized_action == "steer":
            return await self._subagents_steer_payload(context, target, message)

        return self._payload(
            "subagents",
            "error",
            action=action,
            message="Unsupported action. Use list, kill, or steer.",
        )

    async def _message_send_or_reply(
        self,
        context: OpenClawToolContext,
        *,
        action: str,
        message: str | None,
        room_id: str,
        effective_thread_id: str | None,
    ) -> str:
        if message is None or not message.strip():
            return self._payload("message", "error", action=action, message="Message cannot be empty.")
        if action in {"thread-reply", "reply"} and effective_thread_id is None:
            return self._payload("message", "error", action=action, message="thread_id is required for replies.")

        event_id = await self._send_matrix_text(
            context,
            room_id=room_id,
            text=message.strip(),
            thread_id=effective_thread_id,
        )
        if event_id is None:
            return self._payload(
                "message",
                "error",
                action=action,
                room_id=room_id,
                message="Failed to send message to Matrix.",
            )
        return self._payload(
            "message",
            "ok",
            action=action,
            room_id=room_id,
            thread_id=effective_thread_id,
            event_id=event_id,
        )

    async def _message_react(
        self,
        context: OpenClawToolContext,
        *,
        message: str | None,
        room_id: str,
        target: str | None,
    ) -> str:
        if target is None:
            return self._payload("message", "error", action="react", message="target event_id is required.")

        reaction = message.strip() if message and message.strip() else "👍"
        content = {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": target,
                "key": reaction,
            },
        }
        response = await context.client.room_send(
            room_id=room_id,
            message_type="m.reaction",
            content=content,
        )
        if isinstance(response, nio.RoomSendResponse):
            return self._payload(
                "message",
                "ok",
                action="react",
                room_id=room_id,
                target=target,
                reaction=reaction,
                event_id=response.event_id,
            )
        return self._payload(
            "message",
            "error",
            action="react",
            room_id=room_id,
            target=target,
            reaction=reaction,
            response=str(response),
        )

    async def _message_read(
        self,
        context: OpenClawToolContext,
        *,
        room_id: str,
        effective_thread_id: str | None,
    ) -> str:
        read_limit = 20
        if effective_thread_id is not None:
            thread_messages = await fetch_thread_history(context.client, room_id, effective_thread_id)
            return self._payload(
                "message",
                "ok",
                action="read",
                room_id=room_id,
                thread_id=effective_thread_id,
                messages=thread_messages[-read_limit:],
            )

        response = await context.client.room_messages(
            room_id,
            limit=read_limit,
            direction=nio.MessageDirection.back,
            message_filter={"types": ["m.room.message"]},
        )
        if not isinstance(response, nio.RoomMessagesResponse):
            return self._payload(
                "message",
                "error",
                action="read",
                room_id=room_id,
                response=str(response),
            )

        resolved = [
            await extract_and_resolve_message(event, context.client)
            for event in reversed(response.chunk)
            if isinstance(event, nio.RoomMessageText)
        ]
        return self._payload(
            "message",
            "ok",
            action="read",
            room_id=room_id,
            messages=resolved,
        )

    async def message(
        self,
        action: str = "send",
        message: str | None = None,
        channel: str | None = None,
        target: str | None = None,
        thread_id: str | None = None,
    ) -> str:
        """Send or manage cross-channel messages."""
        context = get_openclaw_tool_context()
        if context is None:
            return self._context_error("message")

        normalized_action = action.strip().lower()
        room_id = channel or context.room_id

        if normalized_action in {"send", "thread-reply", "reply"}:
            effective_thread_id = thread_id
            if normalized_action in {"thread-reply", "reply"} and effective_thread_id is None:
                effective_thread_id = context.thread_id
            return await self._message_send_or_reply(
                context,
                action=normalized_action,
                message=message,
                room_id=room_id,
                effective_thread_id=effective_thread_id,
            )
        if normalized_action == "react":
            return await self._message_react(
                context,
                message=message,
                room_id=room_id,
                target=target,
            )
        if normalized_action == "read":
            return await self._message_read(
                context,
                room_id=room_id,
                effective_thread_id=thread_id or context.thread_id,
            )

        return self._payload(
            "message",
            "error",
            action=action,
            message="Unsupported action. Use send, thread-reply, react, or read.",
        )

    async def cron(self, request: str) -> str:
        """Schedule a task using the scheduler tool."""
        if not request.strip():
            return self._payload("cron", "error", message="request cannot be empty")
        result = await self._scheduler.schedule(request)
        return self._payload("cron", "ok", result=result)

    async def web_search(self, query: str, max_results: int = 5) -> str:
        """Search the web via DuckDuckGo alias."""
        if not query.strip():
            return self._payload("web_search", "error", message="query cannot be empty")
        result = self._duckduckgo.web_search(query=query, max_results=max_results)
        return self._payload("web_search", "ok", result=result)

    async def web_fetch(self, url: str) -> str:
        """Fetch web content via website tool alias."""
        if not url.strip():
            return self._payload("web_fetch", "error", message="url cannot be empty")
        result = self._website.read_url(url.strip())
        return self._payload("web_fetch", "ok", result=result)

    def _get_browser_tool(self) -> Toolkit:
        if self._browser_tool is None:
            self._browser_tool = get_tool_by_name("browser")
        return self._browser_tool

    async def browser(
        self,
        action: str,
        target: str | None = None,
        node: str | None = None,
        profile: str | None = None,
        target_url: str | None = None,
        target_id: str | None = None,
        limit: int | None = None,
        max_chars: int | None = None,
        mode: str | None = None,
        snapshot_format: str | None = None,
        refs: str | None = None,
        interactive: bool | None = None,
        compact: bool | None = None,
        depth: int | None = None,
        selector: str | None = None,
        frame: str | None = None,
        labels: bool | None = None,
        full_page: bool | None = None,
        ref: str | None = None,
        element: str | None = None,
        type_: str | None = None,
        level: str | None = None,
        paths: list[str] | None = None,
        input_ref: str | None = None,
        timeout_ms: int | None = None,
        accept: bool | None = None,
        prompt_text: str | None = None,
        request: dict[str, Any] | None = None,
    ) -> str:
        """Invoke the first-class browser tool via OpenClaw-compatible shape."""
        try:
            browser_tool = self._get_browser_tool()
        except ImportError as exc:
            return self._payload("browser", "error", message=f"browser tool unavailable: {exc}")

        browser_function = browser_tool.functions.get("browser") or browser_tool.async_functions.get("browser")
        if browser_function is None or browser_function.entrypoint is None:
            return self._payload("browser", "error", message="browser tool does not expose browser entrypoint.")

        call_kwargs: dict[str, Any] = {
            "action": action,
            "target": target,
            "node": node,
            "profile": profile,
            "targetUrl": target_url,
            "targetId": target_id,
            "limit": limit,
            "maxChars": max_chars,
            "mode": mode,
            "snapshotFormat": snapshot_format,
            "refs": refs,
            "interactive": interactive,
            "compact": compact,
            "depth": depth,
            "selector": selector,
            "frame": frame,
            "labels": labels,
            "fullPage": full_page,
            "ref": ref,
            "element": element,
            "type": type_,
            "level": level,
            "paths": paths,
            "inputRef": input_ref,
            "timeoutMs": timeout_ms,
            "accept": accept,
            "promptText": prompt_text,
            "request": request,
        }
        call_kwargs = {key: value for key, value in call_kwargs.items() if value is not None}

        try:
            result = browser_function.entrypoint(**call_kwargs)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            return self._payload("browser", "error", action=action, message=str(exc))

        return self._payload("browser", "ok", action=action, result=result)

    async def _run_shell(self, command: str, tool_name: str) -> str:
        """Shared shell execution for exec and process."""
        if not command.strip():
            return self._payload(tool_name, "error", message="command cannot be empty")

        parse_error: str | None = None
        try:
            args = shlex.split(command)
        except ValueError as exc:
            parse_error = f"invalid shell command: {exc}"
            args = []

        if parse_error is not None or not args:
            return self._payload(
                tool_name,
                "error",
                command=command,
                message=parse_error or "command parsed to empty args",
            )

        shell_function = self._shell.functions.get("run_shell_command") or self._shell.async_functions.get(
            "run_shell_command",
        )
        if shell_function is None or shell_function.entrypoint is None:
            return self._payload(
                tool_name,
                "error",
                message="shell tool does not expose run_shell_command.",
            )

        # Agno ShellTools doesn't accept per-call env overrides, so for OpenClaw
        # compatibility we intentionally enrich process-wide PATH once before exec aliases.
        # Subsequent subprocess calls in this process observe the merged PATH.
        self._ensure_login_shell_path()

        try:
            result = shell_function.entrypoint(args)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            logger.exception(f"Shell command failed: {command}")
            return self._payload(tool_name, "error", command=command, message="shell command failed")

        return self._payload(tool_name, "ok", command=command, result=result)

    async def exec(self, command: str) -> str:
        """Execute a shell command via shell tool alias."""
        return await self._run_shell(command, "exec")

    async def process(self, command: str) -> str:
        """Execute a shell command (alias for exec)."""
        return await self._run_shell(command, "process")

    # ── Coding tool aliases ────────────────────────────────────────────

    def read_file(
        self,
        path: str,
        offset: int | None = None,
        limit: int | None = None,
    ) -> str:
        """Read a file with line numbers and pagination hints."""
        result = self._coding.read_file(path, offset, limit)
        status = self._coding_status(result)
        return self._payload("read_file", status, result=result)

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        """Replace a specific text occurrence in a file using fuzzy matching."""
        result = self._coding.edit_file(path, old_text, new_text)
        status = self._coding_status(result)
        return self._payload("edit_file", status, result=result)

    def write_file(self, path: str, content: str) -> str:
        """Write content to a file, creating parent directories if needed."""
        result = self._coding.write_file(path, content)
        status = self._coding_status(result)
        return self._payload("write_file", status, result=result)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_case: bool = False,
        literal: bool = False,
        context: int = 0,
        limit: int = 100,
    ) -> str:
        """Search file contents for a pattern."""
        result = self._coding.grep(pattern, path, glob, ignore_case, literal, context, limit)
        status = self._coding_status(result)
        return self._payload("grep", status, result=result)

    def find_files(
        self,
        pattern: str,
        path: str | None = None,
        limit: int = 1000,
    ) -> str:
        """Find files matching a glob pattern."""
        result = self._coding.find_files(pattern, path, limit)
        status = self._coding_status(result)
        return self._payload("find_files", status, result=result)

    def ls(self, path: str | None = None, limit: int = 500) -> str:
        """List directory contents."""
        result = self._coding.ls(path, limit)
        status = self._coding_status(result)
        return self._payload("ls", status, result=result)
