"""Smoke test the mindroom-stack compose environment.

Run via ``python -m scripts.smoke_mindroom_stack <path>`` from the repo root.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from scripts.smoke_helpers import (
    error,
    getenv_int,
    log,
    run_command,
    validate_port,
    wait_for_http_match,
    wait_for_http_status,
)


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
    stack_client_port = getenv_int("STACK_CLIENT_PORT", 18080)

    validate_port("STACK_SYNAPSE_PORT", stack_synapse_port)
    validate_port("STACK_MINDROOM_PORT", stack_mindroom_port)
    validate_port("STACK_CLIENT_PORT", stack_client_port)

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
                    f"CLIENT_HOMESERVER_URL=http://localhost:{stack_synapse_port}",
                    "",
                ],
            ),
            encoding="utf-8",
        )

        compose_text = compose_source.read_text(encoding="utf-8")
        compose_text = compose_text.replace('"8008:8008"', f'"127.0.0.1:{stack_synapse_port}:8008"')
        compose_text = compose_text.replace('"8765:8765"', f'"127.0.0.1:{stack_mindroom_port}:8765"')
        compose_text = compose_text.replace('"8080:80"', f'"127.0.0.1:{stack_client_port}:80"')
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
                "Matrix homeserver",
                attempts=40,
                sleep_seconds=3,
            )

            wait_for_http_status(
                f"http://127.0.0.1:{stack_client_port}/",
                200,
                "MindRoom client",
                attempts=20,
                sleep_seconds=3,
            )
            wait_for_http_match(
                f"http://127.0.0.1:{stack_client_port}/config.json",
                f'"http://localhost:{stack_synapse_port}"',
                "MindRoom client config",
                attempts=20,
                sleep_seconds=3,
            )
            log("[smoke] mindroom-stack checks passed")
        except Exception as exc:
            error(str(exc))
            dump_compose_diagnostics(stack_dir, project_name, env_file, compose_file)
            exit_code = 1
        finally:
            cleanup(stack_dir, project_name, env_file, compose_file)
        return exit_code


if __name__ == "__main__":
    sys.exit(main())
