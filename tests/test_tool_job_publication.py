"""Each agent's live background jobs are published as room state, so a client can show and cancel them."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from structlog.testing import capture_logs

from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.matrix.event_types import BACKGROUND_JOBS_EVENT_TYPE
from mindroom.matrix.identity import MatrixID
from mindroom.orchestration import tool_job_runtime as tool_job_runtime_module
from mindroom.tool_jobs import publication
from mindroom.tool_jobs.publication import JobStatePublisher, _jobs_state_content
from mindroom.tool_jobs.runtime import BackgroundJob, BackgroundOutcome
from tests.tool_job_helpers import (
    completed_delegation_job,
    job_coordinator,
    job_owner,
    start_job,
    tool_job_runtime,
    wait_for_status,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_jobs.runtime import JobClaim, ToolJobRuntime


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
    assert set(jobs[0]) == {"job_id", "thread_id", "kind", "tool", "status", "started_at"}
    assert jobs[0]["tool"] == "run_tests"
    assert jobs[0]["kind"] == "tool"
    assert jobs[0]["thread_id"] == "$thread"
    assert jobs[1]["status"] == "awaiting_approval"


def _aged(job: BackgroundJob, age: timedelta) -> BackgroundJob:
    return replace(job, created_at=(datetime.now(UTC) - age).isoformat())


def test_a_job_running_for_moments_is_not_listed() -> None:
    """A call still awaited inline that ends within seconds never reaches the room; one waiting on a person shows."""
    fresh = timedelta(seconds=1)
    content = _jobs_state_content(
        [
            _aged(_job("job_1"), fresh),
            _aged(_job("job_2", status="awaiting_approval"), fresh),
            _aged(_job("job_3", status="cancel_requested"), fresh),
            _aged(_job("job_4"), timedelta(seconds=60)),
        ],
        awaited={"job_1", "job_2", "job_3", "job_4"},
    )
    jobs = content["jobs"]
    assert isinstance(jobs, list)
    assert sorted(job["job_id"] for job in jobs) == ["job_2", "job_3", "job_4"]
    assert content["total"] == 3


def test_an_awaited_job_is_listed_once_it_has_run_five_seconds() -> None:
    """A call that runs longer than a few seconds shows in the bar; one just under that does not yet."""
    content = _jobs_state_content(
        [_aged(_job("job_1"), timedelta(seconds=4)), _aged(_job("job_2"), timedelta(seconds=6))],
        awaited={"job_1", "job_2"},
    )
    assert [job["job_id"] for job in content["jobs"]] == ["job_2"]  # type: ignore[union-attr]


def test_a_job_its_call_no_longer_waits_for_is_listed_at_once() -> None:
    """A job its call left running in the background shows at once, while one its call still waits for does not."""
    fresh = timedelta(seconds=1)
    content = _jobs_state_content([_aged(_job("job_1"), fresh), _aged(_job("job_2"), fresh)], awaited={"job_2"})
    assert [job["job_id"] for job in content["jobs"]] == ["job_1"]  # type: ignore[union-attr]
    assert content["total"] == 1


def test_a_subagent_job_is_published_under_its_agents_name() -> None:
    """A person sees which agent a subagent job runs, not the tool that started it."""
    job = replace(completed_delegation_job(), status="running")
    content = _jobs_state_content([job])
    jobs = content["jobs"]
    assert isinstance(jobs, list)
    assert (jobs[0]["kind"], jobs[0]["tool"]) == ("subagent", "worker")


def test_job_state_names_the_subagent_and_run_of_a_delegation_job() -> None:
    """A client finds a subagent job's live frames by the child's subagent ID and the run the frames name."""
    job = replace(completed_delegation_job(), status="running")
    job.adapter["child"] = {**job.adapter["child"], "subagent_id": "s1", "delegation_id": "d1"}
    (state,) = _jobs_state_content([job])["jobs"]  # type: ignore[misc]
    assert (state["subagent"], state["run"]) == ("s1", "d1")
    assert "tool_call_id" not in state


def test_job_state_names_the_tool_call_of_a_tool_job() -> None:
    """A client finds a tool job's call in the reply's tool trace by its call ID; a job without one names none."""
    job = _job("job_1")
    job.adapter["tool_call_id"] = "call_1"
    named, unnamed = _jobs_state_content([job, _job("job_2")])["jobs"]  # type: ignore[misc]
    assert named["tool_call_id"] == "call_1"
    assert "tool_call_id" not in unnamed
    assert "subagent" not in named
    assert "run" not in named


@pytest.mark.parametrize("value", ["", None, 7, ["call_1"]])
def test_job_state_leaves_out_a_join_key_that_is_not_a_non_empty_string(value: object) -> None:
    """A join key a client cannot use is left out, so the client shows the job by its name alone."""
    tool_job = _job("job_1")
    tool_job.adapter["tool_call_id"] = value
    subagent_job = replace(completed_delegation_job(), status="running")
    subagent_job.adapter["child"] = {**subagent_job.adapter["child"], "subagent_id": value, "delegation_id": value}
    for job in (tool_job, subagent_job):
        (state,) = _jobs_state_content([job])["jobs"]  # type: ignore[misc]
        assert set(state) == {"job_id", "thread_id", "kind", "tool", "status", "started_at"}


def test_job_state_never_carries_arguments_or_progress() -> None:
    """Room state is not encrypted: neither kind of job publishes its arguments, its task, or its progress."""
    tool_job = replace(_job("job_1"), progress="read /secret/path")
    tool_job.adapter["tool_call_id"] = "call_1"
    subagent_job = replace(completed_delegation_job(), status="running", progress="Calling shell")
    content = _jobs_state_content([tool_job, subagent_job])
    allowed = {"job_id", "thread_id", "kind", "tool", "status", "started_at", "subagent", "run", "tool_call_id"}
    for state in content["jobs"]:  # type: ignore[union-attr]
        assert set(state) <= allowed
    published = json.dumps(content)
    assert "do not publish" not in published
    assert "read /secret/path" not in published
    assert "Inspect files" not in published


def test_hundreds_of_live_jobs_with_the_longest_ids_fit_in_a_state_update_listing_the_newest() -> None:
    """300 live jobs with maximum-length fields still make a state update the homeserver accepts, listing the newest.

    Tuwunel counts the replaced state's content (``unsigned.prev_content``) toward the 65,535-byte event limit, so two
    contents must fit together with the rest of the event, allowed 10,240 bytes as in Tuwunel's admin responder.
    """
    owner = replace(job_owner(), resolved_thread_id="$" + "e" * 43)
    jobs = [
        replace(
            _job("job_0", status="awaiting_approval"),
            job_id=hashlib.sha256(str(index).encode()).hexdigest(),
            owner=owner,
            tool_name="t" * 64,
            created_at=f"2026-10-11T00:{index // 60:02d}:{index % 60:02d}.123456+00:00",
        )
        for index in range(300)
    ]
    content = _jobs_state_content(jobs)
    assert 2 * len(json.dumps(content, separators=(",", ":"))) + 10_240 <= 65_535
    assert content["total"] == 300
    listed = content["jobs"]
    assert isinstance(listed, list)
    # 89 jobs show at these lengths, and about 110 at typical ones.
    assert len(listed) >= 85
    assert [job["job_id"] for job in listed] == [job.job_id for job in jobs[-len(listed) :]]


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
    assert bots["agent_a"].client.room_put_state.await_args.kwargs["content"] == {"jobs": [], "total": 0}


@pytest.mark.asyncio
async def test_a_job_running_for_moments_writes_nothing() -> None:
    """A job its call waits for that has only just started writes no state, in a room never written or one cleared."""
    bots = _bots("agent_a")
    put = bots["agent_a"].client.room_put_state
    publisher = JobStatePublisher()
    fresh = _aged(_job("job_2"), timedelta(seconds=1))
    await publisher.publish(bots.get, [])
    await publisher.publish(bots.get, [fresh], awaited={"job_2"})
    put.assert_not_awaited()
    # An ended job may still be listed by an earlier process, so its room is cleared once.
    await publisher.publish(bots.get, [_job("job_1", status="completed"), fresh], awaited={"job_2"})
    await publisher.publish(bots.get, [_job("job_1", status="completed"), fresh], awaited={"job_2"})
    put.assert_awaited_once()
    assert put.await_args.kwargs["content"] == {"jobs": [], "total": 0}


@pytest.mark.asyncio
async def test_a_job_approved_in_its_first_seconds_stays_listed() -> None:
    """A job listed while it awaited approval stays listed once approved, instead of seeming to have finished."""
    bots = _bots("agent_a")
    put = bots["agent_a"].client.room_put_state
    publisher = JobStatePublisher()
    job = _aged(_job("job_1", status="awaiting_approval"), timedelta(seconds=1))
    await publisher.publish(bots.get, [job], awaited={"job_1"})
    await publisher.publish(bots.get, [replace(job, status="running")], awaited={"job_1"})
    assert put.await_count == 2
    content = put.await_args.kwargs["content"]
    assert [(listed["job_id"], listed["status"]) for listed in content["jobs"]] == [("job_1", "running")]
    assert content["total"] == 1


async def _awaited_job_and_worker(
    tmp_path: Path,
) -> tuple[ToolJobRuntime, JobClaim, asyncio.Task[None], asyncio.Event, AsyncMock]:
    """Start a job whose call keeps waiting for it, and the worker; the event is set once a write lists a job."""
    runtime = await tool_job_runtime(tmp_path)
    owner = job_owner()
    bot = _bot(owner.recipient)
    bot.client.rooms = {owner.room_id: MagicMock()}
    coordinator = job_coordinator(tmp_path, runtime, bot)
    listed = asyncio.Event()

    def write(**call: object) -> nio.RoomPutStateResponse:
        if call["content"]["jobs"]:  # type: ignore[index]
            listed.set()
        return nio.RoomPutStateResponse(event_id="$state", room_id=owner.room_id)

    bot.client.room_put_state.side_effect = write

    async def forever() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    _, claim = await runtime.start("job_1", tool_name="run_tests", depth=0, adapter={}, owner=owner, operation=forever)
    assert claim is not None
    worker = asyncio.create_task(coordinator._run())
    # Many passes of the worker find the job too fresh to list.
    await asyncio.sleep(0.1)
    assert not listed.is_set()
    return runtime, claim, worker, listed, bot.client.room_put_state


@pytest.mark.asyncio
async def test_the_worker_lists_a_job_once_it_has_run_past_the_grace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker's periodic pass lists a running job once it is old enough, though nothing about the job changed."""
    monkeypatch.setattr(tool_job_runtime_module, "_RETRY_SECONDS", 0.01)
    runtime, _claim, worker, listed, put = await _awaited_job_and_worker(tmp_path)
    try:
        # Time passes: the job was started a minute ago, and nothing wakes the worker.
        entry = runtime._entries["job_1"]
        entry.job = _aged(entry.job, timedelta(minutes=1))
        await asyncio.wait_for(listed.wait(), timeout=5)
        assert [job["job_id"] for job in put.await_args.kwargs["content"]["jobs"]] == ["job_1"]
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_the_worker_lists_a_job_at_once_when_its_call_stops_waiting(tmp_path: Path) -> None:
    """A call that leaves its job running in the background, as with `background_after`, shows it without the grace."""
    runtime, claim, worker, listed, put = await _awaited_job_and_worker(tmp_path)
    try:
        await runtime.release_wait("job_1", claim)
        # Well before the worker's periodic pass: the released wait wakes it.
        await asyncio.wait_for(listed.wait(), timeout=1)
        assert [job["job_id"] for job in put.await_args.kwargs["content"]["jobs"]] == ["job_1"]
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await runtime.shutdown()


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
        assert call["content"] == {"jobs": [], "total": 0}
    finally:
        await runtime.shutdown()


