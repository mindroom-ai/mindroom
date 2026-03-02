"""Tests for the SelfConfigTools toolkit."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from mindroom.agents import create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.custom_tools.self_config import SelfConfigTools

_DEFAULT_MODELS = {"default": ModelConfig(provider="openai", id="gpt-4o")}


def _make_config(
    agents: dict[str, AgentConfig] | None = None,
    knowledge_bases: dict[str, KnowledgeBaseConfig] | None = None,
    defaults: DefaultsConfig | None = None,
    models: dict[str, ModelConfig] | None = None,
) -> tuple[Config, Path]:
    """Create a Config, write it to a temp file, and return both."""
    config = Config(
        agents=agents or {},
        knowledge_bases=knowledge_bases or {},
        defaults=defaults or DefaultsConfig(),
        models=models if models is not None else _DEFAULT_MODELS,
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
        config_path = Path(tmp.name)
    config.save_to_yaml(config_path)
    return config, config_path


class TestGetOwnConfig:
    """Tests for SelfConfigTools.get_own_config."""

    def test_get_own_config(self) -> None:
        """Agent should see its own config as YAML."""
        _, config_path = _make_config(
            agents={
                "writer": AgentConfig(display_name="Writer", role="Write things", tools=["googlesearch"]),
            },
        )
        try:
            tool = SelfConfigTools(agent_name="writer", config_path=config_path)
            result = tool.get_own_config()
            assert "writer" in result
            assert "Writer" in result
            assert "Write things" in result
            assert "googlesearch" in result
        finally:
            config_path.unlink(missing_ok=True)

    def test_get_own_config_not_found(self) -> None:
        """Should return an error when the agent doesn't exist in config."""
        _, config_path = _make_config(agents={})
        try:
            tool = SelfConfigTools(agent_name="ghost", config_path=config_path)
            result = tool.get_own_config()
            assert "Error" in result
            assert "ghost" in result
        finally:
            config_path.unlink(missing_ok=True)


