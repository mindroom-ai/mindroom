"""Custom SDK events retain their family and result through managed execution."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator  # noqa: TC003 - Agno resolves tool return annotations at runtime.
from dataclasses import field, make_dataclass
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.agent import CustomEvent
from agno.run.base import BaseRunOutputEvent  # noqa: TC002 - Agno resolves tool return annotations at runtime.
from agno.run.team import CustomEvent as TeamCustomEvent
from agno.run.workflow import CustomEvent as WorkflowCustomEvent

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.job import JobTools
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import ToolJobRuntime, register_background_runtime
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context, _runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", [CustomEvent, TeamCustomEvent, WorkflowCustomEvent])
@pytest.mark.parametrize("mode", ["ordinary", "blocking", "background", "recovered"])
@pytest.mark.parametrize("custom_text", [False, True])
async def test_custom_event_family_and_result_survive_execution(
    tmp_path: Path,
    event_type: type[BaseRunOutputEvent],
    mode: str,
    *,
    custom_text: bool,
) -> None:
    """Compare real SDK delivery with inline and persisted background result retrieval."""
    notice = make_dataclass(
        "Notice",
        [("message", str, field(default="saved custom message"))],
        bases=(event_type,),
        namespace={"__str__": lambda self: f"custom:{self.message}"} if custom_text else {},
    )(created_at=123, run_id="tool-run")
    expected = (str(notice) if event_type is CustomEvent else "") + "tail"
    detached = mode in {"background", "recovered"}
    release = asyncio.Event()

    async def generated() -> AsyncIterator[BaseRunOutputEvent | str]:
        if detached:
            await release.wait()
        yield notice
        yield "tail"

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("generated", "custom-call", **({"wait_timeout": 0} if detached else {}))]),
            ModelResponse(content="done"),
        ],
    )
    if mode != "ordinary":
        register_background_runtime(paths, runtime)
        install_tool_job_execution(model)
    try:
        agent = Agent(id="leader", model=model, tools=[generated, JobTools(paths, owner)])
        async with execution_resources():
            with tool_runtime_context(context):
                events = [
                    event
                    async for event in agent.arun(
                        "start",
                        session_id=context.session_id,
                        stream=True,
                        stream_events=True,
                    )
                ]
                if detached:
                    handle = next(message for message in model.seen_messages if message.role == "tool")
                    job_id = json.loads(handle.content)["job_id"]
                    release.set()
                    waited = await runtime.wait(job_id, owner=owner, depth=0)
                    assert waited.token is not None
                    await runtime.release_wait(job_id, waited.token)
                    if mode == "recovered":
                        await runtime.shutdown()
                        runtime = ToolJobRuntime(tmp_path)
                        register_background_runtime(paths, runtime)
                        await runtime.recover()
                    model.responses.extend(
                        [
                            ModelResponse(tool_calls=[_call("job", "read-custom", action="wait", job_id=job_id)]),
                            ModelResponse(content="retrieved"),
                        ],
                    )
                    await agent.arun("retrieve the result", session_id=context.session_id)
                tool_message = next(message for message in reversed(model.seen_messages) if message.role == "tool")
                assert not tool_message.tool_call_error, tool_message.content
                assert tool_message.content == expected
                if not detached:
                    custom_events = [event for event in events if isinstance(event, event_type)]
                    assert [vars(event)["message"] for event in custom_events] == ["saved custom message"]
                    assert custom_events[0].to_dict()["message"] == "saved custom message"
                    custom_events[0].run_id = None
                    assert "run_id" not in custom_events[0].to_dict()
    finally:
        await runtime.shutdown()
        register_background_runtime(paths, None)
