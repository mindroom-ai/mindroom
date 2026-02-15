"""Test that all registered tools can be instantiated and have their dependencies available."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from mindroom.tool_dependencies import _PIP_TO_IMPORT, _pip_name_to_import, check_deps_installed
from mindroom.tools_metadata import (
    TOOL_METADATA,
    TOOL_REGISTRY,
    SetupType,
    ToolCategory,
    ToolMetadata,
    ToolStatus,
    get_tool_by_name,
)


def _dependency_name(spec: str) -> str:
    part = spec.split(";", 1)[0].strip()
    if "@" in part and "git+" in part:
        part = part.split("@", 1)[0].strip()
    if "[" in part:
        part = part.split("[", 1)[0].strip()
    else:
        for sep in [">=", "<=", "==", ">", "<", "~=", "!="]:
            if sep in part:
                part = part.split(sep, 1)[0].strip()
                break
    return part.lower().replace("_", "-")


def _normalize_tool_dep_name(name: str) -> str:
    aliases = {
        "e2b_code_interpreter": "e2b-code-interpreter",
        "exa_py": "exa-py",
        "lxml_html_clean": "lxml-html-clean",
        "pygithub": "pygithub",
        "youtube_transcript_api": "youtube-transcript-api",
    }
    normalized = aliases.get(name.lower(), name.lower())
    return normalized.replace("_", "-")


def test_all_tools_can_be_imported() -> None:
    """Test that all registered tools can be imported and instantiated."""
    successful = []
    config_required = []
    failed = []

    for tool_name in TOOL_REGISTRY:
        # Check if tool requires configuration based on metadata
        metadata = TOOL_METADATA.get(tool_name)
        requires_config = metadata and metadata.status == ToolStatus.REQUIRES_CONFIG

        try:
            tool_instance = get_tool_by_name(tool_name)
            assert tool_instance is not None
            assert hasattr(tool_instance, "name")
            successful.append(tool_name)
            print(f"✓ {tool_name}")
        except Exception as e:
            if requires_config:
                config_required.append(tool_name)
                # Build a helpful message from metadata
                if metadata and metadata.config_fields:
                    field_names = [field.name for field in metadata.config_fields]
                    config_msg = f"Requires: {', '.join(field_names)}"
                else:
                    config_msg = "Requires configuration"
                print(f"⚠ {tool_name}: {config_msg}")
            else:
                failed.append((tool_name, str(e)))
                print(f"✗ {tool_name}: {e}")

    # Summary
    print("\nSummary:")
    print(f"  Successful: {len(successful)}")
    print(f"  Config required: {len(config_required)}")
    print(f"  Failed: {len(failed)}")

    # Fail the test if any tools failed (excluding config-required ones)
    if failed:
        error_msg = "\nThe following tools failed:\n"
        for tool_name, error in failed:
            error_msg += f"  - {tool_name}: {error}\n"
        pytest.fail(error_msg)


def test_all_tool_dependencies_in_pyproject() -> None:  # noqa: C901
    """Test that each tool dependency is provided by base deps or the tool's optional group."""
    pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        pyproject = tomllib.load(f)

    project_section = pyproject.get("project", {})
    base_dependencies = project_section.get("dependencies", [])
    optional_dependencies = project_section.get("optional-dependencies", {})

    base_dependency_names = {_dependency_name(spec) for spec in base_dependencies}
    optional_dependency_names: dict[str, set[str]] = {}
    for extra_name, extra_specs in optional_dependencies.items():
        optional_dependency_names[extra_name] = {_dependency_name(spec) for spec in extra_specs}

    missing_optional_groups = sorted(tool_name for tool_name in TOOL_METADATA if tool_name not in optional_dependencies)
    if missing_optional_groups:
        pytest.fail(
            "Missing optional dependency groups for tools:\n"
            + "\n".join(f"  - {tool_name}" for tool_name in missing_optional_groups),
        )

    # Packages that conflict with project-pinned versions and can't be added to pyproject.toml
    # brave-search: requires tenacity<9 but project pins tenacity>=9.1.2
    known_conflicts = {"brave-search"}
    missing_dependencies: dict[str, list[str]] = {}

    for tool_name, metadata in sorted(TOOL_METADATA.items()):
        if not metadata.dependencies:
            continue

        extra_names = optional_dependency_names.get(tool_name, set())
        tool_missing: list[str] = []
        for dep in metadata.dependencies:
            normalized = _normalize_tool_dep_name(dep)
            if normalized in known_conflicts:
                continue
            if normalized in base_dependency_names:
                continue
            if normalized in extra_names:
                continue
            tool_missing.append(dep)

        if tool_missing:
            missing_dependencies[tool_name] = tool_missing

    if missing_dependencies:
        error_msg = "\nThe following tool dependencies are missing from base deps and tool extras:\n"
        for tool, deps in sorted(missing_dependencies.items()):
            error_msg += f"  {tool}: {', '.join(deps)}\n"
        pytest.fail(error_msg)
    print("\n✓ All tool dependencies are covered by base or per-tool optional dependencies")


