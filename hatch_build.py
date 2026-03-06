"""Hatch build hook for bundling the frontend into distributable builds."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class FrontendBuildHook(BuildHookInterface):
    """Build the bundled dashboard before creating a distributable wheel."""

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        """Build dashboard assets when the current build target needs them."""
        if self.target_name != "wheel" or version not in {"standard", "editable"}:
            return

        frontend_dir = Path(self.root) / "frontend"
        if not frontend_dir.is_dir():
            msg = f"Frontend sources not found at {frontend_dir}"
            raise RuntimeError(msg)

        bun = shutil.which("bun")
        if bun is None:
            if version == "editable":
                return
            msg = (
                "bun is required to build the bundled frontend for wheel distributions. "
                "Install bun or build from a prebuilt wheel instead."
            )
            raise RuntimeError(msg)

        output_dir = _get_output_dir(frontend_dir, self.directory, version)
        output_dir.mkdir(parents=True, exist_ok=True)

        _build_frontend(frontend_dir, output_dir, bun)

        if version == "standard":
            force_include = build_data.setdefault("force_include", {})
            if not isinstance(force_include, dict):
                msg = "Wheel build data force_include must be a dictionary"
                raise TypeError(msg)
            force_include[str(output_dir)] = "mindroom/_frontend"


def _get_output_dir(frontend_dir: Path, build_directory: str, version: str) -> Path:
    """Return the frontend build output directory for the requested build mode."""
    if version == "editable":
        return frontend_dir / "dist"
    return Path(build_directory) / "frontend-dist"


def _build_frontend(frontend_dir: Path, output_dir: Path, bun: str) -> None:
    """Install frontend deps and write a production build to the output directory."""
    subprocess.run([bun, "install", "--frozen-lockfile"], check=True, cwd=frontend_dir)
    subprocess.run([bun, "run", "tsc"], check=True, cwd=frontend_dir)
    subprocess.run(
        [bun, "run", "vite", "build", "--outDir", str(output_dir)],
        check=True,
        cwd=frontend_dir,
    )
