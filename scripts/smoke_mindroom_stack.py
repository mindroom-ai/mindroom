#!/usr/bin/env python3
"""Smoke test the mindroom-stack compose environment."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
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


def http_contains(url: str, expected: str) -> bool:
    """Return whether an HTTP response contains the expected text."""
    try:
        with urllib.request.urlopen(url, timeout=3.0) as response:  # noqa: S310
            body = response.read().decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return False
    return expected in body


def http_status(url: str) -> int | None:
    """Return the HTTP status code for a URL."""
    try:
        with urllib.request.urlopen(url, timeout=3.0) as response:  # noqa: S310
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (OSError, urllib.error.URLError):
        return None


def wait_for_http_match(url: str, expected: str, label: str, *, attempts: int = 30, sleep_seconds: float = 2.0) -> None:
    """Poll an HTTP endpoint until it contains the expected response content."""
    for _ in range(attempts):
        if http_contains(url, expected):
            log(f"[smoke] {label} ready")
            return
        time.sleep(sleep_seconds)
    msg = f"[error] Timed out waiting for {label} ({url})"
    raise RuntimeError(msg)


def dump_compose_diagnostics(stack_dir: Path, project_name: str, env_file: Path, compose_file: Path) -> None:
    """Best-effort docker compose diagnostics."""
    commands = [
        [
            "docker",
            "compose",
            "--project-directory",
            str(stack_dir),
            "--project-name",
            project_name,
            "--env-file",
            str(env_file),
            "-f",
            str(compose_file),
            "ps",
        ],
        [
            "docker",
            "compose",
            "--project-directory",
            str(stack_dir),
            "--project-name",
            project_name,
            "--env-file",
            str(env_file),
            "-f",
            str(compose_file),
            "logs",
        ],
    ]
    for command in commands:
        error(f"[diagnostics] $ {' '.join(command)}")
        run_command(command, check=False)


def cleanup(stack_dir: Path, project_name: str, env_file: Path, compose_file: Path) -> None:
    """Tear down the compose project and remove temporary files."""
    run_command(
        [
            "docker",
            "compose",
            "--project-directory",
            str(stack_dir),
            "--project-name",
            project_name,
            "--env-file",
            str(env_file),
            "-f",
            str(compose_file),
            "down",
            "-v",
        ],
        check=False,
        capture_output=True,
    )
    env_file.unlink(missing_ok=True)
    compose_file.unlink(missing_ok=True)


def main() -> int:
    """Run the compose smoke test."""
    if len(sys.argv) < 2:
        error(f"Usage: {sys.argv[0]} /path/to/mindroom-stack")
        return 1

    stack_dir = Path(sys.argv[1]).resolve()
    project_name = os.getenv("PROJECT_NAME", "mindroom-stack-smoke")
    stack_synapse_port = getenv_int("STACK_SYNAPSE_PORT", 18008)
    stack_mindroom_port = getenv_int("STACK_MINDROOM_PORT", 18765)
    stack_element_port = getenv_int("STACK_ELEMENT_PORT", 18080)

    validate_port("STACK_SYNAPSE_PORT", stack_synapse_port)
    validate_port("STACK_MINDROOM_PORT", stack_mindroom_port)
    validate_port("STACK_ELEMENT_PORT", stack_element_port)

    compose_source = stack_dir / "compose.yaml"
    if not compose_source.is_file():
        error(f"[error] compose.yaml not found in {stack_dir}")
        return 1

    with tempfile.TemporaryDirectory() as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        env_file = tmp_dir / "mindroom-stack-smoke.env"
        compose_file = tmp_dir / "mindroom-stack-compose.yaml"

        env_file.write_text(
            "\n".join(
                [
                    "POSTGRES_PASSWORD=synapse_password",
                    "MATRIX_SERVER_NAME=matrix.localhost",
                    "OPENAI_API_KEY=test-openai",
                    "ANTHROPIC_API_KEY=test-anthropic",
                    "GOOGLE_API_KEY=",
                    "OPENROUTER_API_KEY=",
                    "OLLAMA_HOST=http://localhost:11434",
                    f"ELEMENT_HOMESERVER_URL=http://localhost:{stack_synapse_port}",
                    "",
                ],
            ),
            encoding="utf-8",
        )

        compose_text = compose_source.read_text(encoding="utf-8")
        compose_text = compose_text.replace('"8008:8008"', f'"127.0.0.1:{stack_synapse_port}:8008"')
        compose_text = compose_text.replace('"8765:8765"', f'"127.0.0.1:{stack_mindroom_port}:8765"')
        compose_text = compose_text.replace('"8080:8080"', f'"127.0.0.1:{stack_element_port}:8080"')
        compose_file.write_text(compose_text, encoding="utf-8")
        exit_code = 0

        try:
            log(f"[smoke] Starting mindroom-stack from {stack_dir}")
            run_command(
                [
                    "docker",
                    "compose",
                    "--project-directory",
                    str(stack_dir),
                    "--project-name",
                    project_name,
                    "--env-file",
                    str(env_file),
                    "-f",
                    str(compose_file),
                    "up",
                    "-d",
                ],
                capture_output=True,
            )

            wait_for_http_match(
                f"http://127.0.0.1:{stack_mindroom_port}/api/ready",
                '"ready"',
                "MindRoom readiness",
                attempts=40,
                sleep_seconds=3,
            )
            wait_for_http_match(
                f"http://127.0.0.1:{stack_mindroom_port}/",
                "MindRoom",
                "MindRoom dashboard",
                attempts=40,
                sleep_seconds=3,
            )
            wait_for_http_match(
                f"http://127.0.0.1:{stack_synapse_port}/_matrix/client/versions",
                '"versions"',
                "Synapse",
                attempts=40,
                sleep_seconds=3,
            )

            element_url = f"http://127.0.0.1:{stack_element_port}/"
            for _ in range(20):
                if http_status(element_url) == 200:
                    log("[smoke] Element ready")
                    log("[smoke] mindroom-stack checks passed")
                    break
                time.sleep(3)
            else:
                msg = f"[error] Timed out waiting for Element ({element_url})"
                raise RuntimeError(msg)  # noqa: TRY301
        except Exception as exc:
            error(str(exc))
            dump_compose_diagnostics(stack_dir, project_name, env_file, compose_file)
            exit_code = 1
        finally:
            cleanup(stack_dir, project_name, env_file, compose_file)
        return exit_code


if __name__ == "__main__":
    sys.exit(main())
