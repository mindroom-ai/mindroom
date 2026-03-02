"""Tests for the agent delegation tool (DelegateTools toolkit)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import mindroom.tools  # noqa: F401
from mindroom.agents import create_agent, describe_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.custom_tools.delegate import MAX_DELEGATION_DEPTH, DelegateTools
from mindroom.tool_system.metadata import TOOL_METADATA

if TYPE_CHECKING:
    from pathlib import Path


def _make_config(agents: dict[str, AgentConfig]) -> Config:
    """Create a minimal Config with the given agents."""
    return Config(
        agents=agents,
        models={"default": ModelConfig(provider="openai", id="gpt-4")},
    )


class TestDelegateTools:
    """Tests for the DelegateTools Toolkit."""

    @pytest.fixture
    def storage_path(self, tmp_path: Path) -> Path:
        """Return a temporary storage path."""
        return tmp_path

    @pytest.fixture
    def config(self) -> Config:
        """Create a test config with leader, code, and research agents."""
        return _make_config(
            {
                "leader": AgentConfig(
                    display_name="Leader",
                    role="Orchestrate tasks",
                    delegate_to=["code", "research"],
                ),
                "code": AgentConfig(
                    display_name="CodeAgent",
                    role="Generate code",
                    tools=["file"],
                ),
                "research": AgentConfig(
                    display_name="ResearchAgent",
                    role="Research topics",
                    tools=["duckduckgo"],
                ),
            },
        )

    @pytest.fixture
    def tools(self, storage_path: Path, config: Config) -> DelegateTools:
        """Create a DelegateTools instance for testing."""
        return DelegateTools(
            agent_name="leader",
            delegate_to=["code", "research"],
            storage_path=storage_path,
            config=config,
            delegation_depth=0,
        )

    def test_toolkit_name(self, tools: DelegateTools) -> None:
        """Test that the toolkit is registered with the correct name."""
        assert tools.name == "delegate"

    def test_toolkit_has_delegate_task(self, tools: DelegateTools) -> None:
        """Test that the toolkit exposes the delegate_task function."""
        func_names = [f.name for f in tools.async_functions.values()]
        assert "delegate_task" in func_names

    def test_instructions_contain_agent_descriptions(self, tools: DelegateTools) -> None:
        """Test that toolkit instructions describe available delegation targets."""
        instructions = tools.instructions
        assert instructions is not None
        assert "code" in instructions
        assert "research" in instructions
        assert "Generate code" in instructions
        assert "Research topics" in instructions

    @pytest.mark.asyncio
    async def test_delegate_to_unknown_agent(self, tools: DelegateTools) -> None:
        """Test that delegating to an unknown agent returns an error."""
        result = await tools.delegate_task("unknown_agent", "do something")
        assert "Cannot delegate to 'unknown_agent'" in result
        assert "code" in result
        assert "research" in result

    @pytest.mark.asyncio
    async def test_delegate_empty_task(self, tools: DelegateTools) -> None:
        """Test that delegating an empty task returns an error."""
        result = await tools.delegate_task("code", "")
        assert "Cannot delegate an empty task" in result

    @pytest.mark.asyncio
    async def test_delegate_whitespace_only_task(self, tools: DelegateTools) -> None:
        """Test that delegating a whitespace-only task returns an error."""
        result = await tools.delegate_task("code", "   ")
        assert "Cannot delegate an empty task" in result

    @pytest.mark.asyncio
    async def test_successful_delegation(self, tools: DelegateTools) -> None:
        """Test that a successful delegation returns the agent's response content."""
        mock_response = MagicMock()
        mock_response.content = "Here is the generated code: print('hello')"

        mock_agent = AsyncMock()
        mock_agent.arun = AsyncMock(return_value=mock_response)

        with patch("mindroom.custom_tools.delegate.create_agent", return_value=mock_agent) as mock_create:
            result = await tools.delegate_task("code", "Write a hello world program")

            mock_create.assert_called_once_with(
                "code",
                tools._config,
                storage_path=tools._storage_path,
                knowledge=None,
                include_interactive_questions=False,
                delegation_depth=1,
            )
            mock_agent.arun.assert_called_once_with("Write a hello world program")
            assert result == "Here is the generated code: print('hello')"

    @pytest.mark.asyncio
    async def test_delegation_with_no_content(self, tools: DelegateTools) -> None:
        """Test that delegation with None content returns a fallback message."""
        mock_response = MagicMock()
        mock_response.content = None

        mock_agent = AsyncMock()
        mock_agent.arun = AsyncMock(return_value=mock_response)

        with patch("mindroom.custom_tools.delegate.create_agent", return_value=mock_agent):
            result = await tools.delegate_task("code", "Do something")
            assert "returned no content" in result

    @pytest.mark.asyncio
    async def test_delegation_error_handling(self, tools: DelegateTools) -> None:
        """Test that exceptions during delegation are caught and returned as error strings."""
        with patch(
            "mindroom.custom_tools.delegate.create_agent",
            side_effect=RuntimeError("Agent creation failed"),
        ):
            result = await tools.delegate_task("code", "Do something")
            assert "Delegation to 'code' failed" in result
            assert "Agent creation failed" in result

    @pytest.mark.asyncio
    async def test_delegation_depth_increments(self, storage_path: Path, config: Config) -> None:
        """Verify that delegation_depth is passed through correctly."""
        tools = DelegateTools(
            agent_name="leader",
            delegate_to=["code"],
            storage_path=storage_path,
            config=config,
            delegation_depth=1,
        )

        mock_response = MagicMock()
        mock_response.content = "done"
        mock_agent = AsyncMock()
        mock_agent.arun = AsyncMock(return_value=mock_response)

        with patch("mindroom.custom_tools.delegate.create_agent", return_value=mock_agent) as mock_create:
            await tools.delegate_task("code", "task")
            mock_create.assert_called_once_with(
                "code",
                config,
                storage_path=storage_path,
                knowledge=None,
                include_interactive_questions=False,
                delegation_depth=2,
            )


