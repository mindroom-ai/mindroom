"""Tests for the SelfConfigTools toolkit."""

from __future__ import annotations

import json
import tempfile
from inspect import signature
from pathlib import Path
from unittest.mock import MagicMock, patch

from mindroom.agents import create_agent
from mindroom.api import config_lifecycle, main
from mindroom.config.agent import AgentConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.config.matrix import MatrixSpaceConfig
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.custom_tools.self_config import SelfConfigTools
from tests.conftest import load_config_yaml, write_config_yaml
from tests.identity_helpers import persist_entity_accounts

_DEFAULT_MODELS = {"default": ModelConfig(provider="openai", id="gpt-4o")}
_BOUND_RUNTIME_PATHS: dict[int, RuntimePaths] = {}


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
    config_dir = Path(tempfile.mkdtemp(prefix="mindroom-self-config-"))
    config_path = config_dir / "config.yaml"
    runtime_paths = resolve_runtime_paths(config_path=config_path)
    write_config_yaml(config, config_path)
    bound = Config.validate_with_runtime(config.authored_model_dump(), runtime_paths)
    persist_entity_accounts(bound, runtime_paths)
    _BOUND_RUNTIME_PATHS[id(bound)] = runtime_paths
    return bound, config_path


def _runtime_paths_for(config: Config, config_path: Path | None = None) -> RuntimePaths:
    runtime_paths = _BOUND_RUNTIME_PATHS.get(id(config))
    if runtime_paths is not None:
        return runtime_paths
    if config_path is None:
        msg = "Test config is missing bound RuntimePaths"
        raise KeyError(msg)
    return resolve_runtime_paths(config_path=config_path)


def _create_agent_for_test(agent_name: str, config: Config) -> object:
    """Create an agent with the explicit runtime bound to the test config."""
    return create_agent(agent_name, config=config, runtime_paths=_runtime_paths_for(config), execution_identity=None)


def _self_config_tools(agent_name: str, config_path: Path) -> SelfConfigTools:
    """Construct SelfConfigTools for one explicit config path."""
    return SelfConfigTools(agent_name=agent_name, runtime_paths=resolve_runtime_paths(config_path=config_path))


def _invalid_plugin_config_path(tmp_path: Path, *, with_agent: bool = True) -> Path:
    """Write one config whose plugin manifest fails runtime validation."""
    plugin_root = tmp_path / "plugins" / "bad-name"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "BadName", "tools_module": None, "skills": []}),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    write_config_yaml(
        Config(
            agents={"writer": AgentConfig(display_name="Writer", role="Write things")} if with_agent else {},
            models=_DEFAULT_MODELS,
            plugins=["./plugins/bad-name"],
        ),
        config_path,
    )
    return config_path


