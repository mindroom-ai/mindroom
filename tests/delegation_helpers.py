"""Shared deterministic models and requester contexts for delegation integration tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

from mindroom.constants import resolve_runtime_paths
from mindroom.event_journal import ApprovalCall
from mindroom.message_target import MessageTarget
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_relation_lookup
from tests.history_helpers import RecordingModel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from agno.models.message import Message
    from agno.models.response import ModelResponse

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationState
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def _runtime_paths(storage_path: Path) -> RuntimePaths:
    """Create explicit runtime paths for delegate-tool agent creation tests."""
    return resolve_runtime_paths(
        config_path=storage_path / "config.yaml",
        storage_path=storage_path,
        process_env={},
    )


def _delegate_runtime_context(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    execution_identity: ToolExecutionIdentity | None = None,
) -> ToolRuntimeContext:
    """Build the requester context every successful delegation requires."""
    room_id = execution_identity.room_id if execution_identity is not None else "!room:example.org"
    source_thread_id = execution_identity.thread_id if execution_identity is not None else None
    resolved_thread_id = execution_identity.resolved_thread_id if execution_identity is not None else None
    session_id = execution_identity.session_id if execution_identity is not None else room_id
    requester_id = execution_identity.requester_id if execution_identity is not None else "@alice:example.org"
    return make_test_tool_runtime_context(
        agent_name="leader",
        target=MessageTarget(
            room_id=room_id,
            source_thread_id=source_thread_id,
            resolved_thread_id=resolved_thread_id,
            reply_to_event_id=None,
            session_id=session_id,
        ),
        requester_id=requester_id,
        client=MagicMock(),
        config=config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )


@dataclass
class DelegationModel(RecordingModel):
    """Deterministic model runs real Agno tool and pause machinery."""

    responses: list[ModelResponse] = field(default_factory=list)

    async def ainvoke(self, *_args: object, **kwargs: object) -> ModelResponse:
        """Consume one planned provider response."""
        self.seen_messages = list(cast("list[Message]", kwargs.get("messages", [])))
        return self.responses.pop(0)

    async def ainvoke_stream(self, *_args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        """Use planned responses through Agno's real streaming machinery."""
        yield await self.ainvoke(*_args, **kwargs)


def _call(name: str, call_id: str, **arguments: object) -> dict[str, object]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _saved_approval_calls(state: DelegationState) -> tuple[ApprovalCall, ...]:
    """Supply the saved journal calls when a test drives delegation without Matrix publication."""
    calls = []
    for tool in state.pending_tools:
        call_id = str(tool["tool_call_id"])
        if state.pending_child_id is None:
            assert tool["tool_name"] in {"run_subagent", "continue_subagent"}
            assert state.pending_agent_name is not None
            invoking_agent, toolkit_name = state.pending_agent_name, "delegate"
        else:
            source = state.pending_tool_sources[call_id]
            invoking_agent, toolkit_name = source.child.child_agent_name, source.toolkit_name
        calls.append(
            ApprovalCall(
                tool_call_id=call_id,
                tool_name=str(tool["tool_name"]),
                invoking_agent=invoking_agent,
                toolkit_name=toolkit_name,
                expires_at_ns=2**62,
            ),
        )
    return tuple(calls)
