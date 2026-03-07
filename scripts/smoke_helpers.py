"""Shared helpers for smoke scripts."""

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


def http_get_text(
    url: str,
    *,
    timeout: float = 3.0,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    body: bytes | None = None,
) -> str:
    """Fetch a URL and return the response body as text."""
    request = urllib.request.Request(url, headers=headers or {}, method=method, data=body)  # noqa: S310
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read().decode("utf-8", errors="replace")


def http_status(url: str, *, timeout: float = 3.0) -> int | None:
    """Return the HTTP status code for a URL."""
    try:
        request = urllib.request.Request(url, method="GET")  # noqa: S310
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (OSError, urllib.error.URLError):
        return None


def http_contains(url: str, expected: str, *, timeout: float = 3.0) -> bool:
    """Return whether the response contains the expected text."""
    try:
        return expected in http_get_text(url, timeout=timeout)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return False


def wait_for_http_status(
    url: str,
    expected_status: int,
    label: str,
    *,
    attempts: int = 30,
    sleep_seconds: float = 2.0,
    timeout: float = 3.0,
) -> None:
    """Poll an HTTP endpoint until it returns the expected status code."""
    for _ in range(attempts):
        if http_status(url, timeout=timeout) == expected_status:
            log(f"[smoke] {label} ready")
            return
        time.sleep(sleep_seconds)
    msg = f"[error] Timed out waiting for {label} ({url})"
    raise RuntimeError(msg)


def wait_for_http_match(
    url: str,
    expected: str,
    label: str,
    *,
    attempts: int = 30,
    sleep_seconds: float = 2.0,
    timeout: float = 3.0,
) -> None:
    """Poll an HTTP endpoint until it contains the expected text."""
    for _ in range(attempts):
        if http_contains(url, expected, timeout=timeout):
            log(f"[smoke] {label} ready")
            return
        time.sleep(sleep_seconds)
    msg = f"[error] Timed out waiting for {label} ({url})"
    raise RuntimeError(msg)
