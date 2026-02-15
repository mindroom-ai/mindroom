"""Tests for config file discovery: find_config() and config_search_locations()."""

from __future__ import annotations

from typing import TYPE_CHECKING

import mindroom.constants as constants_mod

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _patch_config_globals(
    monkeypatch: pytest.MonkeyPatch,
    *,
    env: str | None = None,
    search_paths: list[Path] | None = None,
) -> None:
    """Patch module-level config globals used by find_config / config_search_locations."""
    monkeypatch.setattr(constants_mod, "_CONFIG_PATH_ENV", env)
    if search_paths is not None:
        monkeypatch.setattr(constants_mod, "_CONFIG_SEARCH_PATHS", search_paths)


class TestFindConfig:
    """Tests for find_config()."""

    def test_returns_cwd_config_when_nothing_exists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Falls back to the first search path (./config.yaml) when no file exists."""
        cwd_config = tmp_path / "config.yaml"
        home_config = tmp_path / ".mindroom" / "config.yaml"
        _patch_config_globals(monkeypatch, search_paths=[cwd_config, home_config])

        result = constants_mod.find_config()
        assert result == cwd_config

    def test_returns_home_config_when_cwd_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Discovers ~/.mindroom/config.yaml when ./config.yaml doesn't exist."""
        cwd_config = tmp_path / "config.yaml"
        home_config = tmp_path / ".mindroom" / "config.yaml"
        home_config.parent.mkdir(parents=True)
        home_config.write_text("agents: {}")
        _patch_config_globals(monkeypatch, search_paths=[cwd_config, home_config])

        result = constants_mod.find_config()
        assert result == home_config

    def test_prefers_cwd_over_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """./config.yaml wins over ~/.mindroom/config.yaml when both exist."""
        cwd_config = tmp_path / "config.yaml"
        cwd_config.write_text("agents: {}")
        home_config = tmp_path / ".mindroom" / "config.yaml"
        home_config.parent.mkdir(parents=True)
        home_config.write_text("agents: {}")
        _patch_config_globals(monkeypatch, search_paths=[cwd_config, home_config])

        result = constants_mod.find_config()
        assert result == cwd_config

    def test_env_var_overrides_filesystem_search(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """MINDROOM_CONFIG_PATH takes priority over ./config.yaml."""
        cwd_config = tmp_path / "config.yaml"
        cwd_config.write_text("agents: {}")
        env_config = tmp_path / "custom" / "config.yaml"
        _patch_config_globals(
            monkeypatch,
            env=str(env_config),
            search_paths=[cwd_config],
        )

        result = constants_mod.find_config()
        assert result == env_config

    def test_env_var_expands_tilde(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MINDROOM_CONFIG_PATH with ~ is expanded."""
        from pathlib import Path  # noqa: PLC0415

        _patch_config_globals(monkeypatch, env="~/my_config.yaml")

        result = constants_mod.find_config()
        assert result == Path("~/my_config.yaml").expanduser()
        assert "~" not in str(result)


class TestConfigSearchLocations:
    """Tests for config_search_locations()."""

    def test_returns_default_paths_when_no_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without env var, returns the default search paths."""
        cwd_config = tmp_path / "config.yaml"
        home_config = tmp_path / ".mindroom" / "config.yaml"
        _patch_config_globals(monkeypatch, search_paths=[cwd_config, home_config])

        result = constants_mod.config_search_locations()
        assert len(result) == 2
        assert result[0] == cwd_config.resolve()
        assert result[1] == home_config.resolve()

    def test_env_var_is_first(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env var path appears first in the list."""
        env_config = tmp_path / "custom" / "config.yaml"
        cwd_config = tmp_path / "config.yaml"
        _patch_config_globals(
            monkeypatch,
            env=str(env_config),
            search_paths=[cwd_config],
        )

        result = constants_mod.config_search_locations()
        assert result[0] == env_config.resolve()

    def test_deduplicates_when_env_matches_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """No duplicates when env var points to one of the default paths."""
        cwd_config = tmp_path / "config.yaml"
        _patch_config_globals(
            monkeypatch,
            env=str(cwd_config),
            search_paths=[cwd_config],
        )

        result = constants_mod.config_search_locations()
        resolved_paths = [str(p) for p in result]
        assert len(resolved_paths) == len(set(resolved_paths))
