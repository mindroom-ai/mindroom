"""Tests for the agent-owned router invite recovery tool."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest
from agno.tools import Toolkit

import mindroom.agents as agents_module
from mindroom.agents import apply_tool_approval_capability, create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.custom_tools.invite_router import InviteRouterTools
from mindroom.history.prompt_tokens import _prompt_tool_surface_for_tools
from mindroom.message_target import MessageTarget
from mindroom.tool_approval import evaluate_tool_approval, tool_may_require_approval
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.runtime_context import ToolRuntimeContext


def _tool_context(tmp_path: Path, *, accept_invites: bool = True) -> tuple[ToolRuntimeContext, AsyncMock]:
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    role="Write code",
                    include_default_tools=False,
                ),
            },
            router=RouterConfig(model="default", accept_invites=accept_invites),
        ),
        test_runtime_paths(tmp_path),
    )
    client = AsyncMock()
    context = make_test_tool_runtime_context(
        agent_name="code",
        target=MessageTarget.resolve(
            room_id="!project:localhost",
            thread_id=None,
            reply_to_event_id=None,
        ),
        requester_id="@alice:localhost",
        client=client,
        config=config,
        runtime_paths=runtime_paths_for(config),
        conversation_reader=make_conversation_reader_mock(),
        relations=make_relation_lookup(),
    )
    return context, client


def test_matrix_agents_always_get_zero_argument_invite_router_tool(tmp_path: Path) -> None:
    """Removing automatic injection would strand agents in rooms without the router."""
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    role="Write code",
                    include_default_tools=False,
                ),
            },
            models={"default": ModelConfig(provider="openai", id="gpt-5.6")},
        ),
        test_runtime_paths(tmp_path),
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!project:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="!project:example.org",
    )

    agent = create_agent(
        "code",
        config,
        runtime_paths_for(config),
        execution_identity=identity,
        session_id=identity.session_id,
    )

    toolkit = next(tool for tool in agent.tools if tool.name == "invite_router")
    function = toolkit.get_async_functions()["invite_router"]
    function.process_entrypoint(strict=False)
    prompt_surface = _prompt_tool_surface_for_tools([toolkit])

    assert function.parameters["properties"] == {}
    assert function.parameters.get("required", []) == []
    assert prompt_surface.definition_tokens <= 36
    assert prompt_surface.tool_instructions == ()
    assert "## Tool Execution Environment" not in agent.role


def test_matrix_runtime_ignores_authored_invite_router_function_filters(tmp_path: Path) -> None:
    """Authored filters must not remove the runtime-owned recovery function."""
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    role="Write code",
                    tools=[{"invite_router": {"exclude_tools": ["invite_router"]}}],
                    include_default_tools=False,
                ),
            },
            models={"default": ModelConfig(provider="openai", id="gpt-5.6")},
        ),
        test_runtime_paths(tmp_path),
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!project:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="!project:example.org",
    )

    agent = create_agent(
        "code",
        config,
        runtime_paths_for(config),
        execution_identity=identity,
        session_id=identity.session_id,
    )

    toolkit = next(tool for tool in agent.tools if tool.name == "invite_router")
    assert set(toolkit.get_async_functions()) == {"invite_router"}


def test_invite_router_stays_hidden_without_matrix_room_context(tmp_path: Path) -> None:
    """Non-Matrix callers should not pay for or call a room-only recovery tool."""
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Write code")},
            models={"default": ModelConfig(provider="openai", id="gpt-5.6")},
        ),
        test_runtime_paths(tmp_path),
    )
    identity = ToolExecutionIdentity(
        channel="openai_compat",
        agent_name="code",
        requester_id="api-user",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id="api-session",
    )

    agent = create_agent(
        "code",
        config,
        runtime_paths_for(config),
        execution_identity=identity,
        session_id=identity.session_id,
    )

    assert "invite_router" not in {tool.name for tool in agent.tools}


def test_matrix_agents_reject_local_invite_router_function_collisions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A plugin function must not replace the auto-injected recovery call."""

    class _CollidingToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="invite_router", tools=[self.invite_router])

        def invite_router(self) -> str:
            return "external"

    original_build_agent_toolkit = agents_module.build_agent_toolkit

    def build_colliding_toolkit(tool_name: str, *args: object, **kwargs: object) -> Toolkit:
        if tool_name == "shell":
            return _CollidingToolkit()
        return original_build_agent_toolkit(tool_name, *args, **kwargs)

    monkeypatch.setattr(agents_module, "build_agent_toolkit", build_colliding_toolkit)
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    role="Write code",
                    tools=["shell"],
                    include_default_tools=False,
                ),
            },
            models={"default": ModelConfig(provider="openai", id="gpt-5.6")},
        ),
        test_runtime_paths(tmp_path),
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!project:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="!project:example.org",
    )

    with pytest.raises(ValueError, match=r"invite_router.*reserved"):
        create_agent(
            "code",
            config,
            runtime_paths_for(config),
            execution_identity=identity,
            session_id=identity.session_id,
        )


