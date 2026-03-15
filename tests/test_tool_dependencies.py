"""Test tool dependency resolution, auto-install logic, and pyproject sync."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.dependencies import (
    _PIP_TO_IMPORT,
    _install_optional_extras,
    _install_via_uv_sync,
    _pip_name_to_import,
    auto_install_enabled,
    auto_install_optional_extra,
    check_deps_installed,
    install_command_for_current_python,
)
from mindroom.tool_system.metadata import (
    _TOOL_REGISTRY,
    TOOL_METADATA,
    SetupType,
    ToolCategory,
    ToolMetadata,
    ToolStatus,
    get_tool_by_name,
)

HOOK_SCRIPT = Path(__file__).parent.parent / ".github" / "scripts" / "check_tool_extras_sync.py"
TEST_RUNTIME_PATHS = resolve_runtime_paths(config_path=Path("config.yaml"))


def test_all_tools_can_be_imported() -> None:
    """Test that all registered tools can be imported and instantiated."""
    failed = []
    # OpenBB is still imported by the config-sync suite; skipping it here avoids
    # a duplicate parallel import against the same shared CI virtualenv.
    skip_parallel_imports = {"openbb"}

    for tool_name in _TOOL_REGISTRY:
        if tool_name in skip_parallel_imports:
            continue
        metadata = TOOL_METADATA.get(tool_name)
        requires_config = metadata and metadata.status == ToolStatus.REQUIRES_CONFIG

        try:
            tool_instance = get_tool_by_name(tool_name, TEST_RUNTIME_PATHS)
            assert tool_instance is not None
            assert hasattr(tool_instance, "name")
        except Exception as e:
            if not requires_config:
                failed.append((tool_name, str(e)))

    if failed:
        error_msg = "\nThe following tools failed:\n"
        for tool_name, error in failed:
            error_msg += f"  - {tool_name}: {error}\n"
        pytest.fail(error_msg)


def test_tool_extras_in_sync_with_pyproject() -> None:
    """Run the pre-commit hook script to verify tool registrations match pyproject.toml.

    This reuses the single source of truth (.github/scripts/check_tool_extras_sync.py)
    rather than reimplementing the check, ensuring CI catches sync issues even though
    pre-commit hooks don't run in CI.
    """
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        pytest.fail(f"Tool extras out of sync with pyproject.toml:\n{output}")


def test_full_runtime_image_keeps_sentence_transformers_runtime_only() -> None:
    """The full image should not preinstall the runtime-only sentence_transformers extra."""
    dockerfile = Path("local/instances/deploy/Dockerfile.mindroom").read_text(encoding="utf-8")
    assert "--all-extras --no-extra sentence_transformers" in dockerfile


@pytest.mark.parametrize(
    "dockerfile_path",
    [
        Path("local/instances/deploy/Dockerfile.mindroom"),
        Path("local/instances/deploy/Dockerfile.mindroom-minimal"),
    ],
)
def test_runtime_images_copy_workspace_avatars(dockerfile_path: Path) -> None:
    """Runtime images should still ship workspace avatar assets under /app/avatars."""
    dockerfile = dockerfile_path.read_text(encoding="utf-8")

    assert "COPY avatars /app/avatars" in dockerfile


def test_tools_requiring_config_metadata() -> None:
    """Test that tools marked REQUIRES_CONFIG have config_fields or auth_provider."""
    inconsistent = []

    for tool_name, metadata in TOOL_METADATA.items():
        if (
            metadata.status == ToolStatus.REQUIRES_CONFIG
            and not metadata.config_fields
            and metadata.auth_provider is None
        ):
            inconsistent.append(tool_name)

    if inconsistent:
        pytest.fail(
            "Tools with REQUIRES_CONFIG but no config_fields or auth_provider:\n"
            + "\n".join(f"  - {name}" for name in sorted(inconsistent)),
        )


def test_get_tool_by_name_retries_after_auto_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tool loading should retry once after auto-install succeeds."""
    tool_name = "test_auto_install_tool"
    calls = {"count": 0}

    class DummyToolkit:
        name = "dummy"

    class DummyCredentialsManager:
        def __init__(self) -> None:
            self.base_path = Path("/var/empty/mindroom-dummy-credentials")
            self.shared_base_path = self.base_path

        def load_credentials(self, _tool_name: str) -> dict[str, str]:
            return {}

        def shared_manager(self) -> DummyCredentialsManager:
            return self

    def flaky_factory() -> type[DummyToolkit]:
        calls["count"] += 1
        if calls["count"] == 1:
            msg = "missing dependency"
            raise ImportError(msg)
        return DummyToolkit

    _TOOL_REGISTRY[tool_name] = flaky_factory
    TOOL_METADATA[tool_name] = ToolMetadata(
        name=tool_name,
        display_name="Auto Install Test Tool",
        description="Temporary test tool",
        category=ToolCategory.DEVELOPMENT,
        status=ToolStatus.AVAILABLE,
        setup_type=SetupType.NONE,
        config_fields=[],
        dependencies=[],
    )

    monkeypatch.setattr(
        "mindroom.tool_system.metadata.auto_install_tool_extra",
        lambda name, runtime_paths: name == tool_name and runtime_paths == TEST_RUNTIME_PATHS,
    )
    try:
        tool = get_tool_by_name(tool_name, TEST_RUNTIME_PATHS)
        assert isinstance(tool, DummyToolkit)
        assert calls["count"] == 2
    finally:
        _TOOL_REGISTRY.pop(tool_name, None)
        TOOL_METADATA.pop(tool_name, None)


