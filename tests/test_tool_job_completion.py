"""Completion admission uses immutable claims and current consumption state."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

from tests.conftest import unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _plain_request

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.message_target import MessageTarget
    from mindroom.response_runner import _EarlyPlaceholderState


import pytest

from mindroom.tool_job_completion import ToolJobCompletion
from mindroom.tool_jobs.completion import admit_job_completion
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobSpec, ToolJobRuntime, register_background_runtime
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import message_origin, test_runtime_paths
from tests.response_runner_helpers import _envelope, _target


@pytest.mark.asyncio
async def test_completion_requires_current_unconsumed_exact_claim(tmp_path: Path) -> None:
    """Completion requires current unconsumed exact claim."""
    paths = test_runtime_paths(tmp_path)
    target = _target(thread_id="$thread")
    owner = ToolExecutionIdentity(
        "matrix",
        "general",
        "@human:localhost",
        target.room_id,
        "$thread",
        "$thread",
        target.session_id,
    )
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "answer")

    try:
        await runtime.start(JobSpec("job", "tool", 0), owner=owner, operation=operation)
        waited = await runtime.wait("job", owner=owner, depth=0)
        await runtime.release_wait("job", waited.token)
        await runtime.claim_delivery(
            "job",
            content={"m.mentions": {"user_ids": ["@mindroom_general:localhost"]}},
            transaction_id="claim",
        )
        envelope = replace(
            _envelope(target),
            hook_source="tool_job_completion",
            tool_job_completion=ToolJobCompletion("job", waited.job.generation, "claim", "@mindroom_general:localhost"),
            origin=message_origin(
                sender_id="@mindroom_general:localhost",
                requester_id="@human:localhost",
                source_kind="hook_dispatch",
            ),
        )
        assert await admit_job_completion(envelope, target=target, runtime_paths=paths)
        assert not await admit_job_completion(
            replace(envelope, tool_job_completion=replace(envelope.tool_job_completion, transaction_id="forged")),
            target=target,
            runtime_paths=paths,
        )
        waited = await runtime.wait("job", owner=owner, depth=0)
        await runtime.acknowledge_wait("job", waited.token)
        assert not await admit_job_completion(envelope, target=target, runtime_paths=paths)
    finally:
        register_background_runtime(paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("consume", [False, True])
@pytest.mark.parametrize("member_owned", [False, True])
async def test_completion_rechecks_after_real_lifecycle_lock(tmp_path: Path, consume: bool, member_owned: bool) -> None:
    """Queued completion admission reads consumption only after the prior turn releases its lock."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    paths = runner.deps.runtime_paths
    target = _target(thread_id="$thread")
    owner = ToolExecutionIdentity(
        "matrix",
        "member" if member_owned else "general",
        "@human:localhost",
        target.room_id,
        "$thread",
        "$thread",
        target.session_id,
        transport_agent_name="general",
    )
    runtime = ToolJobRuntime(paths.storage_root)
    register_background_runtime(paths, runtime)
    first_started, release_first = asyncio.Event(), asyncio.Event()
    settled = []
    executed = []

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "answer")

    async def first_operation(_target: MessageTarget, _placeholder: _EarlyPlaceholderState) -> str:
        first_started.set()
        await release_first.wait()
        return "$first-result"

    async def second_operation(_target: MessageTarget, _placeholder: _EarlyPlaceholderState) -> str:
        executed.append(True)
        return "$second-result"

    async def settle() -> None:
        settled.append(True)

    try:
        await runtime.start(JobSpec("queued-job", "tool", 0), owner=owner, operation=operation)
        waited = await runtime.wait("queued-job", owner=owner, depth=0)
        await runtime.release_wait("queued-job", waited.token)
        await runtime.claim_delivery(
            "queued-job",
            content={"m.mentions": {"user_ids": ["@mindroom_general:localhost"]}},
            transaction_id="queued-claim",
        )
        request = _plain_request(target)
        completion = replace(
            _plain_request(target, source_event_id="$notice"),
            on_no_response_handled=settle,
            response_envelope=replace(
                request.response_envelope,
                source_event_id="$notice",
                hook_source="tool_job_completion",
                tool_job_completion=ToolJobCompletion(
                    "queued-job",
                    waited.job.generation,
                    "queued-claim",
                    "@mindroom_general:localhost",
                ),
                origin=message_origin(
                    sender_id="@mindroom_general:localhost",
                    requester_id="@human:localhost",
                    source_kind="hook_dispatch",
                ),
            ),
        )
        first = asyncio.create_task(
            runner._run_locked_response_lifecycle(
                request,
                response_kind="test",
                locked_operation=first_operation,
                signal_queued_message=False,
            ),
        )
        await first_started.wait()
        second = asyncio.create_task(
            runner._run_locked_response_lifecycle(
                completion,
                response_kind="test",
                locked_operation=second_operation,
                signal_queued_message=False,
            ),
        )
        await asyncio.sleep(0)
        assert not second.done()
        if consume:
            waited = await runtime.wait("queued-job", owner=owner, depth=0)
            await runtime.acknowledge_wait("queued-job", waited.token)
        release_first.set()
        await first
        result = await second
        assert result == (None if consume else "$second-result")
        assert settled == ([True] if consume else [])
        assert executed == ([] if consume else [True])
    finally:
        release_first.set()
        register_background_runtime(paths, None)
        await runtime.shutdown()
