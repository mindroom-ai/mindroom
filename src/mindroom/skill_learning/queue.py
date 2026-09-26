"""Durable per-conversation reply counts for automatic skill learning.

Entries are keyed by agent, private worker scope, and session, never by requester, so a shared thread is reviewed
once however many people talk in it. Like Hermes' counter, each completed response to a person adds its model
replies, one per tool-calling step plus the final answer, and a chat-time ``skill_manage`` call restarts the count.
A review subtracts the replies it covered, so replies that arrive while it runs count toward the next one. The counts
live in the storage root, so they survive restarts. A failed review keeps its count for the next completed reply to
retry and is abandoned after three consecutive failures.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Literal

from agno.run.agent import RunOutput
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mindroom.agent_storage import create_session_storage
from mindroom.atomic_file import atomic_write_bytes_at
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
_MAX_FAILURES = 3


class QueueEntry(BaseModel):
    """Reply count and retry state for one conversation scope."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    session: str
    worker_key: str | None
    identity: dict[str, object] | None
    replies: int = 0
    # Bumped whenever a chat-time skill_manage call restarts the count, so a review that started before
    # the restart never subtracts replies it did not cover.
    generation: int = 0
    failures: int = 0
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
    """Store the queue, forgetting conversations without a counted response for 30 days."""
    cutoff = time.time() - _STALE_SECONDS
    current = sorted(
        ((key, entry) for key, entry in state.entries.items() if entry.last_seen_at >= cutoff),
        key=lambda item: item[1].last_seen_at,
    )[-_MAX_ENTRIES:]
    with open_directory_within_root(runtime_paths.storage_root) as storage_fd:
        atomic_write_bytes_at(storage_fd, _STATE_FILENAME, _State(entries=dict(current)).model_dump_json().encode())


def _locked(runtime_paths: RuntimePaths) -> AbstractContextManager[None]:
    runtime_paths.storage_root.mkdir(parents=True, exist_ok=True)
    return advisory_file_lock(runtime_paths.storage_root / "skill_learning_state.lock")


def _scope_worker_key(config: Config, agent_name: str, identity: ToolExecutionIdentity | None) -> str | None:
    execution = resolve_agent_execution(agent_name, config, execution_identity=identity)
    return execution.worker_key if execution.is_private else None


def _key(agent_name: str, worker_key: str | None, session_id: str) -> str:
    return f"{agent_name}:{worker_key}:{session_id}" if worker_key is not None else f"{agent_name}:{session_id}"


def review_key(
    config: Config,
    agent_name: str,
    session_id: str,
    identity: ToolExecutionIdentity | None,
) -> str:
    """Return the conversation scope whose replies one review covers."""
    return _key(agent_name, _scope_worker_key(config, agent_name, identity), session_id)


def _run_replies(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    identity: ToolExecutionIdentity | None,
    run_ids: Sequence[str],
) -> tuple[int, bool]:
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
) -> tuple[str, QueueEntry] | None:
    """Add the model replies of a person's response to its conversation's count, without its content.

    ``run_ids`` are the response's runs, for example one more after it loaded a tool. Returns the conversation's
    entry when its count reached the review interval.
    """
    agent = config.agents.get(agent_name)
    if agent is None or not agent.skill_learning.enabled:
        return None
    replies, restarted = _run_replies(config, runtime_paths, agent_name, execution_identity, run_ids)
    if replies == 0 and not restarted:
        return None
    worker_key = _scope_worker_key(config, agent_name, execution_identity)
    key = _key(agent_name, worker_key, session_id)
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
        count = (
            {"replies": replies, "generation": entry.generation + 1}
            if restarted
            else {"replies": entry.replies + replies}
        )
        entry = entry.model_copy(update={"identity": identity, "last_seen_at": now, **count})
        state.entries[key] = entry
        _write(runtime_paths, state)
    return (key, entry) if entry.replies >= agent.skill_learning.review_interval else None


def _entry_is_current(config: Config, entry: QueueEntry) -> bool:
    agent = config.agents.get(entry.agent)
    if agent is None or not agent.skill_learning.enabled:
        return False
    try:
        return _scope_worker_key(config, entry.agent, entry.execution_identity()) == entry.worker_key
    except (TypeError, ValueError):
        # A malformed or no longer resolvable scope retires only its own entry.
        return False


def drop_retired_reviews(config: Config, runtime_paths: RuntimePaths) -> None:
    """Drop the entries of agents that stopped learning and of changed scopes.

    The orchestrator calls this on every config change, so learning turned off and on again starts every count from
    zero. Scope resolution happens outside the lock that completed responses also take.
    """
    if not (runtime_paths.storage_root / _STATE_FILENAME).exists():
        return
    with _locked(runtime_paths):
        state = _read(runtime_paths)
    retired = {key: entry for key, entry in state.entries.items() if not _entry_is_current(config, entry)}
    if not retired:
        return
    with _locked(runtime_paths):
        state = _read(runtime_paths)
        for key, snapshot in retired.items():
            # A response counted since the snapshot keeps its entry for the next config change to judge.
            if state.entries.get(key) == snapshot:
                del state.entries[key]
        _write(runtime_paths, state)


def settle_review(
    runtime_paths: RuntimePaths,
    key: str,
    *,
    claimed: QueueEntry,
    outcome: Literal["reviewed", "failed", "interrupted"],
) -> None:
    """Close one review attempt.

    A review subtracts the replies it covered, unless the count restarted since it began. A failure keeps them for the
    next completed reply to retry, and the third consecutive one is abandoned like a review. An interruption keeps
    everything.
    """
    if outcome == "interrupted":
        return
    with _locked(runtime_paths):
        state = _read(runtime_paths)
        entry = state.entries.get(key)
        if entry is None:
            return
        failures = entry.failures + 1
        if outcome == "failed" and failures < _MAX_FAILURES:
            update: dict[str, int] = {"failures": failures}
        else:
            covered = claimed.replies if entry.generation == claimed.generation else 0
            update = {"replies": max(0, entry.replies - covered), "failures": 0}
        state.entries[key] = entry.model_copy(update=update)
        _write(runtime_paths, state)
