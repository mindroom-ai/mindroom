"""Test the consolidated ConfigManager tool with fewer methods."""

from __future__ import annotations

import tempfile
from pathlib import Path

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.custom_tools.config_manager import ConfigManagerTools, InfoType


class TestConsolidatedConfigManager:
    """Test the consolidated ConfigManager with only 3 tools."""

    def test_init(self) -> None:
        """Test ConfigManagerTools initialization."""
        cm = ConfigManagerTools()
        assert cm.config_path is not None
        assert cm.name == "config_manager"
        # Should only have 3 tools now
        assert len(cm.tools) == 3
        assert any(tool.__name__ == "get_info" for tool in cm.tools)
        assert any(tool.__name__ == "manage_agent" for tool in cm.tools)
        assert any(tool.__name__ == "manage_team" for tool in cm.tools)

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
            cm = ConfigManagerTools(config_path)
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
            config = Config(teams={})
            config.teams["test_team"] = TeamConfig(
                display_name="Test Team",
                role="Test team role",
                agents=["agent1", "agent2"],
                mode="coordinate",
            )
            config.save_to_yaml(config_path)

        try:
            cm = ConfigManagerTools(config_path)
            result = cm.get_info(info_type="teams")
            assert "Test Team" in result
            assert "test_team" in result
            assert "agent1" in result
            assert "agent2" in result
        finally:
            config_path.unlink(missing_ok=True)

    def test_get_info_available_tools(self) -> None:
        """Test get_info with available_tools info type."""
        cm = ConfigManagerTools()
        result = cm.get_info(info_type="available_tools")
        assert "Available Tools by Category" in result

    def test_get_info_tool_details(self) -> None:
        """Test get_info with tool_details info type."""
        cm = ConfigManagerTools()
        # Should require name parameter
        result = cm.get_info(info_type="tool_details")
        assert "Error" in result
        assert "requires 'name' parameter" in result

        # With valid tool name (using googlesearch which we know exists)
        result = cm.get_info(info_type="tool_details", name="googlesearch")
        assert "Tool: googlesearch" in result

    def test_get_info_tool_details_for_openclaw_preset(self) -> None:
        """Tool details should describe config-only tool presets."""
        cm = ConfigManagerTools()
        result = cm.get_info(info_type="tool_details", name="openclaw_compat")
        assert "Tool Preset: openclaw_compat" in result
        assert "Expands To" in result
        assert "shell, coding, duckduckgo, website, browser, scheduler" in result

    def test_get_info_invalid_type(self) -> None:
        """Test get_info with invalid info type."""
        cm = ConfigManagerTools()
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
            cm = ConfigManagerTools(config_path)
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
            cm = ConfigManagerTools(config_path)
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
            assert config.get_agent_tools("test_agent")[:6] == [
                "shell",
                "coding",
                "duckduckgo",
                "website",
                "browser",
                "scheduler",
            ]
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
            cm = ConfigManagerTools(config_path)
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
            cm = ConfigManagerTools(config_path)
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
            cm = ConfigManagerTools(config_path)
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
            cm = ConfigManagerTools(config_path)
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
            cm = ConfigManagerTools(config_path)
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
            cm = ConfigManagerTools(config_path)
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
            cm = ConfigManagerTools(config_path)
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
            cm = ConfigManagerTools(config_path)
            result = cm.manage_agent(
                operation="validate",
                agent_name="test_agent",
            )
            assert "Validation Results" in result
            assert "test_agent" in result
        finally:
            config_path.unlink(missing_ok=True)

    def test_manage_agent_invalid_operation(self) -> None:
        """Test manage_agent with invalid operation."""
        cm = ConfigManagerTools()
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
            cm = ConfigManagerTools(config_path)

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
            cm = ConfigManagerTools(config_path)
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

    def test_info_type_enum_values(self) -> None:
        """Test that all InfoType enum values work."""
        cm = ConfigManagerTools()

        # Test each enum value
        for info_type in InfoType:
            # Some require name parameter
            if info_type in [InfoType.TOOL_DETAILS, InfoType.AGENT_CONFIG, InfoType.AGENT_TEMPLATE]:
                result = cm.get_info(info_type=info_type.value)
                assert "requires 'name' parameter" in result
            else:
                result = cm.get_info(info_type=info_type.value)
                # Should not error for valid types without name
                assert "Error: Unknown info_type" not in result

    def test_reduced_tool_count(self) -> None:
        """Verify we reduced from 15 tools to just 3."""
        cm = ConfigManagerTools()

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

    def test_agent_template_generation(self) -> None:
        """Test agent template generation through get_info."""
        cm = ConfigManagerTools()

        # Test valid template type
        result = cm.get_info(info_type="agent_template", name="researcher")
        assert "Template for 'researcher' agent" in result
        assert "Research specialist" in result

        # Test invalid template type
        result = cm.get_info(info_type="agent_template", name="invalid_type")
        assert "Unknown template type" in result
        assert "Available templates" in result

    def test_config_schema_info(self) -> None:
        """Test config schema retrieval."""
        cm = ConfigManagerTools()
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
            cm = ConfigManagerTools(config_path)
            result = cm.get_info(info_type="available_models")
            assert "Available Models" in result
            assert "default" in result
            assert "openai" in result
            assert "gpt-4" in result
            assert "fast" in result
            assert "anthropic" in result
        finally:
            config_path.unlink(missing_ok=True)