def test_get_tool_by_name_raises_when_auto_install_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tool loading should raise ImportError when auto-install cannot help."""
    tool_name = "test_auto_install_failure_tool"

    class DummyCredentialsManager:
        def __init__(self) -> None:
            self.base_path = Path("/var/empty/mindroom-dummy-credentials")
            self.shared_base_path = self.base_path

        def load_credentials(self, _tool_name: str) -> dict[str, str]:
            return {}

        def shared_manager(self) -> DummyCredentialsManager:
            return self

    def failing_factory() -> type:
        msg = "dependency missing forever"
        raise ImportError(msg)

    _TOOL_REGISTRY[tool_name] = failing_factory
    TOOL_METADATA[tool_name] = ToolMetadata(
        name=tool_name,
        display_name="Auto Install Failure Tool",
        description="Temporary failing tool",
        category=ToolCategory.DEVELOPMENT,
        status=ToolStatus.AVAILABLE,
        setup_type=SetupType.NONE,
        config_fields=[],
        dependencies=[],
    )

    monkeypatch.setattr(
        "mindroom.tool_system.metadata.auto_install_tool_extra",
        lambda _name, _runtime_paths: False,
    )
    try:
        with pytest.raises(ImportError, match="dependency missing forever"):
            get_tool_by_name(tool_name, TEST_RUNTIME_PATHS)
    finally:
        _TOOL_REGISTRY.pop(tool_name, None)
        TOOL_METADATA.pop(tool_name, None)


def test_check_deps_installed_positive_and_negative() -> None:
    """check_deps_installed returns True for installed packages, False when any is missing."""
    assert check_deps_installed(["pytest"])
    assert not check_deps_installed(["nonexistent_package_xyz_123"])


@pytest.mark.parametrize(("pip_name", "expected_import"), list(_PIP_TO_IMPORT.items()))
def test_pip_to_import_mapping(pip_name: str, expected_import: str) -> None:
    """_pip_name_to_import returns the correct import name for every entry in _PIP_TO_IMPORT."""
    assert _pip_name_to_import(pip_name) == expected_import


def test_pip_to_import_passthrough() -> None:
    """_pip_name_to_import falls back to replacing dashes with underscores."""
    assert _pip_name_to_import("some-normal-package") == "some_normal_package"


def test_pip_to_import_strips_version_specifier() -> None:
    """_pip_name_to_import strips version specifiers before lookup."""
    assert _pip_name_to_import("pyyaml>=6.0") == "yaml"
    assert _pip_name_to_import("requests>=2.0") == "requests"


def test_pip_to_import_mapping_completeness() -> None:
    """Every entry in _PIP_TO_IMPORT should have a key that differs from the naive transform."""
    for pip_name, import_name in _PIP_TO_IMPORT.items():
        naive = pip_name.replace("-", "_")
        assert naive != import_name, (
            f"Mapping entry '{pip_name}' -> '{import_name}' is redundant (naive transform already gives '{naive}')"
        )


def test_install_via_uv_sync_targets_active_virtualenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uv sync should target the active virtualenv when one is in use."""
    captured: dict[str, object] = {}

    def fake_run(
        cmd: list[str],
        *,
        check: bool,
        capture_output: bool,
        cwd: Path,
        env: dict[str, str],
    ) -> SimpleNamespace:
        captured["cmd"] = cmd
        captured["check"] = check
        captured["capture_output"] = capture_output
        captured["cwd"] = cwd
        captured["env"] = env
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("mindroom.tool_system.dependencies._in_virtualenv", lambda: True)
    monkeypatch.setattr("mindroom.tool_system.dependencies.subprocess.run", fake_run)

    assert _install_via_uv_sync(["wikipedia"], quiet=True)
    assert captured["cmd"] == [
        "uv",
        "sync",
        "--locked",
        "--inexact",
        "--no-dev",
        "--active",
        "--extra",
        "wikipedia",
        "-q",
    ]
    assert captured["check"] is False
    assert captured["capture_output"] is True
    assert isinstance(captured["cwd"], Path)
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["VIRTUAL_ENV"] == sys.prefix


