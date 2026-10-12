"""A running reply hears once about its conversation's background jobs that ended, and is asked to collect them."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Never

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse

from mindroom import ai
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.prompts import BACKGROUND_JOBS_ENDED_NOTICE
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.consumption import set_consumption_storage
from mindroom.tool_jobs.conversation import ConversationKey
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.notices import (
    JobNotices,
    _job_notice_text,
    _turn_start_job_note,
    append_turn_start_job_note,
    install_job_notice_hook,
    job_notice_context,
)
from mindroom.tool_jobs.runtime import BackgroundOutcome, register_background_runtime
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.conftest import make_turn_context
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context, _runtime_paths
from tests.tool_job_helpers import assembled_function, lookup, start_job, tool_job_runtime, wait_for_status

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from agno.db.base import BaseDb
    from agno.run.agent import RunOutput

    from mindroom.agent_modes import AgentMode
    from mindroom.hooks.types import EnrichmentItem
    from mindroom.response_turn import ResponseTurnContext
    from mindroom.tool_jobs.runtime import ToolJobRuntime
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


class _Conversation:
    """One agent's conversation with alice, its runtime, and jobs it can end on demand."""

    def __init__(self, runtime: ToolJobRuntime, context: ToolRuntimeContext) -> None:
        self.runtime = runtime
        self.context = context
        self.owner = build_execution_identity_from_runtime_context(context)
        self._releases: dict[str, asyncio.Event] = {}

    async def start(self, job_id: str, *, owner: ToolExecutionIdentity | None = None) -> None:
        """Start a job that runs until `end` completes it."""
        release = self._releases[job_id] = asyncio.Event()

        async def operation() -> BackgroundOutcome:
            await release.wait()
            return BackgroundOutcome("completed", f"result of {job_id}")

        await start_job(
            self.runtime,
            job_id,
            tool_name=f"tool_{job_id}",
            depth=0,
            adapter={},
            owner=owner or self.owner,
            operation=operation,
        )

    async def end(self, *job_ids: str) -> str:
        """Complete jobs, as work finishing while a tool runs; returns the tool's own result."""
        for job_id in job_ids:
            self._releases[job_id].set()
        for job_id in job_ids:
            await wait_for_status(self.runtime, job_id, "completed")
        return "ended"

    async def cancel(self, job_id: str) -> None:
        """Cancel a job from outside the reply, as the jobs bar does."""
        await self.runtime.request_cancel(job_id)
        await wait_for_status(self.runtime, job_id, "cancelled")


async def _conversation(tmp_path: Path) -> _Conversation:
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    return _Conversation(runtime, context)


async def _run(
    conversation: _Conversation,
    responses: list[ModelResponse],
    *,
    notices: bool = True,
    entity_name: str = "leader",
) -> RunOutput:
    """Run one model of the reply, the leader's unless `entity_name` names a team member, whose tools can end jobs."""
    model = DelegationModel(id="test", responses=responses)
    install_tool_job_execution(model)
    install_job_notice_hook(model, entity_name=entity_name)

    async def end_jobs(job_ids: list[str]) -> str:
        """End background jobs."""
        return await conversation.end(*job_ids)

    async def noop() -> str:
        """Do nothing."""
        return "nothing"

    agent = Agent(id="leader", model=model, tools=[end_jobs, noop])
    with tool_runtime_context(conversation.context):
        if not notices:
            return await agent.arun("work", session_id=conversation.context.session_id)
        with job_notice_context(JobNotices()):
            return await agent.arun("work", session_id=conversation.context.session_id)


def _tool_results(response: RunOutput) -> list[str]:
    return [str(message.content) for message in response.messages or [] if message.role == "tool"]


