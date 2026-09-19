"""Control results must reach the turn driver before it chooses a continuation."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.team import Team

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig, ModelConfig
from mindroom.custom_tools.dynamic_tools import DynamicToolsToolkit
from mindroom.dynamic_tool_continuation import continuation_decision_from_tools
from mindroom.thread_models import resolve_thread_model_override
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.authorization import AUTHORITY_METADATA_KEY, authority_snapshot, bind_toolkit_authority
from mindroom.tool_jobs.control import HumanMessageSignal, human_message_signal_context
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import ToolJobRuntime, register_background_runtime
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_execution import DelegationModel, _call

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("team", [False, True])
@pytest.mark.parametrize("when", ["next-turn", "after-toolcall"])
@pytest.mark.parametrize("wait_timeout", [None, 0, 0.001])
async def test_model_control_preserves_timing_across_human_followup(  # noqa: PLR0915
    tmp_path: Path,
    team: bool,
    when: str,
    wait_timeout: float | None,
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
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    toolkit = get_tool_by_name("thread_model", paths, worker_target=None, disable_sandbox_proxy=True)
    bind_toolkit_authority(toolkit, authored_name="thread_model")
    function = toolkit.async_functions["switch_thread_model"]
    started, release = asyncio.Event(), asyncio.Event()
    signal = HumanMessageSignal()

    async def before() -> None:
        started.set()
        await release.wait()

    function.pre_hook = before
    arguments: dict[str, object] = {"model_name": "alternate", "when": when}
    if wait_timeout is not None:
        arguments["wait_timeout"] = wait_timeout
    model = DelegationModel(
        id="test",
        responses=[ModelResponse(tool_calls=[_call("switch_thread_model", "switch", **arguments)])],
    )
    install_tool_job_execution(model)
    metadata = {AUTHORITY_METADATA_KEY: authority_snapshot(config, "leader")}
    actor = (
        Team(id="leader", model=model, members=[], tools=[toolkit], metadata=metadata, telemetry=False)
        if team
        else Agent(id="leader", model=model, tools=[toolkit], metadata=metadata, telemetry=False)
    )
    pending = None
    try:
        async with execution_resources():
            with tool_runtime_context(context), human_message_signal_context(signal):
                pending = asyncio.create_task(actor.arun("Switch the model", session_id=context.session_id))
                if wait_timeout is None:
                    await asyncio.wait_for(started.wait(), 2)
                    signal.notify()
                    done, _ = await asyncio.wait({pending}, timeout=0.05)
                    assert not done, "A control result cannot become a background handle on human follow-up"
                    release.set()
                response = await asyncio.wait_for(pending, 2)
                assert response.tools is not None
                tool = response.tools[0]
                if wait_timeout is None:
                    payload = json.loads(tool.result)
                    assert payload["status"] == "ok"
                    decision = continuation_decision_from_tools(
                        response.tools,
                        original_prompt="Switch",
                        continuation_count=0,
                    )
                    assert decision.model_switch_name == "alternate"
                    assert decision.model_switch_when == when
                    assert (
                        resolve_thread_model_override(paths, "$thread", configured_models=config.models).active
                        == "alternate"
                    )
                else:
                    assert tool.tool_call_error
                    assert "wait_timeout" in tool.result
                    assert not started.is_set()
                    assert (
                        resolve_thread_model_override(paths, "$thread", configured_models=config.models).active is None
                    )
                assert await runtime.list_jobs(owner=owner, depth=0) == []
                schema = model._format_tools([function])[0]["function"]["parameters"]
                assert "wait_timeout" not in schema["properties"]
                dynamic = DynamicToolsToolkit(
                    agent_name="leader",
                    config=config,
                    session_id="session",
                    stop_after_tool_call=True,
                )
                loader_schema = model._format_tools([dynamic.functions["load_tool"]])[0]["function"]["parameters"]
                assert "wait_timeout" not in loader_schema["properties"]
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        register_background_runtime(paths, None)
        await runtime.shutdown()
