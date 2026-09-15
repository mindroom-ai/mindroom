"""Executable entry point for the packaged native desktop helper."""

from __future__ import annotations

import argparse
import asyncio
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from mindroom.constants import resolve_runtime_paths
from mindroom.desktop.native_host import run_native_stdio


def _main() -> None:
    """Resolve explicit runtime paths and serve the parent app."""
    parser = argparse.ArgumentParser(prog="MindRoom Desktop Helper")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--storage-path", type=Path)
    arguments = parser.parse_args()
    try:
        helper_version = version("mindroom")
    except PackageNotFoundError:
        helper_version = "development"
    runtime_paths = resolve_runtime_paths(config_path=arguments.config, storage_path=arguments.storage_path)
    asyncio.run(run_native_stdio(runtime_paths, helper_version=helper_version))


if __name__ == "__main__":
    _main()