class TestUpdateOwnConfig:
    """Tests for SelfConfigTools.update_own_config."""

    def test_update_role(self) -> None:
        """Updating the role should persist to YAML."""
        _, config_path = _make_config(
            agents={"coder": AgentConfig(display_name="Coder", role="Old role")},
        )
        try:
            tool = SelfConfigTools(agent_name="coder", config_path=config_path)
            result = tool.update_own_config(role="New role")
            assert "Successfully" in result
            assert "Role" in result

            # Verify persisted
            reloaded = Config.from_yaml(config_path)
            assert reloaded.agents["coder"].role == "New role"
        finally:
            config_path.unlink(missing_ok=True)

    def test_update_tools_valid(self) -> None:
        """Valid tool names should be accepted."""
        _, config_path = _make_config(
            agents={"coder": AgentConfig(display_name="Coder", role="Code", tools=[])},
        )
        try:
            tool = SelfConfigTools(agent_name="coder", config_path=config_path)
            result = tool.update_own_config(tools=["googlesearch", "calculator"])
            assert "Successfully" in result

            reloaded = Config.from_yaml(config_path)
            assert reloaded.agents["coder"].tools == ["googlesearch", "calculator"]
        finally:
            config_path.unlink(missing_ok=True)

    def test_update_tools_allows_openclaw_compat(self) -> None:
        """openclaw_compat should be accepted in tools updates and expand implied tools."""
        _, config_path = _make_config(
            agents={"coder": AgentConfig(display_name="Coder", role="Code", tools=[])},
        )
        try:
            tool = SelfConfigTools(agent_name="coder", config_path=config_path)
            result = tool.update_own_config(tools=["openclaw_compat", "python"])
            assert "Successfully" in result

            reloaded = Config.from_yaml(config_path)
            assert reloaded.agents["coder"].tools == ["openclaw_compat", "python"]
            effective = reloaded.get_agent_tools("coder")
            assert effective[0] == "openclaw_compat"
            assert "shell" in effective
            assert "matrix_message" in effective
        finally:
            config_path.unlink(missing_ok=True)

    def test_update_tools_invalid(self) -> None:
        """Invalid tool names should be rejected."""
        _, config_path = _make_config(
            agents={"coder": AgentConfig(display_name="Coder", role="Code")},
        )
        try:
            tool = SelfConfigTools(agent_name="coder", config_path=config_path)
            result = tool.update_own_config(tools=["nonexistent_tool"])
            assert "Error" in result
            assert "nonexistent_tool" in result
        finally:
            config_path.unlink(missing_ok=True)

    def test_update_tools_blocks_privileged_tool(self) -> None:
        """Self-config should not allow assigning privileged global-config tools."""
        _, config_path = _make_config(
            agents={"coder": AgentConfig(display_name="Coder", role="Code", tools=["self_config"])},
        )
        try:
            tool = SelfConfigTools(agent_name="coder", config_path=config_path)
            result = tool.update_own_config(tools=["config_manager"])
            assert "Error" in result
            assert "privileged tools" in result
            assert "config_manager" in result

            reloaded = Config.from_yaml(config_path)
            assert reloaded.agents["coder"].tools == ["self_config"]
        finally:
            config_path.unlink(missing_ok=True)

    def test_update_include_default_tools_blocks_when_defaults_contain_privileged(self) -> None:
        """Setting include_default_tools=True should be blocked if defaults.tools has privileged tools."""
        _, config_path = _make_config(
            agents={"coder": AgentConfig(display_name="Coder", role="Code", include_default_tools=False)},
            defaults=DefaultsConfig(tools=["config_manager"]),
        )
        try:
            tool = SelfConfigTools(agent_name="coder", config_path=config_path)
            result = tool.update_own_config(include_default_tools=True)
            assert "Error" in result
            assert "privileged tools" in result
            assert "config_manager" in result

            reloaded = Config.from_yaml(config_path)
            assert reloaded.agents["coder"].include_default_tools is False
        finally:
            config_path.unlink(missing_ok=True)

    def test_update_include_default_tools_allowed_when_defaults_clean(self) -> None:
        """Setting include_default_tools=True should succeed when defaults.tools has no privileged tools."""
        _, config_path = _make_config(
            agents={"coder": AgentConfig(display_name="Coder", role="Code", include_default_tools=False)},
            defaults=DefaultsConfig(tools=["googlesearch"]),
        )
        try:
            tool = SelfConfigTools(agent_name="coder", config_path=config_path)
            result = tool.update_own_config(include_default_tools=True)
            assert "Successfully" in result

            reloaded = Config.from_yaml(config_path)
            assert reloaded.agents["coder"].include_default_tools is True
        finally:
            config_path.unlink(missing_ok=True)

    def test_update_knowledge_bases_valid(self) -> None:
        """Valid knowledge base IDs should be accepted."""
        _, config_path = _make_config(
            agents={"coder": AgentConfig(display_name="Coder", role="Code")},
            knowledge_bases={"docs": KnowledgeBaseConfig(path="./docs")},
        )
        try:
            tool = SelfConfigTools(agent_name="coder", config_path=config_path)
            result = tool.update_own_config(knowledge_bases=["docs"])
            assert "Successfully" in result

            reloaded = Config.from_yaml(config_path)
            assert reloaded.agents["coder"].knowledge_bases == ["docs"]
        finally:
            config_path.unlink(missing_ok=True)

    def test_update_knowledge_bases_invalid(self) -> None:
        """Unknown knowledge base IDs should be rejected."""
        _, config_path = _make_config(
            agents={"coder": AgentConfig(display_name="Coder", role="Code")},
        )
        try:
            tool = SelfConfigTools(agent_name="coder", config_path=config_path)
            result = tool.update_own_config(knowledge_bases=["missing_kb"])
            assert "Error" in result
            assert "missing_kb" in result
        finally:
            config_path.unlink(missing_ok=True)

    def test_update_knowledge_bases_duplicate(self) -> None:
        """Duplicate knowledge base IDs should be rejected."""
        _, config_path = _make_config(
            agents={"coder": AgentConfig(display_name="Coder", role="Code")},
            knowledge_bases={"docs": KnowledgeBaseConfig(path="./docs")},
        )
        try:
            tool = SelfConfigTools(agent_name="coder", config_path=config_path)
            result = tool.update_own_config(knowledge_bases=["docs", "docs"])
            assert "Error" in result
            assert "Duplicate" in result
        finally:
            config_path.unlink(missing_ok=True)

    def test_update_multiple_fields(self) -> None:
        """Multiple fields can be updated at once."""
        _, config_path = _make_config(
            agents={"coder": AgentConfig(display_name="Coder", role="Code")},
        )
        try:
            tool = SelfConfigTools(agent_name="coder", config_path=config_path)
            result = tool.update_own_config(
                display_name="Super Coder",
                role="Write awesome code",
                markdown=False,
            )
            assert "Successfully" in result

            reloaded = Config.from_yaml(config_path)
            assert reloaded.agents["coder"].display_name == "Super Coder"
            assert reloaded.agents["coder"].role == "Write awesome code"
            assert reloaded.agents["coder"].markdown is False
        finally:
            config_path.unlink(missing_ok=True)

    def test_no_change(self) -> None:
        """When all values match current config, report no changes."""
        _, config_path = _make_config(
            agents={"coder": AgentConfig(display_name="Coder", role="Code")},
        )
        try:
            tool = SelfConfigTools(agent_name="coder", config_path=config_path)
            result = tool.update_own_config(role="Code")
            assert "No changes" in result
        finally:
            config_path.unlink(missing_ok=True)

    def test_update_rejects_invalid_thread_mode(self) -> None:
        """Invalid thread_mode should be rejected and not persisted."""
        _, config_path = _make_config(
            agents={"coder": AgentConfig(display_name="Coder", role="Code")},
        )
        try:
            tool = SelfConfigTools(agent_name="coder", config_path=config_path)
            result = tool.update_own_config(thread_mode="invalid")
            assert "Error validating configuration" in result

            reloaded = Config.from_yaml(config_path)
            assert reloaded.agents["coder"].thread_mode == "thread"
        finally:
            config_path.unlink(missing_ok=True)

    def test_update_rejects_mutually_exclusive_history_fields(self) -> None:
        """Both history knobs at once should be rejected and not persisted."""
        _, config_path = _make_config(
            agents={"coder": AgentConfig(display_name="Coder", role="Code")},
        )
        try:
            tool = SelfConfigTools(agent_name="coder", config_path=config_path)
            result = tool.update_own_config(num_history_runs=2, num_history_messages=10)
            assert "Error validating configuration" in result

            reloaded = Config.from_yaml(config_path)
            assert reloaded.agents["coder"].num_history_runs is None
            assert reloaded.agents["coder"].num_history_messages is None
        finally:
            config_path.unlink(missing_ok=True)

    def test_update_agent_not_found(self) -> None:
        """Updating a nonexistent agent should return an error."""
        _, config_path = _make_config(agents={})
        try:
            tool = SelfConfigTools(agent_name="ghost", config_path=config_path)
            result = tool.update_own_config(role="New role")
            assert "Error" in result
            assert "ghost" in result
        finally:
            config_path.unlink(missing_ok=True)


