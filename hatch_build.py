"""Hatch build hook for bundling the frontend into wheel distributions."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class FrontendBuildHook(BuildHookInterface):
    """Build the bundled dashboard before creating a distributable wheel."""

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        """Build the frontend and inject the generated assets into wheel contents."""
        if self.target_name != "wheel" or version != "standard":
            return

        frontend_dir = Path(self.root) / "frontend"
        if not frontend_dir.is_dir():
            msg = f"Frontend sources not found at {frontend_dir}"
            raise RuntimeError(msg)

        bun = shutil.which("bun")
        if bun is None:
            msg = (
                "bun is required to build the bundled frontend for wheel distributions. "
                "Install bun or build from a prebuilt wheel instead."
            )
            raise RuntimeError(msg)

        output_dir = Path(self.directory) / "frontend-dist"
        output_dir.mkdir(parents=True, exist_ok=True)

        subprocess.run([bun, "install", "--frozen-lockfile"], check=True, cwd=frontend_dir)
        subprocess.run(
            [bun, "run", "build", "--", "--outDir", str(output_dir)],
            check=True,
            cwd=frontend_dir,
        )

        force_include = build_data.setdefault("force_include", {})
        if not isinstance(force_include, dict):
            msg = "Wheel build data force_include must be a dictionary"
            raise TypeError(msg)
        force_include[str(output_dir)] = "mindroom/_frontend"
