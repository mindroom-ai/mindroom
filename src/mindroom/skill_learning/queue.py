"""Durable per-conversation review counters for automatic skill learning.

Entries are keyed by agent, private worker scope, and session, never by requester, so a shared thread is reviewed
once however many people talk in it. Completed runs are recorded by ID and counted exactly once when the worker
settles them, so a crash or retry can neither lose nor double-count model replies.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mindroom.file_locks import advisory_file_lock
from mindroom.logging_config import get_logger
from mindroom.runtime_resolution import resolve_agent_execution
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    parse_tool_execution_identity_payload,
    serialize_tool_execution_identity,
)

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Collection
    from contextlib import AbstractContextManager
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_STATE_FILENAME = "skill_learning_state.json"
_MAX_PENDING_RUN_IDS = 500
_MAX_ENTRIES = 5000
_MAX_DUE_ENTRIES = 32
_STALE_SECONDS = 30 * 86400
_RETRY_SECONDS = 60
_MAX_RETRY_SECONDS = 3600
_MAX_FAILURES = 3
_WAKE_EVENTS: set[asyncio.Event] = set()


class QueueEntry(BaseModel):
    """Review counter for one conversation scope."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    session: str
    worker_key: str | None
    identity: dict[str, object] | None
    pending_run_ids: list[str] = Field(default_factory=list)
    iterations: int = 0
    skills_root: str | None = None
    seen_fingerprint: str | None = None
    failures: int = 0
    next_attempt_at: float = 0.0
    last_seen_at: float

    def execution_identity(self) -> ToolExecutionIdentity | None:
        """Return the latest requester scope that completed a run in this conversation."""
        return parse_tool_execution_identity_payload(self.identity) if self.identity is not None else None


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: dict[str, QueueEntry] = Field(default_factory=dict)


def _state_path(runtime_paths: RuntimePaths) -> Path:
    return runtime_paths.storage_root / _STATE_FILENAME


def _read(runtime_paths: RuntimePaths) -> _State:
    path = _state_path(runtime_paths)
    try:
        return _State.model_validate_json(path.read_bytes())
    except FileNotFoundError:
        return _State()
    except ValidationError:
        logger.warning("Resetting unreadable skill learning queue", path=str(path))
        return _State()


def _write(runtime_paths: RuntimePaths, state: _State) -> None:
    path = _state_path(runtime_paths)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(state.model_dump_json(), encoding="utf-8")
    temporary.replace(path)


def _locked(runtime_paths: RuntimePaths) -> AbstractContextManager[None]:
    runtime_paths.storage_root.mkdir(parents=True, exist_ok=True)
    return advisory_file_lock(_state_path(runtime_paths).with_suffix(".lock"))


def skill_learning_enabled(config: Config) -> bool:
    """Return whether any standalone agent opted into skill learning."""
    return any(agent.skill_learning.enabled for agent in config.agents.values())


def _scope_worker_key(config: Config, agent_name: str, identity: ToolExecutionIdentity | None) -> str | None:
    execution = resolve_agent_execution(agent_name, config, execution_identity=identity)
    return execution.worker_key if execution.is_private else None


def queue_skill_review(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    agent_name: str,
    session_id: str,
    execution_identity: ToolExecutionIdentity | None,
    run_id: str,
) -> None:
    """Record one completed run for a later review of its conversation, without storing its content."""
    agent = config.agents.get(agent_name)
    if agent is None or not agent.skill_learning.enabled:
        return
    worker_key = _scope_worker_key(config, agent_name, execution_identity)
    key = f"{agent_name}:{worker_key}:{session_id}" if worker_key is not None else f"{agent_name}:{session_id}"
    identity = serialize_tool_execution_identity(execution_identity) if execution_identity is not None else None
    with _locked(runtime_paths):
        state = _read(runtime_paths)
        entry = state.entries.get(key) or QueueEntry(
            agent=agent_name,
            session=session_id,
            worker_key=worker_key,
            identity=identity,
            last_seen_at=time.time(),
        )
        pending = [run for run in entry.pending_run_ids if run != run_id][-(_MAX_PENDING_RUN_IDS - 1) :]
        state.entries[key] = entry.model_copy(
            update={"identity": identity, "pending_run_ids": [*pending, run_id], "last_seen_at": time.time()},
        )
        _write(runtime_paths, state)
    for wake_event in tuple(_WAKE_EVENTS):
        wake_event.set()