class TestDelegateKnowledge:
    """Test that delegated agents receive their configured knowledge bases."""

    @pytest.mark.asyncio
    async def test_delegation_resolves_knowledge(self, tmp_path: Path) -> None:
        """Delegated agent with knowledge_bases should receive knowledge."""
        config = Config(
            agents={
                "leader": AgentConfig(
                    display_name="Leader",
                    role="Lead",
                    delegate_to=["researcher"],
                ),
                "researcher": AgentConfig(
                    display_name="Researcher",
                    role="Research with knowledge",
                    knowledge_bases=["docs"],
                ),
            },
            models={"default": ModelConfig(provider="openai", id="gpt-4")},
            knowledge_bases={"docs": {"path": "./docs"}},
        )
        tools = DelegateTools(
            agent_name="leader",
            delegate_to=["researcher"],
            storage_path=tmp_path,
            config=config,
            delegation_depth=0,
        )

        mock_knowledge = MagicMock()
        mock_manager = MagicMock()
        mock_manager.get_knowledge.return_value = mock_knowledge

        mock_response = MagicMock()
        mock_response.content = "Found relevant docs"
        mock_agent = AsyncMock()
        mock_agent.arun = AsyncMock(return_value=mock_response)

        with (
            patch("mindroom.custom_tools.delegate.get_knowledge_manager", return_value=mock_manager) as mock_get_km,
            patch("mindroom.custom_tools.delegate.create_agent", return_value=mock_agent) as mock_create,
        ):
            result = await tools.delegate_task("researcher", "Find info about X")

            mock_get_km.assert_called_with("docs")
            mock_create.assert_called_once_with(
                "researcher",
                config,
                storage_path=tmp_path,
                knowledge=mock_knowledge,
                include_interactive_questions=False,
                delegation_depth=1,
            )
            assert result == "Found relevant docs"

    @pytest.mark.asyncio
    async def test_delegation_without_knowledge_passes_none(self, tmp_path: Path) -> None:
        """Delegated agent without knowledge_bases should receive knowledge=None."""
        config = _make_config(
            {
                "leader": AgentConfig(
                    display_name="Leader",
                    role="Lead",
                    delegate_to=["worker"],
                ),
                "worker": AgentConfig(display_name="Worker", role="Work"),
            },
        )
        tools = DelegateTools(
            agent_name="leader",
            delegate_to=["worker"],
            storage_path=tmp_path,
            config=config,
            delegation_depth=0,
        )

        mock_response = MagicMock()
        mock_response.content = "done"
        mock_agent = AsyncMock()
        mock_agent.arun = AsyncMock(return_value=mock_response)

        with patch("mindroom.custom_tools.delegate.create_agent", return_value=mock_agent) as mock_create:
            await tools.delegate_task("worker", "do work")
            mock_create.assert_called_once_with(
                "worker",
                config,
                storage_path=tmp_path,
                knowledge=None,
                include_interactive_questions=False,
                delegation_depth=1,
            )