class TestGetOwnConfig:
    """Tests for SelfConfigTools.get_own_config."""

    def test_init_uses_explicit_config_path(self) -> None:
        """Initialization should preserve the explicitly provided config path."""
        _, config_path = _make_config(
            agents={"writer": AgentConfig(display_name="Writer", role="Write things")},
        )
        try:
            tool = _self_config_tools(agent_name="writer", config_path=config_path)

            assert tool.config_path == config_path.resolve()
            assert "Writer" in tool.get_own_config()
        finally:
            config_path.unlink(missing_ok=True)

    def test_get_own_config(self) -> None:
        """Agent should see its own config as YAML."""
        _, config_path = _make_config(
            agents={
                "writer": AgentConfig(display_name="Writer", role="Write things", tools=["googlesearch"]),
            },
        )
        try:
            tool = _self_config_tools(agent_name="writer", config_path=config_path)
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
            tool = _self_config_tools(agent_name="ghost", config_path=config_path)
            result = tool.get_own_config()
            assert "Error" in result
            assert "ghost" in result
        finally:
            config_path.unlink(missing_ok=True)

    def test_get_own_config_tolerates_invalid_plugin_manifest(self, tmp_path: Path) -> None:
        """Read-only self-config should keep working when runtime plugin loading degrades."""
        config_path = _invalid_plugin_config_path(tmp_path)
        tool = _self_config_tools(agent_name="writer", config_path=config_path)

        result = tool.get_own_config()

        assert "Configuration for 'writer'" in result
        assert "Writer" in result
        assert "Invalid configuration" not in result

    def test_get_own_config_returns_malformed_yaml_error(self, tmp_path: Path) -> None:
        """Malformed YAML should return one user-facing invalid-config message, not raise."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("agents:\n  bad: [\n", encoding="utf-8")
        tool = _self_config_tools(agent_name="writer", config_path=config_path)

        result = tool.get_own_config()

        assert "Invalid configuration" in result
        assert "Could not parse configuration YAML" in result

    def test_get_own_config_returns_missing_config_error(self, tmp_path: Path) -> None:
        """Missing config files should return one user-facing invalid-config message, not raise."""
        tool = _self_config_tools(agent_name="writer", config_path=tmp_path / "missing.yaml")

        result = tool.get_own_config()

        assert "Invalid configuration" in result
        assert "Could not load configuration" in result


class TestUpdateOwnConfig:
    """Tests for SelfConfigTools.update_own_config."""

    def test_update_role(self) -> None:
        """Updating the role should persist to YAML."""
        _, config_path = _make_config(
            agents={"coder": AgentConfig(display_name="Coder", role="Old role")},
        )
        try:
            tool = _self_config_tools(agent_name="coder", config_path=config_path)
            result = tool.update_own_config(role="New role")
            assert "Successfully" in result
            assert "Role" in result

            # Verify persisted
            reloaded = load_config_yaml(config_path)
            assert reloaded.agents["coder"].role == "New role"
        finally:
            config_path.unlink(missing_ok=True)

    def test_update_own_config_advances_registered_api_snapshot_generation(self) -> None:
        """Tool-side self-config writes should advance the in-process API generation immediately."""
        _, config_path = _make_config(
            agents={"coder": AgentConfig(display_name="Coder", role="Old role")},
        )
        try:
            runtime_paths = resolve_runtime_paths(config_path=config_path)
            main.initialize_api_app(main.app, runtime_paths)
            assert config_lifecycle.load_config_into_app(runtime_paths, main.app) is True
            initial_generation = main._app_context(main.app).generation

            tool = _self_config_tools(agent_name="coder", config_path=config_path)
            result = tool.update_own_config(role="New role")

            assert "Successfully" in result
            assert main._app_context(main.app).generation > initial_generation
            assert main._app_context(main.app).config_data["agents"]["coder"]["role"] == "New role"
        finally:
            config_path.unlink(missing_ok=True)

    def test_update_own_config_signature_exposes_only_safe_fields(self) -> None:
        """Self-config should not expose privileged config fields in its callable API."""
        exposed_fields = set(signature(SelfConfigTools.update_own_config).parameters) - {"self"}
        assert exposed_fields == {
            "compress_tool_results",
            "display_name",
            "instructions",
            "learning",
            "learning_mode",
            "markdown",
            "max_tool_calls_from_history",
            "num_history_messages",
            "num_history_runs",
            "role",
            "rooms",
            "show_tool_calls",
            "thread_mode",
        }

    def test_update_own_config_schema_excludes_privileged_fields(self) -> None:
        """Model-facing self-config schema should not advertise privileged config fields."""
        _, config_path = _make_config(
            agents={"coder": AgentConfig(display_name="Coder", role="Code", tools=["self_config"])},
            knowledge_bases={"docs": KnowledgeBaseConfig(path="./docs")},
            models={
                **_DEFAULT_MODELS,
                "expensive": ModelConfig(provider="openai", id="gpt-4o"),
            },
        )
        try:
            tool = _self_config_tools(agent_name="coder", config_path=config_path)
            function = tool.functions["update_own_config"]
            exposed_fields = set((function.parameters.get("properties") or {}).keys())

            assert not exposed_fields.intersection(
                {
                    "context_files",
                    "include_default_tools",
                    "knowledge_bases",
                    "model",
                    "skills",
                    "tools",
                },
            )
        finally:
            config_path.unlink(missing_ok=True)

    def test_update_own_config_returns_invalid_plugin_manifest_error(self, tmp_path: Path) -> None:
        """Write self-config should keep runtime plugin validation in the invalid-config channel."""
        config_path = _invalid_plugin_config_path(tmp_path)
        tool = _self_config_tools(agent_name="writer", config_path=config_path)

        result = tool.update_own_config(role="Updated role")

        assert "Invalid configuration" in result
        assert "Invalid plugin name" in result
        assert "Changes were NOT applied." in result

    def test_update_own_config_returns_malformed_yaml_error(self, tmp_path: Path) -> None:
        """Malformed YAML should be reported through the invalid-config path on writes too."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("agents:\n  bad: [\n", encoding="utf-8")
        tool = _self_config_tools(agent_name="writer", config_path=config_path)

        result = tool.update_own_config(role="Updated role")

        assert "Invalid configuration" in result
        assert "Could not parse configuration YAML" in result
        assert "Changes were NOT applied." in result

    def test_update_own_config_safe_write_preserves_privileged_fields(self) -> None:
        """Allowed self-config writes should leave privileged fields unchanged."""
        _, config_path = _make_config(
            agents={
                "coder": AgentConfig(
                    display_name="Coder",
                    role="Code",
                    tools=["self_config"],
                    include_default_tools=False,
                    knowledge_bases=["docs"],
                    skills=["write-shell-script"],
                    context_files=["notes.md"],
                ),
            },
            knowledge_bases={"docs": KnowledgeBaseConfig(path="./docs")},
            models={
                **_DEFAULT_MODELS,
                "expensive": ModelConfig(provider="openai", id="gpt-4o"),
            },
        )
        try:
            tool = _self_config_tools(agent_name="coder", config_path=config_path)
            result = tool.update_own_config(role="Safer role")
            assert "Successfully" in result

            reloaded = load_config_yaml(config_path)
            assert reloaded.agents["coder"].model == "default"
            assert reloaded.agents["coder"].skills == ["write-shell-script"]
            assert reloaded.agents["coder"].context_files == ["notes.md"]
            assert reloaded.agents["coder"].knowledge_bases == ["docs"]
            assert reloaded.agents["coder"].tool_names == ["self_config"]
            assert reloaded.agents["coder"].include_default_tools is False
        finally:
            config_path.unlink(missing_ok=True)

    def test_update_multiple_fields(self) -> None:
        """Multiple fields can be updated at once."""
        _, config_path = _make_config(
            agents={"coder": AgentConfig(display_name="Coder", role="Code")},
        )
        try:
            tool = _self_config_tools(agent_name="coder", config_path=config_path)
            result = tool.update_own_config(
                display_name="Super Coder",
                role="Write awesome code",
                markdown=False,
            )
            assert "Successfully" in result

            reloaded = load_config_yaml(config_path)
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
            tool = _self_config_tools(agent_name="coder", config_path=config_path)
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
            tool = _self_config_tools(agent_name="coder", config_path=config_path)
            result = tool.update_own_config(thread_mode="invalid")
            assert "Error validating configuration" in result

            reloaded = load_config_yaml(config_path)
            assert reloaded.agents["coder"].thread_mode == "thread"
        finally:
            config_path.unlink(missing_ok=True)

    def test_update_rejects_runtime_invalid_rooms(self) -> None:
        """Runtime-sensitive validation errors should block persistence."""
        config = Config(
            agents={"coder": AgentConfig(display_name="Coder", role="Code", rooms=["lobby"])},
            matrix_space=MatrixSpaceConfig(enabled=True),
            models=_DEFAULT_MODELS,
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
            config_path = Path(tmp.name)
        write_config_yaml(config, config_path)

        try:
            tool = _self_config_tools(agent_name="coder", config_path=config_path)
            result = tool.update_own_config(rooms=["_mindroom_root_space"])
            assert "Invalid configuration" in result
            assert "reserved root Space alias" in result
            assert "Changes were NOT applied." in result

            reloaded = load_config_yaml(config_path)
            assert reloaded.agents["coder"].rooms == ["lobby"]
        finally:
            config_path.unlink(missing_ok=True)

    def test_update_rejects_mutually_exclusive_history_fields(self) -> None:
        """Both history knobs at once should be rejected and not persisted."""
        _, config_path = _make_config(
            agents={"coder": AgentConfig(display_name="Coder", role="Code")},
        )
        try:
            tool = _self_config_tools(agent_name="coder", config_path=config_path)
            result = tool.update_own_config(num_history_runs=2, num_history_messages=10)
            assert "Error validating configuration" in result

            reloaded = load_config_yaml(config_path)
            assert reloaded.agents["coder"].num_history_runs is None
            assert reloaded.agents["coder"].num_history_messages is None
        finally:
            config_path.unlink(missing_ok=True)

    def test_update_agent_not_found(self) -> None:
        """Updating a nonexistent agent should return an error."""
        _, config_path = _make_config(agents={})
        try:
            tool = _self_config_tools(agent_name="ghost", config_path=config_path)
            result = tool.update_own_config(role="New role")
            assert "Error" in result
            assert "ghost" in result
        finally:
            config_path.unlink(missing_ok=True)


class TestAgentCreationInjection:
    """Tests for allow_self_config injection in create_agent."""

    @patch("mindroom.agent_storage.SqliteDb")
    def test_allow_self_config_true_injects_tool(self, _mock_storage: MagicMock) -> None:  # noqa: PT019
        """Agent with allow_self_config=True should have self_config tool."""
        config, _ = _make_config(
            agents={"writer": AgentConfig(display_name="Writer", role="Write", allow_self_config=True)},
        )
        agent = _create_agent_for_test("writer", config=config)
        tool_names = [t.name for t in agent.tools]
        assert "self_config" in tool_names

    @patch("mindroom.agent_storage.SqliteDb")
    def test_allow_self_config_false_no_tool(self, _mock_storage: MagicMock) -> None:  # noqa: PT019
        """Agent with allow_self_config=False should not have self_config tool."""
        config, _ = _make_config(
            agents={"writer": AgentConfig(display_name="Writer", role="Write", allow_self_config=False)},
        )
        agent = _create_agent_for_test("writer", config=config)
        tool_names = [t.name for t in agent.tools]
        assert "self_config" not in tool_names

    @patch("mindroom.agent_storage.SqliteDb")
    def test_defaults_fallback_true(self, _mock_storage: MagicMock) -> None:  # noqa: PT019
        """When agent omits allow_self_config, defaults.allow_self_config=True should inject."""
        config, _ = _make_config(
            agents={"writer": AgentConfig(display_name="Writer", role="Write")},
            defaults=DefaultsConfig(allow_self_config=True),
        )
        agent = _create_agent_for_test("writer", config=config)
        tool_names = [t.name for t in agent.tools]
        assert "self_config" in tool_names

    @patch("mindroom.agent_storage.SqliteDb")
    def test_defaults_fallback_false(self, _mock_storage: MagicMock) -> None:  # noqa: PT019
        """When agent omits allow_self_config, defaults.allow_self_config=False should not inject."""
        config, _ = _make_config(
            agents={"writer": AgentConfig(display_name="Writer", role="Write")},
            defaults=DefaultsConfig(allow_self_config=False),
        )
        agent = _create_agent_for_test("writer", config=config)
        tool_names = [t.name for t in agent.tools]
        assert "self_config" not in tool_names

    @patch("mindroom.agent_storage.SqliteDb")
    def test_agent_override_beats_default(self, _mock_storage: MagicMock) -> None:  # noqa: PT019
        """Agent-level allow_self_config should override default."""
        config, _ = _make_config(
            agents={"writer": AgentConfig(display_name="Writer", role="Write", allow_self_config=False)},
            defaults=DefaultsConfig(allow_self_config=True),
        )
        agent = _create_agent_for_test("writer", config=config)
        tool_names = [t.name for t in agent.tools]
        assert "self_config" not in tool_names

    @patch("mindroom.agent_storage.SqliteDb")
    def test_manual_self_config_tool_loads(self, _mock_storage: MagicMock) -> None:  # noqa: PT019
        """Explicitly configured self_config tool should be loadable."""
        config, _ = _make_config(
            agents={
                "writer": AgentConfig(
                    display_name="Writer",
                    role="Write",
                    allow_self_config=False,
                    tools=["self_config"],
                ),
            },
        )
        agent = _create_agent_for_test("writer", config=config)
        tool_names = [t.name for t in agent.tools]
        assert "self_config" in tool_names

    @patch("mindroom.agent_storage.SqliteDb")
    def test_self_config_not_duplicated_when_manual_and_auto(self, _mock_storage: MagicMock) -> None:  # noqa: PT019
        """Manual self_config plus allow_self_config should still produce one tool instance."""
        config, _ = _make_config(
            agents={
                "writer": AgentConfig(
                    display_name="Writer",
                    role="Write",
                    allow_self_config=True,
                    tools=["self_config"],
                ),
            },
        )
        agent = _create_agent_for_test("writer", config=config)
        tool_names = [t.name for t in agent.tools]
        assert tool_names.count("self_config") == 1

    @patch("mindroom.agent_storage.SqliteDb")
    def test_config_path_threaded_to_self_config_auto(self, _mock_storage: MagicMock) -> None:  # noqa: PT019
        """Auto-injected self_config tool should use the config_path from create_agent."""
        config, config_path = _make_config(
            agents={"writer": AgentConfig(display_name="Writer", role="Write", allow_self_config=True)},
        )
        try:
            agent = _create_agent_for_test(
                "writer",
                config=config,
            )
            self_config_tool = next(t for t in agent.tools if getattr(t, "name", None) == "self_config")
            assert self_config_tool.config_path == config_path.resolve()

            # The tool should be able to read this agent's config from the temp file
            result = self_config_tool.get_own_config()
            assert "Writer" in result
            assert "Error" not in result
        finally:
            config_path.unlink(missing_ok=True)

    @patch("mindroom.agent_storage.SqliteDb")
    def test_config_path_threaded_to_self_config_manual(self, _mock_storage: MagicMock) -> None:  # noqa: PT019
        """Manually listed self_config tool should use the config_path from create_agent."""
        config, config_path = _make_config(
            agents={"writer": AgentConfig(display_name="Writer", role="Write", tools=["self_config"])},
        )
        try:
            agent = _create_agent_for_test(
                "writer",
                config=config,
            )
            self_config_tool = next(t for t in agent.tools if getattr(t, "name", None) == "self_config")
            assert self_config_tool.config_path == config_path.resolve()

            result = self_config_tool.get_own_config()
            assert "Writer" in result
            assert "Error" not in result
        finally:
            config_path.unlink(missing_ok=True)

    @patch("mindroom.agent_storage.SqliteDb")
    def test_config_path_threaded_to_config_manager(self, _mock_storage: MagicMock) -> None:  # noqa: PT019
        """Generic tool loading should thread config_path into config_manager as well."""
        config, config_path = _make_config(
            agents={"writer": AgentConfig(display_name="Writer", role="Write", tools=["config_manager"])},
        )
        try:
            agent = _create_agent_for_test(
                "writer",
                config=config,
            )
            config_manager_tool = next(t for t in agent.tools if getattr(t, "name", None) == "config_manager")
            assert config_manager_tool.config_path == config_path.resolve()

            result = config_manager_tool.get_info(info_type="agents")
            assert "Writer" in result
            assert "Error" not in result
        finally:
            config_path.unlink(missing_ok=True)
