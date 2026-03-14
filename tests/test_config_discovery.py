"""Tests for config file discovery: find_config() and config_search_locations()."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

import mindroom.constants as constants_mod
from mindroom.config.main import Config

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

    def test_returns_home_config_when_nothing_exists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Falls back to ~/.mindroom/config.yaml when no file exists."""
        cwd_config = tmp_path / "config.yaml"
        home_config = tmp_path / ".mindroom" / "config.yaml"
        _patch_config_globals(monkeypatch, search_paths=[cwd_config, home_config])

        result = constants_mod.find_config()
        assert result == home_config

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


class TestResolveConfigRelativePath:
    """Tests for resolve_config_relative_path()."""

    def test_relative_path_resolves_from_config_directory(self, tmp_path: Path) -> None:
        """Relative paths should resolve against the config parent directory."""
        config_path = tmp_path / "cfg" / "config.yaml"
        resolved = constants_mod.resolve_config_relative_path("openclaw_data/memory", config_path=config_path)
        assert resolved == (tmp_path / "cfg" / "openclaw_data" / "memory").resolve()

    def test_absolute_path_is_preserved(self, tmp_path: Path) -> None:
        """Absolute paths should stay absolute."""
        absolute_path = tmp_path / "knowledge"
        resolved = constants_mod.resolve_config_relative_path(absolute_path, config_path=tmp_path / "config.yaml")
        assert resolved == absolute_path.resolve()

    def test_environment_variables_are_expanded_before_resolution(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Config path resolution should treat `${MINDROOM_STORAGE_PATH}` as the runtime storage root."""
        storage_root = tmp_path / "runtime-storage"
        monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))

        resolved = constants_mod.resolve_config_relative_path(
            "${MINDROOM_STORAGE_PATH}/agents/mind/workspace/mind_data/memory",
            config_path=tmp_path / "config.yaml",
        )

        assert resolved == storage_root.resolve() / "agents" / "mind" / "workspace" / "mind_data" / "memory"

    def test_config_from_yaml_loads_sibling_env_for_expansion(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Explicit config paths should use their sibling `.env` for path expansion."""
        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        custom_storage = tmp_path / "custom-storage"
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
            encoding="utf-8",
        )
        (config_dir / ".env").write_text(
            f"MINDROOM_STORAGE_PATH={custom_storage}\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("MINDROOM_STORAGE_PATH", raising=False)

        Config.from_yaml(config_path)

        resolved = constants_mod.resolve_config_relative_path(
            "${MINDROOM_STORAGE_PATH}/kb",
            config_path=config_path,
        )
        assert resolved == custom_storage.resolve() / "kb"


class TestResolveAvatarPath:
    """Tests for resolve_avatar_path()."""

    def test_avatars_dir_uses_storage_path_in_container_for_active_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Containerized runtime should write avatar overrides under persistent storage."""
        active_config = tmp_path / "runtime" / "config.yaml"
        storage_dir = tmp_path / "storage"
        monkeypatch.setenv("DOCKER_CONTAINER", "1")
        constants_mod.set_runtime_paths(config_path=active_config, storage_path=storage_dir)

        resolved = constants_mod.avatars_dir()

        assert resolved == storage_dir / "avatars"

    def test_avatars_dir_keeps_explicit_non_active_config_relative_in_container(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Explicit non-active config paths should still resolve relative to that config."""
        active_config = tmp_path / "runtime" / "config.yaml"
        explicit_config = tmp_path / "workspace" / "config.yaml"
        storage_dir = tmp_path / "storage"
        monkeypatch.setenv("DOCKER_CONTAINER", "1")
        constants_mod.set_runtime_paths(config_path=active_config, storage_path=storage_dir)

        resolved = constants_mod.avatars_dir(config_path=explicit_config)

        assert resolved == explicit_config.parent / "avatars"

    def test_returns_workspace_avatar_when_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Workspace avatars should resolve to their on-disk path."""
        workspace_dir = tmp_path / "workspace"
        bundled_dir = tmp_path / "bundled"
        workspace_avatar = workspace_dir / "agents" / "general.png"
        workspace_avatar.parent.mkdir(parents=True)
        workspace_avatar.write_bytes(b"workspace")
        monkeypatch.setattr(constants_mod, "avatars_dir", lambda **_kwargs: workspace_dir)
        monkeypatch.setattr(constants_mod, "bundled_avatars_dir", lambda: bundled_dir)

        resolved = constants_mod.resolve_avatar_path("agents", "general")

        assert resolved == workspace_avatar

    def test_returns_bundled_avatar_when_workspace_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Runtime lookup should fall back to bundled avatars when no workspace override exists."""
        workspace_dir = tmp_path / "workspace"
        bundled_dir = tmp_path / "bundled"
        bundled_avatar = bundled_dir / "rooms" / "lobby.png"
        bundled_avatar.parent.mkdir(parents=True)
        bundled_avatar.write_bytes(b"bundled")
        monkeypatch.setattr(constants_mod, "avatars_dir", lambda **_kwargs: workspace_dir)
        monkeypatch.setattr(constants_mod, "bundled_avatars_dir", lambda: bundled_dir)

        resolved = constants_mod.resolve_avatar_path("rooms", "lobby")

        assert resolved == bundled_avatar

    def test_returns_workspace_avatar_path_when_no_avatar_exists(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Missing avatars should resolve to the workspace location generation writes to."""
        workspace_dir = tmp_path / "workspace"
        bundled_dir = tmp_path / "bundled"
        monkeypatch.setattr(constants_mod, "avatars_dir", lambda **_kwargs: workspace_dir)
        monkeypatch.setattr(constants_mod, "bundled_avatars_dir", lambda: bundled_dir)

        resolved = constants_mod.resolve_avatar_path("rooms", "nonexistent")

        assert resolved == workspace_dir / "rooms" / "nonexistent.png"

    def test_returns_storage_workspace_avatar_path_in_container_when_no_avatar_exists(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Container runtime should generate missing avatars under persistent storage."""
        active_config = tmp_path / "runtime" / "config.yaml"
        storage_dir = tmp_path / "storage"
        bundled_dir = tmp_path / "bundled"
        monkeypatch.setenv("DOCKER_CONTAINER", "1")
        constants_mod.set_runtime_paths(config_path=active_config, storage_path=storage_dir)
        monkeypatch.setattr(constants_mod, "bundled_avatars_dir", lambda: bundled_dir)

        resolved = constants_mod.resolve_avatar_path("rooms", "nonexistent")

        assert resolved == storage_dir / "avatars" / "rooms" / "nonexistent.png"


class TestStoragePathResolution:
    """Tests for STORAGE_PATH_OBJ import-time canonicalization."""

    def test_storage_path_obj_from_env_is_absolute_and_cwd_stable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Relative MINDROOM_STORAGE_PATH is anchored at import time and stays stable after cwd changes."""
        first_cwd = tmp_path / "first"
        first_cwd.mkdir(parents=True)
        second_cwd = tmp_path / "second"
        second_cwd.mkdir(parents=True)

        with monkeypatch.context() as m:
            m.chdir(first_cwd)
            m.setenv("MINDROOM_STORAGE_PATH", "mindroom_data")

            importlib.reload(constants_mod)
            expected_storage_path = (first_cwd / "mindroom_data").resolve()

            assert expected_storage_path == constants_mod.STORAGE_PATH_OBJ
            assert constants_mod.STORAGE_PATH_OBJ.is_absolute()

            m.chdir(second_cwd)
            assert expected_storage_path == constants_mod.STORAGE_PATH_OBJ

        # Restore module globals to environment defaults for subsequent tests.
        importlib.reload(constants_mod)