async def _quiesce_kept_and_ended_jobs(tmp_path: Path, *, jobs_on: bool) -> tuple[dict[str, object], object]:
    """Shut down with a listed subagent job it keeps for the next start and a listed tool job it ends.

    ``jobs_on`` is the authored config's setting at shutdown. Returns the kept job's listing and the state written.
    """
    runtime = await tool_job_runtime(tmp_path, continues=tool_job_runtime_module._is_subagent_job)
    owner = job_owner()
    bot = _bot(owner.recipient)
    bot.client.rooms = {owner.room_id: MagicMock()}
    coordinator = job_coordinator(tmp_path, runtime, bot)
    coordinator.config_provider = lambda: Config(background_tool_jobs=BackgroundToolJobsConfig(enabled=jobs_on))

    async def forever() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    options = {"depth": 0, "owner": owner, "operation": forever}
    try:
        adapter = completed_delegation_job().adapter
        await start_job(runtime, "kept", tool_name="delegate", kind="delegation", adapter=adapter, **options)
        await start_job(runtime, "ended", tool_name="run_tests", adapter={}, **options)
        await coordinator._publisher.publish(coordinator.bot_provider, runtime.retained_jobs())
        listed = {job["job_id"]: job for job in bot.client.room_put_state.await_args.kwargs["content"]["jobs"]}
        assert listed.keys() == {"kept", "ended"}
        await coordinator.quiesce()
        assert {job.job_id: job.status for job in runtime.retained_jobs()} == {
            "kept": "running",
            "ended": "interrupted",
        }
        return listed["kept"], bot.client.room_put_state.await_args.kwargs["content"]
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_an_orderly_shutdown_keeps_listed_only_the_jobs_the_next_start_continues(tmp_path: Path) -> None:
    """Jobs a shutdown ends leave the rooms' state at once, so no client lists them while MindRoom is down.

    A subagent job it keeps running in storage for the next start to continue stays listed as it was, so its thread
    still shows work while MindRoom restarts.
    """
    kept, written = await _quiesce_kept_and_ended_jobs(tmp_path, jobs_on=True)
    assert written == {"jobs": [kept], "total": 1}


