"""Tests for CLI config subcommands, run-command error handling, and doctor."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import typer
import yaml
from anthropic import PermissionDeniedError
from google.auth.exceptions import DefaultCredentialsError
from typer.testing import CliRunner

import mindroom.constants as constants_module
from mindroom.agents import ensure_default_agent_workspaces
from mindroom.cli.config import _activate_cli_runtime
from mindroom.cli.main import _load_active_config_or_exit, app
from mindroom.config.main import Config
from mindroom.constants import OWNER_MATRIX_USER_ID_PLACEHOLDER
from mindroom.error_handling import AvatarGenerationError, AvatarSyncError
from mindroom.matrix.state import MatrixState
from mindroom.response_tracker import ResponseTracker

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _unwrap_console_output(text: str) -> str:
    return _strip_ansi(text).replace("\n", "")


@pytest.fixture(autouse=True)
def _clear_runtime_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDROOM_CONFIG_PATH", raising=False)
    monkeypatch.delenv("MINDROOM_STORAGE_PATH", raising=False)


def _runtime_path_env(config_path: Path, *, storage_path: Path | None = None) -> dict[str, str]:
    env = {"MINDROOM_CONFIG_PATH": str(config_path.resolve())}
    if storage_path is not None:
        env["MINDROOM_STORAGE_PATH"] = str(storage_path.resolve())
    return env


def _invoke_with_runtime(
    args: list[str],
    config_path: Path,
    *,
    storage_path: Path | None = None,
    env: dict[str, str] | None = None,
    **kwargs: object,
) -> object:
    command_env = _runtime_path_env(config_path, storage_path=storage_path)
    if env:
        command_env.update(env)
    return cast("object", runner.invoke(app, args, env=command_env, **kwargs))


def test_activate_cli_runtime_explicit_path_keeps_exported_storage_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit CLI config paths should still honor exported MINDROOM_STORAGE_PATH."""
    config_path = tmp_path / "config.yaml"
    storage_path = tmp_path / "custom-storage"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_path))

    runtime_paths = _activate_cli_runtime(config_path)

    assert runtime_paths.storage_root == storage_path.resolve()


# ---------------------------------------------------------------------------
# mindroom config init
# ---------------------------------------------------------------------------


