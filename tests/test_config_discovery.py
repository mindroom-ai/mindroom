"""Tests for config file discovery: find_config() and config_search_locations()."""

from __future__ import annotations

import ast
import importlib
import os
from pathlib import Path

import pytest

import mindroom.constants as constants_mod
from mindroom.config.main import Config
from mindroom.matrix.state import MatrixState
from mindroom.response_tracker import ResponseTracker

_RUNTIME_GLOBAL_NAMES = {
    "MATRIX_HOMESERVER",
    "MATRIX_SERVER_NAME",
    "MATRIX_SSL_VERIFY",
    "MINDROOM_NAMESPACE",
    "ENABLE_AI_CACHE",
}
_RUNTIME_ENV_KEYS = {
    "MINDROOM_CONFIG_PATH",
    "MINDROOM_STORAGE_PATH",
    "MINDROOM_CONFIG_TEMPLATE",
    "MATRIX_HOMESERVER",
    "MATRIX_SERVER_NAME",
    "MATRIX_SSL_VERIFY",
    "MINDROOM_NAMESPACE",
    "MINDROOM_ENABLE_AI_CACHE",
}
_RUNTIME_GLOBAL_ALLOWLIST = {"src/mindroom/constants.py"}
_RUNTIME_ENV_ALLOWLIST = {
    "src/mindroom/constants.py",
    "src/mindroom/api/sandbox_runner.py",
    "src/mindroom/workers/backends/local.py",
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _runtime_source_files() -> list[Path]:
    return sorted((_repo_root() / "src" / "mindroom").rglob("*.py"))


def _runtime_constant_aliases(tree: ast.AST) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import):
            continue
        for alias in node.names:
            if alias.name == "mindroom.constants":
                aliases.add(alias.asname or alias.name.split(".")[-1])
    return aliases


def _runtime_global_import_violations(tree: ast.AST, relative_path: str) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module_name = node.module or ""
        if not module_name.endswith("constants"):
            continue
        violations.extend(
            f"{relative_path}:{node.lineno} imports {alias.name}"
            for alias in node.names
            if alias.name in _RUNTIME_GLOBAL_NAMES
        )
    return violations


def _runtime_global_attr_violations(tree: ast.AST, relative_path: str, constant_aliases: set[str]) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in constant_aliases
            and node.attr in _RUNTIME_GLOBAL_NAMES
        ):
            continue
        violations.append(f"{relative_path}:{node.lineno} uses constants.{node.attr}")
    return violations


def _collect_runtime_global_violations() -> list[str]:
    violations: list[str] = []
    for source_path in _runtime_source_files():
        relative_path = source_path.relative_to(_repo_root()).as_posix()
        if relative_path in _RUNTIME_GLOBAL_ALLOWLIST:
            continue

        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        constant_aliases = _runtime_constant_aliases(tree)
        violations.extend(_runtime_global_import_violations(tree, relative_path))
        violations.extend(_runtime_global_attr_violations(tree, relative_path, constant_aliases))

    return sorted(set(violations))


def _collect_runtime_env_violations() -> list[str]:
    violations: list[str] = []
    for source_path in _runtime_source_files():
        relative_path = source_path.relative_to(_repo_root()).as_posix()
        if relative_path in _RUNTIME_ENV_ALLOWLIST:
            continue

        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
                and node.func.attr == "getenv"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in _RUNTIME_ENV_KEYS
            ):
                violations.append(f"{relative_path}:{node.lineno} reads os.getenv({node.args[0].value!r})")

            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "os"
                and node.value.attr == "environ"
                and isinstance(node.slice, ast.Constant)
                and node.slice.value in _RUNTIME_ENV_KEYS
            ):
                violations.append(f"{relative_path}:{node.lineno} reads os.environ[{node.slice.value!r}]")

            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Attribute)
                and isinstance(node.func.value.value, ast.Name)
                and node.func.value.value.id == "os"
                and node.func.value.attr == "environ"
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in _RUNTIME_ENV_KEYS
            ):
                violations.append(f"{relative_path}:{node.lineno} reads os.environ.get({node.args[0].value!r})")

    return sorted(set(violations))