@pytest.mark.asyncio
async def test_a_shutdown_after_jobs_were_turned_off_clears_the_kept_jobs_too(tmp_path: Path) -> None:
    """A shutdown after the config turned jobs off clears the subagent jobs it keeps too.

    The setting takes effect at the next start, which continues no job.
    """
    _kept, written = await _quiesce_kept_and_ended_jobs(tmp_path, jobs_on=False)
    assert written == {"jobs": [], "total": 0}


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
    assert bots["agent_a"].client.room_put_state.await_args.kwargs["content"] == {"jobs": [], "total": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", ["M_FORBIDDEN", "M_TOO_LARGE"])
async def test_a_refused_write_is_not_retried_until_the_jobs_change(status_code: str) -> None:
    """A room that refuses the state, as not allowed or too large, is left alone until its jobs change."""
    bots = _bots("agent_a")
    put = bots["agent_a"].client.room_put_state
    put.return_value = nio.RoomPutStateError("refused", status_code=status_code)
    publisher = JobStatePublisher()
    await publisher.publish(bots.get, [_job("job_1")])
    await publisher.publish(bots.get, [_job("job_1")])
    assert put.await_count == 1
    await publisher.publish(bots.get, [_job("job_1"), _job("job_2")])
    assert put.await_count == 2


@pytest.mark.asyncio
async def test_a_forbidden_room_warns_once_naming_the_agent() -> None:
    """A room that keeps refusing the state warns once per agent, not again each time its jobs change."""
    bots = _bots("agent_a", "agent_b")
    for bot in bots.values():
        bot.client.room_put_state.return_value = nio.RoomPutStateError("forbidden", status_code="M_FORBIDDEN")
    publisher = JobStatePublisher()
    with capture_logs() as logs:
        await publisher.publish(bots.get, [_job("job_1")])
        await publisher.publish(bots.get, [_job("job_1"), _job("job_2")])
        await publisher.publish(bots.get, [_job("job_1"), _job("job_2"), _job("job_3", agent="agent_b")])
    assert bots["agent_a"].client.room_put_state.await_count == 2
    assert [(entry["log_level"], entry["agent"], entry["room_id"]) for entry in logs] == [
        ("warning", "agent_a", "!room:test"),
        ("warning", "agent_b", "!room:test"),
    ]


@pytest.mark.asyncio
async def test_a_room_that_refuses_again_after_a_write_warns_again() -> None:
    """A room that accepted the state and later refuses it again is reported again."""
    bots = _bots("agent_a")
    put = bots["agent_a"].client.room_put_state
    forbidden = nio.RoomPutStateError("forbidden", status_code="M_FORBIDDEN")
    put.side_effect = [forbidden, put.return_value, forbidden]
    publisher = JobStatePublisher()
    with capture_logs() as logs:
        await publisher.publish(bots.get, [_job("job_1")])
        await publisher.publish(bots.get, [_job("job_1"), _job("job_2")])
        await publisher.publish(bots.get, [_job("job_2")])
    assert put.await_count == 3
    assert [entry["log_level"] for entry in logs] == ["warning", "warning"]


@pytest.mark.asyncio
async def test_forbidden_rooms_beyond_the_set_keep_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """A full set keeps the rooms it holds quiet, and only rooms outside it warn on every refused write."""
    monkeypatch.setattr(publication, "_MAX_REFUSED_ROOMS", 1)
    bots = _bots("agent_a", "agent_b")
    for bot in bots.values():
        bot.client.room_put_state.return_value = nio.RoomPutStateError("forbidden", status_code="M_FORBIDDEN")
    publisher = JobStatePublisher()
    with capture_logs() as logs:
        for count in range(1, 4):
            jobs = [_job(f"job_{index}", agent=agent) for index in range(1, count + 1) for agent in bots]
            await publisher.publish(bots.get, jobs)
    assert [entry["agent"] for entry in logs] == ["agent_a", "agent_b", "agent_b", "agent_b"]
