"""Managed composite jobs drain every nested sync child before releasing resources."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.message_target import MessageTarget
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import defer_execution_cleanup
from mindroom.tool_jobs.runtime import register_background_runtime
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    always_owned,
    cancel_settled,
    job_actor,
    job_tool,
    run_job_turn,
    tool_job_runtime,
    toolkit_job_tools,
    wait_for_status,
)

if TYPE_CHECKING:
    from pathlib import Path


def _make_context(tmp_path: Path) -> ToolRuntimeContext:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["workflow"])},
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5")},
        ),
        runtime_paths,
    )
    return make_test_tool_runtime_context(
        agent_name="general",
        target=MessageTarget.resolve(
            room_id="!room:localhost",
            thread_id="$thread:localhost",
            reply_to_event_id="$event:localhost",
        ),
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        room=None,
        storage_path=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True])
async def test_cancel_composite_job_drains_all_sync_children(
    tmp_path: Path,
    parallel: bool,
) -> None:
    """Cancellation keeps resources until every started nested thread has exited."""
    context = _make_context(tmp_path)
    context.config.background_tool_jobs.enabled = True
    owner = context.execution_identity
    runtime = await tool_job_runtime(context.runtime_paths.storage_root)
    pin_background_tool_jobs(context.config, context.runtime_paths)
    register_background_runtime(context.runtime_paths, runtime)
    loop = asyncio.get_running_loop()
    started = [asyncio.Event(), asyncio.Event()]
    release = threading.Event()
    completed: list[int] = []
    cleaned = asyncio.Event()

    def leaf(value: int) -> str:
        if value > 0:
            loop.call_soon_threadsafe(started[value - 1].set)
            assert release.wait(5), "test did not release its worker"
        completed.append(value)
        return str(value)

    nested = job_actor(*toolkit_job_tools(Toolkit(name="nested", tools=[leaf]), authored_name="nested").values())
    requested = [tool_call("leaf", str(value), value=value) for value in (1, 2)]
    child_model = ScriptedModel(
        [
            calls(tool_call("leaf", "0", value=0)),
            *([calls(*requested)] if parallel else [calls(call) for call in requested]),
            answer("done"),
        ],
    )

    async def cleanup() -> None:
        cleaned.set()

    async def composite() -> str:
        # A nested agent runs its own loop inside the job, its calls admitted under the job's ownership.
        result = await run_job_turn(nested, child_model, "Use the tools.", session_id="child")
        return str(result.content)

    model = ScriptedModel([calls(tool_call("composite", "outer", background_after=0)), answer("done")])
    actor = job_actor(job_tool(composite), name="general")

    @owned_tool_execution(enabled=always_owned)
    async def run() -> str:
        result = await run_job_turn(actor, model, "Start the composite tool.", session_id=context.session_id)
        assert defer_execution_cleanup(cleanup)
        return json.loads(str(result.tools[0].result))["job_id"]

    try:
        with tool_runtime_context(context):
            job_id = await run()
            await asyncio.wait_for(started[0].wait(), JOB_TEST_TIMEOUT)
            if parallel:
                await asyncio.wait_for(started[1].wait(), JOB_TEST_TIMEOUT)
            waiter = asyncio.create_task(cancel_settled(runtime, job_id, owner=owner, depth=0))
            await wait_for_status(runtime, job_id, "cancel_requested")
            assert not cleaned.is_set()
            done, _ = await asyncio.wait({waiter}, timeout=0.05)
            assert not done
            assert completed == [0]
            release.set()
            await asyncio.wait_for(waiter, JOB_TEST_TIMEOUT)
            assert cleaned.is_set()
            assert sorted(completed) == ([0, 1, 2] if parallel else [0, 1])
    finally:
        release.set()
        await runtime.shutdown()
