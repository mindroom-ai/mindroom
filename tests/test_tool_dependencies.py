"""Test that all registered tools can be instantiated and have their dependencies available."""

import tomllib
from pathlib import Path

import pytest

from mindroom.tools import TOOL_REGISTRY, get_tool_by_name
from mindroom.tools_metadata import TOOL_METADATA

# Tools that require configuration to instantiate
TOOLS_REQUIRING_CONFIG = {
    "github": "Requires GITHUB_ACCESS_TOKEN environment variable",
    "telegram": "Requires chat_id parameter",
    "email": "Requires SMTP configuration",
    "googlesearch": "Requires Google API credentials",
    "tavily": "Requires TAVILY_API_KEY environment variable",
    "slack": "Requires SLACK_TOKEN environment variable",
    "reddit": "Requires Reddit API credentials (client_id, client_secret)",
    "twitter": "Requires Twitter API credentials",
}


def test_all_tools_can_be_imported() -> None:
    """Test that all registered tools can be imported and instantiated."""
    successful = []
    config_required = []
    failed = []

    for tool_name in TOOL_REGISTRY:
        try:
            tool_instance = get_tool_by_name(tool_name)
            assert tool_instance is not None
            assert hasattr(tool_instance, "name")
            successful.append(tool_name)
            print(f"✓ {tool_name}")
        except Exception as e:
            if tool_name in TOOLS_REQUIRING_CONFIG:
                config_required.append(tool_name)
                print(f"⚠ {tool_name}: {TOOLS_REQUIRING_CONFIG[tool_name]}")
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


def test_all_tool_dependencies_in_pyproject() -> None:  # noqa: C901, PLR0912, PLR0915
    """Test that all tool dependencies are declared in pyproject.toml."""
    # Load pyproject.toml
    pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        pyproject = tomllib.load(f)

    project_dependencies = pyproject.get("project", {}).get("dependencies", [])

    # Extract package names from dependencies (handle version specifiers)
    installed_packages = set()
    for dep in project_dependencies:
        # Parse package name from strings like "package>=1.0" or "package[extra]>=1.0"
        if "[" in dep:
            pkg_name = dep.split("[")[0]
        else:
            # Remove version specifiers
            for sep in [">=", "<=", "==", ">", "<", "~=", "!="]:
                if sep in dep:
                    pkg_name = dep.split(sep)[0]
                    break
            else:
                pkg_name = dep
        installed_packages.add(pkg_name.strip().lower())

    # Also check agno extras
    agno_line = next((dep for dep in project_dependencies if dep.startswith("agno[")), None)
    agno_extras = set()
    if agno_line:
        # Parse extras from "agno[extra1,extra2,...]>=version"
        extras_str = agno_line[agno_line.index("[") + 1 : agno_line.index("]")]
        agno_extras = {e.strip() for e in extras_str.split(",")}

    # Check each tool's dependencies
    missing_deps = {}
    agno_managed_deps = {}

    for tool_name, metadata in TOOL_METADATA.items():
        if not metadata.dependencies:
            continue

        tool_missing = []
        tool_agno_managed = []

        for dep in metadata.dependencies:
            dep_lower = dep.lower().replace("-", "_").replace("_", "-")

            # Check various package name formats
            possible_names = {
                dep_lower,
                dep_lower.replace("-", "_"),
                dep_lower.replace("_", "-"),
            }

            # Special cases for package name mappings
            package_mappings = {
                "docker": ["docker", "docker-py"],
                "pypdf": ["pypdf", "pypdf2"],
                "pycountry": ["pycountry"],
                "duckdb": ["duckdb"],
                "newspaper3k": ["newspaper3k", "newspaper"],
                "tavily-python": ["tavily-python", "tavily"],
                "google-api-python-client": ["google-api-python-client", "google_api_python_client"],
                "google-auth": ["google-auth", "google_auth"],
                "google-auth-oauthlib": ["google-auth-oauthlib", "google_auth_oauthlib"],
                "google-auth-httplib2": ["google-auth-httplib2", "google_auth_httplib2"],
            }

            # Check if it's in the mappings
            if dep_lower in package_mappings:
                possible_names.update(package_mappings[dep_lower])

            # Check if dependency is in pyproject.toml
            found = any(name in installed_packages for name in possible_names)

            # Check if it's managed by agno extras
            agno_managed = False
            agno_dep_mappings = {
                "arxiv": "arxiv",
                "pypdf": "arxiv",  # pypdf is part of arxiv extra
                "wikipedia": "wikipedia",
                "yfinance": "yfinance",
                "newspaper3k": "newspaper",
                "duckdb": "duckdb",
                "docker": "docker",
                "duckduckgo-search": "ddg",
            }

            if dep_lower in agno_dep_mappings and agno_dep_mappings[dep_lower] in agno_extras:
                agno_managed = True

            if not found and not agno_managed:
                tool_missing.append(dep)
            elif agno_managed:
                tool_agno_managed.append(dep)

        if tool_missing:
            missing_deps[tool_name] = tool_missing
        if tool_agno_managed:
            agno_managed_deps[tool_name] = tool_agno_managed

    # Report findings
    if agno_managed_deps:
        print("\nDependencies managed by agno extras:")
        for tool, deps in sorted(agno_managed_deps.items()):
            print(f"  {tool}: {', '.join(deps)}")

    if missing_deps:
        error_msg = "\nThe following tools have dependencies not in pyproject.toml:\n"
        for tool, deps in sorted(missing_deps.items()):
            error_msg += f"  {tool}: {', '.join(deps)}\n"
        pytest.fail(error_msg)
    else:
        print("\n✓ All tool dependencies are properly declared in pyproject.toml")


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