def test_install_command_for_current_python_uses_uv_system_outside_virtualenv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-venv uv installs must target the system interpreter explicitly."""
    monkeypatch.setattr("mindroom.tool_system.dependencies._in_virtualenv", lambda: False)
    monkeypatch.setattr("mindroom.tool_system.dependencies._current_python_has_module", lambda _module_name: False)
    monkeypatch.setattr("mindroom.tool_system.dependencies.shutil.which", lambda _binary: "/usr/bin/uv")

    assert install_command_for_current_python() == [
        "uv",
        "pip",
        "install",
        "--python",
        sys.executable,
        "--system",
    ]


def test_install_command_for_current_python_prefers_current_python_uv_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When PATH lacks uv, the current interpreter should still be able to run `python -m uv`."""
    monkeypatch.setattr("mindroom.tool_system.dependencies._in_virtualenv", lambda: False)
    monkeypatch.setattr(
        "mindroom.tool_system.dependencies._current_python_has_module",
        lambda module_name: module_name == "uv",
    )
    monkeypatch.setattr("mindroom.tool_system.dependencies.shutil.which", lambda _binary: None)

    assert install_command_for_current_python() == [
        sys.executable,
        "-m",
        "uv",
        "pip",
        "install",
        "--python",
        sys.executable,
        "--system",
    ]


def test_install_command_for_current_python_uses_pip_user_outside_virtualenv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-venv pip installs must avoid writing to the managed interpreter directly."""
    monkeypatch.setattr("mindroom.tool_system.dependencies._in_virtualenv", lambda: False)
    monkeypatch.setattr("mindroom.tool_system.dependencies._current_python_has_module", lambda _module_name: False)
    monkeypatch.setattr("mindroom.tool_system.dependencies.shutil.which", lambda _binary: None)

    assert install_command_for_current_python() == [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--user",
    ]


def test_auto_install_optional_extra_supports_non_tool_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-tool optional extras should use the same runtime install path."""
    monkeypatch.setattr("mindroom.tool_system.dependencies.auto_install_enabled", lambda _runtime_paths: True)
    monkeypatch.setattr(
        "mindroom.tool_system.dependencies._available_optional_extras",
        lambda: {"sentence_transformers"},
    )
    monkeypatch.setattr(
        "mindroom.tool_system.dependencies._install_optional_extras",
        lambda extras, *, quiet=False: extras == ["sentence_transformers"] and quiet,
    )

    assert auto_install_optional_extra("sentence_transformers", TEST_RUNTIME_PATHS)


def test_auto_install_optional_extra_matches_installed_metadata_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normalized extra names should resolve when installed metadata uses hyphens."""
    monkeypatch.setattr("mindroom.tool_system.dependencies.auto_install_enabled", lambda _runtime_paths: True)
    monkeypatch.setattr(
        "mindroom.tool_system.dependencies._available_optional_extras",
        lambda: {"sentence-transformers"},
    )
    monkeypatch.setattr(
        "mindroom.tool_system.dependencies._install_optional_extras",
        lambda extras, *, quiet=False: extras == ["sentence-transformers"] and quiet,
    )

    assert auto_install_optional_extra("sentence_transformers", TEST_RUNTIME_PATHS)


def test_auto_install_enabled_uses_runtime_env(tmp_path: Path) -> None:
    """Auto-install disable flags should come from the explicit runtime context."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_NO_AUTO_INSTALL_TOOLS": "1"},
    )

    assert auto_install_enabled(runtime_paths) is False


def test_install_optional_extras_skips_uv_sync_outside_virtualenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside virtualenvs, optional extras should install via pip/uv pip instead of uv sync."""
    calls = {"sync": 0, "env": 0}

    def fake_install_via_uv_sync(_extras: list[str], *, quiet: bool) -> bool:  # noqa: ARG001
        calls["sync"] += 1
        return True

    def fake_install_in_environment(_extras: list[str], *, quiet: bool) -> bool:  # noqa: ARG001
        calls["env"] += 1
        return True

    monkeypatch.setattr("mindroom.tool_system.dependencies._is_uv_tool_install", lambda: False)
    monkeypatch.setattr("mindroom.tool_system.dependencies._has_lockfile", lambda: True)
    monkeypatch.setattr("mindroom.tool_system.dependencies._in_virtualenv", lambda: False)
    monkeypatch.setattr("mindroom.tool_system.dependencies.shutil.which", lambda _binary: "/usr/bin/uv")
    monkeypatch.setattr("mindroom.tool_system.dependencies._install_via_uv_sync", fake_install_via_uv_sync)
    monkeypatch.setattr("mindroom.tool_system.dependencies._install_in_environment", fake_install_in_environment)

    assert _install_optional_extras(["wikipedia"], quiet=True)
    assert calls["sync"] == 0
    assert calls["env"] == 1
