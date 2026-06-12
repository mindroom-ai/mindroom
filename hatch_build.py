"""Hatch build hook for bundling the frontend into distributable builds."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_BUN_INSTALL_MAX_ATTEMPTS = 3
_BUN_INSTALL_RETRY_DELAY_SECONDS = 2.0
_BUILD_FRONTEND_ENV = "MINDROOM_BUILD_FRONTEND"
_GIT_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1\n"


class FrontendBuildHook(BuildHookInterface):
    """Build the bundled dashboard before creating a distributable wheel."""

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        """Build dashboard assets when the current build target needs them."""
        should_build_editable = version == "editable" and _env_flag_enabled(_BUILD_FRONTEND_ENV)
        if self.target_name != "wheel" or (version != "standard" and not should_build_editable):
            return

        frontend_dir = Path(self.root) / "frontend"
        if not frontend_dir.is_dir():
            msg = f"Frontend sources not found at {frontend_dir}"
            raise RuntimeError(msg)

        bun = shutil.which("bun")
        if bun is None:
            msg = _missing_bun_message(version)
            raise RuntimeError(msg)

        output_dir = _get_output_dir(frontend_dir, self.directory, version)
        _build_frontend(frontend_dir, output_dir, bun)

        if version == "editable":
            return

        force_include = build_data.setdefault("force_include", {})
        if not isinstance(force_include, dict):
            msg = "Wheel build data force_include must be a dictionary"
            raise TypeError(msg)
        force_include[str(output_dir)] = "mindroom/_frontend"


def _env_flag_enabled(name: str) -> bool:
    """Return whether an environment flag has a truthy value."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _missing_bun_message(version: str) -> str:
    if version == "editable":
        return f"bun is required when {_BUILD_FRONTEND_ENV}=1. Install bun or unset {_BUILD_FRONTEND_ENV}."
    return (
        "bun is required to build the bundled frontend for wheel distributions. "
        "Install bun or build from a prebuilt wheel instead."
    )


def _get_output_dir(frontend_dir: Path, build_directory: str, version: str) -> Path:
    """Return the frontend build output directory for the requested build mode."""
    if version == "editable":
        return frontend_dir / "dist"
    return Path(build_directory).parent / ".frontend-build" / "frontend-dist"


def _build_frontend(frontend_dir: Path, output_dir: Path, bun: str) -> None:
    """Install frontend deps and write a production build to the output directory."""
    shutil.rmtree(output_dir, ignore_errors=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    _run_command(
        [bun, "install", "--frozen-lockfile"],
        cwd=frontend_dir,
        retries=_BUN_INSTALL_MAX_ATTEMPTS,
        retry_delay_seconds=_BUN_INSTALL_RETRY_DELAY_SECONDS,
    )
    _run_command([bun, "run", "tsc"], cwd=frontend_dir)
    _run_command(
        [bun, "run", "vite", "build", "--outDir", str(output_dir)],
        cwd=frontend_dir,
    )
    _assert_no_git_lfs_pointers(output_dir)


def _assert_no_git_lfs_pointers(output_dir: Path) -> None:
    """Reject frontend builds that accidentally bundle unresolved Git LFS pointers."""
    pointer_files = [
        path.relative_to(output_dir).as_posix()
        for path in output_dir.rglob("*")
        if path.is_file() and path.read_bytes().startswith(_GIT_LFS_POINTER_PREFIX)
    ]
    if not pointer_files:
        return

    joined = ", ".join(pointer_files)
    msg = f"Frontend build contains unresolved Git LFS pointer assets: {joined}. Run git lfs pull before building."
    raise RuntimeError(msg)


def _run_command(
    cmd: list[str],
    *,
    cwd: Path,
    retries: int = 1,
    retry_delay_seconds: float = 0.0,
) -> None:
    """Run one build command, retrying transient subprocess failures when configured."""
    max_attempts = max(1, retries)
    for attempt in range(1, max_attempts + 1):
        try:
            subprocess.run(cmd, check=True, cwd=cwd)
        except subprocess.CalledProcessError:
            if attempt >= max_attempts:
                raise
            if retry_delay_seconds > 0:
                time.sleep(retry_delay_seconds)
        else:
            return
