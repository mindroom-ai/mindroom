"""Tests for CLI config subcommands and run-command error handling."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from typer.testing import CliRunner

from mindroom.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


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
        assert "existing" not in target.read_text()
        assert "agents:" in target.read_text()


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
        result = runner.invoke(app, ["config", "validate", "--config", str(cfg)])
        assert result.exit_code == 0
        assert "valid" in result.output.lower()

    def test_validate_missing_file(self, tmp_path: Path) -> None:
        """Config validate exits 1 when file is missing."""
        missing = tmp_path / "nonexistent.yaml"
        result = runner.invoke(app, ["config", "validate", "--config", str(missing)])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_validate_invalid_config(self, tmp_path: Path) -> None:
        """Config validate shows friendly errors for invalid config."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text("agents: not_a_dict\n")
        result = runner.invoke(app, ["config", "validate", "--config", str(cfg)])
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
        monkeypatch.setattr("mindroom.cli.DEFAULT_AGENTS_CONFIG", tmp_path / "no_such_config.yaml")
        result = runner.invoke(app, ["run"])
        assert result.exit_code == 1
        assert "No config.yaml found" in result.output
        assert "mindroom config init" in result.output

    def test_run_invalid_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Run shows friendly error when config is invalid."""
        bad_cfg = tmp_path / "config.yaml"
        bad_cfg.write_text("agents: not_a_dict\n")
        monkeypatch.setattr("mindroom.cli.DEFAULT_AGENTS_CONFIG", bad_cfg)
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
