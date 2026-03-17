"""Test the consolidated ConfigManager tool with fewer methods."""

from __future__ import annotations

import tempfile
from pathlib import Path

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.config.matrix import MindRoomUserConfig
from mindroom.config.models import DefaultsConfig
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.custom_tools.config_manager import ConfigManagerTools, _InfoType


def _minimal_config_path(tmp_path: Path) -> Path:
    """Write a minimal valid config file for ConfigManager tool tests."""
    config_path = tmp_path / "config.yaml"
    Config(models={"default": {"provider": "openai", "id": "gpt-4o"}}).save_to_yaml(config_path)
    return config_path


def _runtime_paths() -> RuntimePaths:
    return resolve_runtime_paths(config_path=Path("config.yaml"), process_env={})


def _config_manager(config_path: Path) -> ConfigManagerTools:
    """Construct ConfigManagerTools with explicit RuntimePaths."""
    return ConfigManagerTools(resolve_runtime_paths(config_path=config_path, process_env={}))


class TestConsolidatedConfigManager:
    """Test the consolidated ConfigManager with only 3 tools."""

    def test_init(self, tmp_path: Path) -> None:
        """Test ConfigManagerTools initialization."""
        cm = _config_manager(_minimal_config_path(tmp_path))
        assert cm.config_path is not None
        assert cm.name == "config_manager"
        # Should only have 3 tools now
        assert len(cm.tools) == 3
        assert any(tool.__name__ == "get_info" for tool in cm.tools)
        assert any(tool.__name__ == "manage_agent" for tool in cm.tools)
        assert any(tool.__name__ == "manage_team" for tool in cm.tools)

    def test_init_uses_explicit_config_path(self) -> None:
        """Initialization should preserve the explicitly provided config path."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_path = Path(f.name)
            config = Config(agents={})
            config.agents["test"] = AgentConfig(
                display_name="Test Agent",
                role="Test role",
                tools=["googlesearch"],
                model="default",
            )
            config.save_to_yaml(config_path)

        try:
            cm = _config_manager(config_path)

            assert cm.config_path == config_path.resolve()
            assert "Test Agent" in cm.get_info(info_type="agents")
        finally:
            config_path.unlink(missing_ok=True)

    def test_get_info_agents(self) -> None:
        """Test get_info with agents info type."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_path = Path(f.name)
            config = Config(agents={})
            config.agents["test"] = AgentConfig(
                display_name="Test Agent",
                role="Test role",
                tools=["googlesearch"],
                model="default",
            )
            config.save_to_yaml(config_path)

        try:
            cm = _config_manager(config_path)
            result = cm.get_info(info_type="agents")
            assert "Test Agent" in result
            assert "test" in result
            assert "googlesearch" in result
        finally:
            config_path.unlink(missing_ok=True)

    def test_get_info_teams(self) -> None:
        """Test get_info with teams info type."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_path = Path(f.name)
            config = Config(
                agents={
                    "agent1": AgentConfig(display_name="Agent One"),
                    "agent2": AgentConfig(display_name="Agent Two"),
                },
                teams={},
            )
            config.teams["test_team"] = TeamConfig(
                display_name="Test Team",
                role="Test team role",
                agents=["agent1", "agent2"],
                mode="coordinate",
            )
            config.save_to_yaml(config_path)

        try:
            cm = _config_manager(config_path)
            result = cm.get_info(info_type="teams")
            assert "Test Team" in result
            assert "test_team" in result
            assert "agent1" in result
            assert "agent2" in result
        finally:
            config_path.unlink(missing_ok=True)

    def test_get_info_available_tools(self, tmp_path: Path) -> None:
        """Test get_info with available_tools info type."""
        cm = _config_manager(_minimal_config_path(tmp_path))
        result = cm.get_info(info_type="available_tools")
        assert "Available Tools by Category" in result

    def test_get_info_tool_details(self, tmp_path: Path) -> None:
        """Test get_info with tool_details info type."""
        cm = _config_manager(_minimal_config_path(tmp_path))
        # Should require name parameter
        result = cm.get_info(info_type="tool_details")
        assert "Error" in result
        assert "requires 'name' parameter" in result

        # With valid tool name (using googlesearch which we know exists)
        result = cm.get_info(info_type="tool_details", name="googlesearch")
        assert "Tool: googlesearch" in result

    def test_get_info_tool_details_for_openclaw_compat(self, tmp_path: Path) -> None:
        """Tool details should describe openclaw_compat as a registered tool."""
        cm = _config_manager(_minimal_config_path(tmp_path))
        result = cm.get_info(info_type="tool_details", name="openclaw_compat")
        assert "Tool: openclaw_compat" in result
        assert "OpenClaw Compat" in result

    def test_get_info_invalid_type(self, tmp_path: Path) -> None:
        """Test get_info with invalid info type."""
        cm = _config_manager(_minimal_config_path(tmp_path))
        result = cm.get_info(info_type="invalid_type")
        assert "Error: Unknown info_type" in result
        assert "Valid options" in result

    def test_manage_agent_create(self) -> None:
        """Test manage_agent with create operation."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_path = Path(f.name)
            config = Config(agents={})
            config.save_to_yaml(config_path)

        try:
            cm = _config_manager(config_path)
            result = cm.manage_agent(
                operation="create",
                agent_name="test_agent",
                display_name="Test Agent",
                role="Test role",
                tools=[],
                model="default",
            )
            assert "Successfully created" in result
            assert "test_agent" in result

            # Verify agent was created
            config = Config.from_yaml(config_path)
            assert "test_agent" in config.agents
            assert config.agents["test_agent"].display_name == "Test Agent"
        finally:
            config_path.unlink(missing_ok=True)

    def test_manage_agent_create_accepts_openclaw_preset_tool(self) -> None:
        """Agent create should accept preset entries in tools."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_path = Path(f.name)
            config = Config(agents={})
            config.save_to_yaml(config_path)

        try:
            cm = _config_manager(config_path)
            result = cm.manage_agent(
                operation="create",
                agent_name="test_agent",
                display_name="Test Agent",
                role="Test role",
                tools=["openclaw_compat"],
            )
            assert "Successfully created" in result

            config = Config.from_yaml(config_path)
            assert config.agents["test_agent"].tools == ["openclaw_compat"]
            effective = config.get_agent_tools("test_agent")
            assert effective[0] == "openclaw_compat"
            assert "shell" in effective
            assert "matrix_message" in effective
        finally:
            config_path.unlink(missing_ok=True)

    def test_manage_agent_validate_accepts_openclaw_preset_tool(self) -> None:
        """Validate should not flag preset entries as invalid tools."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_path = Path(f.name)
            config = Config(
                agents={
                    "test_agent": AgentConfig(
                        display_name="Test Agent",
                        role="Test role",
                        tools=["openclaw_compat", "python"],
                    ),
                },
            )
            config.save_to_yaml(config_path)

        try:
            cm = _config_manager(config_path)
            result = cm.manage_agent(operation="validate", agent_name="test_agent")
            assert "Invalid tools" not in result
        finally:
            config_path.unlink(missing_ok=True)

    def test_manage_agent_create_rejects_unknown_knowledge_bases(self) -> None:
        """Create must fail when knowledge base IDs are not configured."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_path = Path(f.name)
            config = Config(
                agents={},
                knowledge_bases={
                    "docs": KnowledgeBaseConfig(path="./docs"),
                },
            )
            config.save_to_yaml(config_path)

        try:
            cm = _config_manager(config_path)
            result = cm.manage_agent(
                operation="create",
                agent_name="test_agent",
                display_name="Test Agent",
                role="Test role",
                knowledge_bases=["missing_docs"],
            )
            assert result == "Error: Unknown knowledge bases: missing_docs. Available knowledge bases: docs."

            config = Config.from_yaml(config_path)
            assert "test_agent" not in config.agents
        finally:
            config_path.unlink(missing_ok=True)

    def test_manage_agent_create_rejects_runtime_invalid_config(self) -> None:
        """Create must not persist configs that fail runtime-aware validation."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_path = Path(f.name)
            Config(
                agents={},
                mindroom_user=MindRoomUserConfig(username="mindroom_assistant"),
                models={"default": {"provider": "openai", "id": "gpt-4o"}},
            ).save_to_yaml(config_path)

        try:
            cm = _config_manager(config_path)
            result = cm.manage_agent(
                operation="create",
                agent_name="assistant",
                display_name="Assistant",
                role="Test role",
                tools=[],
                model="default",
            )

            assert "Error" in result
            assert "conflicts" in result
            config = Config.from_yaml(config_path)
            assert "assistant" not in config.agents
        finally:
            config_path.unlink(missing_ok=True)

    def test_manage_agent_create_rejects_duplicate_knowledge_bases(self) -> None:
        """Create must fail when duplicate knowledge base IDs are provided."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_path = Path(f.name)
            config = Config(
                agents={},
                knowledge_bases={
                    "docs": KnowledgeBaseConfig(path="./docs"),
                },
            )
            config.save_to_yaml(config_path)

        try:
            cm = _config_manager(config_path)
            result = cm.manage_agent(
                operation="create",
                agent_name="test_agent",
                display_name="Test Agent",
                role="Test role",
                knowledge_bases=["docs", "docs"],
            )
            assert result == "Error: Duplicate knowledge bases are not allowed: docs."

            config = Config.from_yaml(config_path)
            assert "test_agent" not in config.agents
        finally:
            config_path.unlink(missing_ok=True)

    def test_manage_agent_update(self) -> None:
        """Test manage_agent with update operation."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_path = Path(f.name)
            config = Config(agents={})
            config.agents["test_agent"] = AgentConfig(
                display_name="Old Name",
                role="Old role",
            )
            config.save_to_yaml(config_path)

        try:
            cm = _config_manager(config_path)
            result = cm.manage_agent(
                operation="update",
                agent_name="test_agent",
                display_name="New Name",
            )
            assert "Successfully updated" in result
            assert "Display Name -> New Name" in result

            # Verify agent was updated
            config = Config.from_yaml(config_path)
            assert config.agents["test_agent"].display_name == "New Name"
            assert config.agents["test_agent"].role == "Old role"  # Unchanged
        finally:
            config_path.unlink(missing_ok=True)

    def test_manage_agent_update_rejects_unknown_knowledge_bases(self) -> None:
        """Update must fail when setting unknown knowledge base IDs."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_path = Path(f.name)
            config = Config(
                agents={},
                knowledge_bases={
                    "docs": KnowledgeBaseConfig(path="./docs"),
                },
            )
            config.agents["test_agent"] = AgentConfig(
                display_name="Test Agent",
                role="Test role",
                knowledge_bases=["docs"],
            )
            config.save_to_yaml(config_path)

        try:
            cm = _config_manager(config_path)
            result = cm.manage_agent(
                operation="update",
                agent_name="test_agent",
                knowledge_bases=["missing_docs"],
            )
            assert result == "Error: Unknown knowledge bases: missing_docs. Available knowledge bases: docs."

            config = Config.from_yaml(config_path)
            assert config.agents["test_agent"].knowledge_bases == ["docs"]
        finally:
            config_path.unlink(missing_ok=True)

    def test_manage_agent_update_rejects_duplicate_knowledge_bases(self) -> None:
        """Update must fail when duplicate knowledge base IDs are provided."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_path = Path(f.name)
            config = Config(
                agents={},
                knowledge_bases={
                    "docs": KnowledgeBaseConfig(path="./docs"),
                },
            )
            config.agents["test_agent"] = AgentConfig(
                display_name="Test Agent",
                role="Test role",
                knowledge_bases=["docs"],
            )
            config.save_to_yaml(config_path)

        try:
            cm = _config_manager(config_path)
            result = cm.manage_agent(
                operation="update",
                agent_name="test_agent",
                knowledge_bases=["docs", "docs"],
            )
            assert result == "Error: Duplicate knowledge bases are not allowed: docs."

            config = Config.from_yaml(config_path)
            assert config.agents["test_agent"].knowledge_bases == ["docs"]
        finally:
            config_path.unlink(missing_ok=True)

    def test_manage_agent_learning_field(self) -> None:
        """Test manage_agent supports learning and learning_mode create and update."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_path = Path(f.name)
            Config(agents={}).save_to_yaml(config_path)

        try:
            cm = _config_manager(config_path)
            create_result = cm.manage_agent(
                operation="create",
                agent_name="learning_agent",
                display_name="Learning Agent",
                role="Learns from chats",
                learning=False,
                learning_mode="always",
            )
            assert "Successfully created" in create_result

            update_result = cm.manage_agent(
                operation="update",
                agent_name="learning_agent",
                learning=True,
                learning_mode="agentic",
            )
            assert "Successfully updated" in update_result
            assert "Learning -> True" in update_result
            assert "Learning Mode -> agentic" in update_result

            config = Config.from_yaml(config_path)
            assert config.agents["learning_agent"].learning is True
            assert config.agents["learning_agent"].learning_mode == "agentic"
        finally:
            config_path.unlink(missing_ok=True)

    def test_manage_agent_validate(self) -> None:
        """Test manage_agent with validate operation."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_path = Path(f.name)
            config = Config(agents={})
            config.agents["test_agent"] = AgentConfig(
                display_name="Test Agent",
                role="Test role",
            )
            config.save_to_yaml(config_path)

        try:
            cm = _config_manager(config_path)
            result = cm.manage_agent(
                operation="validate",
                agent_name="test_agent",
            )
            assert "Validation Results" in result
            assert "test_agent" in result
        finally:
            config_path.unlink(missing_ok=True)

    def test_manage_agent_invalid_operation(self, tmp_path: Path) -> None:
        """Test manage_agent with invalid operation."""
        cm = _config_manager(_minimal_config_path(tmp_path))
        result = cm.manage_agent(
            operation="invalid",
            agent_name="test",
        )
        assert "Error: Unknown operation" in result
        assert "Valid options: create, update, validate" in result

    def test_manage_agent_with_memory_tool(self) -> None:
        """Regression: memory tool must be accepted in create/update/validate."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_path = Path(f.name)
            Config(agents={}).save_to_yaml(config_path)

        try:
            cm = _config_manager(config_path)

            # Create accepts memory
            result = cm.manage_agent(
                operation="create",
                agent_name="mem_agent",
                display_name="Mem Agent",
                role="Remembers things",
                tools=["memory"],
                model="default",
            )
            assert "Successfully created" in result
            assert "Error" not in result

            # Update accepts memory alongside other tools
            result = cm.manage_agent(
                operation="update",
                agent_name="mem_agent",
                tools=["memory", "calculator"],
            )
            assert "Successfully updated" in result
            assert "Error" not in result

            # Validate does not flag memory as invalid
            result = cm.manage_agent(
                operation="validate",
                agent_name="mem_agent",
            )
            assert "Invalid tools" not in result
        finally:
            config_path.unlink(missing_ok=True)

    def test_manage_team(self) -> None:
        """Test manage_team tool."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_path = Path(f.name)
            config = Config(agents={}, teams={})
            # Add agents that the team will reference
            config.agents["agent1"] = AgentConfig(
                display_name="Agent 1",
                role="Role 1",
            )
            config.agents["agent2"] = AgentConfig(
                display_name="Agent 2",
                role="Role 2",
            )
            config.save_to_yaml(config_path)

        try:
            cm = _config_manager(config_path)
            result = cm.manage_team(
                team_name="test_team",
                display_name="Test Team",
                role="Test team role",
                agents=["agent1", "agent2"],
                mode="coordinate",
            )
            assert "Successfully created team" in result
            assert "test_team" in result

            # Verify team was created
            config = Config.from_yaml(config_path)
            assert "test_team" in config.teams
            assert config.teams["test_team"].display_name == "Test Team"
            assert config.teams["test_team"].agents == ["agent1", "agent2"]
        finally:
            config_path.unlink(missing_ok=True)

    def test_info_type_enum_values(self, tmp_path: Path) -> None:
        """Test that all InfoType enum values work."""
        cm = _config_manager(_minimal_config_path(tmp_path))

        # Test each enum value
        for info_type in _InfoType:
            # Some require name parameter
            if info_type in [_InfoType.TOOL_DETAILS, _InfoType.AGENT_CONFIG, _InfoType.AGENT_TEMPLATE]:
                result = cm.get_info(info_type=info_type.value)
                assert "requires 'name' parameter" in result
            else:
                result = cm.get_info(info_type=info_type.value)
                # Should not error for valid types without name
                assert "Error: Unknown info_type" not in result

    def test_reduced_tool_count(self, tmp_path: Path) -> None:
        """Verify we reduced from 15 tools to just 3."""
        cm = _config_manager(_minimal_config_path(tmp_path))

        # Should only have 3 tools registered
        assert len(cm.tools) == 3

        # Check the specific tools
        tool_names = [tool.__name__ for tool in cm.tools]
        assert "get_info" in tool_names
        assert "manage_agent" in tool_names
        assert "manage_team" in tool_names

        # Old tool names should NOT be present
        old_tools = [
            "get_mindroom_info",
            "get_config_schema",
            "get_available_models",
            "list_agents",
            "list_teams",
            "list_available_tools",
            "get_tool_details",
            "suggest_tools_for_task",
            "create_agent_config",
            "update_agent_config",
            "create_team_config",
            "validate_agent_config",
            "get_agent_config",
            "generate_agent_template",
        ]
        for old_tool in old_tools:
            assert old_tool not in tool_names

    def test_agent_template_generation(self, tmp_path: Path) -> None:
        """Test agent template generation through get_info."""
        cm = _config_manager(_minimal_config_path(tmp_path))

        # Test valid template type
        result = cm.get_info(info_type="agent_template", name="researcher")
        assert "Template for 'researcher' agent" in result
        assert "Research specialist" in result

        # Test invalid template type
        result = cm.get_info(info_type="agent_template", name="invalid_type")
        assert "Unknown template type" in result
        assert "Available templates" in result

    def test_config_schema_info(self, tmp_path: Path) -> None:
        """Test config schema retrieval."""
        cm = _config_manager(_minimal_config_path(tmp_path))
        result = cm.get_info(info_type="config_schema")
        assert "MindRoom Configuration Schema" in result
        assert "Agent Configuration Fields" in result
        assert "Team Configuration Fields" in result

    def test_available_models_info(self) -> None:
        """Test available models retrieval."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_path = Path(f.name)
            config = Config(
                models={
                    "default": {
                        "provider": "openai",
                        "id": "gpt-4",
                    },
                    "fast": {
                        "provider": "anthropic",
                        "id": "claude-3-haiku",
                    },
                },
            )
            config.save_to_yaml(config_path)

        try:
            cm = _config_manager(config_path)
            result = cm.get_info(info_type="available_models")
            assert "Available Models" in result
            assert "default" in result
            assert "openai" in result
            assert "gpt-4" in result
            assert "fast" in result
            assert "anthropic" in result
        finally:
            config_path.unlink(missing_ok=True)


class TestGetAgentWorkerTools:
    """Tests for Config.get_agent_worker_tools and get_agent_worker_scope."""

    def test_agent_worker_tools_override_takes_precedence(self) -> None:
        """Agent-level worker_tools should override defaults."""
        config = Config(
            defaults=DefaultsConfig(worker_tools=["shell", "file"]),
            agents={
                "code": AgentConfig(display_name="Code", worker_tools=["python"]),
            },
        )
        assert config.get_agent_worker_tools("code", _runtime_paths()) == ["python"]

    def test_worker_tools_fall_back_to_defaults(self) -> None:
        """When agent has no worker_tools, defaults should apply."""
        config = Config(
            defaults=DefaultsConfig(worker_tools=["shell", "file"]),
            agents={
                "code": AgentConfig(display_name="Code"),
            },
        )
        assert config.get_agent_worker_tools("code", _runtime_paths()) == ["shell", "file"]

    def test_worker_tools_use_default_policy_when_unset(self) -> None:
        """When worker_tools are omitted everywhere, the built-in worker routing policy applies."""
        config = Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    tools=["calculator", "shell", "coding"],
                    include_default_tools=False,
                ),
            },
        )
        assert config.get_agent_worker_tools("code", _runtime_paths()) == ["shell", "coding"]

    def test_worker_tools_default_policy_returns_empty_list_for_primary_only_tools(self) -> None:
        """Tools without a worker default should stay local when worker_tools are omitted."""
        config = Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    tools=["calculator", "scheduler"],
                    include_default_tools=False,
                ),
            },
        )
        assert config.get_agent_worker_tools("code", _runtime_paths()) == []

    def test_worker_tools_empty_list_disables_routing(self) -> None:
        """Empty worker_tools should explicitly disable worker routing for that agent."""
        config = Config(
            defaults=DefaultsConfig(worker_tools=["shell"]),
            agents={
                "research": AgentConfig(display_name="Research", worker_tools=[]),
            },
        )
        assert config.get_agent_worker_tools("research", _runtime_paths()) == []

    def test_defaults_empty_list_disables_worker_routing(self) -> None:
        """Empty default worker_tools should disable worker routing for inheriting agents."""
        config = Config(
            defaults=DefaultsConfig(worker_tools=[]),
            agents={
                "code": AgentConfig(display_name="Code"),
            },
        )
        assert config.get_agent_worker_tools("code", _runtime_paths()) == []

    def test_worker_tools_expand_implied_tools(self) -> None:
        """Worker tool resolution should preserve the normal preset expansion behavior."""
        config = Config(
            defaults=DefaultsConfig(worker_tools=["openclaw_compat", "python", "shell"]),
            agents={
                "code": AgentConfig(display_name="Code"),
            },
        )
        assert config.get_agent_worker_tools("code", _runtime_paths()) == [
            "openclaw_compat",
            "python",
            "shell",
            "coding",
            "duckduckgo",
            "website",
            "browser",
            "scheduler",
            "subagents",
            "matrix_message",
            "attachments",
        ]

    def test_worker_scope_prefers_agent_override(self) -> None:
        """Agent-level worker_scope should override defaults."""
        config = Config(
            defaults=DefaultsConfig(worker_scope="shared"),
            agents={
                "code": AgentConfig(display_name="Code", worker_scope="user_agent"),
            },
        )
        assert config.get_agent_worker_scope("code") == "user_agent"

    def test_worker_scope_falls_back_to_defaults(self) -> None:
        """Worker scope should inherit from defaults when agent config omits it."""
        config = Config(
            defaults=DefaultsConfig(worker_scope="user"),
            agents={
                "code": AgentConfig(display_name="Code"),
            },
        )
        assert config.get_agent_worker_scope("code") == "user"
