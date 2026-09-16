"""Current local authority gates retained tools without remote reconstruction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.agent import Agent
from agno.tools import Toolkit
from agno.tools.function import Function

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ToolConfigEntry
from mindroom.tool_jobs.authorization import (
    AUTHORITY_METADATA_KEY,
    authority_snapshot,
    bind_toolkit_authority,
    function_authority,
    locally_allowed,
)
from mindroom.tool_system.registry_state import TOOL_REGISTRY
from mindroom.tool_system.worker_routing import ToolExecutionIdentity

if TYPE_CHECKING:
    import pytest


def test_deferred_job_policy_survives_unloading_but_rejects_new_filters_and_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Historical reads use authored grants and provenance without constructing tools."""
    config = Config(
        agents={"lead": AgentConfig(display_name="Lead", tools=[ToolConfigEntry(name="calculator", defer=True)])},
    )
    owner = ToolExecutionIdentity("matrix", "lead", "@human:localhost", "!room:localhost", None, None, "session")
    toolkit = Toolkit(name="calculator", auto_register=False)
    function = Function(name="add", entrypoint=lambda: None)
    toolkit.functions["add"] = function
    bind_toolkit_authority(toolkit, authored_name="calculator", concrete_name="calculator")
    function._agent = Agent(metadata={AUTHORITY_METADATA_KEY: authority_snapshot(config, "lead")})
    snapshot = function_authority(function)

    def allowed() -> bool:
        return locally_allowed(
            config,
            owner,
            tool_name="add",
            toolkit_name="calculator",
            origin={},
            depth=0,
            authority=snapshot,
        )

    assert allowed()
    config.agents["lead"].tools = [ToolConfigEntry(name="calculator", defer=True, overrides={"exclude_tools": ["add"]})]
    assert not allowed()
    config.agents["lead"].tools = [ToolConfigEntry(name="calculator", defer=True)]
    assert allowed()

    def replacement() -> type[Toolkit]:
        msg = "Historical access must not construct a toolkit"
        raise AssertionError(msg)

    monkeypatch.setitem(TOOL_REGISTRY, "calculator", replacement)
    assert not allowed()


def test_sdk_learning_job_requires_current_enabled_learning() -> None:
    """An arbitrary SDK origin is insufficient; the exact generated feature must remain enabled."""
    config = Config(agents={"lead": AgentConfig(display_name="Lead", learning=True, learning_mode="agentic")})
    owner = ToolExecutionIdentity("matrix", "lead", "@human:localhost", "!room:localhost", None, None, "session")
    origin = {
        "module": "agno.learn.stores.user_memory",
        "qualname": "UserMemoryStore.aget_tools.<locals>.update_user_memory",
    }

    def allowed(tool_name: str) -> bool:
        return locally_allowed(
            config,
            owner,
            tool_name=tool_name,
            toolkit_name=None,
            origin=origin,
            depth=0,
            authority={},
        )

    assert allowed("update_user_memory")
    assert not allowed("arbitrary_callable")
    config.agents["lead"].learning = False
    assert not allowed("update_user_memory")
