"""Background auto-flush for file-backed memory."""

from __future__ import annotations

import asyncio
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypedDict, cast

from agno.agent import Agent
from agno.db.base import SessionType
from agno.session.agent import AgentSession

from mindroom.agents import create_session_storage
from mindroom.ai import get_model_instance
from mindroom.logging_config import get_logger
from mindroom.memory.functions import append_agent_daily_memory, list_all_agent_memories

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.config.memory import MemoryAutoFlushConfig

logger = get_logger(__name__)

_FLUSH_STATE_FILENAME = "memory_flush_state.json"
_STATE_LOCK = threading.Lock()
_WAKE_EVENTS: set[asyncio.Event] = set()


class _FlushSessionEntry(TypedDict, total=False):
    """Persistent flush metadata per (agent, session)."""

    agent_name: str
    session_id: str
    room_id: str | None
    thread_id: str | None
    dirty: bool
    in_flight: bool
    first_dirty_at: int
    last_seen_at: int
    last_session_updated_at: int | None
    last_flushed_at: int | None
    last_flushed_session_updated_at: int | None
    next_attempt_at: int | None
    consecutive_failures: int
    priority_boost_at: int | None
    dirty_revision: int
    flush_started_dirty_revision: int | None


class _FlushState(TypedDict):
    """On-disk auto-flush state payload."""

    version: int
    sessions: dict[str, _FlushSessionEntry]


def _state_path(storage_path: Path) -> Path:
    root = storage_path.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root / _FLUSH_STATE_FILENAME


def _now_ts() -> int:
    return int(datetime.now(UTC).timestamp())


def _empty_state() -> _FlushState:
    return {"version": 1, "sessions": {}}


def _session_key(agent_name: str, session_id: str) -> str:
    return f"{agent_name}:{session_id}"


def _read_state_unlocked(storage_path: Path) -> _FlushState:
    path = _state_path(storage_path)
    if not path.exists():
        return _empty_state()

    payload = path.read_text(encoding="utf-8").strip()
    if not payload:
        return _empty_state()

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        logger.warning("Invalid memory auto-flush state JSON; resetting state")
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    sessions_raw = data.get("sessions")
    sessions = sessions_raw if isinstance(sessions_raw, dict) else {}
    return {"version": 1, "sessions": sessions}


def _write_state_unlocked(storage_path: Path, state: _FlushState) -> None:
    path = _state_path(storage_path)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(f"{json.dumps(state, ensure_ascii=True, indent=2)}\n", encoding="utf-8")
    tmp_path.replace(path)


def _notify_workers() -> None:
    for wake_event in tuple(_WAKE_EVENTS):
        wake_event.set()


def auto_flush_enabled(config: Config) -> bool:
    """Return whether file-memory auto-flush is enabled."""
    return config.memory.auto_flush.enabled and config.uses_file_memory()


def _agent_uses_file_memory(config: Config, agent_name: str) -> bool:
    if agent_name not in config.agents:
        return False
    return config.get_agent_memory_backend(agent_name) == "file"


def mark_auto_flush_dirty_session(
    storage_path: Path,
    config: Config,
    *,
    agent_name: str,
    session_id: str,
    room_id: str | None,
    thread_id: str | None,
) -> None:
    """Mark one agent session as dirty for background auto-flush."""
    if not auto_flush_enabled(config) or not _agent_uses_file_memory(config, agent_name):
        return

    now = _now_ts()
    key = _session_key(agent_name, session_id)

    with _STATE_LOCK:
        state = _read_state_unlocked(storage_path)
        sessions = state["sessions"]
        existing = sessions.get(key, {})
        first_dirty_at = existing.get("first_dirty_at", now)
        dirty_revision = existing.get("dirty_revision", 0)
        if not isinstance(dirty_revision, int):
            dirty_revision = 0
        if not existing.get("dirty", False):
            first_dirty_at = now

        sessions[key] = {
            **existing,
            "agent_name": agent_name,
            "session_id": session_id,
            "room_id": room_id,
            "thread_id": thread_id,
            "dirty": True,
            "dirty_revision": dirty_revision + 1,
            # Keep in-flight status if a flush is already running for this key.
            "in_flight": bool(existing.get("in_flight", False)),
            "first_dirty_at": first_dirty_at,
            "last_seen_at": now,
            "next_attempt_at": None,
        }
        _write_state_unlocked(storage_path, state)

    _notify_workers()


