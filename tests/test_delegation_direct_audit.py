"""Direct delegation records outside native Matrix approval continuation."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.agent import RunErrorEvent, RunOutput
from agno.run.base import RunStatus
from agno.tools.function import Function

from mindroom.config.agent import AgentConfig
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.delegation_audit import observe_child_event
from mindroom.tool_schema_cache import cached_processed_schema
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.access_schema_support import with_responder_access
from tests.history_helpers import RecordingModel
from tests.test_delegate_tools import _delegate_runtime_context, _make_config, _runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from mindroom.response_turn import ResponseTurnContext


@dataclass
class _ToolThenErrorModel(RecordingModel):
    """Run one real tool request, then surface a provider failure through Agno."""

    responses: list[ModelResponse] = field(default_factory=list)

    async def ainvoke(self, *_args: object, **kwargs: object) -> ModelResponse:
        messages = kwargs.get("messages")
        if isinstance(messages, list):
            self.seen_messages = list(messages)
        if self.responses:
            return self.responses.pop(0)
        msg = "provider failed after tool"
        raise RuntimeError(msg)

    async def ainvoke_stream(self, *_args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        yield await self.ainvoke(*_args, **kwargs)


def _identity() -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="leader",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="parent-session",
    )


def _tools(tmp_path: Path) -> tuple[DelegateTools, object, object]:
    config = _make_config(
        {
            "leader": AgentConfig(display_name="Leader", delegate_to=["child"]),
            "child": AgentConfig(display_name="Child"),
        },
    )
    runtime_paths = _runtime_paths(tmp_path)
    identity = _identity()
    return (
        DelegateTools(
            "leader",
            ["child"],
            runtime_paths,
            config,
            execution_identity=identity,
        ),
        config,
        runtime_paths,
    )


def _only_run(tmp_path: Path) -> dict[str, object]:
    paths = list(tmp_path.glob("agents/child/workspace/.mindroom/delegations/*/*/run.json"))
    assert len(paths) == 1
    value = json.loads(paths[0].read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _tool_call(name: str, call_id: str, **arguments: object) -> dict[str, object]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


@pytest.mark.asyncio
async def test_direct_delegation_does_not_infer_success_from_unretained_text(tmp_path: Path) -> None:
    """A text return without a retained terminal run must not be labeled successful."""
    tools, config, runtime_paths = _tools(tmp_path)
    context = _delegate_runtime_context(config, runtime_paths, execution_identity=_identity())

    with (
        tool_runtime_context(context),
        patch(
            "mindroom.custom_tools.delegate.ai_response",
            new_callable=AsyncMock,
            return_value="child answer",
        ),
    ):
        result = await tools.run_subagent("child", "Do the work")

    run = _only_run(tmp_path)
    assert run["status"] == "failed"
    assert run["output"] is None
    assert run["error"] == "Delegated run ended without a retained terminal outcome."
    assert run["source_room_id"] == "!room:example.org"
    assert run["source_thread_id"] == "$thread"
    assert "child answer" in result
    assert str(run["delegation_id"]) in result
    assert str(run["record_reference"]) in result


@pytest.mark.asyncio
async def test_direct_delegation_records_real_tool_before_provider_error(tmp_path: Path) -> None:
    """A real Agno tool result must survive a later provider failure in the same run."""
    tools, config, runtime_paths = _tools(tmp_path)
    context = _delegate_runtime_context(config, runtime_paths, execution_identity=_identity())

    async def lookup(query: str) -> str:
        return f"found: {query}"

    async def run_real_envelope(ctx: object, **kwargs: object) -> str:
        callback = cast("Callable[[str], None]", kwargs["run_id_callback"])
        callback("real-child-run")
        agent = Agent(
            name="child",
            model=_ToolThenErrorModel(
                id="test",
                responses=[
                    ModelResponse(
                        tool_calls=[_tool_call("lookup", "actual-call", query="full query")],
                    ),
                ],
            ),
            tools=[Function.from_callable(lookup)],
        )
        async for event in agent.arun(
            "run the tool",
            session_id=ctx.session_id,
            run_id="real-child-run",
            stream=True,
            stream_events=True,
            yield_run_output=True,
        ):
            await observe_child_event(event)
            if isinstance(event, RunErrorEvent):
                return f"Error generating response: {event.content}"
        pytest.fail("Agno did not surface the provider failure")

    with (
        tool_runtime_context(context),
        patch("mindroom.custom_tools.delegate.ai_response", side_effect=run_real_envelope),
    ):
        result = await tools.run_subagent("child", "Do the work")

    paths = list(tmp_path.glob("agents/child/workspace/.mindroom/delegations/*/*"))
    assert len(paths) == 1
    events = [json.loads(line) for line in (paths[0] / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    run = _only_run(tmp_path)
    assert run["status"] == "failed"
    assert run["error"] == "provider failed after tool"
    tool_call = next(event for event in events if event["kind"] == "tool_call")
    tool_result = next(event for event in events if event["kind"] == "tool_result")
    assert tool_call["event_id"] == "run:real-child-run:tool:actual-call:call"
    assert tool_call["data"]["arguments"] == {"query": "full query"}
    assert tool_result["event_id"] == "run:real-child-run:tool:actual-call:result"
    assert tool_result["data"]["result"] == "found: full query"
    assert "Error generating response: provider failed after tool" in result
    assert str(run["record_reference"]) in result


@pytest.mark.asyncio
async def test_real_parent_tool_call_records_direct_provenance_without_schema_fields(tmp_path: Path) -> None:
    """A real Agno parent call must bind its run and call IDs without exposing model parameters."""
    tools, config, runtime_paths = _tools(tmp_path)
    config = with_responder_access(config, "child", users=[_identity().requester_id])
    context = _delegate_runtime_context(config, runtime_paths, execution_identity=_identity())
    function = tools.async_functions["run_subagent"]
    schema = cached_processed_schema(function, strict=False)
    assert schema is not None
    assert set(schema.parameters["properties"]) == {"agent_name", "task"}

    async def complete_child(ctx: object, **kwargs: object) -> str:
        callback = cast("Callable[[str], None]", kwargs["run_id_callback"])
        callback("direct-child-run")
        await observe_child_event(
            RunOutput(
                run_id="direct-child-run",
                session_id=ctx.session_id,
                status=RunStatus.completed,
                content="child result",
            ),
        )
        return "child result"

    parent = Agent(
        name="leader",
        model=_ToolThenErrorModel(
            id="parent-model",
            responses=[
                ModelResponse(
                    tool_calls=[
                        _tool_call("run_subagent", "parent-delegate-call", agent_name="child", task="Do the work"),
                    ],
                ),
                ModelResponse(content="parent complete"),
            ],
        ),
        tools=[tools],
    )
    with (
        tool_runtime_context(context),
        patch("mindroom.custom_tools.delegate.ai_response", side_effect=complete_child),
    ):
        response = await parent.arun(
            "delegate",
            session_id="parent-session",
            run_id="parent-run",
            user_id=_identity().requester_id,
        )

    assert response.status == RunStatus.completed
    run = _only_run(tmp_path)
    assert run["parent_run_id"] == "parent-run"
    assert run["parent_tool_call_id"] == "parent-delegate-call"


@pytest.mark.asyncio
async def test_denied_outer_hook_clears_direct_provenance(tmp_path: Path) -> None:
    """A hook that skips the tool body must not leak its parent IDs into a later direct call."""
    tools, config, runtime_paths = _tools(tmp_path)
    config = with_responder_access(config, "child", users=[_identity().requester_id])
    context = _delegate_runtime_context(config, runtime_paths, execution_identity=_identity())

    async def deny_call() -> str:
        return "denied before delegation"

    tools.async_functions["run_subagent"].tool_hooks = [deny_call]
    parent = Agent(
        name="leader",
        model=_ToolThenErrorModel(
            id="parent-model",
            responses=[
                ModelResponse(
                    tool_calls=[_tool_call("run_subagent", "denied-call", agent_name="child", task="Skip")],
                ),
                ModelResponse(content="parent complete"),
            ],
        ),
        tools=[tools],
    )
    with tool_runtime_context(context):
        response = await parent.arun(
            "delegate",
            session_id="denied-parent-session",
            run_id="denied-parent-run",
            user_id=_identity().requester_id,
        )
    assert response.status == RunStatus.completed
    assert not list(tmp_path.glob("agents/child/workspace/.mindroom/delegations/*/*/run.json"))

    async def complete_child(ctx: object, **kwargs: object) -> str:
        callback = cast("Callable[[str], None]", kwargs["run_id_callback"])
        callback("later-child-run")
        await observe_child_event(
            RunOutput(
                run_id="later-child-run",
                session_id=ctx.session_id,
                status=RunStatus.completed,
                content="later result",
            ),
        )
        return "later result"

    with (
        tool_runtime_context(context),
        patch("mindroom.custom_tools.delegate.ai_response", side_effect=complete_child),
    ):
        await tools.run_subagent("child", "Run later")

    run = _only_run(tmp_path)
    assert run["parent_run_id"] is None
    assert run["parent_tool_call_id"] is None


@pytest.mark.asyncio
async def test_direct_delegation_records_failure_and_returns_receipt(tmp_path: Path) -> None:
    """Converting an exception into text without a failed audit record must fail this test."""
    tools, config, runtime_paths = _tools(tmp_path)
    context = _delegate_runtime_context(config, runtime_paths, execution_identity=_identity())

    with (
        tool_runtime_context(context),
        patch(
            "mindroom.custom_tools.delegate.ai_response",
            new_callable=AsyncMock,
            side_effect=RuntimeError("child exploded"),
        ),
    ):
        result = await tools.run_subagent("child", "Do the work")

    run = _only_run(tmp_path)
    assert run["status"] == "failed"
    assert run["error"] == "child exploded"
    assert "Delegation to 'child' failed: child exploded" in result
    assert str(run["record_reference"]) in result


@pytest.mark.asyncio
async def test_direct_delegation_records_cancellation_before_propagating(tmp_path: Path) -> None:
    """Propagating cancellation without settling the child record must fail this test."""
    tools, config, runtime_paths = _tools(tmp_path)
    context = _delegate_runtime_context(config, runtime_paths, execution_identity=_identity())

    with (
        tool_runtime_context(context),
        patch(
            "mindroom.custom_tools.delegate.ai_response",
            new_callable=AsyncMock,
            side_effect=asyncio.CancelledError,
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await tools.run_subagent("child", "Do the work")

    run = _only_run(tmp_path)
    assert run["status"] == "cancelled"
    assert run["error"] == "Delegation cancelled."


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [asyncio.CancelledError, RuntimeError])
async def test_direct_delegation_preserves_completed_outcome_after_envelope_error(
    tmp_path: Path,
    error_type: type[BaseException],
) -> None:
    """Cleanup must retain the child outcome and preserve the envelope's cancellation."""
    tools, config, runtime_paths = _tools(tmp_path)
    context = _delegate_runtime_context(config, runtime_paths, execution_identity=_identity())
    error = error_type("Envelope interrupted after child completion")

    async def complete_then_interrupt(ctx: ResponseTurnContext, **kwargs: object) -> str:
        callback = cast("Callable[[str], None]", kwargs["run_id_callback"])
        callback("completed-child-run")
        await observe_child_event(
            RunOutput(
                run_id="completed-child-run",
                session_id=ctx.session_id,
                status=RunStatus.completed,
                content="Child completed.",
            ),
        )
        raise error

    with (
        tool_runtime_context(context),
        patch("mindroom.custom_tools.delegate.ai_response", side_effect=complete_then_interrupt),
    ):
        if error_type is asyncio.CancelledError:
            with pytest.raises(asyncio.CancelledError) as caught:
                await tools.run_subagent("child", "Do the work")
            assert caught.value is error
        else:
            result = await tools.run_subagent("child", "Do the work")
            assert str(error) in result

    run = _only_run(tmp_path)
    assert run["status"] == "completed"
    assert run["output"] == "Child completed."
    assert run["error"] is None


@pytest.mark.asyncio
async def test_native_delegation_leaves_record_ownership_to_driver(tmp_path: Path) -> None:
    """Starting a second record from the native execution envelope must fail this test."""
    tools, config, runtime_paths = _tools(tmp_path)
    context = _delegate_runtime_context(config, runtime_paths, execution_identity=_identity())

    with (
        tool_runtime_context(context),
        patch(
            "mindroom.custom_tools.delegate.ai_response",
            new_callable=AsyncMock,
            return_value="native child answer",
        ) as response,
    ):
        result = await tools.run_delegated_task(
            "child",
            "Do the work",
            session_id="native-session",
            run_id="native-run",
            active_model_name="default",
            supports_native_tool_approval=True,
        )

    assert result == "native child answer"
    assert response.await_args.kwargs["collect_streamed_response"] is True
    assert not list(tmp_path.glob("agents/child/workspace/.mindroom/delegations/*/*/run.json"))