class TestGetAgentSandboxTools:
    """Tests for Config.get_agent_sandbox_tools resolution."""

    def test_agent_override_takes_precedence(self) -> None:
        """Agent-level sandbox_tools should override defaults."""
        config = Config(
            defaults=DefaultsConfig(sandbox_tools=["shell", "file"]),
            agents={
                "code": AgentConfig(display_name="Code", sandbox_tools=["python"]),
            },
        )
        assert config.get_agent_sandbox_tools("code") == ["python"]

    def test_falls_back_to_defaults(self) -> None:
        """When agent has no sandbox_tools, should fall back to defaults."""
        config = Config(
            defaults=DefaultsConfig(sandbox_tools=["shell", "file"]),
            agents={
                "code": AgentConfig(display_name="Code"),
            },
        )
        assert config.get_agent_sandbox_tools("code") == ["shell", "file"]

    def test_both_none_returns_none(self) -> None:
        """When neither agent nor defaults set sandbox_tools, returns None."""
        config = Config(
            agents={
                "code": AgentConfig(display_name="Code"),
            },
        )
        assert config.get_agent_sandbox_tools("code") is None

    def test_agent_empty_list_disables(self) -> None:
        """Agent can explicitly disable sandboxing with empty list."""
        config = Config(
            defaults=DefaultsConfig(sandbox_tools=["shell"]),
            agents={
                "research": AgentConfig(display_name="Research", sandbox_tools=[]),
            },
        )
        assert config.get_agent_sandbox_tools("research") == []

    def test_defaults_empty_list_disables(self) -> None:
        """Defaults can explicitly disable sandboxing with empty list."""
        config = Config(
            defaults=DefaultsConfig(sandbox_tools=[]),
            agents={
                "code": AgentConfig(display_name="Code"),
            },
        )
        assert config.get_agent_sandbox_tools("code") == []

    def test_agent_override_expands_sandbox_tool_presets(self) -> None:
        """Agent-level sandbox presets should expand and dedupe in order."""
        config = Config(
            defaults=DefaultsConfig(sandbox_tools=["file"]),
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    sandbox_tools=["openclaw_compat", "python", "shell"],
                ),
            },
        )

        assert config.get_agent_sandbox_tools("code") == [
            "shell",
            "coding",
            "duckduckgo",
            "website",
            "browser",
            "scheduler",
            "python",
        ]

    def test_defaults_expand_sandbox_tool_presets(self) -> None:
        """Default sandbox presets should expand for agents that inherit defaults."""
        config = Config(
            defaults=DefaultsConfig(sandbox_tools=["openclaw_compat", "python", "shell"]),
            agents={
                "code": AgentConfig(display_name="Code"),
            },
        )

        assert config.get_agent_sandbox_tools("code") == [
            "shell",
            "coding",
            "duckduckgo",
            "website",
            "browser",
            "scheduler",
            "python",
        ]