def reprioritize_auto_flush_sessions(
    storage_path: Path,
    config: Config,
    *,
    agent_name: str,
    active_session_id: str,
) -> None:
    """Raise priority of other dirty sessions for the same agent."""
    if not auto_flush_enabled(config) or not _agent_uses_file_memory(config, agent_name):
        return

    max_reprioritize = config.memory.auto_flush.max_cross_session_reprioritize
    if max_reprioritize <= 0:
        return

    now = _now_ts()
    with _STATE_LOCK:
        state = _read_state_unlocked(storage_path)
        sessions = state["sessions"]
        candidates = [
            (key, entry)
            for key, entry in sessions.items()
            if entry.get("agent_name") == agent_name
            and entry.get("session_id") != active_session_id
            and entry.get("dirty", False)
        ]
        candidates.sort(key=lambda item: item[1].get("first_dirty_at", now))
        for key, entry in candidates[:max_reprioritize]:
            entry["priority_boost_at"] = now
            sessions[key] = entry
        _write_state_unlocked(storage_path, state)

    _notify_workers()


def _coerce_agent_session(raw_session: object) -> AgentSession | None:
    if isinstance(raw_session, AgentSession):
        return raw_session
    if isinstance(raw_session, dict):
        session_payload = cast("dict[str, Any]", raw_session)
        return AgentSession.from_dict(session_payload)
    return None


def _load_agent_session(storage_path: Path, agent_name: str, session_id: str) -> AgentSession | None:
    storage = create_session_storage(agent_name, storage_path)
    raw_session = storage.get_session(session_id, SessionType.AGENT)
    return _coerce_agent_session(raw_session)


def _entry_priority_key(entry: _FlushSessionEntry, now: int) -> tuple[int, int]:
    boosted = entry.get("priority_boost_at")
    priority_rank = 0 if isinstance(boosted, int) and boosted > 0 else 1
    return (priority_rank, entry.get("first_dirty_at", now))


def _select_recent_chat_lines(
    session: AgentSession,
    *,
    max_messages: int,
    max_chars: int,
) -> list[str]:
    messages = session.get_chat_history()
    selected: list[str] = []
    char_count = 0
    for message in reversed(messages):
        role = message.role
        if role not in {"user", "assistant"}:
            continue
        content = message.content
        if not isinstance(content, str):
            continue
        cleaned = " ".join(content.split())
        if not cleaned:
            continue
        if len(cleaned) > 500:
            cleaned = f"{cleaned[:497]}..."
        line = f"{role}: {cleaned}"
        next_count = char_count + len(line) + 1
        if selected and next_count > max_chars:
            break
        selected.append(line)
        char_count = next_count
        if len(selected) >= max_messages:
            break
    selected.reverse()
    return selected


def _normalize_extractor_line(line: str, no_reply_token: str) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    if stripped.upper() == no_reply_token.upper():
        return None
    if stripped.startswith(("- ", "* ")):
        stripped = stripped[2:].strip()
    stripped = re.sub(r"^\d+\.\s+", "", stripped)
    return stripped if stripped else None


def _sanitize_extractor_output(raw_output: str, no_reply_token: str) -> str | None:
    cleaned = raw_output.strip()
    if not cleaned:
        return None
    if cleaned.upper() == no_reply_token.upper():
        return None

    normalized_lines = [
        normalized
        for line in cleaned.splitlines()
        if (normalized := _normalize_extractor_line(line, no_reply_token)) is not None
    ]

    if not normalized_lines:
        return None

    deduped: list[str] = []
    seen: set[str] = set()
    for line in normalized_lines:
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(line)
    return " | ".join(deduped[:10])


