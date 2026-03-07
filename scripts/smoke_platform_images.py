#!/usr/bin/env python3
"""Smoke test the published platform images."""

from __future__ import annotations

import os
import sys

from scripts.smoke_helpers import (
    error,
    getenv_int,
    log,
    run_command,
    validate_port,
    wait_for_http_match,
    wait_for_http_status,
)


def dump_container_diagnostics(container_names: list[str]) -> None:
    """Print best-effort docker diagnostics."""
    for container_name in container_names:
        error(f"[diagnostics] docker logs {container_name}")
        run_command(["docker", "logs", container_name], check=False)


def cleanup(container_names: list[str]) -> None:
    """Remove the smoke containers."""
    for container_name in container_names:
        run_command(["docker", "rm", "-f", container_name], check=False, capture_output=True)


def main() -> int:
    """Run the platform image smoke test."""
    backend_container_name = os.getenv("PLATFORM_BACKEND_CONTAINER_NAME", "platform-backend-smoke")
    frontend_container_name = os.getenv("PLATFORM_FRONTEND_CONTAINER_NAME", "platform-frontend-smoke")
    backend_port = getenv_int("PLATFORM_BACKEND_PORT", 18000)
    frontend_port = getenv_int("PLATFORM_FRONTEND_PORT", 13000)
    backend_image = os.getenv("PLATFORM_BACKEND_IMAGE", "ghcr.io/mindroom-ai/platform-backend:latest")
    frontend_image = os.getenv("PLATFORM_FRONTEND_IMAGE", "ghcr.io/mindroom-ai/platform-frontend:latest")

    validate_port("PLATFORM_BACKEND_PORT", backend_port)
    validate_port("PLATFORM_FRONTEND_PORT", frontend_port)

    container_names = [backend_container_name, frontend_container_name]
    cleanup(container_names)
    exit_code = 0

    try:
        run_command(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                backend_container_name,
                "-p",
                f"{backend_port}:8000",
                backend_image,
            ],
            capture_output=True,
        )
        run_command(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                frontend_container_name,
                "-p",
                f"{frontend_port}:3000",
                frontend_image,
            ],
            capture_output=True,
        )

        wait_for_http_status(
            f"http://127.0.0.1:{backend_port}/health",
            200,
            "Platform backend",
        )
        wait_for_http_match(
            f"http://127.0.0.1:{frontend_port}/",
            "MindRoom",
            "Platform frontend",
        )
        log("[smoke] platform image checks passed")
    except Exception as exc:
        error(str(exc))
        dump_container_diagnostics(container_names)
        exit_code = 1
    finally:
        cleanup(container_names)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
