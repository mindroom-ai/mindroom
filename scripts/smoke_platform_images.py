#!/usr/bin/env python3
"""Smoke test the published platform images."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]


def getenv_int(name: str, default: int) -> int:
    """Read an integer environment variable."""
    return int(os.getenv(name, str(default)))


def validate_port(name: str, port: int) -> None:
    """Ensure a TCP port is within the valid range."""
    if not 1 <= port <= 65535:
        msg = f"{name} must be between 1 and 65535, got {port}"
        raise ValueError(msg)


def log(message: str) -> None:
    """Print a smoke log line."""
    print(message, flush=True)


def error(message: str) -> None:
    """Print an error log line."""
    print(message, file=sys.stderr, flush=True)


def run_command(
    command: list[str],
    *,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess in the repository root."""
    return subprocess.run(
        command,
        check=check,
        capture_output=capture_output,
        text=True,
        cwd=ROOT_DIR,
    )


def http_get_text(url: str) -> str:
    """Return the response body for a URL."""
    with urllib.request.urlopen(url, timeout=3.0) as response:  # noqa: S310
        return response.read().decode("utf-8", errors="replace")


def http_status(url: str) -> int | None:
    """Return the HTTP status code for a URL."""
    try:
        with urllib.request.urlopen(url, timeout=3.0) as response:  # noqa: S310
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (OSError, urllib.error.URLError):
        return None


def http_contains(url: str, expected: str) -> bool:
    """Return whether the response contains the expected text."""
    try:
        return expected in http_get_text(url)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return False


def wait_for_http_status(
    url: str,
    expected_status: int,
    label: str,
    *,
    attempts: int = 30,
    sleep_seconds: float = 2.0,
) -> None:
    """Poll an HTTP endpoint until it returns the expected status code."""
    for _ in range(attempts):
        if http_status(url) == expected_status:
            log(f"[smoke] {label} ready")
            return
        time.sleep(sleep_seconds)
    msg = f"[error] Timed out waiting for {label} ({url})"
    raise RuntimeError(msg)


def wait_for_http_match(url: str, expected: str, label: str, *, attempts: int = 30, sleep_seconds: float = 2.0) -> None:
    """Poll an HTTP endpoint until it contains the expected text."""
    for _ in range(attempts):
        if http_contains(url, expected):
            log(f"[smoke] {label} ready")
            return
        time.sleep(sleep_seconds)
    msg = f"[error] Timed out waiting for {label} ({url})"
    raise RuntimeError(msg)


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