def _patch_config_globals(
    monkeypatch: pytest.MonkeyPatch,
    *,
    env: str | None = None,
    search_paths: list[Path] | None = None,
) -> None:
    """Patch module-level config globals used by find_config / config_search_locations."""
    if env is None:
        monkeypatch.delenv("MINDROOM_CONFIG_PATH", raising=False)
    else:
        monkeypatch.setenv("MINDROOM_CONFIG_PATH", env)
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
        resolved = constants_mod.resolve_config_relative_path(
            "openclaw_data/memory",
            constants_mod.resolve_runtime_paths(config_path=config_path),
        )
        assert resolved == (tmp_path / "cfg" / "openclaw_data" / "memory").resolve()

    def test_absolute_path_is_preserved(self, tmp_path: Path) -> None:
        """Absolute paths should stay absolute."""
        absolute_path = tmp_path / "knowledge"
        resolved = constants_mod.resolve_config_relative_path(
            absolute_path,
            constants_mod.resolve_runtime_paths(config_path=tmp_path / "config.yaml"),
        )
        assert resolved == absolute_path.resolve()

    def test_environment_variables_are_expanded_before_resolution(
        self,
        tmp_path: Path,
    ) -> None:
        """Config path resolution should treat `${MINDROOM_STORAGE_PATH}` as the runtime storage root."""
        storage_root = tmp_path / "runtime-storage"
        runtime_paths = constants_mod.resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            process_env={"MINDROOM_STORAGE_PATH": str(storage_root)},
        )

        resolved = constants_mod.resolve_config_relative_path(
            "${MINDROOM_STORAGE_PATH}/agents/mind/workspace/mind_data/memory",
            runtime_paths,
        )

        assert resolved == storage_root.resolve() / "agents" / "mind" / "workspace" / "mind_data" / "memory"

    def test_rejects_non_runtime_placeholders(self, tmp_path: Path) -> None:
        """Config-relative paths should fail closed for unsupported env placeholders."""
        with pytest.raises(ValueError, match="only support"):
            constants_mod.resolve_config_relative_path(
                "${HOME}/kb",
                constants_mod.resolve_runtime_paths(config_path=tmp_path / "config.yaml"),
            )

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
            constants_mod.resolve_runtime_paths(config_path=config_path),
        )
        assert resolved == custom_storage.resolve() / "kb"

    def test_explicit_config_path_uses_its_own_sibling_env_over_active_runtime_storage(
        self,
        tmp_path: Path,
    ) -> None:
        """Alternate config loads must not inherit the active runtime storage root."""
        active_dir = tmp_path / "active"
        other_dir = tmp_path / "other"
        active_dir.mkdir(parents=True, exist_ok=True)
        other_dir.mkdir(parents=True, exist_ok=True)
        active_config = active_dir / "config.yaml"
        other_config = other_dir / "config.yaml"
        for path in (active_config, other_config):
            path.write_text(
                "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
                encoding="utf-8",
            )
        (active_dir / ".env").write_text(
            f"MINDROOM_STORAGE_PATH={active_dir / 'storage-active'}\n",
            encoding="utf-8",
        )
        (other_dir / ".env").write_text(
            f"MINDROOM_STORAGE_PATH={other_dir / 'storage-other'}\n",
            encoding="utf-8",
        )

        constants_mod.set_runtime_paths(config_path=active_config)

        resolved = constants_mod.resolve_config_relative_path(
            "${MINDROOM_STORAGE_PATH}/kb",
            constants_mod.resolve_runtime_paths(config_path=other_config),
        )

        assert resolved == (other_dir / "storage-other" / "kb").resolve()

    def test_config_from_yaml_does_not_override_existing_shell_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Config loads should not mutate already-exported process env values."""
        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
            encoding="utf-8",
        )
        (config_dir / ".env").write_text(
            "OPENAI_API_KEY=from-file\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("OPENAI_API_KEY", "from-shell")

        config = Config.from_yaml(config_path)

        assert (
            constants_mod.runtime_env_value("OPENAI_API_KEY", runtime_paths=config.require_runtime_paths())
            == "from-shell"
        )

    def test_explicit_runtime_paths_use_process_env_for_non_path_values(
        self,
        tmp_path: Path,
    ) -> None:
        """Explicit RuntimePaths should carry non-path env values without ambient fallbacks."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
            encoding="utf-8",
        )

        runtime_paths = constants_mod.resolve_runtime_paths(
            config_path=config_path,
            process_env={
                "MINDROOM_NAMESPACE": "alpha1234",
                "MATRIX_HOMESERVER": "https://hs.example",
                "MATRIX_SERVER_NAME": "server.example",
            },
        )

        config = Config.from_yaml(runtime_paths=runtime_paths)

        assert constants_mod.runtime_mindroom_namespace(runtime_paths=runtime_paths) == "alpha1234"
        assert constants_mod.runtime_matrix_homeserver(runtime_paths=runtime_paths) == "https://hs.example"
        assert constants_mod.runtime_matrix_server_name(runtime_paths=runtime_paths) == "server.example"
        assert config.domain == "server.example"

    def test_config_domain_uses_sibling_env_matrix_homeserver(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Config.domain should resolve MATRIX_HOMESERVER from the explicit config's sibling `.env`."""
        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
            encoding="utf-8",
        )
        (config_dir / ".env").write_text(
            "MATRIX_HOMESERVER=https://example.org\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("MATRIX_HOMESERVER", raising=False)

        config = Config.from_yaml(config_path)

        assert config.domain == "example.org"

    def test_config_from_yaml_rejects_runtime_paths_and_config_path_together(self, tmp_path: Path) -> None:
        """Config loads should not accept duplicated runtime context."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
            encoding="utf-8",
        )
        runtime_paths = constants_mod.resolve_runtime_paths(config_path=config_path)

        with pytest.raises(ValueError, match="either runtime_paths or config_path"):
            Config.from_yaml(config_path, runtime_paths=runtime_paths)

    def test_config_from_yaml_explicit_path_does_not_inherit_activated_runtime_storage(self, tmp_path: Path) -> None:
        """Explicit path loads should stay local unless the caller passes runtime_paths."""
        config_path = tmp_path / "config.yaml"
        storage_path = tmp_path / "override-storage"
        config_path.write_text(
            "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
            encoding="utf-8",
        )

        constants_mod.set_runtime_paths(config_path=config_path, storage_path=storage_path)

        config = Config.from_yaml(config_path)

        assert config.runtime_paths is not None
        assert config.runtime_paths.storage_root == (tmp_path / "mindroom_data").resolve()

    def test_activate_runtime_paths_promotes_runtime_path_env_contract(
        self,
        tmp_path: Path,
    ) -> None:
        """Activation should return a runtime object that carries the resolved path env values."""
        config_path = tmp_path / "custom" / "config.yaml"
        storage_path = tmp_path / "override-storage"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
            encoding="utf-8",
        )

        explicit_runtime_paths = constants_mod.resolve_runtime_paths(config_path=config_path, storage_path=storage_path)
        activated = constants_mod.activate_runtime_paths(explicit_runtime_paths)

        assert activated.config_path == config_path.resolve()
        assert activated.storage_root == storage_path.resolve()
        assert activated.process_env["MINDROOM_CONFIG_PATH"] == str(config_path.resolve())
        assert activated.process_env["MINDROOM_STORAGE_PATH"] == str(storage_path.resolve())

    def test_ensure_writable_config_path_uses_active_runtime_template_env(self, tmp_path: Path) -> None:
        """Template seeding should honor MINDROOM_CONFIG_TEMPLATE from the active runtime `.env`."""
        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.yaml"
        template_path = tmp_path / "template.yaml"
        template_path.write_text("agents: {}\nmodels: {}\n", encoding="utf-8")
        (config_dir / ".env").write_text(
            f"MINDROOM_CONFIG_TEMPLATE={template_path}\n",
            encoding="utf-8",
        )

        runtime_paths = constants_mod.set_runtime_paths(config_path=config_path)

        assert constants_mod.ensure_writable_config_path(runtime_paths=runtime_paths) is True
        assert config_path.read_text(encoding="utf-8") == "agents: {}\nmodels: {}\n"


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
        runtime_paths = constants_mod.set_runtime_paths(config_path=active_config, storage_path=storage_dir)

        resolved = constants_mod.avatars_dir(runtime_paths=runtime_paths)

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

        resolved = constants_mod.avatars_dir(constants_mod.resolve_runtime_paths(config_path=explicit_config))

        assert resolved == explicit_config.parent / "avatars"

    def test_returns_workspace_avatar_when_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Workspace avatars should resolve to their on-disk path."""
        workspace_dir = tmp_path / "workspace"
        bundled_dir = tmp_path / "bundled"
        workspace_avatar = workspace_dir / "agents" / "general.png"
        workspace_avatar.parent.mkdir(parents=True)
        workspace_avatar.write_bytes(b"workspace")
        monkeypatch.setattr(constants_mod, "avatars_dir", lambda _runtime_paths: workspace_dir)
        monkeypatch.setattr(constants_mod, "bundled_avatars_dir", lambda: bundled_dir)

        resolved = constants_mod.resolve_avatar_path("agents", "general", constants_mod.resolve_runtime_paths())

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
        monkeypatch.setattr(constants_mod, "avatars_dir", lambda _runtime_paths: workspace_dir)
        monkeypatch.setattr(constants_mod, "bundled_avatars_dir", lambda: bundled_dir)

        resolved = constants_mod.resolve_avatar_path("rooms", "lobby", constants_mod.resolve_runtime_paths())

        assert resolved == bundled_avatar

    def test_returns_workspace_avatar_path_when_no_avatar_exists(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Missing avatars should resolve to the workspace location generation writes to."""
        workspace_dir = tmp_path / "workspace"
        bundled_dir = tmp_path / "bundled"
        monkeypatch.setattr(constants_mod, "avatars_dir", lambda _runtime_paths: workspace_dir)
        monkeypatch.setattr(constants_mod, "bundled_avatars_dir", lambda: bundled_dir)

        resolved = constants_mod.resolve_avatar_path("rooms", "nonexistent", constants_mod.resolve_runtime_paths())

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
        runtime_paths = constants_mod.set_runtime_paths(config_path=active_config, storage_path=storage_dir)
        monkeypatch.setattr(constants_mod, "bundled_avatars_dir", lambda: bundled_dir)

        resolved = constants_mod.resolve_avatar_path("rooms", "nonexistent", runtime_paths)

        assert resolved == storage_dir / "avatars" / "rooms" / "nonexistent.png"


class TestStoragePathResolution:
    """Tests for primary runtime storage-root canonicalization."""

    def test_storage_path_obj_from_env_is_absolute_and_cwd_stable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Relative MINDROOM_STORAGE_PATH is anchored when the runtime object is created."""
        first_cwd = tmp_path / "first"
        first_cwd.mkdir(parents=True)
        second_cwd = tmp_path / "second"
        second_cwd.mkdir(parents=True)

        with monkeypatch.context() as m:
            m.chdir(first_cwd)
            m.setenv("MINDROOM_STORAGE_PATH", "mindroom_data")
            runtime_paths = constants_mod.resolve_primary_runtime_paths(process_env=dict(os.environ))
            expected_storage_path = (first_cwd / "mindroom_data").resolve()

            assert expected_storage_path == runtime_paths.storage_root
            assert runtime_paths.storage_root.is_absolute()

            m.chdir(second_cwd)
            assert expected_storage_path == runtime_paths.storage_root


class TestRuntimeContextConsumers:
    """Regression tests for modules that follow the active runtime context."""

    def test_imported_modules_follow_runtime_context_changes_without_reload(self, tmp_path: Path) -> None:
        """Explicit runtime-aware helpers should use the passed runtime context without reloads."""
        identity_mod = importlib.import_module("mindroom.matrix.identity")
        first_dir = tmp_path / "first"
        second_dir = tmp_path / "second"
        first_dir.mkdir(parents=True)
        second_dir.mkdir(parents=True)
        first_config = first_dir / "config.yaml"
        second_config = second_dir / "config.yaml"
        first_config.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
        second_config.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
        (first_dir / ".env").write_text(
            "MINDROOM_NAMESPACE=alpha1234\nMATRIX_SERVER_NAME=alpha.example\n",
            encoding="utf-8",
        )
        (second_dir / ".env").write_text(
            "MINDROOM_NAMESPACE=beta1234\nMATRIX_SERVER_NAME=beta.example\n",
            encoding="utf-8",
        )

        first_runtime_paths = constants_mod.set_runtime_paths(config_path=first_config)
        assert (
            identity_mod.agent_username_localpart("general", runtime_paths=first_runtime_paths)
            == "mindroom_general_alpha1234"
        )
        assert (
            identity_mod.extract_server_name_from_homeserver(
                "http://localhost:8008",
                runtime_paths=first_runtime_paths,
            )
            == "alpha.example"
        )

        second_runtime_paths = constants_mod.set_runtime_paths(config_path=second_config)
        assert (
            identity_mod.agent_username_localpart("general", runtime_paths=second_runtime_paths)
            == "mindroom_general_beta1234"
        )
        assert (
            identity_mod.extract_server_name_from_homeserver(
                "http://localhost:8008",
                runtime_paths=second_runtime_paths,
            )
            == "beta.example"
        )

    def test_runtime_path_consumers_follow_activated_runtime_paths(self, tmp_path: Path) -> None:
        """Runtime consumers should switch roots when activation changes the runtime context."""
        first_config = tmp_path / "first" / "config.yaml"
        second_config = tmp_path / "second" / "config.yaml"
        first_storage = tmp_path / "storage-a"
        second_storage = tmp_path / "storage-b"
        first_config.parent.mkdir(parents=True)
        second_config.parent.mkdir(parents=True)
        first_config.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
        second_config.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")

        first_runtime_paths = constants_mod.set_runtime_paths(config_path=first_config, storage_path=first_storage)
        tracker_a = ResponseTracker("general", base_path=first_storage / "tracking")
        MatrixState().save(runtime_paths=first_runtime_paths)
        assert tracker_a.base_path == first_storage / "tracking"
        assert tracker_a._responses_file == first_storage / "tracking" / "general_responded.json"
        assert constants_mod.matrix_state_file(runtime_paths=first_runtime_paths) == first_storage / "matrix_state.yaml"

        second_runtime_paths = constants_mod.set_runtime_paths(
            config_path=second_config,
            storage_path=second_storage,
        )
        tracker_b = ResponseTracker("general", base_path=second_storage / "tracking")
        MatrixState().save(runtime_paths=second_runtime_paths)
        assert tracker_b.base_path == second_storage / "tracking"
        assert tracker_b._responses_file == second_storage / "tracking" / "general_responded.json"
        assert (
            constants_mod.matrix_state_file(runtime_paths=second_runtime_paths) == second_storage / "matrix_state.yaml"
        )


class TestRuntimeGuardrails:
    """Lint-style tests that keep runtime-path access centralized."""

    def test_no_new_import_time_runtime_global_usage_outside_allowlist(self) -> None:
        """Prevent new imports or attribute reads of mutable runtime globals."""
        violations = _collect_runtime_global_violations()
        assert not violations, "\n".join(violations)

    def test_no_new_direct_runtime_env_reads_outside_allowlist(self) -> None:
        """Prevent new direct runtime-varying env reads outside approved modules."""
        violations = _collect_runtime_env_violations()
        assert not violations, "\n".join(violations)