@pytest.mark.asyncio
async def test_invite_router_targets_persisted_router_in_current_room(tmp_path: Path) -> None:
    """A wrong room or configurable user target would widen the recovery tool's authority."""
    context, client = _tool_context(tmp_path)
    client.room_get_state_event.side_effect = [
        nio.RoomGetStateEventError(
            "Not found",
            status_code="M_NOT_FOUND",
            room_id="!project:localhost",
        ),
        nio.RoomGetStateEventResponse(
            content={"membership": "join"},
            event_type="m.room.member",
            state_key="@mindroom_router:localhost",
            room_id="!project:localhost",
        ),
    ]
    client.room_invite.return_value = nio.RoomInviteResponse()

    with tool_runtime_context(context):
        result = await InviteRouterTools().invite_router()

    assert result == "Router joined."
    client.room_invite.assert_awaited_once_with("!project:localhost", "@mindroom_router:localhost")


@pytest.mark.asyncio
async def test_invite_router_waits_for_delayed_router_join(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Recovery must not report readiness while router membership is still pending."""
    context, client = _tool_context(tmp_path)
    client.room_get_state_event.side_effect = [
        nio.RoomGetStateEventError(
            "Not found",
            status_code="M_NOT_FOUND",
            room_id="!project:localhost",
        ),
        nio.RoomGetStateEventResponse(
            content={"membership": "invite"},
            event_type="m.room.member",
            state_key="@mindroom_router:localhost",
            room_id="!project:localhost",
        ),
        nio.RoomGetStateEventResponse(
            content={"membership": "join"},
            event_type="m.room.member",
            state_key="@mindroom_router:localhost",
            room_id="!project:localhost",
        ),
    ]
    client.room_invite.return_value = nio.RoomInviteResponse()
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)

    with tool_runtime_context(context):
        result = await InviteRouterTools().invite_router()

    assert result == "Router joined."
    assert client.room_get_state_event.await_count == 3
    sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_invite_router_reports_disabled_router_auto_accept(tmp_path: Path) -> None:
    """Sending an invite while router acceptance is disabled would promise recovery that cannot happen."""
    context, client = _tool_context(tmp_path, accept_invites=False)

    with tool_runtime_context(context):
        result = await InviteRouterTools().invite_router()

    assert result == "Error: Router auto-accept is disabled."
    client.room_invite.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("membership", "expected"),
    [
        ("invite", "Router invite pending; retry after it joins."),
        ("join", "Router already joined."),
    ],
)
async def test_invite_router_is_idempotent_for_existing_membership(
    tmp_path: Path,
    membership: str,
    expected: str,
) -> None:
    """Duplicate router invites can fail even though recovery is already underway or complete."""
    context, client = _tool_context(tmp_path)
    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={"membership": membership},
        event_type="m.room.member",
        state_key="@mindroom_router:localhost",
        room_id="!project:localhost",
    )
    with tool_runtime_context(context):
        result = await InviteRouterTools().invite_router()

    assert result == expected
    client.room_invite.assert_not_awaited()


@pytest.mark.asyncio
async def test_invite_router_reports_joined_before_disabled_auto_accept(tmp_path: Path) -> None:
    """Disabling future invites must not hide that router recovery is already complete."""
    context, client = _tool_context(tmp_path, accept_invites=False)
    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={"membership": "join"},
        event_type="m.room.member",
        state_key="@mindroom_router:localhost",
        room_id="!project:localhost",
    )

    with tool_runtime_context(context):
        result = await InviteRouterTools().invite_router()

    assert result == "Router already joined."
    client.room_invite.assert_not_awaited()


@pytest.mark.asyncio
async def test_invite_router_cannot_require_router_backed_approval(tmp_path: Path) -> None:
    """Approval-gating the recovery call would deadlock when the router is absent."""
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Write code")},
            tool_approval={"default": "require_approval"},
        ),
        test_runtime_paths(tmp_path),
    )

    toolkit = apply_tool_approval_capability(
        InviteRouterTools(),
        config,
        supports_native_tool_approval=True,
        registered_tool_name="invite_router",
    )
    assert toolkit is not None
    assert toolkit.get_async_functions()["invite_router"].requires_confirmation is not True


@pytest.mark.asyncio
async def test_same_named_external_function_does_not_bypass_approval(tmp_path: Path) -> None:
    """Function name alone must not grant the built-in recovery exemption."""
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Write code")},
            tool_approval={"default": "require_approval"},
        ),
        test_runtime_paths(tmp_path),
    )

    def invite_router() -> str:
        return "external"

    toolkit = apply_tool_approval_capability(
        Toolkit(name="invite_router", tools=[invite_router]),
        config,
        supports_native_tool_approval=True,
        registered_tool_name="external",
    )

    assert toolkit is not None
    assert toolkit.get_functions()["invite_router"].requires_confirmation is True
    assert tool_may_require_approval(config, "invite_router")
    requires_approval, _ = await evaluate_tool_approval(
        config,
        runtime_paths_for(config),
        "invite_router",
        {},
        "code",
    )
    assert requires_approval


@pytest.mark.asyncio
async def test_invite_router_reports_matrix_invite_failure(tmp_path: Path) -> None:
    """A refused invite must not tell the agent that router recovery is underway."""
    context, client = _tool_context(tmp_path)
    client.room_get_state_event.return_value = nio.RoomGetStateEventError(
        "Not found",
        status_code="M_NOT_FOUND",
        room_id="!project:localhost",
    )
    client.room_invite.return_value = nio.RoomInviteError("Forbidden", status_code="M_FORBIDDEN")

    with tool_runtime_context(context):
        result = await InviteRouterTools().invite_router()

    assert result == "Error: Router invite failed; current agent may lack invite permission."
