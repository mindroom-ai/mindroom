"""Executable entry point for the packaged native desktop helper."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
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
    if getattr(sys, "frozen", False) and not any(name in os.environ for name in ("SSL_CERT_FILE", "SSL_CERT_DIR")):
        # Frozen OpenSSL may retain the build machine's absent CA path. Use the
        # shipped roots, preserving explicit trust configuration and verification.
        import certifi  # noqa: PLC0415 - Only the packaged helper needs this fallback.

        os.environ["SSL_CERT_FILE"] = certifi.where()
    try:
        helper_version = version("mindroom")
    except PackageNotFoundError:
        helper_version = "development"
    runtime_paths = resolve_runtime_paths(config_path=arguments.config, storage_path=arguments.storage_path)
    asyncio.run(run_native_stdio(runtime_paths, helper_version=helper_version))


if __name__ == "__main__":
    _main()