class TestDelegateToolRegistration:
    """Test that the delegate tool is properly registered in the metadata registry."""

    def test_delegate_in_tool_metadata(self) -> None:
        """Test that delegate tool appears in the metadata registry."""
        assert "delegate" in TOOL_METADATA
        meta = TOOL_METADATA["delegate"]
        assert meta.display_name == "Agent Delegation"
        assert meta.status.value == "available"
        assert meta.setup_type.value == "none"
        assert meta.category.value == "productivity"


class TestDelegateConfigValidation:
    """Test config validation for delegate_to field."""

    def test_valid_delegate_to(self) -> None:
        """Test that valid delegate_to targets are accepted."""
        config = _make_config(
            {
                "leader": AgentConfig(
                    display_name="Leader",
                    role="Lead",
                    delegate_to=["worker"],
                ),
                "worker": AgentConfig(display_name="Worker", role="Work"),
            },
        )
        assert config.agents["leader"].delegate_to == ["worker"]

    def test_delegate_to_unknown_agent(self) -> None:
        """Test that referencing an unknown agent in delegate_to raises ValueError."""
        with pytest.raises(ValueError, match="delegates to unknown agent 'nonexistent'"):
            _make_config(
                {
                    "leader": AgentConfig(
                        display_name="Leader",
                        role="Lead",
                        delegate_to=["nonexistent"],
                    ),
                },
            )

    def test_delegate_to_self(self) -> None:
        """Test that self-delegation raises ValueError."""
        with pytest.raises(ValueError, match="cannot delegate to itself"):
            _make_config(
                {
                    "leader": AgentConfig(
                        display_name="Leader",
                        role="Lead",
                        delegate_to=["leader"],
                    ),
                },
            )

    def test_empty_delegate_to(self) -> None:
        """Test that empty delegate_to is the default."""
        config = _make_config(
            {
                "agent": AgentConfig(display_name="Agent", role="Do things"),
            },
        )
        assert config.agents["agent"].delegate_to == []