@pytest.mark.asyncio
async def test_jobs_that_end_between_steps_are_reported_once_together(tmp_path: Path) -> None:
    """Two jobs ending during one tool call make one notice on its result, and the next result has none."""
    conversation = await _conversation(tmp_path)
    try:
        await conversation.start("job_a")
        await conversation.start("job_b")
        response = await _run(
            conversation,
            [
                ModelResponse(tool_calls=[_call("end_jobs", "end", job_ids=["job_a", "job_b"])]),
                ModelResponse(tool_calls=[_call("noop", "noop")]),
                ModelResponse(content="done"),
            ],
        )
        first, second = _tool_results(response)
        assert first.startswith("ended")
        assert first.count(BACKGROUND_JOBS_ENDED_NOTICE) == 1
        assert "`job_a`" in first
        assert "`job_b`" in first
        assert second == "nothing"
    finally:
        await conversation.runtime.shutdown()


def test_the_notice_asks_to_acknowledge_with_collect() -> None:
    """The notice asks for one collect call, even for jobs whose output is not needed."""
    assert 'job(action="collect")' in BACKGROUND_JOBS_ENDED_NOTICE
    assert "even" in BACKGROUND_JOBS_ENDED_NOTICE


@pytest.mark.asyncio
async def test_the_notice_names_each_job_and_how_it_ended(tmp_path: Path) -> None:
    """Each ended job gets one line with its ID, tool, and status."""
    conversation = await _conversation(tmp_path)
    try:
        await conversation.start("job_a")
        await conversation.end("job_a")
        job = await lookup(conversation.runtime, "job_a", owner=conversation.owner, depth=0)
        assert _job_notice_text([job], "Collect.") == "`job_a` (tool_job_a) completed\nCollect."
    finally:
        await conversation.runtime.shutdown()


@pytest.mark.asyncio
async def test_a_team_member_does_not_hear_the_teams_notices(tmp_path: Path) -> None:
    """Only the entity answering the conversation hears its jobs ended; a member's tool batch leaves them to it."""
    conversation = await _conversation(tmp_path)
    try:
        await conversation.start("job_a")
        response = await _run(
            conversation,
            [
                ModelResponse(tool_calls=[_call("end_jobs", "end", job_ids=["job_a"])]),
                ModelResponse(content="done"),
            ],
            entity_name="member",
        )
        assert _tool_results(response) == ["ended"]
        context = conversation.context
        key = ConversationKey(
            context.recipient,
            context.room_id,
            context.resolved_thread_id,
            context.requester_id,
            silent=False,
        )
        assert [job.job_id for job in await conversation.runtime.ended_jobs(key)] == ["job_a"]
    finally:
        await conversation.runtime.shutdown()


