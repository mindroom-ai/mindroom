"""Durable per-conversation review markers for automatic skill learning.

Entries are keyed by agent, private worker scope, and session, never by requester, so a shared thread is reviewed
once however many people talk in it. Progress is read from the session itself: the model replies in runs after
``reviewed_through``, the position of the newest run a review already covered. A position is the run's creation
time and its run index; Agno never renumbers run indexes, and a new run can only reuse the index of a deleted
newest run with a later creation time, so compaction or redaction deleting runs cannot hide new ones.
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
    from contextlib import AbstractContextManager

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

RunPosition = tuple[int, int]
_STATE_FILENAME = "skill_learning_state.json"
_MAX_ENTRIES = 5000
_MAX_DUE_ENTRIES = 32
_STALE_SECONDS = 30 * 86400
_RETRY_SECONDS = 60
_MAX_RETRY_SECONDS = 3600
_MAX_FAILURES = 3
SKILL_LEARNING_WAKE = WakeSignal()


class QueueEntry(BaseModel):
    """Review marker for one conversation scope."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    session: str
    worker_key: str | None
    identity: dict[str, object] | None
    # The first count places the marker just before the response that created the entry, so enabling learning
    # never reviews a conversation's older history.
    first_run_id: str | None = None
    reviewed_through: RunPosition | None = None
    has_new_runs: bool = True
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
    run_id: str,
) -> None:
    """Mark one conversation for counting after a person's response completed, without storing content."""
    agent = config.agents.get(agent_name)
    if agent is None or not agent.skill_learning.enabled:
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
            first_run_id=run_id,
            last_seen_at=now,
        )
        state.entries[key] = entry.model_copy(update={"identity": identity, "has_new_runs": True, "last_seen_at": now})
        _write(runtime_paths, state)
    SKILL_LEARNING_WAKE.notify()


def _entry_is_current(config: Config, entry: QueueEntry, now: float) -> bool:
    agent = config.agents.get(entry.agent)
    if agent is None or not agent.skill_learning.enabled:
        return False
    if now - entry.last_seen_at > _STALE_SECONDS and not entry.has_new_runs:
        return False
    try:
        return _scope_worker_key(config, entry.agent, entry.execution_identity()) == entry.worker_key
    except ValueError:
        return False


def claim_due_reviews(config: Config, runtime_paths: RuntimePaths, *, now: float) -> list[tuple[str, QueueEntry]]:
    """Drop retired entries and return conversations with new runs to count or a review to retry.

    Scope resolution happens outside the lock that completed responses also take.
    """
    with _locked(runtime_paths):
        state = _read(runtime_paths)
    current = sorted(
        ((key, entry) for key, entry in state.entries.items() if _entry_is_current(config, entry, now)),
        key=lambda item: item[1].last_seen_at,
    )[-_MAX_ENTRIES:]
    dropped = {key: state.entries[key] for key in state.entries.keys() - {key for key, _entry in current}}
    if dropped:
        with _locked(runtime_paths):
            state = _read(runtime_paths)
            for key, snapshot in dropped.items():
                # A run queued since the snapshot keeps its entry for the next cycle to judge.
                if state.entries.get(key) == snapshot:
                    del state.entries[key]
            _write(runtime_paths, state)
    due = [
        (key, entry)
        for key, entry in current
        if entry.next_attempt_at <= now and (entry.has_new_runs or entry.failures)
    ]
    return due[:_MAX_DUE_ENTRIES]


def record_count(
    runtime_paths: RuntimePaths,
    key: str,
    *,
    claimed: QueueEntry,
    start: RunPosition,
    newest: RunPosition,
    skills_root: str,
    fingerprint: str,
) -> RunPosition:
    """Mark the conversation counted and return the run position its count starts after.

    ``start`` is the position just before the response that created the entry and ``newest`` the newest one.
    Hermes resets its counter when the agent saves a skill itself; here counting restarts after ``newest`` when
    anyone other than the learner changed the workspace skills since this conversation last looked. The
    comparison uses the stored state, which reviews of other conversations move forward when the learner itself
    changes skills.
    """
    with _locked(runtime_paths):
        state = _read(runtime_paths)
        entry = state.entries.get(key)
        if entry is None:
            return newest
        if entry.seen_fingerprint not in {None, fingerprint}:
            reviewed_through = newest
        else:
            reviewed_through = start if entry.reviewed_through is None else entry.reviewed_through
        update: dict[str, object] = {
            "first_run_id": None,
            "reviewed_through": reviewed_through,
            "skills_root": skills_root,
            "seen_fingerprint": fingerprint,
        }
        if entry.last_seen_at == claimed.last_seen_at:
            # A response that completed after the claim keeps its mark for the next cycle to count.
            update["has_new_runs"] = False
        state.entries[key] = entry.model_copy(update=update)
        _write(runtime_paths, state)
    return reviewed_through


def settle_review(
    runtime_paths: RuntimePaths,
    key: str,
    *,
    outcome: Literal["reviewed", "failed", "interrupted"],
    now: float,
    through: RunPosition | None = None,
    learner_change: tuple[str, str] | None = None,
) -> None:
    """Close one review attempt.

    A review moves the marker to ``through``, the newest run it saw; a failure keeps the marker for a backed-off
    retry until it is abandoned; an interruption by shutdown keeps everything for the next start. A failure that
    happened before counting passes no ``through``, and abandoning it keeps the marker. ``learner_change`` is the
    ``(before, after)``
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
        done: dict[str, object] = {"failures": 0, "next_attempt_at": 0.0}
        if through is not None:
            done["reviewed_through"] = through
        if outcome == "reviewed":
            update = done
        elif outcome == "interrupted":
            update = {}
        elif failures < _MAX_FAILURES:
            delay = min(_MAX_RETRY_SECONDS, _RETRY_SECONDS * 2 ** (failures - 1))
            update = {"failures": failures, "next_attempt_at": now + delay}
        else:
            update = done
        state.entries[key] = entry.model_copy(update=update)
        _write(runtime_paths, state)
