"""Durable per-conversation review counters for automatic skill learning.

Entries are keyed by agent, private worker scope, and session, never by requester, so a shared thread is reviewed
once however many people talk in it. Completed runs are recorded by ID and folded into the counter in the same
locked write that removes them, so a crash or retry can neither lose nor double-count model replies.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mindroom.atomic_file import atomic_write_bytes_at
from mindroom.background_loop import WakeSignal
from mindroom.file_locks import advisory_file_lock
from mindroom.logging_config import get_logger
from mindroom.path_confinement import open_directory_within_root
from mindroom.runtime_resolution import resolve_agent_execution
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    parse_tool_execution_identity_payload,
    serialize_tool_execution_identity,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from contextlib import AbstractContextManager

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
SKILL_LEARNING_WAKE = WakeSignal()


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


def _read(runtime_paths: RuntimePaths) -> _State:
    path = runtime_paths.storage_root / _STATE_FILENAME
    try:
        return _State.model_validate_json(path.read_bytes())
    except FileNotFoundError:
        return _State()
    except ValidationError:
        logger.warning("Resetting unreadable skill learning queue", path=str(path))
        return _State()


def _write(runtime_paths: RuntimePaths, state: _State) -> None:
    with open_directory_within_root(runtime_paths.storage_root) as storage_fd:
        atomic_write_bytes_at(storage_fd, _STATE_FILENAME, state.model_dump_json().encode())


def _locked(runtime_paths: RuntimePaths) -> AbstractContextManager[None]:
    runtime_paths.storage_root.mkdir(parents=True, exist_ok=True)
    return advisory_file_lock(runtime_paths.storage_root / "skill_learning_state.lock")


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
    run_ids: Sequence[str],
) -> None:
    """Record the persisted runs of one completed response for a later review, without storing content."""
    agent = config.agents.get(agent_name)
    if agent is None or not agent.skill_learning.enabled or not run_ids:
        return
    worker_key = _scope_worker_key(config, agent_name, execution_identity)
    key = f"{agent_name}:{worker_key}:{session_id}" if worker_key is not None else f"{agent_name}:{session_id}"
    identity = serialize_tool_execution_identity(execution_identity) if execution_identity is not None else None
    now = time.time()
    with _locked(runtime_paths):
        state = _read(runtime_paths)
        entry = state.entries.get(key) or QueueEntry(
            agent=agent_name,
            session=session_id,
            worker_key=worker_key,
            identity=identity,
            last_seen_at=now,
        )
        pending = [run for run in entry.pending_run_ids if run not in run_ids]
        state.entries[key] = entry.model_copy(
            update={
                "identity": identity,
                "pending_run_ids": [*pending, *run_ids][-_MAX_PENDING_RUN_IDS:],
                "last_seen_at": now,
            },
        )
        _write(runtime_paths, state)
    SKILL_LEARNING_WAKE.notify()


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
    """Drop retired entries and return conversations with runs to count or a review to retry.

    Scope resolution happens outside the lock that completed responses also take.
    """
    with _locked(runtime_paths):
        state = _read(runtime_paths)
    current = sorted(
        ((key, entry) for key, entry in state.entries.items() if _entry_is_current(config, entry, now)),
        key=lambda item: item[1].last_seen_at,
    )[-_MAX_ENTRIES:]
    dropped = state.entries.keys() - {key for key, _entry in current}
    if dropped:
        with _locked(runtime_paths):
            state = _read(runtime_paths)
            for key in dropped:
                state.entries.pop(key, None)
            _write(runtime_paths, state)
    due = [
        (key, entry)
        for key, entry in current
        if entry.next_attempt_at <= now
        and (entry.pending_run_ids or entry.iterations >= config.agents[entry.agent].skill_learning.review_interval)
    ]
    return due[:_MAX_DUE_ENTRIES]


def record_count(
    runtime_paths: RuntimePaths,
    key: str,
    counted_run_ids: Collection[str],
    *,
    replies: int,
    skills_root: str,
    fingerprint: str,
) -> int:
    """Fold counted runs into the conversation's counter and return it.

    Hermes resets its counter when the agent saves a skill itself; here the counter restarts when anyone other
    than the learner changed the workspace skills since this conversation last looked. The comparison uses the
    stored state, which reviews of other conversations move forward when the learner itself changes skills.
    """
    with _locked(runtime_paths):
        state = _read(runtime_paths)
        entry = state.entries.get(key)
        if entry is None:
            return 0
        foreign_change = entry.seen_fingerprint not in {None, fingerprint}
        iterations = 0 if foreign_change else entry.iterations + replies
        state.entries[key] = entry.model_copy(
            update={
                "pending_run_ids": [run for run in entry.pending_run_ids if run not in counted_run_ids],
                "iterations": iterations,
                "skills_root": skills_root,
                "seen_fingerprint": fingerprint,
            },
        )
        _write(runtime_paths, state)
    return iterations


def settle_review(
    runtime_paths: RuntimePaths,
    key: str,
    *,
    outcome: Literal["reviewed", "failed", "interrupted"],
    now: float,
    learner_change: tuple[str, str] | None = None,
) -> None:
    """Close one review attempt.

    A review restarts the counter; a failure keeps it for a backed-off retry until it is abandoned; an
    interruption by shutdown keeps everything for the next start. ``learner_change`` is the ``(before, after)``
    skills fingerprint around the learner's own writes: every conversation that saw ``before`` in the same
    workspace moves to ``after``, so those writes never read as someone else's edits.
    """
    with _locked(runtime_paths):
        state = _read(runtime_paths)
        entry = state.entries.get(key)
        if entry is None:
            return
        if learner_change is not None:
            before, after = learner_change
            for other_key, other in state.entries.items():
                if other.skills_root == entry.skills_root and other.seen_fingerprint == before:
                    state.entries[other_key] = other.model_copy(update={"seen_fingerprint": after})
            entry = state.entries[key]
        failures = entry.failures + 1
        if outcome == "reviewed":
            update: dict[str, object] = {"failures": 0, "next_attempt_at": 0.0, "iterations": 0}
        elif outcome == "interrupted":
            update = {}
        elif failures < _MAX_FAILURES:
            delay = min(_MAX_RETRY_SECONDS, _RETRY_SECONDS * 2 ** (failures - 1))
            update = {"failures": failures, "next_attempt_at": now + delay}
        else:
            update = {"failures": 0, "next_attempt_at": 0.0, "iterations": 0, "pending_run_ids": []}
        state.entries[key] = entry.model_copy(update=update)
        _write(runtime_paths, state)