@pytest.mark.asyncio
async def test_a_reply_is_not_told_that_its_own_finished_call_ended(tmp_path: Path) -> None:
    """A managed call that returns inside its reply's wait is the reply's own result, not a job to collect."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader")})
    context = replace(_delegate_runtime_context(config, paths), membership_turn_id="$request")
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)

    def storage_factory() -> BaseDb:
        return create_session_storage("leader", config, paths, owner)

    async def fast_tool() -> str:
        return "actual result"

    async def second_tool() -> str:
        return "second result"

    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("fast_tool", "call-1")]),
            ModelResponse(tool_calls=[_call("second_tool", "call-2")]),
            ModelResponse(content="done"),
        ],
    )
    install_tool_job_execution(model)
    install_job_notice_hook(model, entity_name="leader")
    storage = storage_factory()
    agent = Agent(
        id="leader",
        model=model,
        tools=[assembled_function(fast_tool), assembled_function(second_tool)],
        db=storage,
    )

    @owned_tool_execution()
    async def reply() -> RunOutput:
        set_consumption_storage(storage_factory)
        return await agent.arun("start", session_id=context.session_id, user_id=context.requester_id)

    try:
        with tool_runtime_context(context):
            response = await reply()
        results = _tool_results(response)
        assert results[0].startswith("actual result")
        assert all(BACKGROUND_JOBS_ENDED_NOTICE not in result for result in results), results
    finally:
        await runtime.shutdown()
        storage.close()


@pytest.mark.asyncio
async def test_another_requesters_jobs_are_not_noticed(tmp_path: Path) -> None:
    """In a shared room, bob's job ending during alice's reply is not noticed there."""
    conversation = await _conversation(tmp_path)
    try:
        await conversation.start("bobs_job", owner=replace(conversation.owner, requester_id="@bob:example.org"))
        response = await _run(
            conversation,
            [
                ModelResponse(tool_calls=[_call("end_jobs", "end", job_ids=["bobs_job"])]),
                ModelResponse(content="done"),
            ],
        )
        assert _tool_results(response) == ["ended"]
    finally:
        await conversation.runtime.shutdown()


@pytest.mark.asyncio
async def test_a_turn_starts_with_a_note_for_jobs_that_ended_before_it(tmp_path: Path) -> None:
    """Jobs that ended between turns, cancelled ones included, open the next turn, once per reply."""
    conversation = await _conversation(tmp_path)
    try:
        await conversation.start("finished")
        await conversation.end("finished")
        await conversation.start("cancelled")
        await conversation.cancel("cancelled")
        notices = JobNotices()
        with tool_runtime_context(conversation.context), job_notice_context(notices):
            note = await _turn_start_job_note()
            assert note is not None
            assert "`finished`" in note
            assert "`cancelled` (tool_cancelled) cancelled" in note
            # Every attempt of the reply opens with the same note, which its tool results then do not repeat.
            assert await _turn_start_job_note() == note
        assert notices.noticed == {"finished", "cancelled"}
    finally:
        await conversation.runtime.shutdown()


@pytest.mark.asyncio
async def test_an_interrupted_job_is_noted_as_one_that_may_have_partly_run(tmp_path: Path) -> None:
    """A job a restart interrupted is reported like a finished one, warning that it may have partly run."""
    conversation = await _conversation(tmp_path)

    async def interrupted() -> BackgroundOutcome:
        return BackgroundOutcome("interrupted", "Restart interrupted the job.")

    try:
        await start_job(
            conversation.runtime,
            "work",
            tool_name="tool_work",
            depth=0,
            adapter={},
            owner=conversation.owner,
            operation=interrupted,
        )
        await wait_for_status(conversation.runtime, "work", "interrupted")
        with tool_runtime_context(conversation.context), job_notice_context(JobNotices()):
            note = await _turn_start_job_note()
        assert note is not None
        assert "`work` (tool_work) interrupted; it may have partly run" in note
    finally:
        await conversation.runtime.shutdown()


@pytest.mark.asyncio
async def test_a_noticed_cancellation_needs_no_collect(tmp_path: Path) -> None:
    """Being told of a cancellation acknowledges it; a finished job still waits for its collect."""
    conversation = await _conversation(tmp_path)
    try:
        await conversation.start("finished")
        await conversation.end("finished")
        await conversation.start("cancelled")
        await conversation.cancel("cancelled")
        with tool_runtime_context(conversation.context), job_notice_context(JobNotices()):
            await _turn_start_job_note()
        assert (await lookup(conversation.runtime, "cancelled", owner=conversation.owner, depth=0)).consumed
        assert not (await lookup(conversation.runtime, "finished", owner=conversation.owner, depth=0)).consumed
    finally:
        await conversation.runtime.shutdown()


@pytest.mark.asyncio
async def test_a_cancellation_whose_acknowledgement_fails_is_noticed_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed save of a noticed cancellation neither breaks the reply nor keeps the job claimed."""
    conversation = await _conversation(tmp_path)
    try:
        await conversation.start("cancelled")
        await conversation.cancel("cancelled")

        async def fail(*_args: object) -> None:
            msg = "store unavailable"
            raise RuntimeError(msg)

        with monkeypatch.context() as patched:
            patched.setattr(conversation.runtime, "acknowledge_wait", fail)
            with tool_runtime_context(conversation.context), job_notice_context(JobNotices()):
                assert "`cancelled`" in str(await _turn_start_job_note())
        with tool_runtime_context(conversation.context), job_notice_context(JobNotices()):
            assert "`cancelled`" in str(await _turn_start_job_note())
        assert (await lookup(conversation.runtime, "cancelled", owner=conversation.owner, depth=0)).consumed
    finally:
        await conversation.runtime.shutdown()