def register_wake_event(event: asyncio.Event) -> None:
    """Wake this worker whenever a run is queued in this process."""
    _WAKE_EVENTS.add(event)


def unregister_wake_event(event: asyncio.Event) -> None:
    """Stop waking a retired worker."""
    _WAKE_EVENTS.discard(event)


def _entry_is_current(config: Config, entry: QueueEntry, now: float) -> bool:
    agent = config.agents.get(entry.agent)
    if agent is None or not agent.skill_learning.enabled:
        return False
    if now - entry.last_seen_at > _STALE_SECONDS and not entry.pending_run_ids:
        return False
    try:
        return _scope_worker_key(config, entry.agent, entry.execution_identity()) == entry.worker_key
    except ValueError:
        return False


def claim_due_reviews(config: Config, runtime_paths: RuntimePaths, *, now: float) -> list[tuple[str, QueueEntry]]:
    """Drop retired entries and return conversations with runs to count or a review to retry."""
    with _locked(runtime_paths):
        state = _read(runtime_paths)
        current = {key: entry for key, entry in state.entries.items() if _entry_is_current(config, entry, now)}
        newest = sorted(current.items(), key=lambda item: item[1].last_seen_at)[-_MAX_ENTRIES:]
        state.entries = dict(newest)
        _write(runtime_paths, state)
    due = [
        (key, entry)
        for key, entry in state.entries.items()
        if entry.next_attempt_at <= now
        and (entry.pending_run_ids or entry.iterations >= config.agents[entry.agent].skill_learning.review_interval)
    ]
    return sorted(due, key=lambda item: item[1].last_seen_at)[:_MAX_DUE_ENTRIES]


def settle_review(
    runtime_paths: RuntimePaths,
    key: str,
    counted_run_ids: Collection[str],
    *,
    iterations: int,
    skills_root: str | None = None,
    fingerprint: str | None = None,
    previous_fingerprint: str | None = None,
    failed_at: float | None = None,
) -> None:
    """Store counted runs, the review counter, and the skills state this conversation saw.

    ``previous_fingerprint`` names the state the learner changed, so every conversation that saw it moves forward
    too instead of mistaking the learner's own edits for someone else's. Omitted skills state stays unchanged.
    A failure keeps the counter and uncounted runs for a backed-off retry and abandons them after repeated failures.
    """
    with _locked(runtime_paths):
        state = _read(runtime_paths)
        if key not in state.entries:
            return
        if previous_fingerprint is not None and fingerprint is not None:
            for other_key, other in state.entries.items():
                if other.skills_root == skills_root and other.seen_fingerprint == previous_fingerprint:
                    state.entries[other_key] = other.model_copy(update={"seen_fingerprint": fingerprint})
        entry = state.entries[key]
        failures = entry.failures + 1 if failed_at is not None else 0
        retry = failed_at is not None and failures < _MAX_FAILURES
        abandoned = failed_at is not None and not retry
        state.entries[key] = entry.model_copy(
            update={
                "pending_run_ids": []
                if abandoned
                else [run for run in entry.pending_run_ids if run not in counted_run_ids],
                "iterations": 0 if abandoned else iterations,
                "skills_root": skills_root or entry.skills_root,
                "seen_fingerprint": fingerprint or entry.seen_fingerprint,
                "failures": failures if retry else 0,
                "next_attempt_at": (
                    failed_at + min(_MAX_RETRY_SECONDS, _RETRY_SECONDS * 2 ** (failures - 1))
                    if retry and failed_at is not None
                    else 0.0
                ),
            },
        )
        _write(runtime_paths, state)
