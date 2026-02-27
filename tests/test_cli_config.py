"""Tests for CLI config subcommands, run-command error handling, and doctor."""

from __future__ import annotations

import json
import re
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
from typer.testing import CliRunner

from mindroom.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# mindroom config init
# ---------------------------------------------------------------------------


class TestConfigInit:
    """Tests for `mindroom config init`."""

    def test_init_creates_config(self, tmp_path: Path) -> None:
        """Config init creates a valid config.yaml at the target path."""
        target = tmp_path / "config.yaml"
        result = runner.invoke(app, ["config", "init", "--path", str(target)])
        assert result.exit_code == 0
        assert target.exists()
        content = target.read_text()
        assert "agents:" in content
        assert "models:" in content

    def test_init_minimal(self, tmp_path: Path) -> None:
        """Config init --minimal creates a bare-minimum config."""
        target = tmp_path / "config.yaml"
        result = runner.invoke(app, ["config", "init", "--path", str(target), "--minimal"])
        assert result.exit_code == 0
        content = target.read_text()
        assert "agents:" in content
        assert "# MindRoom Configuration (minimal)" in content

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

        env_content = (tmp_path / ".env").read_text()
        assert "MATRIX_HOMESERVER=https://mindroom.chat" in env_content
        assert "MATRIX_SERVER_NAME=mindroom.chat" in env_content
        assert "MINDROOM_PROVISIONING_URL=https://mindroom.chat" in env_content
        assert "MATRIX_REGISTRATION_TOKEN=" in env_content

    def test_init_creates_env_with_dashboard_key(self, tmp_path: Path) -> None:
        """Config init writes a random MINDROOM_API_KEY to .env."""
        target = tmp_path / "config.yaml"
        result = runner.invoke(app, ["config", "init", "--path", str(target)])
        assert result.exit_code == 0

        env_path = tmp_path / ".env"
        assert env_path.exists()

        content = env_path.read_text()
        backend_match = re.search(r"^MINDROOM_API_KEY=(.+)$", content, flags=re.MULTILINE)
        assert backend_match is not None
        assert backend_match.group(1)
        # VITE_API_KEY should NOT be in the template (auth is handled at proxy layer)
        assert "VITE_API_KEY" not in content

    def test_init_force_does_not_overwrite_existing_env(self, tmp_path: Path) -> None:
        """Config init --force should never overwrite an existing .env file."""
        target = tmp_path / "config.yaml"
        env_path = tmp_path / ".env"
        env_path.write_text("ANTHROPIC_API_KEY=sk-existing\n")
        result = runner.invoke(app, ["config", "init", "--path", str(target), "--force"])
        assert result.exit_code == 0
        assert env_path.read_text() == "ANTHROPIC_API_KEY=sk-existing\n"

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
        result = runner.invoke(app, ["config", "init", "--path", str(target), "--force"])
        assert result.exit_code == 0
        content = target.read_text()
        assert content != "existing"
        assert "agents:" in content


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
        with patch("mindroom.cli_config.subprocess.run") as mock_run:
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
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-5-latest\n"
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


# ---------------------------------------------------------------------------
# run command error handling
# ---------------------------------------------------------------------------


class TestRunErrorHandling:
    """Tests for friendly error messages in `mindroom run`."""

    def test_run_missing_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Run shows friendly error when config is missing."""
        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", tmp_path / "no_such_config.yaml")
        result = runner.invoke(app, ["run"])
        assert result.exit_code == 1
        assert "No config.yaml found" in result.output
        assert "mindroom config init" in result.output

    def test_run_invalid_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Run shows friendly error when config is invalid."""
        bad_cfg = tmp_path / "config.yaml"
        bad_cfg.write_text("agents: not_a_dict\n")
        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", bad_cfg)
        result = runner.invoke(app, ["run"])
        assert result.exit_code == 1
        assert "Invalid configuration" in result.output


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
        """Top-level help includes the config subcommand."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "config" in result.output


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

    def test_run_passes_api_defaults(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Run passes api=True, port=8765, host=0.0.0.0 by default."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-5-latest\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n",
        )
        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", cfg)
        mock_main = AsyncMock()
        with patch("mindroom.bot.main", mock_main):
            result = runner.invoke(app, ["run"])
        assert result.exit_code == 0
        mock_main.assert_awaited_once()
        kwargs = mock_main.call_args
        assert kwargs.kwargs["api"] is True
        assert kwargs.kwargs["api_port"] == 8765
        assert kwargs.kwargs["api_host"] == "0.0.0.0"  # noqa: S104

    def test_run_no_api_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Run --no-api passes api=False to bot main."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-5-latest\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n",
        )
        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", cfg)
        mock_main = AsyncMock()
        with patch("mindroom.bot.main", mock_main):
            result = runner.invoke(app, ["run", "--no-api"])
        assert result.exit_code == 0
        assert mock_main.call_args.kwargs["api"] is False

    def test_run_custom_port_and_host(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Run --api-port and --api-host are forwarded to bot main."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-5-latest\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n",
        )
        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", cfg)
        mock_main = AsyncMock()
        with patch("mindroom.bot.main", mock_main):
            result = runner.invoke(app, ["run", "--api-port", "9000", "--api-host", "127.0.0.1"])
        assert result.exit_code == 0
        assert mock_main.call_args.kwargs["api_port"] == 9000
        assert mock_main.call_args.kwargs["api_host"] == "127.0.0.1"


# ---------------------------------------------------------------------------
# mindroom doctor
# ---------------------------------------------------------------------------

_VALID_CONFIG = (
    "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-5-latest\n"
    "agents:\n  assistant:\n    display_name: Assistant\n    model: default\n"
    "router:\n  model: default\n"
)


def _patch_homeserver_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch httpx.get to simulate a reachable homeserver."""
    resp = httpx.Response(200, json={"versions": ["v1.1"]})
    monkeypatch.setattr("mindroom.cli.MATRIX_HOMESERVER", "http://localhost:8008")
    monkeypatch.setattr("mindroom.cli.httpx.get", lambda *_a, **_kw: resp)


