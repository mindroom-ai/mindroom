"""Each agent's live background jobs in each room, published as room state so clients can show and cancel them."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.event_types import BACKGROUND_JOBS_EVENT_TYPE
from mindroom.tool_jobs.runtime import TERMINAL_STATUSES

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.matrix.identity import MatrixID
    from mindroom.tool_jobs.runtime import BackgroundJob

logger = get_logger(__name__)
# Agents meet new rooms without limit, so once the set is full, rooms outside it warn on every refused write.
_MAX_REFUSED_ROOMS = 4096
# Tuwunel counts the replaced state's content toward the 65,535-byte event limit, so two contents share it with the rest
# of the event. Allowing 10,240 bytes for that rest (the envelope reserve Tuwunel's admin responder uses for its own
# messages), each write lists jobs in under half of what remains; a typical job takes about 300 bytes.
_MAX_LISTED_BYTES = 27_000
# Refusals the same content meets again: the room does not let the bot write this state, or the event is too large.
_FINAL_REFUSALS = frozenset({"M_FORBIDDEN", "M_TOO_LARGE"})


class _PublishingBot(Protocol):
    """The Matrix account that answers for the jobs it publishes."""

    @property
    def client(self) -> nio.AsyncClient | None: ...

    @property
    def matrix_id(self) -> MatrixID: ...


def _job_state(job: BackgroundJob) -> dict[str, object]:
    # Room state is not encrypted, so it names the job and never carries its arguments or results.
    return {
        "job_id": job.job_id,
        "thread_id": job.owner.resolved_thread_id,
        "kind": "subagent" if job.kind == "delegation" else "tool",
        "tool": job.tool_name,
        "status": job.status,
        "requester_id": job.owner.requester_id,
        "started_at": job.created_at,
    }


def _jobs_state_content(jobs: Sequence[BackgroundJob]) -> dict[str, object]:
    """Return the state content listing the newest live jobs among ``jobs`` that fit, oldest first, and their total."""
    live = sorted(
        (job for job in jobs if job.status not in TERMINAL_STATUSES),
        key=lambda job: (job.created_at, job.job_id),
    )
    listed: list[dict[str, object]] = []
    size = 0
    for job in reversed(live):
        state = _job_state(job)
        size += len(json.dumps(state, separators=(",", ":"))) + 1
        if size > _MAX_LISTED_BYTES:
            break
        listed.append(state)
    return {"jobs": listed[::-1], "total": len(live)}


@dataclass
class JobStatePublisher:
    """Write each agent's jobs state in each room whenever it changes."""

    # What each (agent, room) state holds after its last successful write.
    _published: dict[tuple[str, str], dict[str, object]] = field(default_factory=dict)
    # Each (agent, room) whose last write was refused, so the refusal warns once however often the jobs change.
    _refused: set[tuple[str, str]] = field(default_factory=set)

    async def publish(self, bots: Callable[[str], _PublishingBot | None], jobs: Sequence[BackgroundJob]) -> None:
        """Rewrite the state of every agent and room whose live jobs changed; a failed write is retried next time.

        ``jobs`` are every retained job, so a new process also clears state an earlier one left listing jobs that a
        restart interrupted.
        """
        groups: dict[tuple[str, str], list[BackgroundJob]] = {pair: [] for pair in self._published}
        for job in jobs:
            if job.owner.room_id is not None:
                groups.setdefault((job.owner.recipient, job.owner.room_id), []).append(job)
        for (agent, room_id), group in groups.items():
            content = _jobs_state_content(group)
            if self._published.get((agent, room_id)) == content:
                continue
            bot = bots(agent)
            # A room the bot left can no longer show its jobs, and refuses its writes.
            if bot is None or bot.client is None or room_id not in bot.client.rooms:
                continue
            try:
                response = await bot.client.room_put_state(
                    room_id=room_id,
                    event_type=BACKGROUND_JOBS_EVENT_TYPE,
                    content=content,
                    state_key=bot.matrix_id.full_id,
                )
            except Exception:
                logger.warning("Publishing background jobs state failed; retrying", room_id=room_id, exc_info=True)
                continue
            if isinstance(response, nio.RoomPutStateResponse):
                self._published[(agent, room_id)] = content
                self._refused.discard((agent, room_id))
            elif isinstance(response, nio.RoomPutStateError) and response.status_code in _FINAL_REFUSALS:
                self._warn_refused(agent, room_id, response.status_code)
                self._published[(agent, room_id)] = content
            else:
                logger.warning("Background jobs state was refused; retrying", room_id=room_id, response=str(response))

    def _warn_refused(self, agent: str, room_id: str, status_code: str) -> None:
        """Warn that the room refused the agent's state, unless it already refused the agent's last write."""
        if (agent, room_id) in self._refused:
            return
        if len(self._refused) < _MAX_REFUSED_ROOMS:
            self._refused.add((agent, room_id))
        logger.warning(
            "Background jobs state was refused in this room",
            agent=agent,
            room_id=room_id,
            status_code=status_code,
            event_type=BACKGROUND_JOBS_EVENT_TYPE,
        )
