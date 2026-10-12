"""A person can cancel an agent's background jobs from the client, and a bot leaving a room cancels its jobs there."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom import reply_lifecycle as rl
from mindroom.commands.handler import handle_command
from mindroom.commands.parsing import command_parser
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.event_journal import EventClass, EventKind, InboundEvent
from mindroom.matrix.identity import MatrixID
from mindroom.matrix_identifiers import agent_username_localpart
from mindroom.message_target import MessageTarget
from mindroom.reply_presentation import Presentation, encode_presentation
from mindroom.requester_identity import runtime_matrix_domain
from mindroom.response_sources import ResponseSources
from mindroom.tool_jobs.client_cancel import (
    JobCancelRequest,
    _parse_job_cancel,
    cancel_room_jobs,
    handle_job_cancel_event,
    person_may_cancel,
)
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import register_background_runtime
from tests.authorization_helpers import make_test_command_handler_context
from tests.conftest import make_conversation_reader_mock
from tests.delegation_helpers import _runtime_paths
from tests.tool_job_helpers import job_coordinator, job_owner, start_job, tool_job_runtime, wait_for_status

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_jobs.runtime import BackgroundOutcome, ToolJobRuntime


async def _forever() -> BackgroundOutcome:
    await asyncio.Event().wait()
    raise AssertionError


async def _start(
    runtime: ToolJobRuntime,
    job_id: str,
    *,
    agent: str = "agent_a",
    thread: str | None = "$thread",
) -> None:
    owner = replace(job_owner(), agent_name=agent, resolved_thread_id=thread, thread_id=thread)
    await start_job(runtime, job_id, tool_name="run_tests", depth=0, adapter={}, owner=owner, operation=_forever)


def _status(runtime: ToolJobRuntime, job_id: str) -> str:
    return next(job.status for job in runtime.retained_jobs() if job.job_id == job_id)


def test_parse_accepts_one_job_or_all_and_rejects_anything_else() -> None:
    """A cancel names one job or all of its thread's jobs; anything else is not a cancel."""
    assert _parse_job_cancel({"job_id": "a"}) == JobCancelRequest(job_id="a", thread_id=None)
    thread = {"m.relates_to": {"rel_type": "m.thread", "event_id": "$root"}}
    assert _parse_job_cancel({"all": True, **thread}) == JobCancelRequest(job_id=None, thread_id="$root")
    assert _parse_job_cancel({}) is None
    assert _parse_job_cancel({"job_id": 3}) is None
    assert _parse_job_cancel({"all": "yes"}) is None


@pytest.mark.asyncio
async def test_a_cancel_reaches_only_the_owning_agent(tmp_path: Path) -> None:
    """Every bot sees the cancel event; only the agent that owns the job cancels it."""
    runtime = await tool_job_runtime(tmp_path)
    try:
        await _start(runtime, "job_a", agent="agent_a")
        content = {"job_id": "job_a"}
        assert (
            await handle_job_cancel_event(
                runtime,
                agent_name="agent_b",
                room_id="!room:test",
                content=content,
                sender_may_cancel=True,
            )
            == ()
        )
        assert _status(runtime, "job_a") == "running"
        cancelled = await handle_job_cancel_event(
            runtime,
            agent_name="agent_a",
            room_id="!room:test",
            content=content,
            sender_may_cancel=True,
        )
        assert [job.job_id for job in cancelled] == ["job_a"]
        await wait_for_status(runtime, "job_a", "cancelled")
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_cancel_all_stops_only_this_threads_jobs(tmp_path: Path) -> None:
    """Stop all in a thread leaves the agent's jobs in other threads and the main timeline running."""
    runtime = await tool_job_runtime(tmp_path)
    try:
        await _start(runtime, "here", thread="$root")
        await _start(runtime, "elsewhere", thread="$other")
        await _start(runtime, "main", thread=None)
        content = {"all": True, "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"}}
        cancelled = await handle_job_cancel_event(
            runtime,
            agent_name="agent_a",
            room_id="!room:test",
            content=content,
            sender_may_cancel=True,
        )
        assert [job.job_id for job in cancelled] == ["here"]
        await wait_for_status(runtime, "here", "cancelled")
        assert (_status(runtime, "elsewhere"), _status(runtime, "main")) == ("running", "running")
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_an_unauthorized_sender_cancels_nothing(tmp_path: Path) -> None:
    """A sender who may not talk to the agent here, such as another agent account, cancels nothing."""
    runtime = await tool_job_runtime(tmp_path)
    try:
        await _start(runtime, "job_a")
        assert (
            await handle_job_cancel_event(
                runtime,
                agent_name="agent_a",
                room_id="!room:test",
                content={"all": True, "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread"}},
                sender_may_cancel=False,
            )
            == ()
        )
        assert _status(runtime, "job_a") == "running"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_leaving_a_room_cancels_its_jobs_there(tmp_path: Path) -> None:
    """The bot's jobs in a room it left are cancelled; its jobs elsewhere keep running."""
    runtime = await tool_job_runtime(tmp_path)
    try:
        await _start(runtime, "left")
        owner = replace(job_owner(), agent_name="agent_a", room_id="!other:test")
        await start_job(runtime, "stays", tool_name="run_tests", depth=0, adapter={}, owner=owner, operation=_forever)
        await cancel_room_jobs(runtime, agent_name="agent_a", room_id="!room:test")
        await wait_for_status(runtime, "left", "cancelled")
        assert _status(runtime, "stays") == "running"
    finally:
        await runtime.shutdown()


