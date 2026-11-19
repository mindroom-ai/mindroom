"""Tests for the agent-owned router invite recovery tool."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from agno.tools import Toolkit

import mindroom.agents as agents_module
from mindroom.agents import create_agent
from mindroom.approval.tool_approval import evaluate_tool_approval, tool_may_require_approval
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.custom_tools.invite_router import InviteRouterTools
from mindroom.llm.messages import stable_serialize
from mindroom.message_target import MessageTarget
from mindroom.tool_system.output_files import OUTPUT_PATH_ARGUMENT
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.tool_function import ToolGate
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.fakes.token_counter import quarter_tokens
from tests.tool_approval_helpers import gated_functions
from tests.turn_model_helpers import turn_model

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.runtime_context import ToolRuntimeContext


def _tool_context(
    tmp_path: Path,
    *,
    accept_invites: bool | list[str] = True,
    transport_agent_name: str | None = None,
) -> tuple[ToolRuntimeContext, AsyncMock]:
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    role="Write code",
                    include_default_tools=False,
                ),
            },
            teams=(
                {
                    "builders": TeamConfig(
                        display_name="Builders",
                        role="Build software",
                        agents=["code"],
                    ),
                }
                if transport_agent_name == "builders"
                else {}
            ),
            router=RouterConfig(model="default", accept_invites=accept_invites),
            models={"default": ModelConfig(provider="openai", id="gpt-6-astra")},
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
        transport_agent_name=transport_agent_name,
        client=client,
        config=config,
        runtime_paths=runtime_paths_for(config),
        conversation_reader=make_conversation_reader_mock(),
        relations=make_relation_lookup(),
    )
    return context, client


def test_matrix_agents_get_zero_argument_invite_router_in_standard_tool_environment(tmp_path: Path) -> None:
    """The normal execution inventory must describe every available local tool."""
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    role="Write code",
                    include_default_tools=False,
                ),
            },
            models={"default": ModelConfig(provider="openai", id="gpt-6-astra")},
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
        knowledge=None,
        model=turn_model(config, runtime_paths_for(config), "code"),
        plugins=config_plugins(config, runtime_paths_for(config)),
    )

    [toolkit] = [toolkit for toolkit in agent.spec.toolkits if toolkit.name == "invite_router"]
    [function] = toolkit.functions
    properties = dict(function.parameters["properties"])
    # Like every eligible function of an agent, it also takes the optional argument that saves its output to a file.
    assert properties.pop(OUTPUT_PATH_ARGUMENT)["default"] is None
    own_parameters = {**function.parameters, "properties": properties}
    definition = {"name": function.name, "description": function.description, "parameters": own_parameters}
    definition_tokens = quarter_tokens(stable_serialize([definition]))

    assert properties == {}
    assert function.parameters.get("required", []) == []
    assert definition_tokens <= 36
    assert toolkit.instructions is None
    assert "## Tool Execution Environment" in agent.prompt.role
    assert "`invite_router`" in agent.prompt.role


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
            models={"default": ModelConfig(provider="openai", id="gpt-6-astra")},
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
        knowledge=None,
        model=turn_model(config, runtime_paths_for(config), "code"),
        plugins=config_plugins(config, runtime_paths_for(config)),
    )

    [toolkit] = [toolkit for toolkit in agent.spec.toolkits if toolkit.name == "invite_router"]
    assert {function.name for function in toolkit.functions} == {"invite_router"}


def test_invite_router_stays_hidden_without_matrix_room_context(tmp_path: Path) -> None:
    """Non-Matrix callers should not pay for or call a room-only recovery tool."""
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Write code")},
            models={"default": ModelConfig(provider="openai", id="gpt-6-astra")},
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
        knowledge=None,
        model=turn_model(config, runtime_paths_for(config), "code"),
        plugins=config_plugins(config, runtime_paths_for(config)),
    )

    assert "invite_router" not in {toolkit.name for toolkit in agent.spec.toolkits}


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
            models={"default": ModelConfig(provider="openai", id="gpt-6-astra")},
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
            knowledge=None,
            model=turn_model(config, runtime_paths_for(config), "code"),
            plugins=config_plugins(config, runtime_paths_for(config)),
        )


@pytest.mark.asyncio
async def test_invite_router_cannot_require_router_backed_approval(tmp_path: Path) -> None:
    """Approval-gating the recovery call would deadlock when the router is absent."""
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Write code")},
            models={"default": ModelConfig(provider="openai", id="gpt-6-astra")},
            tool_approval={"default": "require_approval"},
        ),
        test_runtime_paths(tmp_path),
    )

    functions = gated_functions(InviteRouterTools(), config, registered_tool_name="invite_router")
    assert functions["invite_router"].gate is None


@pytest.mark.asyncio
async def test_same_named_external_function_does_not_bypass_approval(tmp_path: Path) -> None:
    """Function name alone must not grant the built-in recovery exemption."""
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Write code")},
            models={"default": ModelConfig(provider="openai", id="gpt-6-astra")},
            tool_approval={"default": "require_approval"},
        ),
        test_runtime_paths(tmp_path),
    )

    def invite_router() -> str:
        return "external"

    functions = gated_functions(
        Toolkit(name="invite_router", tools=[invite_router]),
        config,
        registered_tool_name="external",
    )

    assert functions["invite_router"].gate is ToolGate.POLICY
    assert tool_may_require_approval(config, "invite_router")
    requires_approval, _ = await evaluate_tool_approval(
        config,
        runtime_paths_for(config),
        "invite_router",
        {},
        "code",
        None,
    )
    assert requires_approval


def test_a_function_named_after_its_registered_tool_stays_gated(tmp_path: Path) -> None:
    """Only the reserved recovery function skips approval rules, not any function its own registered tool offers."""
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Write code")},
            models={"default": ModelConfig(provider="openai", id="gpt-6-astra")},
            tool_approval={"default": "require_approval"},
        ),
        test_runtime_paths(tmp_path),
    )

    def lookup() -> str:
        return "found"

    functions = gated_functions(Toolkit(name="lookup", tools=[lookup]), config)

    assert functions["lookup"].gate is ToolGate.POLICY


@pytest.mark.asyncio
async def test_invite_router_answers_no_longer_needed(tmp_path: Path) -> None:
    """The kept tool name invites nobody: an unregistered owner ID must never receive a room invite."""
    context, client = _tool_context(tmp_path)

    with tool_runtime_context(context):
        result = await InviteRouterTools().invite_router()

    assert result == "invite_router is no longer needed: approvals, commands and access work without another account."
    client.room_invite.assert_not_awaited()
    client.room_get_state_event.assert_not_awaited()