async def _build_existing_memory_context(
    *,
    agent_name: str,
    storage_path: Path,
    config: Config,
) -> str:
    context_config = config.memory.auto_flush.extractor.include_memory_context
    if context_config.memory_snippets <= 0:
        return ""

    max_memories = max(context_config.memory_snippets * 8, context_config.memory_snippets)
    memories = await list_all_agent_memories(agent_name, storage_path, config, limit=max_memories)
    if not memories:
        return ""

    snippets: list[str] = []
    for memory in reversed(memories):
        text = memory.get("memory", "").strip()
        if not text:
            continue
        if len(text) > context_config.snippet_max_chars:
            text = f"{text[: context_config.snippet_max_chars - 3]}..."
        snippets.append(f"- {text}")
        if len(snippets) >= context_config.memory_snippets:
            break

    snippets.reverse()
    return "\n".join(snippets)


async def _extract_memory_summary(
    *,
    config: Config,
    storage_path: Path,
    agent_name: str,
    session_id: str,
    lines: list[str],
) -> str | None:
    extractor = config.memory.auto_flush.extractor
    if not lines:
        return None

    existing_context = await _build_existing_memory_context(
        agent_name=agent_name,
        storage_path=storage_path,
        config=config,
    )
    existing_block = (
        f"\nExisting memory snippets (avoid duplicates):\n{existing_context}\n"
        if existing_context
        else "\nExisting memory snippets: (none)\n"
    )
    excerpt = "\n".join(lines)
    prompt = (
        "Extract only durable memories from this conversation excerpt.\n"
        "Keep only stable facts, explicit preferences, decisions, commitments, and action items.\n"
        "Skip chit-chat, temporary statements, and one-off tool output.\n"
        f"If nothing should be stored, output exactly: {extractor.no_reply_token}\n"
        "Output plain lines only, one memory per line, no commentary.\n"
        f"{existing_block}\n"
        "Conversation excerpt:\n"
        f"{excerpt}\n"
    )

    model_name = config.get_entity_model_name(agent_name)
    model = get_model_instance(config, model_name)
    extractor_agent = Agent(
        name="MemoryAutoFlushExtractor",
        role="Extract durable memory statements for long-term memory storage.",
        model=model,
    )
    response = await extractor_agent.arun(prompt, session_id=f"memory_auto_flush_extract:{agent_name}:{session_id}")
    content = response.content
    raw_output = content if isinstance(content, str) else str(content or "")
    return _sanitize_extractor_output(raw_output, extractor.no_reply_token)


def _retry_cooldown_seconds(settings: MemoryAutoFlushConfig, failures: int) -> int:
    if failures <= 0:
        return settings.retry_cooldown_seconds
    multiplier = 2 ** max(0, failures - 1)
    return min(
        settings.max_retry_cooldown_seconds,
        settings.retry_cooldown_seconds * multiplier,
    )