async def _command(
    tmp_path: Path,
    text: str,
    *,
    thread: str | None = "$thread",
    agent: AgentConfig | None = None,
) -> str:
    """Run one chat command as alice in the conversation, and return the reply it sent."""
    config = Config(
        agents={"agent_a": agent or AgentConfig(display_name="Agent A")},
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
    )
    send_response = AsyncMock(return_value="$reply")
    context = make_test_command_handler_context(
        client=AsyncMock(),
        config=config,
        runtime_paths=_runtime_paths(tmp_path),
        logger=MagicMock(),
        conversation_reader=make_conversation_reader_mock(),
        stable_target=MessageTarget.resolve("!room:test", thread, "$command"),
        record_handled_turn=AsyncMock(),
        record_command_result=AsyncMock(),
        send_response=send_response,
    )
    command = command_parser.parse(text)
    assert command is not None
    event = SimpleNamespace(sender="@alice:test", event_id="$command", body=text, source={"content": {"body": text}})
    await handle_command(
        context=context,
        room=nio.MatrixRoom(room_id="!room:test", own_user_id="@mindroom_router:test"),
        event=event,
        command=command,
        requester_user_id="@alice:test",
    )
    return send_response.await_args.args[0]


async def _command_runtime(tmp_path: Path) -> ToolJobRuntime:
    runtime = await tool_job_runtime(tmp_path)
    paths = _runtime_paths(tmp_path)
    pin_background_tool_jobs(Config(background_tool_jobs=BackgroundToolJobsConfig(enabled=True)), paths)
    register_background_runtime(paths, runtime)
    return runtime


@pytest.mark.asyncio
async def test_jobs_command_lists_this_conversations_jobs(tmp_path: Path) -> None:
    """In a client without the jobs bar, `!jobs` lists the jobs running in this conversation."""
    runtime = await _command_runtime(tmp_path)
    try:
        await _start(runtime, "job_here", thread="$thread")
        await _start(runtime, "job_elsewhere", thread="$other")
        reply = await _command(tmp_path, "!jobs")
        assert "`job_here`" in reply
        assert "run_tests" in reply
        assert "job_elsewhere" not in reply
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_jobs_command_outside_a_thread_lists_the_whole_rooms_jobs(tmp_path: Path) -> None:
    """In a room that answers in threads, `!jobs` sent at the top level lists every job of the room."""
    runtime = await _command_runtime(tmp_path)
    try:
        await _start(runtime, "thread_1", thread="$thread")
        await _start(runtime, "thread_2", thread="$other")
        reply = await _command(tmp_path, "!jobs", thread=None)
        assert "`thread_1`" in reply
        assert "`thread_2`" in reply
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_cancel_job_command_cancels_one_job_and_replies(tmp_path: Path) -> None:
    """`!cancel_job <id>` stops that job and says so."""
    runtime = await _command_runtime(tmp_path)
    try:
        await _start(runtime, "job_a")
        reply = await _command(tmp_path, "!cancel_job job_a")
        assert "`job_a`" in reply
        await wait_for_status(runtime, "job_a", "cancelled")
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_jobs_lists_short_ids_that_cancel_job_accepts(tmp_path: Path) -> None:
    """`!jobs` shows each job by a short ID, and `!cancel_job` takes any prefix that names one job."""
    runtime = await _command_runtime(tmp_path)
    try:
        await _start(runtime, "a1b2c3d4e5f60718")
        await _start(runtime, "a1ffffffffffffff")
        listing = await _command(tmp_path, "!jobs")
        assert "`a1b2c3d4`" in listing
        assert "a1b2c3d4e5f60718" not in listing
        assert "several" in await _command(tmp_path, "!cancel_job a1")
        assert _status(runtime, "a1b2c3d4e5f60718") == "running"
        assert "`a1b2c3d4`" in await _command(tmp_path, "!cancel_job a1b2c3d4")
        await wait_for_status(runtime, "a1b2c3d4e5f60718", "cancelled")
        assert _status(runtime, "a1ffffffffffffff") == "running"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_cancel_job_all_cancels_this_threads_jobs(tmp_path: Path) -> None:
    """`!cancel_job all` stops the jobs of this conversation and leaves other threads' jobs running."""
    runtime = await _command_runtime(tmp_path)
    try:
        await _start(runtime, "here", thread="$thread")
        await _start(runtime, "elsewhere", thread="$other")
        await _command(tmp_path, "!cancel_job all")
        await wait_for_status(runtime, "here", "cancelled")
        assert _status(runtime, "elsewhere") == "running"
    finally:
        await runtime.shutdown()