class TestDelegateAutoInjection:
    """Test that DelegateTools is auto-injected when delegate_to is configured."""

    @patch("mindroom.agents.SqliteDb")
    def test_auto_inject_delegate_tool(self, mock_storage: MagicMock) -> None:  # noqa: ARG002
        """Agent with delegate_to should automatically get the delegate tool."""
        config = _make_config(
            {
                "leader": AgentConfig(
                    display_name="Leader",
                    role="Lead",
                    delegate_to=["worker"],
                ),
                "worker": AgentConfig(display_name="Worker", role="Work"),
            },
        )
        agent = create_agent("leader", config=config, include_interactive_questions=False)
        tool_names = [tool.name for tool in agent.tools]
        assert "delegate" in tool_names

    @patch("mindroom.agents.SqliteDb")
    def test_no_delegate_tool_without_config(self, mock_storage: MagicMock) -> None:  # noqa: ARG002
        """Agent without delegate_to should not get the delegate tool."""
        config = _make_config(
            {
                "worker": AgentConfig(display_name="Worker", role="Work"),
            },
        )
        agent = create_agent("worker", config=config, include_interactive_questions=False)
        tool_names = [tool.name for tool in agent.tools]
        assert "delegate" not in tool_names

    @patch("mindroom.agents.SqliteDb")
    def test_depth_limit_prevents_injection(self, mock_storage: MagicMock) -> None:  # noqa: ARG002
        """At max depth, delegate tool should not be auto-injected."""
        config = _make_config(
            {
                "leader": AgentConfig(
                    display_name="Leader",
                    role="Lead",
                    delegate_to=["worker"],
                ),
                "worker": AgentConfig(display_name="Worker", role="Work"),
            },
        )
        agent = create_agent(
            "leader",
            config=config,
            include_interactive_questions=False,
            delegation_depth=MAX_DELEGATION_DEPTH,
        )
        tool_names = [tool.name for tool in agent.tools]
        assert "delegate" not in tool_names

    @patch("mindroom.agents.SqliteDb")
    def test_explicit_delegate_skipped_when_delegate_to_empty(self, mock_storage: MagicMock) -> None:  # noqa: ARG002
        """Explicit 'delegate' in tools list should be skipped when delegate_to is empty."""
        config = _make_config(
            {
                "leader": AgentConfig(
                    display_name="Leader",
                    role="Lead",
                    tools=["delegate"],
                ),
            },
        )
        agent = create_agent("leader", config=config, include_interactive_questions=False)
        tool_names = [tool.name for tool in agent.tools]
        assert "delegate" not in tool_names

    @patch("mindroom.agents.SqliteDb")
    def test_depth_limit_prevents_explicit_delegate_tool(self, mock_storage: MagicMock) -> None:  # noqa: ARG002
        """At max depth, explicit 'delegate' in tools list should be skipped."""
        config = _make_config(
            {
                "leader": AgentConfig(
                    display_name="Leader",
                    role="Lead",
                    tools=["delegate"],
                    delegate_to=["worker"],
                ),
                "worker": AgentConfig(display_name="Worker", role="Work"),
            },
        )
        agent = create_agent(
            "leader",
            config=config,
            include_interactive_questions=False,
            delegation_depth=MAX_DELEGATION_DEPTH,
        )
        tool_names = [tool.name for tool in agent.tools]
        assert "delegate" not in tool_names

    @patch("mindroom.agents.SqliteDb")
    def test_depth_limit_prevents_default_tools_delegate(self, mock_storage: MagicMock) -> None:  # noqa: ARG002
        """At max depth, 'delegate' from defaults.tools should be skipped."""
        config = Config(
            agents={
                "leader": AgentConfig(
                    display_name="Leader",
                    role="Lead",
                    delegate_to=["worker"],
                ),
                "worker": AgentConfig(display_name="Worker", role="Work"),
            },
            models={"default": ModelConfig(provider="openai", id="gpt-4")},
            defaults=DefaultsConfig(tools=["delegate"]),
        )
        agent = create_agent(
            "leader",
            config=config,
            include_interactive_questions=False,
            delegation_depth=MAX_DELEGATION_DEPTH,
        )
        tool_names = [tool.name for tool in agent.tools]
        assert "delegate" not in tool_names


class TestDescribeAgentDelegation:
    """Test that describe_agent includes delegation info."""

    def test_describe_agent_with_delegation(self) -> None:
        """Test that describe_agent output includes delegation targets."""
        config = _make_config(
            {
                "leader": AgentConfig(
                    display_name="Leader",
                    role="Lead the team",
                    delegate_to=["code", "research"],
                ),
                "code": AgentConfig(display_name="CodeAgent", role="Code"),
                "research": AgentConfig(display_name="ResearchAgent", role="Research"),
            },
        )
        description = describe_agent("leader", config)
        assert "Can delegate to: code, research" in description

    def test_describe_agent_without_delegation(self) -> None:
        """Test that describe_agent output omits delegation when not configured."""
        config = _make_config(
            {
                "worker": AgentConfig(display_name="Worker", role="Do work"),
            },
        )
        description = describe_agent("worker", config)
        assert "delegate" not in description.lower()