class TestConfigInit:
    """Tests for `mindroom config init`."""

    def test_init_creates_config(self, tmp_path: Path) -> None:
        """Config init creates a valid config.yaml at the target path."""
        target = tmp_path / "config.yaml"
        result = runner.invoke(app, ["config", "init", "--path", str(target)], input="openai\n")
        assert result.exit_code == 0
        assert target.exists()
        content = target.read_text()
        assert "agents:" in content
        assert "models:" in content
        assert "authorization:" in content
        assert "matrix_space:" in content
        assert "matrix_space:\n  enabled: true\n  name: MindRoom" in content
        assert OWNER_MATRIX_USER_ID_PLACEHOLDER in content

    def test_init_full_profile_adds_mindroom_style_mind(self, tmp_path: Path) -> None:
        """Full template should include MindRoom-style Mind memory/context setup."""
        target = tmp_path / "config.yaml"
        result = runner.invoke(app, ["config", "init", "--path", str(target), "--provider", "openai"])
        assert result.exit_code == 0
        config = yaml.safe_load(target.read_text())
        mind = config["agents"]["mind"]

        assert mind["display_name"] == "Mind"
        assert mind["include_default_tools"] is False
        assert mind["learning"] is False
        assert mind["memory_backend"] == "file"
        assert mind["memory_file_path"] == "mind_data"
        assert mind["rooms"] == ["personal"]
        assert mind["context_files"] == [
            "mind_data/SOUL.md",
            "mind_data/AGENTS.md",
            "mind_data/USER.md",
            "mind_data/IDENTITY.md",
            "mind_data/TOOLS.md",
            "mind_data/HEARTBEAT.md",
        ]
        assert mind["knowledge_bases"] == ["mind_memory"]
        assert mind["tools"] == [
            "shell",
            "coding",
            "duckduckgo",
            "website",
            "browser",
            "scheduler",
            "subagents",
            "matrix_message",
        ]
        assert mind["skills"] == ["mindroom-docs"]
        assert config["knowledge_bases"]["mind_memory"]["path"] == (
            "${MINDROOM_STORAGE_PATH}/agents/mind/workspace/mind_data/memory"
        )
        assert config["knowledge_bases"]["mind_memory"]["watch"] is True
        assert config["memory"]["backend"] == "file"
        assert config["memory"]["embedder"]["provider"] == "sentence_transformers"
        assert config["memory"]["embedder"]["config"]["model"] == "sentence-transformers/all-MiniLM-L6-v2"
        assert config["memory"]["file"]["max_entrypoint_lines"] == 200
        assert config["memory"]["auto_flush"]["enabled"] is True
        assert "openclaw_compat" not in target.read_text()

        env_content = (tmp_path / ".env").read_text()
        assert f"MINDROOM_STORAGE_PATH={(tmp_path / 'mindroom_data').resolve()}" in env_content

    def test_init_full_profile_creates_mind_workspace_files(self, tmp_path: Path) -> None:
        """Full template should scaffold the required mind_data files."""
        target = tmp_path / "config.yaml"
        result = runner.invoke(app, ["config", "init", "--path", str(target), "--provider", "openai"])
        assert result.exit_code == 0

        workspace = tmp_path / "mindroom_data" / "agents" / "mind" / "workspace" / "mind_data"
        assert workspace.exists()
        assert (workspace / "memory").exists()
        assert (workspace / "SOUL.md").exists()
        assert (workspace / "AGENTS.md").exists()
        assert (workspace / "USER.md").exists()
        assert (workspace / "IDENTITY.md").exists()
        assert (workspace / "TOOLS.md").exists()
        assert (workspace / "HEARTBEAT.md").exists()
        assert (workspace / "MEMORY.md").exists()
        assert not (workspace / "BOOT.md").exists()

    def test_init_full_profile_respects_storage_path_override(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Full template should scaffold the Mind workspace under MINDROOM_STORAGE_PATH when set."""
        target = tmp_path / "config.yaml"
        storage_root = tmp_path / "custom-storage"
        monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))

        result = runner.invoke(app, ["config", "init", "--path", str(target), "--provider", "openai"])
        assert result.exit_code == 0

        config = yaml.safe_load(target.read_text())
        workspace = storage_root / "agents" / "mind" / "workspace" / "mind_data"
        assert workspace.exists()
        assert (workspace / "memory").exists()
        assert (workspace / "SOUL.md").exists()
        assert (workspace / "MEMORY.md").exists()
        assert config["knowledge_bases"]["mind_memory"]["path"] == (
            "${MINDROOM_STORAGE_PATH}/agents/mind/workspace/mind_data/memory"
        )

        env_content = (tmp_path / ".env").read_text()
        assert f"MINDROOM_STORAGE_PATH={storage_root.resolve()}" in env_content

    def test_init_full_profile_runtime_storage_override_keeps_mind_workspace_and_kb_in_sync(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The starter Mind profile should follow the active runtime storage root, not the init-time one."""
        target = tmp_path / "config.yaml"
        result = runner.invoke(app, ["config", "init", "--path", str(target), "--provider", "openai"])
        assert result.exit_code == 0

        runtime_storage = tmp_path / "alternate-storage"
        monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(runtime_storage))

        config = Config.from_yaml(target)
        runtime_paths = constants_module.resolve_runtime_paths(config_path=target)
        resolved_knowledge_path = constants_module.resolve_config_relative_path(
            config.knowledge_bases["mind_memory"].path,
            runtime_paths,
        )
        assert (
            resolved_knowledge_path
            == runtime_storage.resolve() / "agents" / "mind" / "workspace" / "mind_data" / "memory"
        )

        ensure_default_agent_workspaces(config, runtime_storage)
        runtime_workspace = runtime_storage / "agents" / "mind" / "workspace" / "mind_data"
        assert (runtime_workspace / "SOUL.md").exists()
        assert (runtime_workspace / "AGENTS.md").exists()
        assert (runtime_workspace / "USER.md").exists()
        assert (runtime_workspace / "IDENTITY.md").exists()
        assert (runtime_workspace / "TOOLS.md").exists()
        assert (runtime_workspace / "HEARTBEAT.md").exists()
        assert (runtime_workspace / "MEMORY.md").exists()

    def test_init_without_path_uses_detected_default_location(
        self,
        tmp_path: Path,
    ) -> None:
        """Config init without --path should write to the detected config location."""
        default_cfg = tmp_path / ".mindroom" / "config.yaml"
        result = runner.invoke(
            app,
            ["config", "init"],
            env={"MINDROOM_CONFIG_PATH": str(default_cfg)},
            input="openai\n",
        )

        assert result.exit_code == 0
        assert default_cfg.exists()
        assert (default_cfg.parent / ".env").exists()

    def test_init_preserved_env_without_storage_path_uses_literal_storage_root(
        self,
        tmp_path: Path,
    ) -> None:
        """Preserving an env file without MINDROOM_STORAGE_PATH must not emit a broken placeholder path."""
        target = tmp_path / "config.yaml"
        env_path = tmp_path / ".env"
        env_path.write_text("ANTHROPIC_API_KEY=sk-existing\n", encoding="utf-8")

        result = runner.invoke(
            app,
            ["config", "init", "--path", str(target), "--provider", "openai"],
            input="n\n",
        )

        assert result.exit_code == 0
        config = yaml.safe_load(target.read_text())
        assert config["knowledge_bases"]["mind_memory"]["path"] == (
            "./mindroom_data/agents/mind/workspace/mind_data/memory"
        )
        assert "${MINDROOM_STORAGE_PATH}" not in target.read_text()
        assert (tmp_path / "mindroom_data" / "agents" / "mind" / "workspace" / "mind_data").exists()
        assert env_path.read_text() == "ANTHROPIC_API_KEY=sk-existing\n"

    def test_init_minimal(self, tmp_path: Path) -> None:
        """Config init --minimal creates a bare-minimum config."""
        target = tmp_path / "config.yaml"
        result = runner.invoke(app, ["config", "init", "--path", str(target), "--minimal"])
        assert result.exit_code == 0
        content = target.read_text()
        assert "agents:" in content
        assert "# MindRoom Configuration (minimal)" in content
        assert "authorization:" in content
        assert "matrix_space:" in content
        assert "matrix_space:\n  enabled: true\n  name: MindRoom" in content
        assert OWNER_MATRIX_USER_ID_PLACEHOLDER in content

    def test_init_profile_minimal(self, tmp_path: Path) -> None:
        """Config init --profile minimal creates the minimal template."""
        target = tmp_path / "config.yaml"
        result = runner.invoke(app, ["config", "init", "--path", str(target), "--profile", "minimal"])
        assert result.exit_code == 0
        content = target.read_text()
        assert "# MindRoom Configuration (minimal)" in content

    def test_init_profile_public_writes_public_matrix_defaults(self, tmp_path: Path) -> None:
        """Public profile should prefill hosted Matrix defaults and token placeholder."""
        target = tmp_path / "config.yaml"
        result = runner.invoke(app, ["config", "init", "--path", str(target), "--profile", "public"])
        assert result.exit_code == 0
        config_content = target.read_text()
        assert "mindroom_user:" not in config_content

        env_content = (tmp_path / ".env").read_text()
        assert "MATRIX_HOMESERVER=https://mindroom.chat" in env_content
        assert "MATRIX_SERVER_NAME=mindroom.chat" in env_content
        assert "MINDROOM_PROVISIONING_URL=https://mindroom.chat" in env_content
        assert "MATRIX_REGISTRATION_TOKEN=" in env_content
        assert "\n\n\n# AI provider API keys" not in env_content

        # Public profile next steps must include the pairing command
        output = _strip_ansi(result.output)
        assert "mindroom connect --pair-code" in output

    def test_init_profile_public_vertexai_anthropic_writes_hosted_vertex_defaults(self, tmp_path: Path) -> None:
        """Hosted Vertex profile should use Vertex Claude defaults and hosted Matrix settings."""
        target = tmp_path / "config.yaml"
        result = runner.invoke(
            app,
            ["config", "init", "--path", str(target), "--profile", "public-vertexai-anthropic"],
        )
        assert result.exit_code == 0

        config = yaml.safe_load(target.read_text())
        assert "mindroom_user" not in config
        assert config["models"]["default"]["provider"] == "vertexai_claude"
        assert config["models"]["default"]["id"] == "claude-sonnet-4-6"

        env_content = (tmp_path / ".env").read_text()
        assert "MATRIX_HOMESERVER=https://mindroom.chat" in env_content
        assert "ANTHROPIC_VERTEX_PROJECT_ID=your-gcp-project-id" in env_content
        assert "CLOUD_ML_REGION=us-central1" in env_content
        assert "gcloud auth application-default login" in env_content
        assert "\nOPENAI_API_KEY=" not in env_content
        assert "\nOPENROUTER_API_KEY=" not in env_content

        output = _strip_ansi(result.output)
        assert "mindroom connect --pair-code" in output
        assert "Vertex AI project/region and Google auth" in output

    def test_init_full_profile_omits_pairing_step(self, tmp_path: Path) -> None:
        """Full profile next steps should NOT mention pairing."""
        target = tmp_path / "config.yaml"
        result = runner.invoke(app, ["config", "init", "--path", str(target), "--provider", "openai"])
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "mindroom connect" not in output

    def test_init_creates_env_with_dashboard_key(self, tmp_path: Path) -> None:
        """Config init writes a random MINDROOM_API_KEY to .env."""
        target = tmp_path / "config.yaml"
        result = runner.invoke(app, ["config", "init", "--path", str(target)], input="openai\n")
        assert result.exit_code == 0

        env_path = tmp_path / ".env"
        assert env_path.exists()

        content = env_path.read_text()
        backend_match = re.search(r"^MINDROOM_API_KEY=(.+)$", content, flags=re.MULTILINE)
        assert backend_match is not None
        assert backend_match.group(1)
        # VITE_API_KEY should NOT be in the template (auth is handled at proxy layer)
        assert "VITE_API_KEY" not in content

    def test_init_force_overwrites_existing_env(self, tmp_path: Path) -> None:
        """Config init --force should overwrite an existing .env file."""
        target = tmp_path / "config.yaml"
        env_path = tmp_path / ".env"
        env_path.write_text("ANTHROPIC_API_KEY=sk-existing\n")
        result = runner.invoke(
            app,
            ["config", "init", "--path", str(target), "--force", "--provider", "openai"],
        )
        assert result.exit_code == 0
        new_content = env_path.read_text()
        assert "ANTHROPIC_API_KEY=sk-existing" not in new_content
        assert "OPENAI_API_KEY=" in new_content

    def test_init_prompts_to_overwrite_env_separately(self, tmp_path: Path) -> None:
        """Config init should ask separately about overwriting .env when it exists."""
        target = tmp_path / "config.yaml"
        env_path = tmp_path / ".env"
        env_path.write_text("ANTHROPIC_API_KEY=sk-existing\n")
        # Answer 'n' to .env overwrite prompt
        result = runner.invoke(
            app,
            ["config", "init", "--path", str(target), "--provider", "openai"],
            input="n\n",
        )
        assert result.exit_code == 0
        assert env_path.read_text() == "ANTHROPIC_API_KEY=sk-existing\n"

    def test_init_keeps_existing_env_without_storage_root_and_resolves_to_default_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Keeping an existing `.env` without MINDROOM_STORAGE_PATH should still produce a working starter config."""
        target = tmp_path / "config.yaml"
        env_path = tmp_path / ".env"
        env_path.write_text("OPENAI_API_KEY=sk-existing\n", encoding="utf-8")
        monkeypatch.delenv("MINDROOM_STORAGE_PATH", raising=False)

        result = runner.invoke(
            app,
            ["config", "init", "--path", str(target), "--provider", "openai"],
            input="n\n",
        )

        assert result.exit_code == 0
        workspace = tmp_path / "mindroom_data" / "agents" / "mind" / "workspace" / "mind_data"
        assert (workspace / "SOUL.md").exists()
        config = yaml.safe_load(target.read_text(encoding="utf-8"))
        runtime_paths = constants_module.resolve_runtime_paths(config_path=target)
        resolved_kb_path = constants_module.resolve_config_relative_path(
            config["knowledge_bases"]["mind_memory"]["path"],
            runtime_paths,
        )
        assert resolved_kb_path == workspace / "memory"

    def test_init_keeps_existing_env_storage_root_for_workspace_and_knowledge_base(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Keeping an existing `.env` with MINDROOM_STORAGE_PATH should keep starter files under that root."""
        target = tmp_path / "config.yaml"
        env_path = tmp_path / ".env"
        custom_root = tmp_path / "custom-root"
        env_path.write_text(
            f"MINDROOM_STORAGE_PATH={custom_root}\nOPENAI_API_KEY=sk-existing\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("MINDROOM_STORAGE_PATH", raising=False)

        result = runner.invoke(
            app,
            ["config", "init", "--path", str(target), "--provider", "openai"],
            input="n\n",
        )

        assert result.exit_code == 0
        workspace = custom_root / "agents" / "mind" / "workspace" / "mind_data"
        assert (workspace / "SOUL.md").exists()
        config = yaml.safe_load(target.read_text(encoding="utf-8"))
        runtime_paths = constants_module.resolve_runtime_paths(config_path=target)
        resolved_kb_path = constants_module.resolve_config_relative_path(
            config["knowledge_bases"]["mind_memory"]["path"],
            runtime_paths,
        )
        assert resolved_kb_path == workspace / "memory"
        assert env_path.read_text(encoding="utf-8").startswith(f"MINDROOM_STORAGE_PATH={custom_root}\n")

    def test_init_overwrites_env_when_confirmed(self, tmp_path: Path) -> None:
        """Config init should overwrite .env when user confirms."""
        target = tmp_path / "config.yaml"
        env_path = tmp_path / ".env"
        env_path.write_text("ANTHROPIC_API_KEY=sk-existing\n")
        result = runner.invoke(
            app,
            ["config", "init", "--path", str(target), "--provider", "openai"],
            input="y\n",
        )
        assert result.exit_code == 0
        new_content = env_path.read_text()
        assert "ANTHROPIC_API_KEY=sk-existing" not in new_content
        assert "Env file overwritten" in _strip_ansi(result.output)

    def test_init_refuses_overwrite_without_force(self, tmp_path: Path) -> None:
        """Config init prompts before overwriting and aborts on 'n'."""
        target = tmp_path / "config.yaml"
        target.write_text("existing")
        result = runner.invoke(app, ["config", "init", "--path", str(target)], input="n\n")
        assert result.exit_code == 0
        assert target.read_text() == "existing"

    def test_init_force_overwrites(self, tmp_path: Path) -> None:
        """Config init --force overwrites without prompting."""
        target = tmp_path / "config.yaml"
        target.write_text("existing")
        result = runner.invoke(
            app,
            ["config", "init", "--path", str(target), "--force", "--provider", "openai"],
        )
        assert result.exit_code == 0
        content = target.read_text()
        assert content != "existing"
        assert "agents:" in content

    def test_init_openai_preset_uses_openai_models(self, tmp_path: Path) -> None:
        """Config init --provider openai prepopulates OpenAI defaults."""
        target = tmp_path / "config.yaml"
        result = runner.invoke(app, ["config", "init", "--path", str(target), "--provider", "openai"])
        assert result.exit_code == 0
        content = target.read_text()
        assert "provider: openai" in content
        assert "id: gpt-5.4" in content

    def test_init_openrouter_preset_uses_openrouter_models(self, tmp_path: Path) -> None:
        """Config init --provider openrouter uses OpenRouter with Claude model."""
        target = tmp_path / "config.yaml"
        result = runner.invoke(app, ["config", "init", "--path", str(target), "--provider", "openrouter"])
        assert result.exit_code == 0
        content = target.read_text()
        assert "provider: openrouter" in content
        assert "anthropic/claude-sonnet-4.6" in content

    def test_init_anthropic_preset_uses_anthropic_models_and_local_embedder(self, tmp_path: Path) -> None:
        """Config init --provider anthropic should only require the Anthropic API key."""
        target = tmp_path / "config.yaml"
        result = runner.invoke(app, ["config", "init", "--path", str(target), "--provider", "anthropic"])
        assert result.exit_code == 0

        config = yaml.safe_load(target.read_text())
        assert config["models"]["default"]["provider"] == "anthropic"
        assert config["models"]["default"]["id"] == "claude-sonnet-4-6"
        assert config["memory"]["embedder"]["provider"] == "sentence_transformers"
        assert config["memory"]["embedder"]["config"]["model"] == "sentence-transformers/all-MiniLM-L6-v2"

        env_content = (tmp_path / ".env").read_text()
        assert "ANTHROPIC_API_KEY=your-anthropic-key-here" in env_content
        assert "\n# OPENAI_API_KEY=your-openai-key-here" in env_content
        assert "\n# OPENROUTER_API_KEY=your-openrouter-key-here" in env_content

    def test_init_vertexai_claude_preset_uses_vertex_models(self, tmp_path: Path) -> None:
        """Config init --provider vertexai_claude uses Vertex AI Claude defaults."""
        target = tmp_path / "config.yaml"
        result = runner.invoke(app, ["config", "init", "--path", str(target), "--provider", "vertexai_claude"])
        assert result.exit_code == 0
        content = target.read_text()
        assert "provider: vertexai_claude" in content
        assert "id: claude-sonnet-4-6" in content

        env_content = (tmp_path / ".env").read_text()
        assert "ANTHROPIC_VERTEX_PROJECT_ID=your-gcp-project-id" in env_content
        assert "CLOUD_ML_REGION=us-central1" in env_content


# ---------------------------------------------------------------------------
# mindroom config show
# ---------------------------------------------------------------------------


class TestConfigShow:
    """Tests for `mindroom config show`."""

    def test_show_existing_config(self, tmp_path: Path) -> None:
        """Config show --raw prints file contents."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text("agents:\n  test:\n    display_name: Test\n")
        result = runner.invoke(app, ["config", "show", "--path", str(cfg), "--raw"])
        assert result.exit_code == 0
        assert "agents:" in result.output

    def test_show_missing_config(self, tmp_path: Path) -> None:
        """Config show exits 1 when config is missing."""
        missing = tmp_path / "nonexistent.yaml"
        result = runner.invoke(app, ["config", "show", "--path", str(missing)])
        assert result.exit_code == 1
        assert "No config file found" in result.output

    def test_show_missing_config_lists_explicit_path_first(self, tmp_path: Path) -> None:
        """Config show should render the explicit missing path as the first search location."""
        missing = tmp_path / "nonexistent.yaml"
        result = runner.invoke(app, ["config", "show", "--path", str(missing)], terminal_width=200)

        assert result.exit_code == 1
        output = _unwrap_console_output(result.output)
        assert str(missing.resolve()) in output
        assert output.index(str(missing.resolve())) < output.index(str(Path("config.yaml").resolve()))


# ---------------------------------------------------------------------------
# mindroom config edit
# ---------------------------------------------------------------------------


class TestConfigEdit:
    """Tests for `mindroom config edit`."""

    def test_edit_missing_config(self, tmp_path: Path) -> None:
        """Config edit exits 1 when config is missing."""
        missing = tmp_path / "nonexistent.yaml"
        result = runner.invoke(app, ["config", "edit", "--path", str(missing)])
        assert result.exit_code == 1
        assert "No config file found" in result.output

    def test_edit_opens_editor(self, tmp_path: Path) -> None:
        """Config edit invokes subprocess.run with the editor."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text("agents: {}")
        with patch("mindroom.cli.config.subprocess.run") as mock_run:
            mock_run.return_value = None
            result = runner.invoke(app, ["config", "edit", "--path", str(cfg)])
            assert result.exit_code == 0
            mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# mindroom config validate
# ---------------------------------------------------------------------------


class TestConfigValidate:
    """Tests for `mindroom config validate`."""

    def test_validate_valid_config(self, tmp_path: Path) -> None:
        """Config validate reports success for a valid config."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-6\n"
            "agents:\n  assistant:\n    display_name: Assistant\n    model: default\n"
            "router:\n  model: default\n",
        )
        result = runner.invoke(app, ["config", "validate", "--path", str(cfg)])
        assert result.exit_code == 0
        assert "valid" in result.output.lower()

    def test_validate_missing_file(self, tmp_path: Path) -> None:
        """Config validate exits 1 when file is missing."""
        missing = tmp_path / "nonexistent.yaml"
        result = runner.invoke(app, ["config", "validate", "--path", str(missing)])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_validate_invalid_config(self, tmp_path: Path) -> None:
        """Config validate shows friendly errors for invalid config."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text("agents: not_a_dict\n")
        result = runner.invoke(app, ["config", "validate", "--path", str(cfg)])
        assert result.exit_code == 1
        assert "Issues found" in result.output

    def test_validate_accepts_file_based_api_key_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config validate does not warn when provider secrets are supplied via *_FILE."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n  default:\n    provider: openai\n    id: gpt-5.4\n"
            "agents:\n  assistant:\n    display_name: Assistant\n    model: default\n"
            "router:\n  model: default\n",
        )
        secret_file = tmp_path / "openai_key"
        secret_file.write_text("sk-test", encoding="utf-8")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY_FILE", str(secret_file))

        result = runner.invoke(app, ["config", "validate", "--path", str(cfg)])

        assert result.exit_code == 0
        assert "Missing environment variables" not in result.output

    def test_validate_warns_for_missing_vertexai_claude_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Config validate should warn about missing Vertex AI project settings."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n  default:\n    provider: vertexai_claude\n    id: claude-sonnet-4-6\n"
            "agents:\n  assistant:\n    display_name: Assistant\n    model: default\n"
            "router:\n  model: default\n",
        )
        monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
        monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID_FILE", raising=False)
        monkeypatch.delenv("CLOUD_ML_REGION", raising=False)
        monkeypatch.delenv("CLOUD_ML_REGION_FILE", raising=False)

        result = runner.invoke(app, ["config", "validate", "--path", str(cfg)])

        assert result.exit_code == 0
        assert "Missing environment variables" in result.output
        assert "ANTHROPIC_VERTEX_PROJECT_ID" in result.output
        assert "CLOUD_ML_REGION" in result.output

    def test_validate_uses_active_config_sibling_env_from_exported_config_path(self, tmp_path: Path) -> None:
        """Config validate should honor the sibling .env of the exported active config path."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n  default:\n    provider: openai\n    id: gpt-5.4\n"
            "agents:\n  assistant:\n    display_name: Assistant\n    model: default\n"
            "router:\n  model: default\n",
        )
        (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-test\n", encoding="utf-8")

        result = runner.invoke(app, ["config", "validate"], env={"MINDROOM_CONFIG_PATH": str(cfg)})

        assert result.exit_code == 0
        assert "Missing environment variables" not in result.output


# ---------------------------------------------------------------------------
# mindroom config path
# ---------------------------------------------------------------------------


class TestConfigPath:
    """Tests for `mindroom config path`."""

    def test_path_shows_location(self) -> None:
        """Config path prints the resolved config location."""
        result = runner.invoke(app, ["config", "path"])
        assert result.exit_code == 0
        assert "Resolved config path" in result.output

    def test_path_with_explicit_path_lists_that_path_first(self, tmp_path: Path) -> None:
        """Config path should render the explicit path as the first search location."""
        missing = tmp_path / "missing.yaml"
        result = runner.invoke(app, ["config", "path", "--path", str(missing)], terminal_width=200)

        assert result.exit_code == 0
        output = _unwrap_console_output(result.output)
        assert str(missing.resolve()) in output
        assert output.index(str(missing.resolve())) < output.index(str(Path("config.yaml").resolve()))


# ---------------------------------------------------------------------------
# run command error handling
# ---------------------------------------------------------------------------


class TestRunErrorHandling:
    """Tests for friendly error messages in `mindroom run`."""

    def test_run_missing_config(self, tmp_path: Path) -> None:
        """Run shows friendly error when config is missing."""
        result = _invoke_with_runtime(["run"], tmp_path / "no_such_config.yaml")
        assert result.exit_code == 1
        assert "No config.yaml found" in result.output
        assert "mindroom config init" in result.output

    def test_load_active_config_or_exit_uses_runtime_process_env_for_missing_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Top-level missing-config output should respect the explicit runtime snapshot."""
        missing = tmp_path / "runtime-missing.yaml"
        ambient = tmp_path / "ambient-missing.yaml"
        runtime_paths = constants_module.resolve_primary_runtime_paths(config_path=missing, process_env={})
        monkeypatch.setenv("MINDROOM_CONFIG_PATH", str(ambient))

        with pytest.raises(typer.Exit):
            _load_active_config_or_exit(runtime_paths)

        output = _unwrap_console_output(capsys.readouterr().out)
        assert str(missing.resolve()) in output
        assert str(ambient.resolve()) not in output

    def test_run_invalid_config(self, tmp_path: Path) -> None:
        """Run shows friendly error when config is invalid."""
        bad_cfg = tmp_path / "config.yaml"
        bad_cfg.write_text("agents: not_a_dict\n")
        result = _invoke_with_runtime(["run"], bad_cfg)
        assert result.exit_code == 1
        assert "Invalid configuration" in result.output

    def test_avatars_generate_reports_avatar_generation_failure(
        self,
        tmp_path: Path,
    ) -> None:
        """Avatar generation should exit cleanly when generation fails."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-6\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n"
            "matrix_space:\n  enabled: false\n"
            "authorization:\n  global_users: []\n",
        )

        with patch(
            "mindroom.avatar_generation.run_avatar_generation",
            AsyncMock(side_effect=AvatarGenerationError("Avatar generation failed. See errors above.")),
        ):
            result = _invoke_with_runtime(["avatars", "generate"], cfg)

        assert result.exit_code == 1
        assert "Avatar generation failed" in _strip_ansi(result.output)


# ---------------------------------------------------------------------------
# version & help
# ---------------------------------------------------------------------------


class TestVersionAndHelp:
    """Tests for version and help commands."""

    def test_version_command(self) -> None:
        """Version command prints the version string."""
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "Mindroom version" in result.output

    def test_help_mentions_config(self) -> None:
        """Top-level help includes key subcommands."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "config" in result.output
        assert "avatars" in result.output