def test_another_agent_never_may_cancel(tmp_path: Path) -> None:
    """A person who may talk to the entity may stop its work; another agent's account never may."""
    config = Config(agents={"agent_a": AgentConfig(display_name="Agent A")})
    paths = _runtime_paths(tmp_path)
    agent_id = MatrixID.from_username(agent_username_localpart("agent_a", paths), runtime_matrix_domain(paths)).full_id
    allowed = {"may_talk": lambda _sender: True}
    assert person_may_cancel("@alice:test", config=config, runtime_paths=paths, **allowed)
    assert not person_may_cancel(agent_id, config=config, runtime_paths=paths, **allowed)
    assert not person_may_cancel("@alice:test", config=config, runtime_paths=paths, may_talk=lambda _sender: False)


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_cancel_job_command_refuses_a_sender_without_access(tmp_path: Path) -> None:
    """Someone who may not talk to the job's agent here cannot cancel its job by command."""
    runtime = await _command_runtime(tmp_path)
    try:
        await _start(runtime, "job_a")
        restricted = AgentConfig(
            display_name="Agent A",
            access=ResponderAccessConfig(users=["@bob:test"], current_room_members=False),
        )
        reply = await _command(tmp_path, "!cancel_job job_a", agent=restricted)
        assert reply == "No running background job here has that ID; `!jobs` lists them."
        assert _status(runtime, "job_a") == "running"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_stop_on_the_reply_leaves_its_job_running(tmp_path: Path) -> None:
    """🛑 on a reply stops the reply; the job it started keeps running until the jobs bar or a command cancels it."""
    runtime = await tool_job_runtime(tmp_path)
    coordinator = job_coordinator(tmp_path, runtime, MagicMock())
    # The requester keeps access, so only a Stop could end the job here.
    coordinator._denied = lambda _job: False
    journal = coordinator._journal
    assert journal is not None
    principal = journal.principal("@mindroom_agent_a:test")
    owner = job_owner()
    assert owner.room_id is not None
    await principal.admit(
        InboundEvent(
            event_id="$turn",
            room_id=owner.room_id,
            thread_id=owner.resolved_thread_id,
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender="@alice:test",
            origin_server_ts=1,
            source={},
        ),
    )
    await principal.replies.write_generation("gen-1")
    claimed = (
        await principal.replies.claim(
            rl.ClaimRequest(
                span_id="span-1",
                delivery_id="$turn",
                sources=ResponseSources(("$turn",), ("$turn",)),
                bot_generation="gen-1",
                now_ns=10,
                new_reply_id="reply-1",
                entity_name=owner.recipient,
                room_id=owner.room_id,
                thread_id=owner.resolved_thread_id,
                membership_epoch=0,
                empty_presentation=encode_presentation(Presentation()),
            ),
        )
    ).transition
    assert claimed.reply is not None

    try:
        await start_job(
            runtime,
            "job_a",
            tool_name="run_tests",
            depth=0,
            adapter={},
            owner=owner,
            operation=_forever,
            source_event_id="$turn",
        )
        stop = rl.stop(claimed.reply, claimed.claimed, rl.StopFacts(receipt_order=5, span_live=False), now_ns=30)
        await principal.replies.update(claimed.reply.reply_id, lambda _current: stop)
        await coordinator._reconcile()
        assert _status(runtime, "job_a") == "running"
    finally:
        await runtime.shutdown()
