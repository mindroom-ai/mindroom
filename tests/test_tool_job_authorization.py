"""Current local authority gates retained tools without remote reconstruction."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import mcp.types as mcp_types
import pytest
from agno.agent import Agent
from agno.tools import Toolkit
from agno.tools.function import Function

import mindroom.tool_jobs.authorization as authorization_module
from mindroom.agents import build_agent_toolkit
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, EffectiveToolConfig, ToolConfigEntry
from mindroom.constants import resolve_runtime_paths
from mindroom.mcp.manager import MCPServerManager
from mindroom.mcp.registry import resolved_mcp_tool_state
from mindroom.mcp.toolkit import MindRoomMCPToolkit
from mindroom.tool_jobs.authorization import (
    _configured_tool_allowed,
    authority_snapshot,
    bind_actor_authority,
    bind_toolkit_authority,
    function_authority,
    locally_allowed,
)
from mindroom.tool_system.construction import ToolConstruction, bind_toolkit_construction
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.registry_state import TOOL_REGISTRY
from mindroom.tool_system.worker_routing import ToolExecutionIdentity

if TYPE_CHECKING:
    from pathlib import Path

    from mcp import ClientSession


_OWNER = ToolExecutionIdentity("matrix", "lead", "@human:localhost", "!room:localhost", None, None, "session")


@pytest.mark.parametrize("change", ["files_only", "remove_one", "private_disabled", "keyword_memory", "missing_scope"])
def test_retained_knowledge_requires_the_exact_current_source_policy(change: str) -> None:
    """A surviving search capability cannot retain a removed source or an uncaptured scope."""
    agent: dict[str, object] = {"display_name": "Lead", "knowledge_bases": ["first", "second"]}
    if change == "private_disabled":
        agent = {
            "display_name": "Lead",
            "private": {"per": "user_agent", "knowledge": {"enabled": True, "path": "knowledge"}},
        }
    elif change == "keyword_memory":
        agent = {"display_name": "Lead", "memory_backend": "file", "memory_search": {"mode": "semantic"}}
    config = Config.model_validate(
        {
            "agents": {"lead": agent},
            "knowledge_bases": {"first": {"path": "first"}, "second": {"path": "second"}},
        },
    )
    captured = authority_snapshot(config, "lead")

    def allowed() -> bool:
        return locally_allowed(
            config,
            _OWNER,
            tool_name="search_knowledge_base",
            toolkit_name=None,
            depth=0,
            origin={
                "module": "agno.agent._default_tools",
                "qualname": "create_knowledge_search_tool.search_knowledge_base",
            },
            authority=captured,
        )

    assert allowed()
    if change == "files_only":
        for base in config.knowledge_bases.values():
            base.mode = "files"
    elif change == "remove_one":
        config.agents["lead"].knowledge_bases.pop()
    elif change == "private_disabled":
        private = config.agents["lead"].private
        assert private is not None
        assert private.knowledge is not None
        private.knowledge.enabled = False
    elif change == "keyword_memory":
        config.agents["lead"].memory_search.mode = "keyword"
    else:
        captured.clear()
    assert not allowed()


def test_direct_toolkit_retains_authored_configuration_grant(tmp_path: Path) -> None:
    """Direct toolkit construction admits its current options and rejects changed ones."""
    entry = ToolConfigEntry(name="dynamic_workflow", overrides={"allowed_tools": ["calculator"]})
    config = Config(agents={"lead": AgentConfig(display_name="Lead", tools=[entry])})
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    toolkit = build_agent_toolkit(
        entry.name,
        agent_name="lead",
        config=config,
        runtime_paths=paths,
        worker_tools=[],
        runtime_overrides=None,
        tool_config_overrides=config.resolve_entity("lead").authored_tool_configs[0].tool_config_overrides,
        execution_identity=_OWNER,
        session_id=_OWNER.session_id,
    )
    assert toolkit is not None
    bind_toolkit_authority(toolkit, authored_name=entry.name)
    function = toolkit.get_async_functions()["list_workflows"]
    function._agent = bind_actor_authority(Agent(), authority_snapshot(config, "lead"))
    authority = function_authority(function)

    def allowed() -> bool:
        return locally_allowed(
            config,
            _OWNER,
            tool_name=function.name,
            toolkit_name=function.owning_toolkit,
            origin={},
            depth=0,
            authority=authority,
        )

    assert allowed()
    entry.overrides.clear()
    assert not allowed()


def _calculator_authority(config: Config) -> dict[str, object]:
    toolkit = Toolkit(name="calculator", auto_register=False)
    function = Function(name="add", entrypoint=lambda: None)
    toolkit.functions["add"] = function
    bind_toolkit_construction(toolkit, ToolConstruction.from_factory("calculator", TOOL_REGISTRY["calculator"]))
    bind_toolkit_authority(toolkit, authored_name="calculator")
    function._agent = bind_actor_authority(Agent(), authority_snapshot(config, "lead"))
    return function_authority(function)


def _calculator_allowed(config: Config, authority: dict[str, object]) -> bool:
    return locally_allowed(
        config,
        _OWNER,
        tool_name="add",
        toolkit_name="calculator",
        origin={},
        depth=0,
        authority=authority,
    )


@pytest.mark.parametrize(
    ("overrides", "expected_allowed"),
    [
        pytest.param({}, True, id="absent"),
        pytest.param({"include_tools": None}, True, id="null"),
        pytest.param({"include_tools": []}, False, id="empty"),
        pytest.param({"include_tools": ["add"]}, True, id="allowed"),
        pytest.param({"include_tools": ["subtract"]}, False, id="denied"),
    ],
)
def test_authored_include_policy_matches_real_toolkit_surface(
    tmp_path: Path,
    overrides: dict[str, object],
    expected_allowed: bool,
) -> None:
    """Retained authority must match absent, null, empty, and named toolkit filters."""
    config = Config(
        agents={
            "lead": AgentConfig(
                display_name="Lead",
                tools=[ToolConfigEntry(name="calculator", defer=True, overrides=overrides)],
            ),
        },
    )
    effective = config.resolve_entity("lead").authored_deferred_tool_configs[0]
    toolkit = get_tool_by_name(
        "calculator",
        resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage"),
        tool_config_overrides=effective.tool_config_overrides,
        disable_sandbox_proxy=True,
        worker_target=None,
    )

    assert ("add" in toolkit.functions) is expected_allowed
    assert _calculator_allowed(config, _calculator_authority(config)) is expected_allowed


@pytest.mark.parametrize(
    ("agent_overrides", "expected_allowed"),
    [
        pytest.param({}, False, id="inherited_exclusion"),
        pytest.param({"exclude_tools": None}, True, id="null_clears_inherited_exclusion"),
    ],
)
def test_nullable_authored_exclusion_matches_inherited_toolkit_surface(
    tmp_path: Path,
    agent_overrides: dict[str, object],
    expected_allowed: bool,
) -> None:
    """A null authored exclusion clears the inherited denylist without crashing authorization."""
    config = Config(
        defaults=DefaultsConfig(
            tools=[ToolConfigEntry(name="calculator", overrides={"exclude_tools": ["add"]})],
        ),
        agents={
            "lead": AgentConfig(
                display_name="Lead",
                tools=[ToolConfigEntry(name="calculator", defer=True, overrides=agent_overrides)],
            ),
        },
    )
    effective = config.resolve_entity("lead").authored_deferred_tool_configs[0]
    toolkit = get_tool_by_name(
        "calculator",
        resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage"),
        tool_config_overrides=effective.tool_config_overrides,
        disable_sandbox_proxy=True,
        worker_target=None,
    )

    assert ("add" in toolkit.functions) is expected_allowed
    assert _calculator_allowed(config, _calculator_authority(config)) is expected_allowed


@pytest.mark.parametrize(
    ("server_include", "authored_include", "expected_allowed"),
    [
        pytest.param([], None, True, id="empty_server_include_is_unrestricted"),
        pytest.param(["read"], None, True, id="server_include_allows_name"),
        pytest.param(["write"], None, False, id="server_include_denies_name"),
        pytest.param([], [], True, id="empty_mcp_assignment_include_is_unrestricted"),
        pytest.param([], ["read"], True, id="mcp_assignment_include_allows_name"),
        pytest.param([], ["write"], False, id="mcp_assignment_include_denies_name"),
    ],
)
def test_mcp_server_and_authored_include_conventions_remain_distinct(
    monkeypatch: pytest.MonkeyPatch,
    server_include: list[str],
    authored_include: list[str] | None,
    expected_allowed: bool,
) -> None:
    """MCP assignment and server empty includes retain their unrestricted convention."""
    config = Config.model_validate(
        {
            "agents": {"lead": {"display_name": "Lead"}},
            "mcp_servers": {
                "demo": {
                    "transport": "stdio",
                    "command": "test-server",
                    "include_tools": server_include,
                },
            },
        },
    )
    overrides = {} if authored_include is None else {"include_tools": authored_include}
    entry = EffectiveToolConfig(name="mcp_demo", tool_config_overrides=overrides)
    registry, _ = resolved_mcp_tool_state(config)
    monkeypatch.setitem(TOOL_REGISTRY, "mcp_demo", registry["mcp_demo"])
    construction_origin = authorization_module.tool_registry_origins()["mcp_demo"]

    assert (
        _configured_tool_allowed(
            config,
            _OWNER,
            entry,
            "read",
            {"mcp_server_id": "demo", "mcp_tool_name": "read"},
            {"name": "mcp_demo", "factory_origin": construction_origin},
        )
        is expected_allowed
    )


@pytest.mark.parametrize(
    ("server_filters", "authored_filters"),
    [
        pytest.param(
            {"include_tools": ["read"]},
            {"exclude_tools": ["read"]},
            id="server_includes_assignment_excludes",
        ),
        pytest.param(
            {"exclude_tools": ["read"]},
            {"include_tools": ["read"]},
            id="server_excludes_assignment_includes",
        ),
    ],
)
@pytest.mark.asyncio
async def test_mcp_cross_owner_exclusion_wins_for_construction_and_retained_authorization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    server_filters: dict[str, list[str]],
    authored_filters: dict[str, list[str]],
) -> None:
    """A denylist at either MCP filter layer must hide and deauthorize a remote tool."""
    config = Config.model_validate(
        {
            "agents": {"lead": {"display_name": "Lead"}},
            "mcp_servers": {
                "demo": {
                    "transport": "stdio",
                    "command": "test-server",
                    **server_filters,
                },
            },
        },
    )

    class _SingleToolSession:
        @staticmethod
        async def list_tools(cursor: str | None = None) -> mcp_types.ListToolsResult:
            assert cursor is None
            return mcp_types.ListToolsResult(
                tools=[
                    mcp_types.Tool(
                        name="read",
                        description="Read",
                        inputSchema={"type": "object", "properties": {}},
                    ),
                ],
            )

    manager = MCPServerManager(
        resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage"),
    )
    catalog = await manager._discover_catalog(
        "demo",
        config.mcp_servers["demo"],
        cast("ClientSession", _SingleToolSession()),
        mcp_types.InitializeResult(
            protocolVersion="2025-03-26",
            capabilities=mcp_types.ServerCapabilities(),
            serverInfo=mcp_types.Implementation(name="demo", version="1.0"),
        ),
    )
    toolkit = MindRoomMCPToolkit(
        server_id="demo",
        manager=manager,
        catalog=catalog,
        server_config=config.mcp_servers["demo"],
        include_tools=authored_filters.get("include_tools"),
        exclude_tools=authored_filters.get("exclude_tools"),
    )
    entry = EffectiveToolConfig(name="mcp_demo", tool_config_overrides=authored_filters)
    registry, _ = resolved_mcp_tool_state(config)
    monkeypatch.setitem(TOOL_REGISTRY, "mcp_demo", registry["mcp_demo"])
    construction_origin = authorization_module.tool_registry_origins()["mcp_demo"]

    assert "demo_read" not in toolkit.async_functions
    assert not _configured_tool_allowed(
        config,
        _OWNER,
        entry,
        "read",
        {"mcp_server_id": "demo", "mcp_tool_name": "read"},
        {"name": "mcp_demo", "factory_origin": construction_origin},
    )


def test_mcp_oauth_helpers_bypass_remote_tool_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP assignment filters apply to remote names, not always-visible local OAuth helpers."""
    config = Config.model_validate(
        {
            "agents": {"lead": {"display_name": "Lead"}},
            "mcp_servers": {
                "demo": {
                    "transport": "streamable-http",
                    "url": "https://mcp.example.test/api",
                    "auth": {
                        "type": "oauth",
                        "discovery": "manual",
                        "authorization_url": "https://auth.example.test/authorize",
                        "token_url": "https://auth.example.test/token",
                    },
                },
            },
        },
    )
    overrides = {"include_tools": [], "exclude_tools": ["demo_connection_status"]}
    toolkit = MindRoomMCPToolkit(
        server_id="demo",
        manager=None,
        catalog=None,
        tool_name="mcp_demo",
        server_config=config.mcp_servers["demo"],
        include_tools=overrides["include_tools"],
        exclude_tools=overrides["exclude_tools"],
    )
    entry = EffectiveToolConfig(name="mcp_demo", tool_config_overrides=overrides)
    registry, _ = resolved_mcp_tool_state(config)
    monkeypatch.setitem(TOOL_REGISTRY, "mcp_demo", registry["mcp_demo"])
    construction_origin = authorization_module.tool_registry_origins()["mcp_demo"]

    assert "demo_connection_status" in toolkit.async_functions
    assert _configured_tool_allowed(
        config,
        _OWNER,
        entry,
        "demo_connection_status",
        {"mcp_server_id": "demo"},
        {"name": "mcp_demo", "factory_origin": construction_origin},
    )


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
    bind_toolkit_construction(toolkit, ToolConstruction.from_factory("calculator", TOOL_REGISTRY["calculator"]))
    bind_toolkit_authority(toolkit, authored_name="calculator")
    function._agent = bind_actor_authority(Agent(), authority_snapshot(config, "lead"))
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
            authority=authority_snapshot(config, "lead"),
        )

    assert allowed("update_user_memory")
    assert not allowed("arbitrary_callable")
    config.agents["lead"].learning = False
    assert not allowed("update_user_memory")
