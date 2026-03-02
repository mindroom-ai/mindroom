"""Test tool dependency resolution, auto-install logic, and pyproject sync."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from mindroom.tool_system.dependencies import (
    _PIP_TO_IMPORT,
    _install_via_uv_sync,
    _pip_name_to_import,
    check_deps_installed,
    install_tool_extras,
)
from mindroom.tool_system.metadata import (
    TOOL_METADATA,
    TOOL_REGISTRY,
    SetupType,
    ToolCategory,
    ToolMetadata,
    ToolStatus,
    get_tool_by_name,
)

HOOK_SCRIPT = Path(__file__).parent.parent / ".github" / "scripts" / "check_tool_extras_sync.py"


def test_all_tools_can_be_imported() -> None:
    """Test that all registered tools can be imported and instantiated."""
    failed = []

    for tool_name in TOOL_REGISTRY:
        metadata = TOOL_METADATA.get(tool_name)
        requires_config = metadata and metadata.status == ToolStatus.REQUIRES_CONFIG

        try:
            tool_instance = get_tool_by_name(tool_name)
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
        def load_credentials(self, _tool_name: str) -> dict[str, str]:
            return {}

    def flaky_factory() -> type[DummyToolkit]:
        calls["count"] += 1
        if calls["count"] == 1:
            msg = "missing dependency"
            raise ImportError(msg)
        return DummyToolkit

    TOOL_REGISTRY[tool_name] = flaky_factory
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

    monkeypatch.setattr("mindroom.tool_system.metadata.auto_install_tool_extra", lambda name: name == tool_name)
    monkeypatch.setattr("mindroom.tool_system.metadata.get_credentials_manager", lambda: DummyCredentialsManager())

    try:
        tool = get_tool_by_name(tool_name)
        assert isinstance(tool, DummyToolkit)
        assert calls["count"] == 2
    finally:
        TOOL_REGISTRY.pop(tool_name, None)
        TOOL_METADATA.pop(tool_name, None)


def test_get_tool_by_name_raises_when_auto_install_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tool loading should raise ImportError when auto-install cannot help."""
    tool_name = "test_auto_install_failure_tool"

    class DummyCredentialsManager:
        def load_credentials(self, _tool_name: str) -> dict[str, str]:
            return {}

    def failing_factory() -> type:
        msg = "dependency missing forever"
        raise ImportError(msg)

    TOOL_REGISTRY[tool_name] = failing_factory
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

    monkeypatch.setattr("mindroom.tool_system.metadata.auto_install_tool_extra", lambda _name: False)
    monkeypatch.setattr("mindroom.tool_system.metadata.get_credentials_manager", lambda: DummyCredentialsManager())

    try:
        with pytest.raises(ImportError, match="dependency missing forever"):
            get_tool_by_name(tool_name)
    finally:
        TOOL_REGISTRY.pop(tool_name, None)
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


def test_install_tool_extras_skips_uv_sync_outside_virtualenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside virtualenvs, tool extras should install via pip/uv pip instead of uv sync."""
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

    assert install_tool_extras(["wikipedia"], quiet=True)
    assert calls["sync"] == 0
    assert calls["env"] == 1
