"""Auto-install support for per-tool optional dependencies."""

from __future__ import annotations

import importlib.metadata as importlib_metadata
import importlib.util
import os
import shutil
import subprocess
import sys
import tomllib
from functools import cache
from pathlib import Path

_PACKAGE_NAME = "mindroom"
_RECEIPT_NAME = "uv-receipt.toml"
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Packages where the pip install name differs from the Python import name.
# Only includes cases where replacing dashes with underscores is insufficient.
_PIP_TO_IMPORT: dict[str, str] = {
    "atlassian-python-api": "atlassian",
    "beautifulsoup4": "bs4",
    "e2b-code-interpreter": "e2b",
    "firecrawl-py": "firecrawl",
    "linkup-sdk": "linkup",
    "mem0ai": "mem0",
    "newspaper4k": "newspaper",
    "google-api-python-client": "googleapiclient",
    "google-auth": "google.auth",
    "google-cloud-bigquery": "google.cloud.bigquery",
    "google-genai": "google.genai",
    "google-maps-places": "google.maps",
    "google-search-results": "serpapi",
    "psycopg-binary": "psycopg",
    "py-trello": "trello",
    "pygithub": "github",
    "pyyaml": "yaml",
    "tavily-python": "tavily",
    "spider-client": "spider",
}


def _pip_name_to_import(pip_name: str) -> str:
    """Convert a pip package name to its top-level import module name."""
    normalized = pip_name.strip().lower().replace("_", "-")
    # Strip version specifiers
    for sep in (">=", "<=", "==", ">", "<", "~=", "!="):
        if sep in normalized:
            normalized = normalized.split(sep, 1)[0].strip()
            break
    if normalized in _PIP_TO_IMPORT:
        return _PIP_TO_IMPORT[normalized]
    return normalized.replace("-", "_")


def check_deps_installed(dependencies: list[str]) -> bool:
    """Check if all dependencies are importable using find_spec (no side effects)."""
    for dep in dependencies:
        module_name = _pip_name_to_import(dep)
        if importlib.util.find_spec(module_name) is None:
            return False
    return True


def auto_install_enabled() -> bool:
    """Return whether automatic tool dependency installation is enabled."""
    return os.environ.get("MINDROOM_NO_AUTO_INSTALL_TOOLS", "").lower() not in {"1", "true", "yes"}


def _has_lockfile() -> bool:
    """Check if uv.lock is available alongside pyproject.toml."""
    return (_PROJECT_ROOT / "uv.lock").exists()


@cache
def _available_tool_extras() -> set[str]:
    """Discover available tool extras from pyproject or installed metadata."""
    pyproject_path = _PROJECT_ROOT / "pyproject.toml"
    if pyproject_path.exists():
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        optional = data.get("project", {}).get("optional-dependencies", {})
        return set(optional.keys())

    try:
        metadata = importlib_metadata.metadata(_PACKAGE_NAME)
    except importlib_metadata.PackageNotFoundError:
        return set()
    return set(metadata.get_all("Provides-Extra") or [])


def _is_uv_tool_install() -> bool:
    """Check if running from a uv tool environment."""
    return (Path(sys.prefix) / _RECEIPT_NAME).exists()


def _in_virtualenv() -> bool:
    return sys.prefix != sys.base_prefix


def _get_current_uv_tool_extras() -> list[str]:
    receipt = Path(sys.prefix) / _RECEIPT_NAME
    if not receipt.exists():
        return []
    data = tomllib.loads(receipt.read_text(encoding="utf-8"))
    requirements = data.get("tool", {}).get("requirements", [])
    for requirement in requirements:
        if requirement.get("name") == _PACKAGE_NAME:
            return requirement.get("extras", [])
    return []


def _install_via_uv_tool(extras: list[str], *, quiet: bool) -> bool:
    extras_str = ",".join(extras)
    package_spec = f"{_PACKAGE_NAME}[{extras_str}]"
    major, minor = sys.version_info[:2]
    python_version = f"{major}.{minor}"
    cmd = ["uv", "tool", "install", package_spec, "--force", "--python", python_version]
    if quiet:
        cmd.append("-q")
    result = subprocess.run(cmd, check=False)
    return result.returncode == 0


def _install_cmd() -> list[str]:
    in_venv = _in_virtualenv()
    if shutil.which("uv"):
        cmd = ["uv", "pip", "install", "--python", sys.executable]
        if not in_venv:
            cmd.append("--system")
        return cmd
    cmd = [sys.executable, "-m", "pip", "install"]
    if not in_venv:
        cmd.append("--user")
    return cmd


def _install_via_uv_sync(extras: list[str], *, quiet: bool) -> bool:
    """Install extras using ``uv sync --locked --inexact`` for pinned versions from uv.lock."""
    cmd = ["uv", "sync", "--locked", "--inexact", "--no-dev"]
    env = os.environ.copy()
    if _in_virtualenv():
        # Ensure uv targets the interpreter that is currently running MindRoom.
        cmd.append("--active")
        env["VIRTUAL_ENV"] = sys.prefix
    for extra in extras:
        cmd.extend(["--extra", extra])
    if quiet:
        cmd.append("-q")
    result = subprocess.run(cmd, check=False, capture_output=quiet, cwd=_PROJECT_ROOT, env=env)
    return result.returncode == 0


def _install_in_environment(extras: list[str], *, quiet: bool) -> bool:
    extras_str = ",".join(extras)
    package_spec = f"{_PACKAGE_NAME}[{extras_str}]"
    cmd = [*_install_cmd(), package_spec]
    result = subprocess.run(cmd, check=False, capture_output=quiet)
    return result.returncode == 0


def _install_tool_extras(extras: list[str], *, quiet: bool = False) -> bool:
    """Install one or more tool extras into the current environment.

    Prefers ``uv sync --locked`` when uv.lock is available (exact pinned versions).
    Falls back to ``uv pip install`` or ``pip install`` otherwise.
    """
    if not extras:
        return False
    if _is_uv_tool_install():
        current_extras = _get_current_uv_tool_extras()
        merged = sorted(set(current_extras) | set(extras))
        return _install_via_uv_tool(merged, quiet=quiet)
    if _has_lockfile() and shutil.which("uv") and _in_virtualenv():
        return _install_via_uv_sync(extras, quiet=quiet)
    return _install_in_environment(extras, quiet=quiet)


def auto_install_tool_extra(tool_name: str) -> bool:
    """Auto-install a tool extra when supported and enabled."""
    if not auto_install_enabled():
        return False
    if tool_name not in _available_tool_extras():
        return False
    return _install_tool_extras([tool_name], quiet=True)


def ensure_tool_deps(dependencies: list[str], tool_extra: str) -> None:
    """Ensure dependencies are installed, auto-installing via tool extra if needed.

    Uses find_spec to check availability (no side effects), then auto-installs
    and invalidates import caches if necessary.

    Raises ImportError if dependencies cannot be satisfied.
    """
    if check_deps_installed(dependencies):
        return
    if not auto_install_tool_extra(tool_extra):
        missing = ", ".join(dependencies)
        msg = f"Missing dependencies: {missing}. Install with: pip install 'mindroom[{tool_extra}]'"
        raise ImportError(msg)
    importlib.invalidate_caches()