# ---------------------------------------------------------------------------
# run command: API server flags
# ---------------------------------------------------------------------------


class TestRunApiFlags:
    """Tests for --api/--no-api, --api-port, --api-host flags."""

    def test_run_help_shows_api_flags(self) -> None:
        """Run --help lists the new API server flags."""
        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "--api" in output
        assert "--no-api" in output
        assert "--api-port" in output
        assert "--api-host" in output

    def test_run_passes_api_defaults(self, tmp_path: Path) -> None:
        """Run passes api=True, port=8765, host=0.0.0.0 by default."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-6\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n"
            "matrix_space:\n  enabled: false\n",
        )
        mock_main = AsyncMock()
        with patch("mindroom.orchestrator.main", mock_main):
            result = _invoke_with_runtime(["run"], cfg)
        assert result.exit_code == 0
        mock_main.assert_awaited_once()
        kwargs = mock_main.call_args
        assert kwargs.kwargs["api"] is True
        assert kwargs.kwargs["api_port"] == 8765
        assert kwargs.kwargs["api_host"] == "0.0.0.0"  # noqa: S104

    def test_run_no_api_flag(self, tmp_path: Path) -> None:
        """Run --no-api passes api=False to bot main."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-6\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n"
            "matrix_space:\n  enabled: false\n",
        )
        mock_main = AsyncMock()
        with patch("mindroom.orchestrator.main", mock_main):
            result = _invoke_with_runtime(["run", "--no-api"], cfg)
        assert result.exit_code == 0
        assert mock_main.call_args.kwargs["api"] is False

    def test_run_custom_port_and_host(self, tmp_path: Path) -> None:
        """Run --api-port and --api-host are forwarded to bot main."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-6\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n"
            "matrix_space:\n  enabled: false\n",
        )
        mock_main = AsyncMock()
        with patch("mindroom.orchestrator.main", mock_main):
            result = _invoke_with_runtime(["run", "--api-port", "9000", "--api-host", "127.0.0.1"], cfg)
        assert result.exit_code == 0
        assert mock_main.call_args.kwargs["api_port"] == 9000
        assert mock_main.call_args.kwargs["api_host"] == "127.0.0.1"

    def test_run_storage_path_updates_runtime_paths(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Run should thread `--storage-path` through the explicit runtime context."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-6\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n"
            "matrix_space:\n  enabled: false\n",
        )
        runtime_storage = tmp_path / "runtime-storage"
        monkeypatch.delenv("MINDROOM_STORAGE_PATH", raising=False)

        async def _fake_main(**kwargs: object) -> None:
            runtime_paths = kwargs["runtime_paths"]
            assert isinstance(runtime_paths, constants_module.RuntimePaths)
            assert runtime_paths.storage_root == runtime_storage.resolve()
            assert runtime_paths.storage_root == runtime_storage.resolve()
            assert constants_module.tracking_dir(runtime_paths) == runtime_storage.resolve() / "tracking"
            assert constants_module.matrix_state_file(runtime_paths) == runtime_storage.resolve() / "matrix_state.yaml"
            assert (
                ResponseTracker("agent", base_path=runtime_storage.resolve() / "tracking").base_path
                == runtime_storage.resolve() / "tracking"
            )
            MatrixState().save(runtime_paths=runtime_paths)
            assert (runtime_storage.resolve() / "matrix_state.yaml").exists()

        with patch("mindroom.orchestrator.main", AsyncMock(side_effect=_fake_main)) as mock_main:
            result = _invoke_with_runtime(["run", "--storage-path", str(runtime_storage)], cfg)

        assert result.exit_code == 0
        mock_main.assert_awaited_once()


class TestAvatarsCommands:
    """Tests for `mindroom avatars`."""

    def test_avatars_help_lists_subcommands(self) -> None:
        """The avatars command should expose generate and sync subcommands."""
        result = runner.invoke(app, ["avatars", "--help"])
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "generate" in output
        assert "sync" in output

    def test_avatars_generate_runs_generation(self, tmp_path: Path) -> None:
        """Avatar generation command should invoke the generation workflow."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-6\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n"
            "matrix_space:\n  enabled: false\n",
        )
        run_avatar_generation = AsyncMock()
        with patch("mindroom.avatar_generation.run_avatar_generation", run_avatar_generation):
            result = _invoke_with_runtime(["avatars", "generate"], cfg)
        assert result.exit_code == 0
        run_avatar_generation.assert_awaited_once()
        assert run_avatar_generation.await_args.args[0].config_path == cfg.resolve()

    def test_avatars_sync_runs_matrix_sync(self, tmp_path: Path) -> None:
        """Avatar sync command should invoke the Matrix sync workflow."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-6\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n"
            "matrix_space:\n  enabled: false\n",
        )
        set_room_avatars_in_matrix = AsyncMock()

        with patch("mindroom.avatar_generation.set_room_avatars_in_matrix", set_room_avatars_in_matrix):
            result = _invoke_with_runtime(["avatars", "sync"], cfg)

        assert result.exit_code == 0
        set_room_avatars_in_matrix.assert_awaited_once()
        assert set_room_avatars_in_matrix.await_args.args[0].config_path == cfg.resolve()

    def test_avatars_sync_reports_sync_failure(self, tmp_path: Path) -> None:
        """Unexpected avatar sync failures should propagate for debugging."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-6\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n"
            "matrix_space:\n  enabled: false\n",
        )
        with patch(
            "mindroom.avatar_generation.set_room_avatars_in_matrix",
            AsyncMock(side_effect=RuntimeError("boom")),
        ):
            result = _invoke_with_runtime(["avatars", "sync"], cfg)

        assert result.exit_code == 1
        assert isinstance(result.exception, RuntimeError)
        assert str(result.exception) == "boom"

    def test_avatars_sync_requires_initialized_router_account(
        self,
        tmp_path: Path,
    ) -> None:
        """Avatar sync should fail when the router account has not been initialized yet."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-6\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n"
            "matrix_space:\n  enabled: false\n",
        )
        with patch(
            "mindroom.avatar_generation.set_room_avatars_in_matrix",
            AsyncMock(side_effect=AvatarSyncError("No router account found in Matrix state.")),
        ):
            result = _invoke_with_runtime(["avatars", "sync"], cfg)

        assert result.exit_code == 1
        assert "No router account found in Matrix state." in _strip_ansi(result.output)


# ---------------------------------------------------------------------------
# mindroom doctor
# ---------------------------------------------------------------------------

_VALID_CONFIG = (
    "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-6\n"
    "agents:\n  assistant:\n    display_name: Assistant\n    model: default\n"
    "router:\n  model: default\n"
)
_VALID_VERTEXAI_CLAUDE_CONFIG = (
    "models:\n  default:\n    provider: vertexai_claude\n    id: claude-sonnet-4-6\n"
    "agents:\n  assistant:\n    display_name: Assistant\n    model: default\n"
    "router:\n  model: default\n"
)
_VALID_MULTI_VERTEXAI_CLAUDE_CONFIG = (
    "models:\n"
    "  default:\n    provider: vertexai_claude\n    id: claude-sonnet-4-6\n"
    "  fast:\n    provider: vertexai_claude\n    id: claude-haiku-4-5\n"
    "agents:\n  assistant:\n    display_name: Assistant\n    model: default\n"
    "router:\n  model: default\n"
)


def _patch_homeserver_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch httpx.get to simulate a reachable homeserver."""
    resp = httpx.Response(200, json={"versions": ["v1.1"]})
    monkeypatch.setattr(
        "mindroom.cli.doctor.constants.runtime_matrix_homeserver",
        lambda *_args, **_kwargs: "http://localhost:8008",
    )
    monkeypatch.setattr("mindroom.cli.doctor.httpx.get", lambda *_a, **_kw: resp)