class TestAgentCreationInjection:
    """Tests for allow_self_config injection in create_agent."""

    @patch("mindroom.agents.SqliteDb")
    def test_allow_self_config_true_injects_tool(self, _mock_storage: MagicMock) -> None:  # noqa: PT019
        """Agent with allow_self_config=True should have self_config tool."""
        config = Config.from_yaml()
        # Pick an existing agent and enable self_config
        agent_name = next(iter(config.agents))
        config.agents[agent_name].allow_self_config = True

        agent = create_agent(agent_name, config=config)
        tool_names = [t.name for t in agent.tools]
        assert "self_config" in tool_names

    @patch("mindroom.agents.SqliteDb")
    def test_allow_self_config_false_no_tool(self, _mock_storage: MagicMock) -> None:  # noqa: PT019
        """Agent with allow_self_config=False should not have self_config tool."""
        config = Config.from_yaml()
        agent_name = next(iter(config.agents))
        config.agents[agent_name].allow_self_config = False

        agent = create_agent(agent_name, config=config)
        tool_names = [t.name for t in agent.tools]
        assert "self_config" not in tool_names

    @patch("mindroom.agents.SqliteDb")
    def test_defaults_fallback_true(self, _mock_storage: MagicMock) -> None:  # noqa: PT019
        """When agent omits allow_self_config, defaults.allow_self_config=True should inject."""
        config = Config.from_yaml()
        agent_name = next(iter(config.agents))
        config.agents[agent_name].allow_self_config = None  # not set
        config.defaults.allow_self_config = True

        agent = create_agent(agent_name, config=config)
        tool_names = [t.name for t in agent.tools]
        assert "self_config" in tool_names

    @patch("mindroom.agents.SqliteDb")
    def test_defaults_fallback_false(self, _mock_storage: MagicMock) -> None:  # noqa: PT019
        """When agent omits allow_self_config, defaults.allow_self_config=False should not inject."""
        config = Config.from_yaml()
        agent_name = next(iter(config.agents))
        config.agents[agent_name].allow_self_config = None
        config.defaults.allow_self_config = False

        agent = create_agent(agent_name, config=config)
        tool_names = [t.name for t in agent.tools]
        assert "self_config" not in tool_names

    @patch("mindroom.agents.SqliteDb")
    def test_agent_override_beats_default(self, _mock_storage: MagicMock) -> None:  # noqa: PT019
        """Agent-level allow_self_config should override default."""
        config = Config.from_yaml()
        agent_name = next(iter(config.agents))
        config.defaults.allow_self_config = True
        config.agents[agent_name].allow_self_config = False

        agent = create_agent(agent_name, config=config)
        tool_names = [t.name for t in agent.tools]
        assert "self_config" not in tool_names

    @patch("mindroom.agents.SqliteDb")
    def test_manual_self_config_tool_loads(self, _mock_storage: MagicMock) -> None:  # noqa: PT019
        """Explicitly configured self_config tool should be loadable."""
        config = Config.from_yaml()
        agent_name = next(iter(config.agents))
        config.agents[agent_name].allow_self_config = False
        config.agents[agent_name].tools = ["self_config"]

        agent = create_agent(agent_name, config=config)
        tool_names = [t.name for t in agent.tools]
        assert "self_config" in tool_names

    @patch("mindroom.agents.SqliteDb")
    def test_self_config_not_duplicated_when_manual_and_auto(self, _mock_storage: MagicMock) -> None:  # noqa: PT019
        """Manual self_config plus allow_self_config should still produce one tool instance."""
        config = Config.from_yaml()
        agent_name = next(iter(config.agents))
        config.agents[agent_name].allow_self_config = True
        config.agents[agent_name].tools = ["self_config"]

        agent = create_agent(agent_name, config=config)
        tool_names = [t.name for t in agent.tools]
        assert tool_names.count("self_config") == 1

    @patch("mindroom.agents.SqliteDb")
    def test_config_path_threaded_to_self_config_auto(self, _mock_storage: MagicMock) -> None:  # noqa: PT019
        """Auto-injected self_config tool should use the config_path from create_agent."""
        config, config_path = _make_config(
            agents={"writer": AgentConfig(display_name="Writer", role="Write", allow_self_config=True)},
        )
        try:
            agent = create_agent("writer", config=config, config_path=config_path)
            self_config_tool = next(t for t in agent.tools if getattr(t, "name", None) == "self_config")
            assert self_config_tool.config_path == config_path

            # The tool should be able to read this agent's config from the temp file
            result = self_config_tool.get_own_config()
            assert "Writer" in result
            assert "Error" not in result
        finally:
            config_path.unlink(missing_ok=True)

    @patch("mindroom.agents.SqliteDb")
    def test_config_path_threaded_to_self_config_manual(self, _mock_storage: MagicMock) -> None:  # noqa: PT019
        """Manually listed self_config tool should use the config_path from create_agent."""
        config, config_path = _make_config(
            agents={"writer": AgentConfig(display_name="Writer", role="Write", tools=["self_config"])},
        )
        try:
            agent = create_agent("writer", config=config, config_path=config_path)
            self_config_tool = next(t for t in agent.tools if getattr(t, "name", None) == "self_config")
            assert self_config_tool.config_path == config_path

            result = self_config_tool.get_own_config()
            assert "Writer" in result
            assert "Error" not in result
        finally:
            config_path.unlink(missing_ok=True)
