"""Durable per-conversation reply counts for automatic skill learning.

Entries are keyed by agent, private worker scope, and session, never by requester, so a shared thread is reviewed
once however many people talk in it. Like Hermes' counter, each completed response to a person adds its model
replies, one per tool-calling step plus the final answer, and a review subtracts the replies it covered, so replies
that arrive while it runs count toward the next one. The counts live in the storage root, so they survive restarts.
A failed review keeps its count for a backed-off retry and is abandoned after three failures.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Literal

from agno.run.agent import RunOutput
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mindroom.agent_storage import create_session_storage
from mindroom.atomic_file import atomic_write_bytes_at
from mindroom.background_loop import WakeSignal
from mindroom.file_locks import advisory_file_lock
from mindroom.logging_config import get_logger
from mindroom.path_confinement import open_directory_within_root
from mindroom.runtime_resolution import resolve_agent_execution
from mindroom.skill_learning.transcript import count_model_replies
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    parse_tool_execution_identity_payload,
    serialize_tool_execution_identity,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import AbstractContextManager

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_STATE_FILENAME = "skill_learning_state.json"
_MAX_ENTRIES = 5000
_STALE_SECONDS = 30 * 86400
_RETRY_SECONDS = 60
_MAX_RETRY_SECONDS = 3600
_MAX_FAILURES = 3
SKILL_LEARNING_WAKE = WakeSignal()


class QueueEntry(BaseModel):
    """Reply count and retry state for one conversation scope."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    session: str
    worker_key: str | None
    identity: dict[str, object] | None
    replies: int = 0
    failures: int = 0
    next_attempt_at: float = 0.0
    last_seen_at: float

    def execution_identity(self) -> ToolExecutionIdentity | None:
        """Return the scope of the latest person whose completed response counted in this conversation."""
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


def _run_replies(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    identity: ToolExecutionIdentity | None,
    run_ids: Sequence[str],
) -> int:
    storage = create_session_storage(agent_name, config, runtime_paths, execution_identity=identity)
    try:
        runs = [storage.get_run(run_id) for run_id in dict.fromkeys(run_ids)]
    finally:
        storage.close()
    return count_model_replies(run for run in runs if isinstance(run, RunOutput))


def queue_skill_review(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    agent_name: str,
    session_id: str,
    execution_identity: ToolExecutionIdentity | None,
    run_ids: Sequence[str],
) -> None:
    """Add the model replies of a person's response to its conversation's count, without its content.

    ``run_ids`` are every run the response produced, for example one more after it loaded a tool. A run paused for
    approval counts nothing yet; the approved continuation that completes it counts it with all of its replies.
    """
    agent = config.agents.get(agent_name)
    if agent is None or not agent.skill_learning.enabled:
        return
    replies = _run_replies(config, runtime_paths, agent_name, execution_identity, run_ids)
    if replies == 0:
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
        entry = entry.model_copy(update={"identity": identity, "replies": entry.replies + replies, "last_seen_at": now})
        state.entries[key] = entry
        _write(runtime_paths, state)
    if entry.replies >= agent.skill_learning.review_interval:
        SKILL_LEARNING_WAKE.notify()


def _is_due(config: Config, entry: QueueEntry) -> bool:
    return entry.replies >= config.agents[entry.agent].skill_learning.review_interval


def _entry_is_current(config: Config, entry: QueueEntry, now: float) -> bool:
    agent = config.agents.get(entry.agent)
    if agent is None or not agent.skill_learning.enabled:
        return False
    if now - entry.last_seen_at > _STALE_SECONDS and not _is_due(config, entry):
        return False
    try:
        return _scope_worker_key(config, entry.agent, entry.execution_identity()) == entry.worker_key
    except (TypeError, ValueError):
        # A malformed or no longer resolvable scope retires only its own entry.
        return False


def drop_retired_reviews(config: Config, runtime_paths: RuntimePaths, *, now: float) -> list[tuple[str, QueueEntry]]:
    """Drop entries of disabled agents, idle conversations short of a review, and changed scopes; return the rest.

    The orchestrator also calls this on every config change, so learning turned off and on again starts every count
    from zero. Scope resolution happens outside the lock that completed responses also take.
    """
    if not (runtime_paths.storage_root / _STATE_FILENAME).exists():
        return []
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
                # A response counted since the snapshot keeps its entry for the next cycle to judge.
                if state.entries.get(key) == snapshot:
                    del state.entries[key]
            _write(runtime_paths, state)
    return current


def claim_due_reviews(config: Config, runtime_paths: RuntimePaths, *, now: float) -> list[tuple[str, QueueEntry]]:
    """Drop retired entries and return conversations whose count reached the interval, retries waiting their turn."""
    current = drop_retired_reviews(config, runtime_paths, now=now)
    return [(key, entry) for key, entry in current if _is_due(config, entry) and entry.next_attempt_at <= now]


def settle_review(
    runtime_paths: RuntimePaths,
    key: str,
    *,
    claimed: QueueEntry,
    outcome: Literal["reviewed", "failed", "interrupted"],
    now: float,
) -> None:
    """Close one review attempt.

    A review subtracts the replies it covered. A failure keeps them for a backed-off retry, and the third one is
    abandoned like a review. An interruption by shutdown keeps everything for the next start.
    """
    with _locked(runtime_paths):
        state = _read(runtime_paths)
        entry = state.entries.get(key)
        if entry is None:
            return
        failures = entry.failures + 1
        done = {"replies": max(0, entry.replies - claimed.replies), "failures": 0, "next_attempt_at": 0.0}
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