def _patch_homeserver_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch httpx.get to simulate an unreachable homeserver."""
    monkeypatch.setattr("mindroom.cli.MATRIX_HOMESERVER", "http://localhost:8008")

    def _raise(*_a: object, **_kw: object) -> None:
        msg = "Connection refused"
        raise httpx.ConnectError(msg)

    monkeypatch.setattr("mindroom.cli.httpx.get", _raise)


class TestDoctor:
    """Tests for `mindroom doctor`."""

    def test_all_checks_pass(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor reports all green when everything is fine."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_VALID_CONFIG)
        storage = tmp_path / "storage"
        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", cfg)
        monkeypatch.setattr("mindroom.cli.STORAGE_PATH", str(storage))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")  # for default memory embedder
        _patch_homeserver_ok(monkeypatch)

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "✓" in result.output
        assert "✗" not in result.output
        assert "6 passed" in result.output
        assert "0 failed" in result.output
        assert "1 warning" in result.output  # memory LLM not configured
        assert "Providers:" in result.output
        assert "anthropic (1 model)" in result.output
        assert "API key valid" in result.output

    def test_uses_status_for_steps(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor wraps checks in status contexts for interactive progress feedback."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_VALID_CONFIG)
        storage = tmp_path / "storage"
        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", cfg)
        monkeypatch.setattr("mindroom.cli.STORAGE_PATH", str(storage))
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

        monkeypatch.setattr("mindroom.cli.console.status", _status)

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert len(status_messages) == 6
        assert any("Matrix homeserver" in msg for msg in status_messages)
        assert any("memory config" in msg for msg in status_messages)

    def test_missing_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor reports failure when config file is missing."""
        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", tmp_path / "missing.yaml")
        monkeypatch.setattr("mindroom.cli.STORAGE_PATH", str(tmp_path / "storage"))
        _patch_homeserver_ok(monkeypatch)

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 1
        assert "Config file not found" in result.output

    def test_invalid_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor reports failure when config is invalid YAML/schema."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text("agents: not_a_dict\n")
        storage = tmp_path / "storage"
        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", cfg)
        monkeypatch.setattr("mindroom.cli.STORAGE_PATH", str(storage))
        _patch_homeserver_ok(monkeypatch)

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 1
        assert "Config invalid" in result.output

    def test_missing_api_key_is_warning(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor warns (not fails) on missing API keys."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_VALID_CONFIG)
        storage = tmp_path / "storage"
        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", cfg)
        monkeypatch.setattr("mindroom.cli.STORAGE_PATH", str(storage))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        _patch_homeserver_ok(monkeypatch)

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "ANTHROPIC_API_KEY not set" in result.output
        assert "3 warnings" in result.output

    def test_homeserver_unreachable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor reports failure when Matrix homeserver is unreachable."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_VALID_CONFIG)
        storage = tmp_path / "storage"
        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", cfg)
        monkeypatch.setattr("mindroom.cli.STORAGE_PATH", str(storage))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        _patch_homeserver_fail(monkeypatch)

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 1
        assert "Matrix homeserver unreachable" in result.output

    def test_storage_not_writable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor reports failure when storage directory is not writable."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_VALID_CONFIG)
        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", cfg)
        monkeypatch.setattr("mindroom.cli.STORAGE_PATH", "/proc/fake_mindroom_storage")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        _patch_homeserver_ok(monkeypatch)

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 1
        assert "Storage not writable" in result.output

    def test_skips_config_checks_when_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor skips config-validation and provider checks when config is missing."""
        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", tmp_path / "missing.yaml")
        monkeypatch.setattr("mindroom.cli.STORAGE_PATH", str(tmp_path / "storage"))
        _patch_homeserver_ok(monkeypatch)

        result = runner.invoke(app, ["doctor"])
        assert "Config valid" not in result.output
        assert "Providers:" not in result.output
        assert "API key" not in result.output

    def test_invalid_api_key_is_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor reports failure when an API key is rejected by the provider."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_VALID_CONFIG)
        storage = tmp_path / "storage"
        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", cfg)
        monkeypatch.setattr("mindroom.cli.STORAGE_PATH", str(storage))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-invalid")
        monkeypatch.setattr("mindroom.cli.MATRIX_HOMESERVER", "http://localhost:8008")

        def _mock_get(url: str, **_kw: object) -> httpx.Response:
            if "/_matrix/" in str(url):
                return httpx.Response(200, json={"versions": ["v1.1"]})
            return httpx.Response(401, json={"error": "invalid"})

        monkeypatch.setattr("mindroom.cli.httpx.get", _mock_get)

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 1
        assert "API key invalid" in result.output

    def test_provider_summary_multiple_providers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor shows provider summary with correct model counts."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "models:\n"
            "  default:\n    provider: anthropic\n    id: claude-sonnet-4-5-latest\n"
            "  fast:\n    provider: anthropic\n    id: claude-haiku-3-5-latest\n"
            "  gpt:\n    provider: openai\n    id: gpt-4o\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n",
        )
        storage = tmp_path / "storage"
        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", cfg)
        monkeypatch.setattr("mindroom.cli.STORAGE_PATH", str(storage))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        _patch_homeserver_ok(monkeypatch)

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "anthropic (2 models)" in result.output
        assert "openai (1 model)" in result.output

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
        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", cfg)
        monkeypatch.setattr("mindroom.cli.STORAGE_PATH", str(storage))
        monkeypatch.setenv("OPENAI_API_KEY", "sk-local")
        monkeypatch.setattr("mindroom.cli.MATRIX_HOMESERVER", "http://localhost:8008")

        called_urls: list[str] = []

        def _mock_get(url: str, **_kw: object) -> httpx.Response:
            called_urls.append(str(url))
            return httpx.Response(200, json={})

        monkeypatch.setattr("mindroom.cli.httpx.get", _mock_get)

        result = runner.invoke(app, ["doctor"])
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
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-5-latest\n"
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
        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", cfg)
        monkeypatch.setattr("mindroom.cli.STORAGE_PATH", str(storage))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        _patch_homeserver_ok(monkeypatch)

        result = runner.invoke(app, ["doctor"])
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
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-5-latest\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n"
            "memory:\n"
            "  llm:\n"
            "    provider: openai\n"
            "    config:\n"
            "      model: gpt-4o-mini\n",
        )
        storage = tmp_path / "storage"
        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", cfg)
        monkeypatch.setattr("mindroom.cli.STORAGE_PATH", str(storage))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        _patch_homeserver_ok(monkeypatch)

        result = runner.invoke(app, ["doctor"])
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
            "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-5-latest\n"
            "agents:\n  a:\n    display_name: A\n    model: default\n"
            "router:\n  model: default\n"
            "memory:\n"
            "  llm:\n"
            "    provider: openai\n"
            "    config:\n"
            "      model: gpt-4o-mini\n",
        )
        storage = tmp_path / "storage"
        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", cfg)
        monkeypatch.setattr("mindroom.cli.STORAGE_PATH", str(storage))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        _patch_homeserver_ok(monkeypatch)

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "Memory LLM (openai): OPENAI_API_KEY not set" in result.output


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
        cfg.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n")
        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", cfg)
        monkeypatch.setattr("mindroom.cli.socket.gethostname", lambda: "devbox")

        monkeypatch.setattr(
            "mindroom.cli.httpx.post",
            lambda *_a, **_kw: httpx.Response(
                200,
                json={
                    "client_id": "client-123",
                    "client_secret": "secret-123",
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

        result = runner.invoke(
            app,
            [
                "connect",
                "--pair-code",
                "ABCD-EFGH",
                "--provisioning-url",
                "https://provisioning.example",
            ],
        )

        assert result.exit_code == 0
        assert "Paired successfully" in result.output
        env_content = (tmp_path / ".env").read_text()
        assert "MINDROOM_PROVISIONING_URL=https://provisioning.example" in env_content
        assert "MINDROOM_LOCAL_CLIENT_ID=client-123" in env_content
        assert "MINDROOM_LOCAL_CLIENT_SECRET=secret-123" in env_content

    def test_connect_no_persist_prints_exports(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--no-persist-env should print export commands and avoid writing .env."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n")
        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", cfg)
        monkeypatch.setattr(
            "mindroom.cli.httpx.post",
            lambda *_a, **_kw: httpx.Response(
                200,
                json={"client_id": "client-123", "client_secret": "secret-123"},
            ),
        )

        result = runner.invoke(
            app,
            [
                "connect",
                "--pair-code",
                "ABCD-EFGH",
                "--provisioning-url",
                "https://provisioning.example",
                "--no-persist-env",
            ],
        )

        assert result.exit_code == 0
        assert "export MINDROOM_PROVISIONING_URL=https://provisioning.example" in result.output
        assert "export MINDROOM_LOCAL_CLIENT_ID=client-123" in result.output
        assert "export MINDROOM_LOCAL_CLIENT_SECRET=secret-123" in result.output
        assert not (tmp_path / ".env").exists()


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

        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", cfg)
        monkeypatch.setattr("mindroom.cli.STORAGE_PATH", str(tmp_path / "mindroom_data"))
        monkeypatch.setattr("mindroom.cli.sys.platform", "linux")
        monkeypatch.setattr("mindroom.cli.shutil.which", lambda _name: "/usr/bin/docker")
        monkeypatch.setattr("mindroom.cli.httpx.get", lambda *_a, **_kw: httpx.Response(200, json={}))
        monkeypatch.setattr("mindroom.cli.time.sleep", lambda *_a, **_kw: None)

        commands: list[list[str]] = []

        def _fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            commands.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("mindroom.cli.subprocess.run", _fake_run)

        result = runner.invoke(
            app,
            [
                "local-stack-setup",
                "--synapse-dir",
                str(synapse_dir),
                "--cinny-port",
                "18080",
                "--cinny-container-name",
                "mindroom-cinny-test",
            ],
        )

        assert result.exit_code == 0
        assert ["docker", "compose", "up", "-d"] in commands
        assert any(cmd[:3] == ["docker", "rm", "-f"] for cmd in commands)
        assert any(cmd[:3] == ["docker", "run", "-d"] for cmd in commands)

        cinny_config = tmp_path / "mindroom_data" / "local" / "cinny-config.json"
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

        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", cfg)
        monkeypatch.setattr("mindroom.cli.STORAGE_PATH", str(tmp_path / "mindroom_data"))
        monkeypatch.setattr("mindroom.cli.sys.platform", "linux")
        monkeypatch.setattr("mindroom.cli.shutil.which", lambda _name: "/usr/bin/docker")
        monkeypatch.setattr("mindroom.cli.httpx.get", lambda *_a, **_kw: httpx.Response(200, json={}))
        monkeypatch.setattr("mindroom.cli.time.sleep", lambda *_a, **_kw: None)

        commands: list[list[str]] = []

        def _fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            commands.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("mindroom.cli.subprocess.run", _fake_run)

        result = runner.invoke(app, ["local-stack-setup", "--skip-synapse"])

        assert result.exit_code == 0
        assert ["docker", "compose", "up", "-d"] not in commands
        assert any(cmd[:3] == ["docker", "run", "-d"] for cmd in commands)

    def test_no_persist_env_prints_inline_command(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--no-persist-env should not write .env and should print inline env usage."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n")

        monkeypatch.setattr("mindroom.cli.CONFIG_PATH", cfg)
        monkeypatch.setattr("mindroom.cli.STORAGE_PATH", str(tmp_path / "mindroom_data"))
        monkeypatch.setattr("mindroom.cli.sys.platform", "linux")
        monkeypatch.setattr("mindroom.cli.shutil.which", lambda _name: "/usr/bin/docker")
        monkeypatch.setattr("mindroom.cli.httpx.get", lambda *_a, **_kw: httpx.Response(200, json={}))
        monkeypatch.setattr("mindroom.cli.time.sleep", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            "mindroom.cli.subprocess.run",
            lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
        )

        result = runner.invoke(app, ["local-stack-setup", "--skip-synapse", "--no-persist-env"])

        assert result.exit_code == 0
        assert not (tmp_path / ".env").exists()
        assert "MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false" in result.output
        assert "uv run" in result.output
        assert "mindroom run" in result.output

    def test_rejects_unsupported_platform(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Command fails on unsupported operating systems."""
        monkeypatch.setattr("mindroom.cli.sys.platform", "win32")
        monkeypatch.setattr("mindroom.cli.shutil.which", lambda _name: "/usr/bin/docker")

        result = runner.invoke(app, ["local-stack-setup", "--skip-synapse"])

        assert result.exit_code == 1
        assert "supports Linux and macOS only" in result.output

    def test_requires_docker_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Command fails when Docker is missing from PATH."""
        monkeypatch.setattr("mindroom.cli.sys.platform", "linux")
        monkeypatch.setattr("mindroom.cli.shutil.which", lambda _name: None)

        result = runner.invoke(app, ["local-stack-setup", "--skip-synapse"])

        assert result.exit_code == 1
        assert "Docker is required" in result.output
