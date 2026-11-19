"""Tests for the retired `invite_router` tool name, kept so old configs and prompts load."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.agents import create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.custom_tools.invite_router import InviteRouterTools
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.turn_model_helpers import turn_model

if TYPE_CHECKING:
    from pathlib import Path


def _room_turn_toolkit_names(tmp_path: Path, tools: list[str]) -> set[str]:
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(display_name="Code", role="Write code", include_default_tools=False, tools=tools)
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
    return {toolkit.name for toolkit in agent.spec.toolkits}


def test_invite_router_is_not_offered_by_default(tmp_path: Path) -> None:
    """A Matrix room turn no longer adds the tool to an agent that does not list it."""
    assert "invite_router" not in _room_turn_toolkit_names(tmp_path, [])


def test_authored_invite_router_still_loads(tmp_path: Path) -> None:
    """A config that lists the retired name loads and offers the stub like any other tool."""
    assert "invite_router" in _room_turn_toolkit_names(tmp_path, ["invite_router"])


@pytest.mark.asyncio
async def test_invite_router_answers_no_longer_needed() -> None:
    """The kept tool name invites nobody and says so."""
    result = await InviteRouterTools().invite_router()

    assert result == "invite_router is no longer needed: approvals, commands and access work without another account."
