"""Each agent's live background jobs are published as room state, so a client can show and cancel them."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.matrix.event_types import BACKGROUND_JOBS_EVENT_TYPE
from mindroom.matrix.identity import MatrixID
from mindroom.tool_jobs.publication import JobStatePublisher, _jobs_state_content
from mindroom.tool_jobs.runtime import BackgroundJob, BackgroundOutcome
from tests.tool_job_helpers import job_owner, start_job, tool_job_runtime, wait_for_status

if TYPE_CHECKING:
    from pathlib import Path


def _job(job_id: str, *, agent: str = "agent_a", room: str = "!room:test", status: str = "running") -> BackgroundJob:
    owner = replace(job_owner(), agent_name=agent, room_id=room, resolved_thread_id="$thread")
    return BackgroundJob(
        job_id=job_id,
        owner=owner,
        tool_name="run_tests",
        depth=0,
        adapter={"arguments": {"secret": "do not publish"}},
        status=status,  # type: ignore[arg-type]
        created_at=f"2026-10-11T00:00:0{job_id[-1]}+00:00",
    )


def _bot(agent: str) -> MagicMock:
    bot = MagicMock()
    bot.matrix_id = MatrixID.parse(f"@mindroom_{agent}:test")
    bot.client.rooms = {"!room:test": MagicMock()}
    bot.client.room_put_state = AsyncMock(
        return_value=nio.RoomPutStateResponse(event_id="$state", room_id="!room:test"),
    )
    return bot


def _bots(*agents: str) -> dict[str, MagicMock]:
    return {agent: _bot(agent) for agent in agents}


def test_content_lists_live_jobs_without_arguments() -> None:
    """Only identifying facts are published: room state is not encrypted, so no arguments or results."""
    content = _jobs_state_content(
        [_job("job_2", status="awaiting_approval"), _job("job_1"), _job("job_3", status="completed")],
    )
    jobs = content["jobs"]
    assert isinstance(jobs, list)
    assert [job["job_id"] for job in jobs] == ["job_1", "job_2"]
    assert set(jobs[0]) == {"job_id", "thread_id", "kind", "tool", "status", "requester_id", "started_at"}
    assert jobs[0]["tool"] == "run_tests"
    assert jobs[0]["kind"] == "tool"
    assert jobs[0]["thread_id"] == "$thread"
    assert jobs[1]["status"] == "awaiting_approval"


@pytest.mark.asyncio
async def test_each_agent_publishes_its_own_state_in_each_room() -> None:
    """Two agents with jobs in one room write two state events, each keyed by the agent's own account."""
    bots = _bots("agent_a", "agent_b")
    await JobStatePublisher().publish(bots.get, [_job("job_1"), _job("job_2", agent="agent_b")])
    for agent, bot in bots.items():
        bot.client.room_put_state.assert_awaited_once()
        call = bot.client.room_put_state.await_args.kwargs
        assert call["room_id"] == "!room:test"
        assert call["event_type"] == BACKGROUND_JOBS_EVENT_TYPE
        assert call["state_key"] == f"@mindroom_{agent}:test"


@pytest.mark.asyncio
async def test_unchanged_content_is_not_written_again() -> None:
    """A pass that finds the same live jobs writes nothing."""
    bots = _bots("agent_a")
    publisher = JobStatePublisher()
    await publisher.publish(bots.get, [_job("job_1")])
    await publisher.publish(bots.get, [_job("job_1")])
    assert bots["agent_a"].client.room_put_state.await_count == 1


@pytest.mark.asyncio
async def test_a_room_whose_jobs_all_ended_is_cleared() -> None:
    """When the last job of a room ends, its state lists no jobs."""
    bots = _bots("agent_a")
    publisher = JobStatePublisher()
    await publisher.publish(bots.get, [_job("job_1")])
    await publisher.publish(bots.get, [_job("job_1", status="completed")])
    assert bots["agent_a"].client.room_put_state.await_args.kwargs["content"] == {"jobs": []}


@pytest.mark.asyncio
async def test_a_failed_write_is_retried_on_the_next_pass() -> None:
    """A write the homeserver refused is written again on the next pass."""
    bots = _bots("agent_a")
    put = bots["agent_a"].client.room_put_state
    put.side_effect = [nio.RoomPutStateError("forbidden"), put.return_value]
    publisher = JobStatePublisher()
    await publisher.publish(bots.get, [_job("job_1")])
    await publisher.publish(bots.get, [_job("job_1")])
    assert put.await_count == 2


@pytest.mark.asyncio
async def test_recovered_interrupted_jobs_leave_the_published_state(tmp_path: Path) -> None:
    """A new process clears state an earlier one left listing jobs a restart interrupted."""
    runtime = await tool_job_runtime(tmp_path)
    owner = job_owner()

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("interrupted", "Restart interrupted the job.")

    try:
        await start_job(runtime, "job_1", tool_name="run_tests", depth=0, adapter={}, owner=owner, operation=operation)
        await wait_for_status(runtime, "job_1", "interrupted")
        bots = _bots(owner.recipient)
        await JobStatePublisher().publish(bots.get, runtime.retained_jobs())
        call = bots[owner.recipient].client.room_put_state.await_args.kwargs
        assert call["room_id"] == owner.room_id
        assert call["content"] == {"jobs": []}
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_room_the_bot_left_gets_no_write() -> None:
    """The bot writes no state into a room it is no longer in."""
    bots = _bots("agent_a")
    bots["agent_a"].client.rooms = {}
    await JobStatePublisher().publish(bots.get, [_job("job_1")])
    bots["agent_a"].client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_bot_that_rejoins_a_room_rewrites_its_state() -> None:
    """A list written before the bot left is replaced when it is back, though the jobs changed while it was gone."""
    bots = _bots("agent_a")
    publisher = JobStatePublisher()
    await publisher.publish(bots.get, [_job("job_1")])
    rooms = bots["agent_a"].client.rooms
    bots["agent_a"].client.rooms = {}
    await publisher.publish(bots.get, [_job("job_1", status="cancelled")])
    bots["agent_a"].client.rooms = rooms
    await publisher.publish(bots.get, [])
    assert bots["agent_a"].client.room_put_state.await_count == 2
    assert bots["agent_a"].client.room_put_state.await_args.kwargs["content"] == {"jobs": []}


@pytest.mark.asyncio
async def test_a_forbidden_write_is_not_retried_until_the_jobs_change() -> None:
    """A room where the bot may not write the state is left alone until its jobs change."""
    bots = _bots("agent_a")
    put = bots["agent_a"].client.room_put_state
    put.return_value = nio.RoomPutStateError("forbidden", status_code="M_FORBIDDEN")
    publisher = JobStatePublisher()
    await publisher.publish(bots.get, [_job("job_1")])
    await publisher.publish(bots.get, [_job("job_1")])
    assert put.await_count == 1
    await publisher.publish(bots.get, [_job("job_1"), _job("job_2")])
    assert put.await_count == 2
