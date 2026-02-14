"""Auto-install support for per-tool optional dependencies."""

from __future__ import annotations

import importlib.metadata as importlib_metadata
import os
import shutil
import subprocess
import sys
import tomllib
from functools import cache
from pathlib import Path

_PACKAGE_NAME = "mindroom"
_RECEIPT_NAME = "uv-receipt.toml"


def auto_install_enabled() -> bool:
    """Return whether automatic tool dependency installation is enabled."""
    return os.environ.get("MINDROOM_NO_AUTO_INSTALL_TOOLS", "").lower() not in {"1", "true", "yes"}


@cache
def available_tool_extras() -> set[str]:
    """Discover available tool extras from pyproject or installed metadata."""
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if pyproject_path.exists():
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        optional = data.get("project", {}).get("optional-dependencies", {})
        return set(optional.keys())

    try:
        metadata = importlib_metadata.metadata(_PACKAGE_NAME)
    except importlib_metadata.PackageNotFoundError:
        return set()
    return set(metadata.get_all("Provides-Extra") or [])


def is_uv_tool_install() -> bool:
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


def _install_in_environment(extras: list[str], *, quiet: bool) -> bool:
    extras_str = ",".join(extras)
    package_spec = f"{_PACKAGE_NAME}[{extras_str}]"
    cmd = [*_install_cmd(), package_spec]
    result = subprocess.run(cmd, check=False, capture_output=quiet)
    return result.returncode == 0


def install_tool_extras(extras: list[str], *, quiet: bool = False) -> bool:
    """Install one or more tool extras into the current environment."""
    if not extras:
        return False
    if is_uv_tool_install():
        current_extras = _get_current_uv_tool_extras()
        merged = sorted(set(current_extras) | set(extras))
        return _install_via_uv_tool(merged, quiet=quiet)
    return _install_in_environment(extras, quiet=quiet)


def auto_install_tool_extra(tool_name: str) -> bool:
    """Auto-install a tool extra when supported and enabled."""
    if not auto_install_enabled():
        return False
    if tool_name not in available_tool_extras():
        return False
    return install_tool_extras([tool_name], quiet=True)
