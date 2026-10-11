"""Each agent's live background jobs in each room, published as room state so clients can show and cancel them."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import nio

from mindroom.logging_config import get_logger
from mindroom.tool_jobs.runtime import TERMINAL_STATUSES

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.matrix.identity import MatrixID
    from mindroom.tool_jobs.runtime import BackgroundJob

logger = get_logger(__name__)

_BACKGROUND_JOBS_STATE_TYPE = "io.mindroom.background_jobs"


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
    """Return the state content listing the live jobs among ``jobs``, oldest first."""
    live = sorted(
        (job for job in jobs if job.status not in TERMINAL_STATUSES),
        key=lambda job: (job.created_at, job.job_id),
    )
    return {"jobs": [_job_state(job) for job in live]}


@dataclass
class JobStatePublisher:
    """Write each agent's jobs state in each room whenever it changes."""

    # What each (agent, room) state holds after its last successful write.
    _published: dict[tuple[str, str], dict[str, object]] = field(default_factory=dict)

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
            if bot is None or bot.client is None:
                continue
            try:
                response = await bot.client.room_put_state(
                    room_id=room_id,
                    event_type=_BACKGROUND_JOBS_STATE_TYPE,
                    content=content,
                    state_key=bot.matrix_id.full_id,
                )
            except Exception:
                logger.warning("Publishing background jobs state failed; retrying", room_id=room_id, exc_info=True)
                continue
            if isinstance(response, nio.RoomPutStateResponse):
                self._published[(agent, room_id)] = content
            else:
                logger.warning("Background jobs state was refused; retrying", room_id=room_id, response=str(response))