@dataclass
class MemoryAutoFlushWorker:
    """Background worker that flushes dirty sessions to file memory."""

    storage_path: Path
    config_provider: Callable[[], Config | None]
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _wake_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def stop(self) -> None:
        """Request graceful shutdown of the worker loop."""
        self._stop_event.set()
        self._wake_event.set()

    async def run(self) -> None:
        """Run periodic auto-flush cycles until stopped."""
        _WAKE_EVENTS.add(self._wake_event)
        try:
            while not self._stop_event.is_set():
                config = self.config_provider()
                interval = 30
                if config is not None and auto_flush_enabled(config):
                    await self._run_cycle(config)
                    interval = config.memory.auto_flush.flush_interval_seconds
                self._wake_event.clear()
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=interval)
                except TimeoutError:
                    continue
        finally:
            _WAKE_EVENTS.discard(self._wake_event)

    async def _run_cycle(self, config: Config) -> None:  # noqa: C901, PLR0912, PLR0915
        now = _now_ts()
        settings = config.memory.auto_flush

        with _STATE_LOCK:
            state = _read_state_unlocked(self.storage_path)
            sessions = state["sessions"]
            stale_keys = [
                key
                for key, entry in sessions.items()
                if now - entry.get("last_seen_at", now) > settings.stale_ttl_seconds
            ]
            for key in stale_keys:
                del sessions[key]
            non_file_agent_keys = [
                key
                for key, entry in sessions.items()
                if isinstance(entry.get("agent_name"), str) and not _agent_uses_file_memory(config, entry["agent_name"])
            ]
            for key in non_file_agent_keys:
                del sessions[key]
            _write_state_unlocked(self.storage_path, state)

        with _STATE_LOCK:
            state = _read_state_unlocked(self.storage_path)
            sessions = state["sessions"]
            dirty_items = [(key, entry) for key, entry in sessions.items() if entry.get("dirty", False)]
            dirty_items.sort(key=lambda item: _entry_priority_key(item[1], now))

        selected_keys: list[str] = []
        per_agent_count: dict[str, int] = {}
        max_total = settings.batch.max_sessions_per_cycle
        max_per_agent = settings.batch.max_sessions_per_agent_per_cycle

        for key, entry in dirty_items:
            if len(selected_keys) >= max_total:
                break
            if entry.get("in_flight", False):
                continue
            next_attempt_at = entry.get("next_attempt_at")
            if isinstance(next_attempt_at, int) and next_attempt_at > now:
                continue

            agent_name = entry.get("agent_name")
            session_id = entry.get("session_id")
            if not isinstance(agent_name, str) or not isinstance(session_id, str):
                continue

            if per_agent_count.get(agent_name, 0) >= max_per_agent:
                continue

            session = _load_agent_session(self.storage_path, agent_name, session_id)
            if session is None:
                continue
            session_updated_at = session.updated_at
            entry["last_session_updated_at"] = session_updated_at
            last_flushed = entry.get("last_flushed_session_updated_at")
            if (
                isinstance(last_flushed, int)
                and isinstance(session_updated_at, int)
                and session_updated_at <= last_flushed
            ):
                entry["dirty"] = False
                with _STATE_LOCK:
                    latest_state = _read_state_unlocked(self.storage_path)
                    latest_state["sessions"][key] = entry
                    _write_state_unlocked(self.storage_path, latest_state)
                continue

            idle_ready = now - entry.get("last_seen_at", now) >= settings.idle_seconds
            age_ready = now - entry.get("first_dirty_at", now) >= settings.max_dirty_age_seconds
            if not (idle_ready or age_ready):
                continue

            selected_keys.append(key)
            per_agent_count[agent_name] = per_agent_count.get(agent_name, 0) + 1
            with _STATE_LOCK:
                latest_state = _read_state_unlocked(self.storage_path)
                latest_entry = latest_state["sessions"].get(key, entry)
                latest_entry["in_flight"] = True
                latest_entry["last_session_updated_at"] = session_updated_at
                flush_started_dirty_revision = latest_entry.get("dirty_revision", 0)
                if not isinstance(flush_started_dirty_revision, int):
                    flush_started_dirty_revision = 0
                latest_entry["flush_started_dirty_revision"] = flush_started_dirty_revision
                latest_state["sessions"][key] = latest_entry
                _write_state_unlocked(self.storage_path, latest_state)

        for key in selected_keys:
            await self._process_session_key(config, key)

    async def _process_session_key(self, config: Config, key: str) -> None:  # noqa: PLR0915
        now = _now_ts()
        settings = config.memory.auto_flush
        with _STATE_LOCK:
            state = _read_state_unlocked(self.storage_path)
            entry = state["sessions"].get(key)
        if entry is None:
            return

        agent_name = entry.get("agent_name")
        session_id = entry.get("session_id")
        session_updated_at = entry.get("last_session_updated_at")
        if not isinstance(agent_name, str) or not isinstance(session_id, str):
            return

        wrote_memory = False
        try:
            wrote_memory = await asyncio.wait_for(
                self._flush_session(config, agent_name=agent_name, session_id=session_id),
                timeout=settings.extractor.max_extraction_seconds,
            )
        except TimeoutError:
            with _STATE_LOCK:
                latest_state = _read_state_unlocked(self.storage_path)
                latest_entry = latest_state["sessions"].get(key, entry)
                failures = latest_entry.get("consecutive_failures", 0) + 1
                cooldown = _retry_cooldown_seconds(settings, failures)
                latest_entry["consecutive_failures"] = failures
                latest_entry["next_attempt_at"] = now + cooldown
                latest_entry["in_flight"] = False
                latest_entry.pop("flush_started_dirty_revision", None)
                latest_state["sessions"][key] = latest_entry
                _write_state_unlocked(self.storage_path, latest_state)
            logger.warning(
                "Memory auto-flush timed out",
                agent=agent_name,
                session_id=session_id,
                timeout_seconds=settings.extractor.max_extraction_seconds,
            )
            return
        except Exception:
            with _STATE_LOCK:
                latest_state = _read_state_unlocked(self.storage_path)
                latest_entry = latest_state["sessions"].get(key, entry)
                failures = latest_entry.get("consecutive_failures", 0) + 1
                cooldown = _retry_cooldown_seconds(settings, failures)
                latest_entry["consecutive_failures"] = failures
                latest_entry["next_attempt_at"] = now + cooldown
                latest_entry["in_flight"] = False
                latest_entry.pop("flush_started_dirty_revision", None)
                latest_state["sessions"][key] = latest_entry
                _write_state_unlocked(self.storage_path, latest_state)
            logger.exception("Memory auto-flush failed", agent=agent_name, session_id=session_id)
            return

        latest_session_updated_at: int | None = None
        latest_session = _load_agent_session(self.storage_path, agent_name, session_id)
        if latest_session is not None and isinstance(latest_session.updated_at, int):
            latest_session_updated_at = latest_session.updated_at

        with _STATE_LOCK:
            latest_state = _read_state_unlocked(self.storage_path)
            latest_entry = latest_state["sessions"].get(key, entry)
            flush_started_dirty_revision = entry.get("flush_started_dirty_revision")
            has_newer_dirty_marks = (
                isinstance(flush_started_dirty_revision, int)
                and isinstance(latest_entry.get("dirty_revision"), int)
                and latest_entry["dirty_revision"] > flush_started_dirty_revision
            )
            has_newer_updates = (
                isinstance(latest_session_updated_at, int)
                and isinstance(session_updated_at, int)
                and latest_session_updated_at > session_updated_at
            )
            # Only requeue if the session was explicitly marked dirty again during this flush.
            latest_entry["dirty"] = (
                has_newer_dirty_marks if isinstance(flush_started_dirty_revision, int) else has_newer_updates
            )
            latest_entry["in_flight"] = False
            latest_entry["last_flushed_at"] = now
            if isinstance(latest_session_updated_at, int):
                latest_entry["last_session_updated_at"] = latest_session_updated_at
            if isinstance(session_updated_at, int):
                latest_entry["last_flushed_session_updated_at"] = session_updated_at
            latest_entry["next_attempt_at"] = None
            latest_entry["consecutive_failures"] = 0
            latest_entry.pop("flush_started_dirty_revision", None)
            if not latest_entry["dirty"]:
                latest_entry["priority_boost_at"] = None
            latest_state["sessions"][key] = latest_entry
            _write_state_unlocked(self.storage_path, latest_state)

        logger.debug(
            "Memory auto-flush completed",
            agent=agent_name,
            session_id=session_id,
            wrote_memory=wrote_memory,
        )

    async def _flush_session(self, config: Config, *, agent_name: str, session_id: str) -> bool:
        session = _load_agent_session(self.storage_path, agent_name, session_id)
        if session is None:
            return False

        extractor = config.memory.auto_flush.extractor
        lines = _select_recent_chat_lines(
            session,
            max_messages=extractor.max_messages_per_flush,
            max_chars=extractor.max_chars_per_flush,
        )
        memory_summary = await _extract_memory_summary(
            config=config,
            storage_path=self.storage_path,
            agent_name=agent_name,
            session_id=session_id,
            lines=lines,
        )
        if memory_summary is None:
            return False

        session_updated = session.updated_at if isinstance(session.updated_at, int) else 0
        flush_marker = f"auto_flush:{session_id}:{session_updated}"
        memory_content = f"[{flush_marker}] {memory_summary}"

        append_agent_daily_memory(
            memory_content,
            agent_name=agent_name,
            storage_path=self.storage_path,
            config=config,
        )
        return True