def test_no_unused_dependencies() -> None:  # noqa: C901, PLR0912
    """Test that all dependencies in pyproject.toml are actually used by tools."""
    # Load pyproject.toml
    pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        pyproject = tomllib.load(f)

    project_dependencies = pyproject.get("project", {}).get("dependencies", [])

    # Collect all declared dependencies from tools
    used_packages = set()
    for metadata in TOOL_METADATA.values():
        if metadata.dependencies:
            for dep in metadata.dependencies:
                # Normalize package name
                dep_lower = dep.lower().replace("_", "-")
                used_packages.add(dep_lower)

    # Add core dependencies that are always needed
    core_deps = {
        "agno",  # Core agno package
        "chromadb",  # Memory storage
        "diskcache",  # Caching
        "fastapi",  # Web API
        "loguru",  # Logging
        "matrix-nio",  # Matrix client
        "mcp",  # Model Context Protocol
        "mem0ai",  # Memory
        "pydantic",  # Data validation
        "python-dotenv",  # Environment variables
        "pyyaml",  # YAML config
        "rich",  # CLI output
        "structlog",  # Structured logging
        "typer",  # CLI framework
        "uvicorn",  # ASGI server
        "watchdog",  # File watching
        "markdown",  # Matrix message formatting
        "spotipy",  # Spotify integration (future)
    }

    # Check each dependency
    potentially_unused = []
    for dep in project_dependencies:
        # Parse package name
        if "[" in dep:
            pkg_name = dep.split("[")[0]
        else:
            for sep in [">=", "<=", "==", ">", "<", "~=", "!="]:
                if sep in dep:
                    pkg_name = dep.split(sep)[0]
                    break
            else:
                pkg_name = dep

        pkg_name = pkg_name.strip().lower()

        # Check if it's used
        if pkg_name not in used_packages and pkg_name not in core_deps:
            # Check alternate names
            alt_names = {
                pkg_name.replace("-", "_"),
                pkg_name.replace("_", "-"),
            }
            if not any(name in used_packages for name in alt_names):
                potentially_unused.append(dep)

    if potentially_unused:
        print("\nPotentially unused dependencies (may be indirect or core deps):")
        for dep in potentially_unused:
            print(f"  - {dep}")

    print(f"\nTotal dependencies: {len(project_dependencies)}")
    print(f"Tool dependencies: {len(used_packages)}")
    print(f"Core dependencies: {len(core_deps)}")


def test_tools_requiring_config_metadata() -> None:
    """Test that tools requiring configuration are properly marked in metadata."""
    tools_with_config_fields = []
    tools_with_status = []
    inconsistent_tools = []

    for tool_name, metadata in TOOL_METADATA.items():
        has_config_fields = bool(metadata.config_fields)
        has_config_status = metadata.status == ToolStatus.REQUIRES_CONFIG

        if has_config_fields and metadata.config_fields is not None:
            field_names = [field.name for field in metadata.config_fields]
            tools_with_config_fields.append((tool_name, field_names))

        if has_config_status:
            tools_with_status.append(tool_name)

        # Check for inconsistencies
        # Only check that tools marked REQUIRES_CONFIG actually have fields
        # Tools with optional config can have status AVAILABLE
        if has_config_status and not has_config_fields and metadata.auth_provider is None:
            inconsistent_tools.append((tool_name, "status is REQUIRES_CONFIG but no config_fields specified"))

    # Report findings
    print("\nTools requiring configuration:")
    for tool_name, field_names in sorted(tools_with_config_fields):
        print(f"  {tool_name}: {', '.join(field_names)}")

    print(f"\nTotal tools with config fields: {len(tools_with_config_fields)}")
    print(f"Tools marked with REQUIRES_CONFIG status: {len(tools_with_status)}")

    if inconsistent_tools:
        error_msg = "\nInconsistent configuration metadata found:\n"
        for tool_name, issue in inconsistent_tools:
            error_msg += f"  {tool_name}: {issue}\n"
        pytest.fail(error_msg)


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

    monkeypatch.setattr("mindroom.tools_metadata.auto_install_tool_extra", lambda name: name == tool_name)
    monkeypatch.setattr("mindroom.tools_metadata.get_credentials_manager", lambda: DummyCredentialsManager())

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

    monkeypatch.setattr("mindroom.tools_metadata.auto_install_tool_extra", lambda _name: False)
    monkeypatch.setattr("mindroom.tools_metadata.get_credentials_manager", lambda: DummyCredentialsManager())

    try:
        with pytest.raises(ImportError, match="dependency missing forever"):
            get_tool_by_name(tool_name)
    finally:
        TOOL_REGISTRY.pop(tool_name, None)
        TOOL_METADATA.pop(tool_name, None)


def test_check_deps_installed_with_installed_packages() -> None:
    """check_deps_installed returns True for packages that are installed."""
    assert check_deps_installed(["pytest", "loguru"])


def test_check_deps_installed_with_missing_package() -> None:
    """check_deps_installed returns False when any dependency is missing."""
    assert not check_deps_installed(["pytest", "nonexistent_package_xyz_123"])


def test_check_deps_installed_empty_list() -> None:
    """check_deps_installed returns True for an empty dependency list."""
    assert check_deps_installed([])


@pytest.mark.parametrize(
    ("pip_name", "expected_import"),
    [
        ("beautifulsoup4", "bs4"),
        ("pyyaml", "yaml"),
        ("pygithub", "github"),
        ("google-api-python-client", "googleapiclient"),
        ("e2b-code-interpreter", "e2b"),
        ("exa-py", "exa_py"),
        ("google-auth", "google.auth"),
    ],
)
def test_pip_to_import_mapping(pip_name: str, expected_import: str) -> None:
    """_pip_name_to_import correctly maps known special cases."""
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