def _patch_homeserver_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch httpx.get to simulate an unreachable homeserver."""
    monkeypatch.setattr(
        "mindroom.cli.doctor.constants.runtime_matrix_homeserver",
        lambda *_args, **_kwargs: "http://localhost:8008",
    )

    def _raise(*_a: object, **_kw: object) -> None:
        msg = "Connection refused"
        raise httpx.ConnectError(msg)

    monkeypatch.setattr("mindroom.cli.doctor.httpx.get", _raise)


class TestDoctor:
    """Tests for `mindroom doctor`."""

    def test_all_checks_pass(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor reports all green when everything is fine."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_VALID_CONFIG)
        storage = tmp_path / "storage"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")  # for default memory embedder
        _patch_homeserver_ok(monkeypatch)

        result = _invoke_with_runtime(["doctor"], cfg, storage_path=storage)
        assert result.exit_code == 0
        assert "✓" in result.output
        assert "✗" not in result.output
        assert "6 passed" in result.output
        assert "0 failed" in result.output
        assert "1 warning" in result.output  # memory LLM not configured
        assert "Providers:" in result.output
        assert "anthropic (1 model)" in result.output
        assert "API key valid" in result.output

    def test_doctor_uses_active_config_sibling_env_from_exported_config_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Doctor should honor active-config sibling env keys without pre-activating runtime globals."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_VALID_CONFIG)
        (tmp_path / ".env").write_text(
            "ANTHROPIC_API_KEY=sk-test\nOPENAI_API_KEY=sk-test\n",
            encoding="utf-8",
        )
        _patch_homeserver_ok(monkeypatch)

        result = runner.invoke(app, ["doctor"], env={"MINDROOM_CONFIG_PATH": str(cfg)})

        assert result.exit_code == 0
        assert "ANTHROPIC_API_KEY not set" not in result.output
        assert "OPENAI_API_KEY not set" not in result.output

    def test_uses_status_for_steps(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor wraps checks in status contexts for interactive progress feedback."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_VALID_CONFIG)
        storage = tmp_path / "storage"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        _patch_homeserver_ok(monkeypatch)

        status_messages: list[str] = []

        class _DummyStatus:
            def __enter__(self) -> None:
                return None

            def __exit__(self, *_args: object) -> bool:
                return False

        def _status(message: str, **_kwargs: object) -> _DummyStatus:
            status_messages.append(message)
            return _DummyStatus()

        monkeypatch.setattr("mindroom.cli.doctor.console.status", _status)

        result = _invoke_with_runtime(["doctor"], cfg, storage_path=storage)
        assert result.exit_code == 0
        assert len(status_messages) == 6
        assert any("Matrix homeserver" in msg for msg in status_messages)
        assert any("memory config" in msg for msg in status_messages)

    def test_missing_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor reports failure when config file is missing."""
        _patch_homeserver_ok(monkeypatch)

        result = _invoke_with_runtime(["doctor"], tmp_path / "missing.yaml", storage_path=tmp_path / "storage")
        assert result.exit_code == 1
        assert "Config file not found" in result.output

    def test_invalid_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor reports failure when config is invalid YAML/schema."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text("agents: not_a_dict\n")
        storage = tmp_path / "storage"
        _patch_homeserver_ok(monkeypatch)

        result = _invoke_with_runtime(["doctor"], cfg, storage_path=storage)
        assert result.exit_code == 1
        assert "Config invalid" in result.output

    def test_missing_api_key_is_warning(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor warns (not fails) on missing API keys."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_VALID_CONFIG)
        storage = tmp_path / "storage"
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        _patch_homeserver_ok(monkeypatch)

        result = _invoke_with_runtime(["doctor"], cfg, storage_path=storage)
        assert result.exit_code == 0
        assert "ANTHROPIC_API_KEY not set" in result.output
        assert "3 warnings" in result.output

    def test_homeserver_unreachable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor reports failure when Matrix homeserver is unreachable."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_VALID_CONFIG)
        storage = tmp_path / "storage"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        _patch_homeserver_fail(monkeypatch)

        result = _invoke_with_runtime(["doctor"], cfg, storage_path=storage)
        assert result.exit_code == 1
        assert "Matrix homeserver unreachable" in result.output

    def test_storage_not_writable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor reports failure when storage directory is not writable."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_VALID_CONFIG)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        _patch_homeserver_ok(monkeypatch)

        result = _invoke_with_runtime(["doctor"], cfg, storage_path=Path("/proc/fake_mindroom_storage"))
        assert result.exit_code == 1
        assert "Storage not writable" in result.output

    def test_skips_config_checks_when_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor skips config-validation and provider checks when config is missing."""
        _patch_homeserver_ok(monkeypatch)

        result = _invoke_with_runtime(["doctor"], tmp_path / "missing.yaml", storage_path=tmp_path / "storage")
        assert "Config valid" not in result.output
        assert "Providers:" not in result.output
        assert "API key" not in result.output

    def test_invalid_api_key_is_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor reports failure when an API key is rejected by the provider."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_VALID_CONFIG)
        storage = tmp_path / "storage"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-invalid")
        monkeypatch.setattr(
            "mindroom.cli.doctor.constants.runtime_matrix_homeserver",
            lambda *_args, **_kwargs: "http://localhost:8008",
        )

        def _mock_get(url: str, **_kw: object) -> httpx.Response:
            if "/_matrix/" in str(url):
                return httpx.Response(200, json={"versions": ["v1.1"]})
            return httpx.Response(401, json={"error": "invalid"})

        monkeypatch.setattr("mindroom.cli.doctor.httpx.get", _mock_get)

        result = _invoke_with_runtime(["doctor"], cfg, storage_path=storage)
        assert result.exit_code == 1
        assert "API key invalid" in result.output

    def test_provider_summary_multiple_providers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor shows provider summary with correct model counts."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n"
            "  default:\n    provider: anthropic\n    id: claude-sonnet-4-6\n"
            "  fast:\n    provider: anthropic\n    id: claude-haiku-4-5\n"
            "  gpt:\n    provider: openai\n    id: gpt-4o\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n",
        )
        storage = tmp_path / "storage"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        _patch_homeserver_ok(monkeypatch)

        result = _invoke_with_runtime(["doctor"], cfg, storage_path=storage)
        assert result.exit_code == 0
        assert "anthropic (2 models)" in result.output
        assert "openai (1 model)" in result.output

    def test_vertexai_claude_connection_check_passes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Doctor validates Vertex AI Claude with a tiny messages smoke test."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_VALID_VERTEXAI_CLAUDE_CONFIG)
        storage = tmp_path / "storage"
        monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "mindroom-test")
        monkeypatch.setenv("CLOUD_ML_REGION", "us-central1")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        _patch_homeserver_ok(monkeypatch)

        called: dict[str, object] = {}

        class _FakeMessages:
            def create(self, **kwargs: object) -> SimpleNamespace:
                called["kwargs"] = kwargs
                return SimpleNamespace(content=[{"type": "text", "text": "OK"}])

        class _FakeVertexModel:
            def __init__(self, **kwargs: object) -> None:
                called["model_id"] = kwargs["id"]
                called["client_kwargs"] = kwargs
                self.id = str(kwargs["id"])

            def get_request_params(self) -> dict[str, object]:
                return {"timeout": called["client_kwargs"]["timeout"]}

            def get_client(self) -> SimpleNamespace:
                return SimpleNamespace(messages=_FakeMessages())

        monkeypatch.setattr("mindroom.cli.doctor.VertexAIClaude", _FakeVertexModel)

        result = _invoke_with_runtime(["doctor"], cfg, storage_path=storage)
        assert result.exit_code == 0
        assert "vertexai_claude connection valid for claude-sonnet-4-6" in result.output
        assert called["model_id"] == "claude-sonnet-4-6"
        assert called["client_kwargs"] == {
            "id": "claude-sonnet-4-6",
            "project_id": "mindroom-test",
            "region": "us-central1",
            "timeout": 10,
        }
        assert called["kwargs"] == {
            "model": "claude-sonnet-4-6",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "timeout": 10,
        }

    def test_vertexai_claude_connection_checks_each_configured_model(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Doctor validates every configured Vertex AI Claude model by its actual ID."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_VALID_MULTI_VERTEXAI_CLAUDE_CONFIG)
        storage = tmp_path / "storage"
        monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "mindroom-test")
        monkeypatch.setenv("CLOUD_ML_REGION", "us-central1")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        _patch_homeserver_ok(monkeypatch)

        created_models: list[str] = []
        requested_models: list[str] = []

        class _FakeMessages:
            def create(self, **kwargs: object) -> SimpleNamespace:
                requested_models.append(str(kwargs["model"]))
                return SimpleNamespace(content=[{"type": "text", "text": "OK"}])

        class _FakeVertexModel:
            def __init__(self, **kwargs: object) -> None:
                created_models.append(str(kwargs["id"]))

            def get_request_params(self) -> dict[str, object]:
                return {"timeout": 10}

            def get_client(self) -> SimpleNamespace:
                return SimpleNamespace(messages=_FakeMessages())

        monkeypatch.setattr("mindroom.cli.doctor.VertexAIClaude", _FakeVertexModel)

        result = _invoke_with_runtime(["doctor"], cfg, storage_path=storage)
        assert result.exit_code == 0
        assert "vertexai_claude connection valid for claude-sonnet-4-6" in result.output
        assert "vertexai_claude connection valid for claude-haiku-4-5" in result.output
        assert created_models == ["claude-sonnet-4-6", "claude-haiku-4-5"]
        assert requested_models == ["claude-sonnet-4-6", "claude-haiku-4-5"]

    def test_vertexai_claude_missing_env_is_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Doctor warns when Vertex AI Claude is configured without required env vars."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_VALID_VERTEXAI_CLAUDE_CONFIG)
        storage = tmp_path / "storage"
        monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
        monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID_FILE", raising=False)
        monkeypatch.delenv("CLOUD_ML_REGION", raising=False)
        monkeypatch.delenv("CLOUD_ML_REGION_FILE", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        _patch_homeserver_ok(monkeypatch)

        result = _invoke_with_runtime(["doctor"], cfg, storage_path=storage)
        assert result.exit_code == 0
        assert "vertexai_claude: could not validate connection" in result.output
        assert "ANTHROPIC_VERTEX_PROJECT_ID" in result.output
        assert "CLOUD_ML_REGION" in result.output

    def test_vertexai_claude_missing_adc_is_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Doctor warns when Vertex AI Claude cannot load Google credentials."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_VALID_VERTEXAI_CLAUDE_CONFIG)
        storage = tmp_path / "storage"
        monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "mindroom-test")
        monkeypatch.setenv("CLOUD_ML_REGION", "us-central1")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        _patch_homeserver_ok(monkeypatch)

        class _MissingCredentialsMessages:
            def create(self, **_kwargs: object) -> None:
                message = "ADC unavailable"
                raise DefaultCredentialsError(message)

        class _MissingCredentialsModel:
            def __init__(self, **kwargs: object) -> None:
                self.id = str(kwargs["id"])

            def get_request_params(self) -> dict[str, object]:
                return {"timeout": 10}

            def get_client(self) -> SimpleNamespace:
                return SimpleNamespace(messages=_MissingCredentialsMessages())

        monkeypatch.setattr("mindroom.cli.doctor.VertexAIClaude", _MissingCredentialsModel)

        result = _invoke_with_runtime(["doctor"], cfg, storage_path=storage)
        assert result.exit_code == 0
        assert "vertexai_claude: could not validate connection" in result.output
        assert "ADC" in result.output
        assert "unavailable" in result.output

    def test_vertexai_claude_api_rejection_is_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Doctor fails when Vertex AI rejects the configured Claude request."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_VALID_VERTEXAI_CLAUDE_CONFIG)
        storage = tmp_path / "storage"
        monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "mindroom-test")
        monkeypatch.setenv("CLOUD_ML_REGION", "us-central1")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        _patch_homeserver_ok(monkeypatch)

        class _RejectedMessages:
            def create(self, **_kwargs: object) -> None:
                response = httpx.Response(403, request=httpx.Request("POST", "https://vertex.test"))
                message = "denied"
                raise PermissionDeniedError(message, response=response, body={"error": "denied"})

        class _RejectedModel:
            def __init__(self, **kwargs: object) -> None:
                self.id = str(kwargs["id"])

            def get_request_params(self) -> dict[str, object]:
                return {"timeout": 10}

            def get_client(self) -> SimpleNamespace:
                return SimpleNamespace(messages=_RejectedMessages())

        monkeypatch.setattr("mindroom.cli.doctor.VertexAIClaude", _RejectedModel)

        result = _invoke_with_runtime(["doctor"], cfg, storage_path=storage)
        assert result.exit_code == 1
        assert "vertexai_claude connection failed for claude-sonnet-4-6" in result.output
        assert "HTTP 403" in result.output

    def test_custom_base_url_validation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor validates against custom base_url when configured."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n"
            "  default:\n"
            "    provider: openai\n"
            "    id: local-model\n"
            "    extra_kwargs:\n"
            "      base_url: http://localhost:9292/v1\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n",
        )
        storage = tmp_path / "storage"
        monkeypatch.setenv("OPENAI_API_KEY", "sk-local")
        monkeypatch.setattr(
            "mindroom.cli.doctor.constants.runtime_matrix_homeserver",
            lambda *_args, **_kwargs: "http://localhost:8008",
        )

        called_urls: list[str] = []

        def _mock_get(url: str, **_kw: object) -> httpx.Response:
            called_urls.append(str(url))
            if "/_matrix/client/versions" in str(url):
                return httpx.Response(200, json={"versions": ["v1.1"]})
            return httpx.Response(200, json={})

        monkeypatch.setattr("mindroom.cli.doctor.httpx.get", _mock_get)

        result = _invoke_with_runtime(["doctor"], cfg, storage_path=storage)
        assert result.exit_code == 0
        assert "API key valid" in result.output
        # Should validate against the custom base_url, not api.openai.com
        assert any("localhost:9292" in u for u in called_urls)

    def test_memory_ollama_embedder_checks_reachability(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Doctor checks ollama embedder reachability via /api/tags."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-6\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n"
            "memory:\n"
            "  embedder:\n"
            "    provider: ollama\n"
            "    config:\n"
            "      model: nomic-embed-text\n"
            "      host: http://localhost:11434\n",
        )
        storage = tmp_path / "storage"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        _patch_homeserver_ok(monkeypatch)

        result = _invoke_with_runtime(["doctor"], cfg, storage_path=storage)
        assert result.exit_code == 0
        assert "Memory embedder: ollama reachable" in result.output

    def test_memory_configured_llm_validates_key(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Doctor validates configured memory LLM API key."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-6\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n"
            "memory:\n"
            "  llm:\n"
            "    provider: openai\n"
            "    config:\n"
            "      model: gpt-4o-mini\n",
        )
        storage = tmp_path / "storage"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        _patch_homeserver_ok(monkeypatch)

        result = _invoke_with_runtime(["doctor"], cfg, storage_path=storage)
        assert result.exit_code == 0
        assert "Memory LLM: openai/gpt-4o-mini API key valid" in result.output
        assert "Memory embedder:" in result.output

    def test_memory_llm_missing_key_is_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Doctor warns when memory LLM API key is not set."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-6\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n"
            "memory:\n"
            "  llm:\n"
            "    provider: openai\n"
            "    config:\n"
            "      model: gpt-4o-mini\n",
        )
        storage = tmp_path / "storage"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        _patch_homeserver_ok(monkeypatch)

        result = _invoke_with_runtime(["doctor"], cfg, storage_path=storage)
        assert result.exit_code == 0
        assert "Memory LLM (openai): OPENAI_API_KEY not set" in result.output

    def test_memory_openai_embedder_host_runs_embeddings_smoke_test(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Doctor validates custom OpenAI embedder hosts using /embeddings."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-6\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n"
            "memory:\n"
            "  embedder:\n"
            "    provider: openai\n"
            "    config:\n"
            "      model: embeddinggemma:300m\n"
            "      host: http://llama.local/v1\n",
        )
        storage = tmp_path / "storage"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        _patch_homeserver_ok(monkeypatch)

        called_urls: list[str] = []

        def _mock_post(url: str, **_kwargs: object) -> httpx.Response:
            called_urls.append(str(url))
            return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})

        monkeypatch.setattr("mindroom.cli.doctor.httpx.post", _mock_post)

        result = _invoke_with_runtime(["doctor"], cfg, storage_path=storage)
        assert result.exit_code == 0
        assert "embeddings endpoint reachable" in result.output
        assert any(url.endswith("/embeddings") for url in called_urls)

    def test_memory_openai_embedder_local_host_error_has_hint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Doctor adds a local-network hint for .local routing failures."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-6\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n"
            "memory:\n"
            "  embedder:\n"
            "    provider: openai\n"
            "    config:\n"
            "      model: embeddinggemma:300m\n"
            "      host: http://llama.local/v1\n",
        )
        storage = tmp_path / "storage"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        _patch_homeserver_ok(monkeypatch)

        def _mock_post(*_args: object, **_kwargs: object) -> httpx.Response:
            msg = "[Errno 65] No route to host"
            raise httpx.ConnectError(msg)

        monkeypatch.setattr("mindroom.cli.doctor.httpx.post", _mock_post)

        result = _invoke_with_runtime(["doctor"], cfg, storage_path=storage)
        assert result.exit_code == 0
        assert "could not reach embeddings" in result.output
        assert "endpoint (http://llama.local/v1)" in result.output
        assert "reachable LAN" in result.output
        assert "instead of .local" in result.output

    def test_memory_sentence_transformers_embedder_runs_local_smoke_test(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Doctor validates sentence-transformers embedders by loading the local model."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-6\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n"
            "memory:\n"
            "  embedder:\n"
            "    provider: sentence_transformers\n"
            "    config:\n"
            "      model: sentence-transformers/all-MiniLM-L6-v2\n",
        )
        storage = tmp_path / "storage"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        _patch_homeserver_ok(monkeypatch)

        class _FakeEmbedder:
            def get_embedding(self, text: str) -> list[float]:
                assert text == "mindroom doctor embedder check"
                return [0.1, 0.2, 0.3]

        monkeypatch.setattr(
            "mindroom.cli.doctor.create_sentence_transformers_embedder",
            lambda _model: _FakeEmbedder(),
        )

        result = _invoke_with_runtime(["doctor"], cfg, storage_path=storage)
        assert result.exit_code == 0
        assert "sentence_transformers/sentence-transformers/all-MiniLM-L6-v2" in result.output
        assert "local model loaded" in result.output


# ---------------------------------------------------------------------------
# mindroom connect
# ---------------------------------------------------------------------------


class TestConnect:
    """Tests for `mindroom connect` pairing command."""

    def test_connect_persists_local_provisioning_credentials(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Successful pairing should write provisioning credentials to .env."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models: {}\nagents: {}\nrouter:\n  model: default\n"
            "authorization:\n"
            "  default_room_access: false\n"
            "  global_users:\n"
            f"    - {OWNER_MATRIX_USER_ID_PLACEHOLDER}\n"
            "  agent_reply_permissions:\n"
            '    "*":\n'
            f"      - {OWNER_MATRIX_USER_ID_PLACEHOLDER}\n",
        )
        monkeypatch.setattr("mindroom.cli.main.socket.gethostname", lambda: "devbox")

        monkeypatch.setattr(
            "mindroom.cli.main.httpx.post",
            lambda *_a, **_kw: httpx.Response(
                200,
                json={
                    "client_id": "client-123",
                    "client_secret": "secret-123",
                    "namespace": "a1b2c3d4",
                    "owner_user_id": "@alice:mindroom.chat",
                    "connection": {
                        "id": "conn-1",
                        "client_name": "devbox",
                        "fingerprint": "sha256:abc",
                        "created_at": "2026-02-27T12:00:00Z",
                        "last_seen_at": "2026-02-27T12:00:00Z",
                        "revoked_at": None,
                    },
                },
            ),
        )

        result = _invoke_with_runtime(
            ["connect", "--pair-code", "ABCD-EFGH", "--provisioning-url", "https://provisioning.example"],
            cfg,
        )

        assert result.exit_code == 0
        assert "Paired successfully" in result.output
        env_content = (tmp_path / ".env").read_text()
        assert "MINDROOM_PROVISIONING_URL=https://provisioning.example" in env_content
        assert "MINDROOM_LOCAL_CLIENT_ID=client-123" in env_content
        assert "MINDROOM_LOCAL_CLIENT_SECRET=secret-123" in env_content
        assert "MINDROOM_NAMESPACE=a1b2c3d4" in env_content
        assert "MINDROOM_OWNER_USER_ID=" not in env_content
        updated_config = cfg.read_text()
        assert OWNER_MATRIX_USER_ID_PLACEHOLDER not in updated_config
        assert "@alice:mindroom.chat" in updated_config

    def test_connect_path_overrides_env_and_config_target(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--path should control where .env is written and which config gets placeholder updates."""
        default_cfg = tmp_path / "default" / "config.yaml"
        default_cfg.parent.mkdir(parents=True, exist_ok=True)
        default_cfg.write_text(
            "models: {}\nagents: {}\nrouter:\n  model: default\n"
            "authorization:\n"
            "  default_room_access: false\n"
            "  global_users:\n"
            f"    - {OWNER_MATRIX_USER_ID_PLACEHOLDER}\n",
        )
        target_cfg = tmp_path / "custom" / "config.yaml"
        target_cfg.parent.mkdir(parents=True, exist_ok=True)
        target_cfg.write_text(
            "models: {}\nagents: {}\nrouter:\n  model: default\n"
            "authorization:\n"
            "  default_room_access: false\n"
            "  global_users:\n"
            f"    - {OWNER_MATRIX_USER_ID_PLACEHOLDER}\n",
        )
        monkeypatch.setattr(
            "mindroom.cli.main.httpx.post",
            lambda *_a, **_kw: httpx.Response(
                200,
                json={
                    "client_id": "client-123",
                    "client_secret": "secret-123",
                    "namespace": "a1b2c3d4",
                    "owner_user_id": "@alice:mindroom.chat",
                },
            ),
        )

        result = _invoke_with_runtime(
            [
                "connect",
                "--pair-code",
                "ABCD-EFGH",
                "--provisioning-url",
                "https://provisioning.example",
                "--path",
                str(target_cfg),
            ],
            default_cfg,
        )

        assert result.exit_code == 0
        assert (target_cfg.parent / ".env").exists()
        assert not (default_cfg.parent / ".env").exists()
        updated_target_config = target_cfg.read_text()
        unchanged_default_config = default_cfg.read_text()
        assert OWNER_MATRIX_USER_ID_PLACEHOLDER not in updated_target_config
        assert "@alice:mindroom.chat" in updated_target_config
        assert OWNER_MATRIX_USER_ID_PLACEHOLDER in unchanged_default_config

    def test_connect_no_persist_prints_exports(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--no-persist-env should print export commands and avoid writing .env."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n")
        monkeypatch.setattr(
            "mindroom.cli.main.httpx.post",
            lambda *_a, **_kw: httpx.Response(
                200,
                json={
                    "client_id": "client-123",
                    "client_secret": "secret-123",
                    "namespace": "a1b2c3d4",
                    "owner_user_id": "@alice:mindroom.chat",
                },
            ),
        )

        result = _invoke_with_runtime(
            [
                "connect",
                "--pair-code",
                "ABCD-EFGH",
                "--provisioning-url",
                "https://provisioning.example",
                "--no-persist-env",
            ],
            cfg,
        )

        assert result.exit_code == 0
        assert "export MINDROOM_PROVISIONING_URL=https://provisioning.example" in result.output
        assert "export MINDROOM_LOCAL_CLIENT_ID=client-123" in result.output
        assert "export MINDROOM_LOCAL_CLIENT_SECRET=secret-123" in result.output
        assert "export MINDROOM_NAMESPACE=a1b2c3d4" in result.output
        assert "Owner user ID from pairing: @alice:mindroom.chat" in result.output
        assert "export MINDROOM_OWNER_USER_ID=" not in result.output
        assert not (tmp_path / ".env").exists()

    def test_connect_uses_runtime_env_default_provisioning_url(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Provisioning URL default should be read at command runtime."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n")
        monkeypatch.setenv("MINDROOM_PROVISIONING_URL", "https://env-provisioning.example")

        called: dict[str, object] = {}

        def _fake_post(url: str, **kwargs: object) -> httpx.Response:
            called["url"] = url
            called["kwargs"] = kwargs
            return httpx.Response(
                200,
                json={
                    "client_id": "client-123",
                    "client_secret": "secret-123",
                    "namespace": "a1b2c3d4",
                    "owner_user_id": "@alice:mindroom.chat",
                },
            )

        monkeypatch.setattr("mindroom.cli.main.httpx.post", _fake_post)

        result = _invoke_with_runtime(["connect", "--pair-code", "ABCD-EFGH", "--no-persist-env"], cfg)

        assert result.exit_code == 0
        assert called["url"] == "https://env-provisioning.example/v1/local-mindroom/pair/complete"
        assert "export MINDROOM_PROVISIONING_URL=https://env-provisioning.example" in result.output
        assert "export MINDROOM_NAMESPACE=a1b2c3d4" in result.output
        assert "Owner user ID from pairing: @alice:mindroom.chat" in result.output
        assert "export MINDROOM_OWNER_USER_ID=" not in result.output

    def test_connect_warns_when_owner_user_id_is_malformed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Malformed owner_user_id should warn and skip placeholder replacement."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models: {}\nagents: {}\nrouter:\n  model: default\n"
            "authorization:\n"
            "  default_room_access: false\n"
            "  global_users:\n"
            f"    - {OWNER_MATRIX_USER_ID_PLACEHOLDER}\n",
        )
        monkeypatch.setattr(
            "mindroom.cli.main.httpx.post",
            lambda *_a, **_kw: httpx.Response(
                200,
                json={
                    "client_id": "client-123",
                    "client_secret": "secret-123",
                    "namespace": "a1b2c3d4",
                    "owner_user_id": "not-a-mxid",
                },
            ),
        )

        result = _invoke_with_runtime(
            ["connect", "--pair-code", "ABCD-EFGH", "--provisioning-url", "https://provisioning.example"],
            cfg,
        )

        assert result.exit_code == 0
        assert "malformed owner_user_id" in _strip_ansi(result.output)
        updated_config = cfg.read_text()
        assert OWNER_MATRIX_USER_ID_PLACEHOLDER in updated_config

    def test_connect_passes_matrix_ssl_verify_to_httpx(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Connect should pass MATRIX_SSL_VERIFY through to httpx.post."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n")
        monkeypatch.setattr("mindroom.cli.main.constants.runtime_matrix_ssl_verify", lambda *_args, **_kwargs: False)

        called: dict[str, object] = {}

        def _fake_post(url: str, **kwargs: object) -> httpx.Response:
            called["url"] = url
            called["kwargs"] = kwargs
            return httpx.Response(
                200,
                json={
                    "client_id": "client-123",
                    "client_secret": "secret-123",
                    "namespace": "a1b2c3d4",
                    "owner_user_id": "@alice:mindroom.chat",
                },
            )

        monkeypatch.setattr("mindroom.cli.main.httpx.post", _fake_post)

        result = _invoke_with_runtime(
            [
                "connect",
                "--pair-code",
                "ABCD-EFGH",
                "--provisioning-url",
                "https://provisioning.example",
                "--no-persist-env",
            ],
            cfg,
        )

        assert result.exit_code == 0
        kwargs = called["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["verify"] is False


# ---------------------------------------------------------------------------
# mindroom local-stack-setup
# ---------------------------------------------------------------------------


class TestLocalStackSetup:
    """Tests for `mindroom local-stack-setup`."""

    def test_starts_synapse_and_cinny_containers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Command starts Synapse compose and the Cinny container."""
        synapse_dir = tmp_path / "matrix"
        synapse_dir.mkdir()
        (synapse_dir / "docker-compose.yml").write_text("services: {}\n")
        cfg = tmp_path / "config.yaml"
        cfg.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n")
        storage_path = tmp_path / "mindroom_data"
        monkeypatch.setattr("mindroom.cli.local_stack.sys.platform", "linux")
        monkeypatch.setattr("mindroom.cli.local_stack.shutil.which", lambda _name: "/usr/bin/docker")
        monkeypatch.setattr(
            "mindroom.cli.local_stack.httpx.get",
            lambda *_a, **_kw: httpx.Response(200, json={"versions": ["v1.1"]}),
        )
        monkeypatch.setattr("mindroom.cli.local_stack.time.sleep", lambda *_a, **_kw: None)

        commands: list[list[str]] = []

        def _fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            commands.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("mindroom.cli.local_stack.subprocess.run", _fake_run)

        result = _invoke_with_runtime(
            [
                "local-stack-setup",
                "--synapse-dir",
                str(synapse_dir),
                "--cinny-port",
                "18080",
                "--cinny-container-name",
                "mindroom-cinny-test",
            ],
            cfg,
            storage_path=storage_path,
        )

        assert result.exit_code == 0
        assert ["docker", "compose", "up", "-d"] in commands
        assert any(cmd[:3] == ["docker", "rm", "-f"] for cmd in commands)
        assert any(cmd[:3] == ["docker", "run", "-d"] for cmd in commands)

        cinny_config = storage_path / "local" / "cinny-config.json"
        assert cinny_config.exists()
        config = json.loads(cinny_config.read_text())
        assert config["homeserverList"] == ["http://localhost:8008"]
        assert config["featuredCommunities"]["rooms"] == ["#lobby:localhost"]

        env_path = tmp_path / ".env"
        assert env_path.exists()
        env_content = env_path.read_text()
        assert "MATRIX_HOMESERVER=http://localhost:8008" in env_content
        assert "MATRIX_SSL_VERIFY=false" in env_content
        assert "MATRIX_SERVER_NAME=localhost" in env_content
        assert "Local stack is ready." in result.output

    def test_skip_synapse_skips_compose_start(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--skip-synapse should not run docker compose up."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n")
        storage_path = tmp_path / "mindroom_data"
        monkeypatch.setattr("mindroom.cli.local_stack.sys.platform", "linux")
        monkeypatch.setattr("mindroom.cli.local_stack.shutil.which", lambda _name: "/usr/bin/docker")
        monkeypatch.setattr(
            "mindroom.cli.local_stack.httpx.get",
            lambda *_a, **_kw: httpx.Response(200, json={"versions": ["v1.1"]}),
        )
        monkeypatch.setattr("mindroom.cli.local_stack.time.sleep", lambda *_a, **_kw: None)

        commands: list[list[str]] = []

        def _fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            commands.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("mindroom.cli.local_stack.subprocess.run", _fake_run)

        result = _invoke_with_runtime(["local-stack-setup", "--skip-synapse"], cfg, storage_path=storage_path)

        assert result.exit_code == 0
        assert ["docker", "compose", "up", "-d"] not in commands
        assert any(cmd[:3] == ["docker", "run", "-d"] for cmd in commands)

    def test_no_persist_env_prints_inline_command(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--no-persist-env should not write .env and should print inline env usage."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n")
        storage_path = tmp_path / "mindroom_data"
        monkeypatch.setattr("mindroom.cli.local_stack.sys.platform", "linux")
        monkeypatch.setattr("mindroom.cli.local_stack.shutil.which", lambda _name: "/usr/bin/docker")
        monkeypatch.setattr(
            "mindroom.cli.local_stack.httpx.get",
            lambda *_a, **_kw: httpx.Response(200, json={"versions": ["v1.1"]}),
        )
        monkeypatch.setattr("mindroom.cli.local_stack.time.sleep", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            "mindroom.cli.local_stack.subprocess.run",
            lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
        )

        result = _invoke_with_runtime(
            ["local-stack-setup", "--skip-synapse", "--no-persist-env"],
            cfg,
            storage_path=storage_path,
        )

        assert result.exit_code == 0
        assert not (tmp_path / ".env").exists()
        assert "MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false" in result.output
        assert "uv run" in result.output
        assert "mindroom run" in result.output

    def test_rejects_unsupported_platform(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Command fails on unsupported operating systems."""
        monkeypatch.setattr("mindroom.cli.local_stack.sys.platform", "win32")
        monkeypatch.setattr("mindroom.cli.local_stack.shutil.which", lambda _name: "/usr/bin/docker")

        result = runner.invoke(app, ["local-stack-setup", "--skip-synapse"])

        assert result.exit_code == 1
        assert "supports Linux and macOS only" in result.output

    def test_requires_docker_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Command fails when Docker is missing from PATH."""
        monkeypatch.setattr("mindroom.cli.local_stack.sys.platform", "linux")
        monkeypatch.setattr("mindroom.cli.local_stack.shutil.which", lambda _name: None)

        result = runner.invoke(app, ["local-stack-setup", "--skip-synapse"])

        assert result.exit_code == 1
        assert "Docker is required" in result.output
