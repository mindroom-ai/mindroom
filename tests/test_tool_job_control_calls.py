"""Control results must reach the turn driver before it chooses a continuation."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig, ModelConfig
from mindroom.thread_models import resolve_thread_model_override
from mindroom.tool_jobs.authorization import authority_snapshot
from mindroom.tool_jobs.control import pending_human_messages_context
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import register_background_runtime
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.turn_continuation import continuation_decision_from_tools
from tests.delegation_helpers import _delegate_runtime_context, _runtime_paths
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.tool_job_helpers import JOB_TEST_TIMEOUT, job_actor, run_job_turn, tool_job_runtime, toolkit_job_tools

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.invocation import ToolCall, ToolNext


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["next-turn", "after-toolcall"])
@pytest.mark.parametrize("background_after", [None, 0, 0.001])
async def test_model_control_preserves_timing_across_human_followup(  # noqa: PLR0915
    tmp_path: Path,
    when: str,
    background_after: float | None,
) -> None:
    """Control calls finish inline; unsupported wait budgets fail before changing the model."""
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
        agents={"leader": AgentConfig(display_name="Leader", tools=["thread_model"])},
        models={"default": ModelConfig(provider="openai", id="gpt-6-astra")},
    )
    config.models["alternate"] = config.models["default"].model_copy()
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(config, paths)
    context = replace(
        context,
        target=replace(context.target, source_thread_id="$thread", resolved_thread_id="$thread", session_id="session"),
    )
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    toolkit = get_tool_by_name("thread_model", paths, worker_target=None, disable_sandbox_proxy=True)
    tools = toolkit_job_tools(toolkit, authored_name="thread_model", snapshot=authority_snapshot(config, "leader"))
    started, release = asyncio.Event(), asyncio.Event()
    signal = asyncio.Event()

    async def before(call: ToolCall, call_next: ToolNext) -> object:
        started.set()
        await release.wait()
        return await call_next(call.arguments)

    arguments: dict[str, object] = {"model_name": "alternate", "when": when}
    if background_after is not None:
        arguments["background_after"] = background_after
    model = ScriptedModel([calls(tool_call("switch_thread_model", "switch", **arguments)), answer("switched")])
    pending = None
    try:
        async with execution_resources():
            with tool_runtime_context(context), pending_human_messages_context(signal):
                pending = asyncio.create_task(
                    run_job_turn(
                        job_actor(*tools.values()),
                        model,
                        "Switch the model",
                        session_id=context.session_id,
                        middleware=(before,),
                    ),
                )
                if background_after is None:
                    await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
                    signal.set()
                    done, _ = await asyncio.wait({pending}, timeout=0.05)
                    assert not done, "A control result cannot become a background handle on human follow-up"
                    release.set()
                response = await asyncio.wait_for(pending, JOB_TEST_TIMEOUT)
                assert response.tools is not None
                tool = response.tools[0]
                if background_after is None:
                    payload = json.loads(str(tool.result))
                    assert payload["status"] == "ok"
                    decision = continuation_decision_from_tools(
                        response.tools,
                        original_prompt="Switch",
                        continuation_count=0,
                    )
                    assert decision.model_switch_name == "alternate"
                    assert decision.model_switch_when == when
                    assert (
                        resolve_thread_model_override(paths, "$thread", config=config).active.get("leader")
                        == "alternate"
                    )
                else:
                    assert tool.tool_call_error
                    assert "background_after" in str(tool.result)
                    assert not started.is_set()
                    assert resolve_thread_model_override(paths, "$thread", config=config).active == {}
                assert await runtime.list_jobs(owner=owner, depth=0) == []
                [schema] = [item for item in model.requests[0].tools if item.name == "switch_thread_model"]
                assert "background_after" not in schema.parameters["properties"]
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await runtime.shutdown()
