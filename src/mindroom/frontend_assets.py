"""Helpers for locating or building the bundled dashboard assets."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

_PACKAGE_FRONTEND_DIR = Path(__file__).resolve().parent / "_frontend"
_REPO_FRONTEND_SOURCE_DIR = Path(__file__).resolve().parents[2] / "frontend"
_REPO_FRONTEND_DIST_DIR = _REPO_FRONTEND_SOURCE_DIR / "dist"
_FRONTEND_BUILD_ATTEMPTED = False


def _resolve_frontend_dist_dir(runtime_paths: RuntimePaths) -> Path | None:
    """Return the bundled or locally built dashboard directory if it exists."""
    override = runtime_paths.env_value("MINDROOM_FRONTEND_DIST")
    if override:
        override_path = Path(override).expanduser().resolve()
        return override_path if override_path.is_dir() else None

    for candidate in (_PACKAGE_FRONTEND_DIR, _REPO_FRONTEND_DIST_DIR):
        if candidate.is_dir():
            return candidate

    return None


def ensure_frontend_dist_dir(runtime_paths: RuntimePaths) -> Path | None:
    """Return dashboard assets, building the repo checkout when needed."""
    existing = _resolve_frontend_dist_dir(runtime_paths)
    if existing is not None:
        return existing

    return _build_repo_frontend_dist(runtime_paths)


def _build_repo_frontend_dist(runtime_paths: RuntimePaths) -> Path | None:
    """Build `frontend/dist` for source checkouts when Bun is available."""
    global _FRONTEND_BUILD_ATTEMPTED

    if _FRONTEND_BUILD_ATTEMPTED:
        return _resolve_frontend_dist_dir(runtime_paths)
    _FRONTEND_BUILD_ATTEMPTED = True

    if runtime_paths.env_value("MINDROOM_AUTO_BUILD_FRONTEND") == "0":
        return None

    package_json = _REPO_FRONTEND_SOURCE_DIR / "package.json"
    if not _REPO_FRONTEND_SOURCE_DIR.is_dir() or not package_json.is_file():
        return None

    bun = shutil.which("bun")
    if bun is None:
        return None

    print(f"Dashboard assets missing; building frontend in {_REPO_FRONTEND_SOURCE_DIR}")
    subprocess.run([bun, "install", "--frozen-lockfile"], check=True, cwd=_REPO_FRONTEND_SOURCE_DIR)
    subprocess.run([bun, "run", "tsc"], check=True, cwd=_REPO_FRONTEND_SOURCE_DIR)
    subprocess.run([bun, "run", "vite", "build"], check=True, cwd=_REPO_FRONTEND_SOURCE_DIR)
    return _resolve_frontend_dist_dir(runtime_paths)