@pytest.mark.asyncio
async def test_no_notice_without_a_running_turn_context(tmp_path: Path) -> None:
    """A model run outside a reply's notice context, such as a background job's own run, adds no notice."""
    conversation = await _conversation(tmp_path)
    try:
        await conversation.start("job_a")
        response = await _run(
            conversation,
            [
                ModelResponse(tool_calls=[_call("end_jobs", "end", job_ids=["job_a"])]),
                ModelResponse(content="done"),
            ],
            notices=False,
        )
        assert _tool_results(response) == ["ended"]
        with tool_runtime_context(conversation.context):
            assert await _turn_start_job_note() is None
    finally:
        await conversation.runtime.shutdown()


@pytest.mark.asyncio
async def test_every_attempt_of_a_reply_opens_with_the_same_note(tmp_path: Path) -> None:
    """A reply's own scope keeps its note across attempts, whether it streams or not."""
    conversation = await _conversation(tmp_path)
    try:
        await conversation.start("finished")
        await conversation.end("finished")

        @owned_tool_execution()
        async def blocking() -> list[tuple[EnrichmentItem, ...]]:
            return [await append_turn_start_job_note(()) for _attempt in range(2)]

        @owned_tool_execution()
        async def streaming() -> AsyncIterator[tuple[EnrichmentItem, ...]]:
            for _attempt in range(2):
                yield await append_turn_start_job_note(())

        with tool_runtime_context(conversation.context):
            first, retry = await blocking()
            streamed = [items async for items in streaming()]
        assert first == retry
        assert [item.key for item in first] == ["background_jobs"]
        assert "`finished`" in first[0].text
        assert not first[0].persist
        # Another reply is told again: only collecting the job acknowledges it.
        assert streamed == [first, first]
    finally:
        await conversation.runtime.shutdown()


class _PreparationStoppedError(Exception):
    """Stops a reply's preparation once the test has seen what it was given."""


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_mode", ["standard", "minimal"])
async def test_only_a_standard_reply_opens_with_the_job_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_mode: AgentMode,
) -> None:
    """A minimal-mode reply cannot collect jobs, so it leaves them to the agent's next standard reply."""
    conversation = await _conversation(tmp_path)
    seen: list[tuple[str, ...]] = []

    async def prepare(turn: ResponseTurnContext, **_kwargs: object) -> Never:
        seen.append(tuple(item.key for item in turn.transient_enrichment_items))
        raise _PreparationStoppedError

    monkeypatch.setattr(ai, "_prepare_agent_and_prompt", prepare)
    try:
        await conversation.start("job_a")
        await conversation.end("job_a")
        with (
            tool_runtime_context(conversation.context),
            job_notice_context(JobNotices()),
            pytest.raises(_PreparationStoppedError),
        ):
            await ai._prepare_agent_run_context(
                replace(make_turn_context("leader"), agent_mode=agent_mode),
                prompt="hello",
                session_id=conversation.context.session_id,
                runtime_paths=conversation.context.runtime_paths,
                config=conversation.context.config,
                scope_context=None,
                thread_history=None,
                knowledge=None,
                include_interactive_questions=True,
                tool_function_filter=None,
                include_openai_compat_guidance=False,
                execution_identity=None,
                compaction_lifecycle=None,
                delegation_depth=0,
                refresh_scheduler=None,
                model_prompt=None,
                current_timestamp_ms=None,
                current_event_id=None,
                current_prompt_is_structured=False,
                turn_recorder=None,
                pipeline_timing=None,
            )
        assert ("background_jobs" in seen[0]) is (agent_mode == "standard")
    finally:
        await conversation.runtime.shutdown()
