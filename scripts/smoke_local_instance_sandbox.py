"""Smoke test sandbox runner isolation in the local instance Compose deployment.

Run via ``python -m scripts.smoke_local_instance_sandbox`` from the repo root.
Set ``MINDROOM_IMAGE`` to a MindRoom image that already exists locally; the smoke never builds it.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

from scripts.smoke_helpers import ROOT_DIR, error, getenv_int, log, run_command, validate_port

DEPLOY_DIR = ROOT_DIR / "local" / "instances" / "deploy"
COMPOSE_FILES = [DEPLOY_DIR / "docker-compose.yml", DEPLOY_DIR / "docker-compose.synapse.yml"]
SERVICES = ["mindroom", "sandbox-runner", "sandbox-relay", "postgres", "redis"]
PYTHON = "/app/.venv/bin/python"

# Replaces the runtime with a listener on its API port, because this smoke checks reachability, not MindRoom.
OVERRIDE = """\
services:
  mindroom:
    command: ["/app/.venv/bin/python", "-m", "http.server", "8765"]
    depends_on: !reset {{}}
networks:
  mynetwork:
    name: {external_network}
"""

TCP_PROBE = """
import socket, sys
for target in sys.argv[1:]:
    host, port = target.rsplit(":", 1)
    try:
        socket.create_connection((host, int(port)), timeout=3).close()
    except OSError:
        print(target, "closed")
    else:
        print(target, "open")
"""

HTTP_PROBE = """
import sys, urllib.error, urllib.request
headers = {"X-Mindroom-Sandbox-Token": sys.argv[2]} if len(sys.argv) > 2 else {}
try:
    with urllib.request.urlopen(urllib.request.Request(sys.argv[1], headers=headers), timeout=5) as response:
        print(response.status)
except urllib.error.HTTPError as exc:
    print(exc.code)
except OSError:
    print(0)
"""


def compose_command(project_name: str, env_file: Path, override_file: Path, *args: str) -> list[str]:
    """Return a Compose command for the smoke project."""
    files = [argument for path in [*COMPOSE_FILES, override_file] for argument in ("-f", str(path))]
    return ["docker", "compose", "--env-file", str(env_file), *files, "-p", project_name, *args]


def exec_python(compose: list[str], service: str, code: str, *args: str) -> str:
    """Run a Python snippet inside one smoke service and return its output."""
    command = [*compose, "exec", "-T", service, PYTHON, "-c", code, *args]
    return run_command(command, check=False, capture_output=True).stdout.strip()


def tcp_reachability(compose: list[str], service: str, targets: list[str]) -> dict[str, bool]:
    """Return which host:port targets accept TCP connections from inside one service."""
    lines = exec_python(compose, service, TCP_PROBE, *targets).splitlines()
    return {target: state == "open" for target, state in (line.rsplit(" ", 1) for line in lines)}


def runtime_network_ip(container_name: str, network_name: str) -> str:
    """Return a container's address on one Docker network."""
    result = run_command(
        ["docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}", container_name],
        capture_output=True,
    )
    return json.loads(result.stdout)[network_name]["IPAddress"]


def wait_for_relay(compose: list[str]) -> None:
    """Poll the runner health endpoint through the relay until it answers."""
    for _ in range(60):
        if exec_python(compose, "mindroom", HTTP_PROBE, "http://sandbox-relay:8766/healthz") == "200":
            log("[smoke] sandbox runner answers through the relay")
            return
        time.sleep(2)
    msg = "[error] Timed out waiting for the sandbox runner through the relay"
    raise RuntimeError(msg)


def expect(condition: bool, message: str) -> None:
    """Fail the smoke with a message unless the condition holds."""
    if not condition:
        msg = f"[error] {message}"
        raise RuntimeError(msg)
    log(f"[smoke] {message}")


def run_checks(compose: list[str], project_name: str, proxy_token: str) -> None:
    """Check that the runner reaches nothing on the runtime network while the relay still serves MindRoom."""
    wait_for_relay(compose)
    workers_url = "http://sandbox-relay:8766/api/sandbox-runner/workers"
    expect(
        exec_python(compose, "mindroom", HTTP_PROBE, workers_url) == "401",
        "runner rejects relayed calls without the proxy token",
    )
    expect(
        exec_python(compose, "mindroom", HTTP_PROBE, workers_url, proxy_token) == "200",
        "runner accepts relayed calls carrying the proxy token",
    )
    relay_forwarding = run_command(
        [*compose, "exec", "-T", "sandbox-relay", "cat", "/proc/sys/net/ipv4/ip_forward"],
        capture_output=True,
    ).stdout.strip()
    expect(relay_forwarding == "0", "relay does not forward IP packets")

    network_name = f"{project_name}_mindroom-network"
    runtime_targets = ["mindroom:8765", "postgres:5432", "redis:6379"]
    runtime_targets += [
        f"{runtime_network_ip(f'{project_name}-{service}', network_name)}:{port}"
        for service, port in [("mindroom", 8765), ("postgres", 5432), ("redis", 6379)]
    ]
    from_runtime = tcp_reachability(compose, "mindroom", runtime_targets)
    expect(all(from_runtime.values()), f"runtime network services accept connections from MindRoom: {from_runtime}")
    from_runner = tcp_reachability(compose, "sandbox-runner", runtime_targets)
    expect(not any(from_runner.values()), f"sandbox runner reaches no runtime network service: {from_runner}")

    redis_reply = run_command([*compose, "exec", "-T", "redis", "redis-cli", "ping"], capture_output=True).stdout
    expect("NOAUTH" in redis_reply, "Redis requires its generated password")


def main() -> int:
    """Run the sandbox isolation smoke test."""
    project_name = os.getenv("PROJECT_NAME", "mindroom-sandbox-smoke")
    mindroom_port = getenv_int("SMOKE_MINDROOM_PORT", 18865)
    validate_port("SMOKE_MINDROOM_PORT", mindroom_port)
    image = os.getenv("MINDROOM_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
    external_network = f"{project_name}-external"
    proxy_token = secrets.token_hex(32)

    with tempfile.TemporaryDirectory() as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        data_dir = tmp_dir / "data"
        for subdir in ["config", "mindroom_data", "logs"]:
            (data_dir / subdir).mkdir(parents=True)
        (data_dir / "config" / "config.yaml").write_text("agents: {}\n", encoding="utf-8")
        env_file = tmp_dir / "instance.env"
        env_file.write_text(
            "\n".join(
                [
                    f"INSTANCE_ENV_FILE={env_file}",
                    f"INSTANCE_NAME={project_name}",
                    f"MINDROOM_IMAGE={image}",
                    f"MINDROOM_PORT={mindroom_port}",
                    f"DATA_DIR={data_dir}",
                    "INSTANCE_DOMAIN=sandbox-smoke.localhost",
                    "MATRIX_SERVER_NAME=m-sandbox-smoke.localhost",
                    f"MINDROOM_API_KEY={secrets.token_hex(32)}",
                    f"MINDROOM_SANDBOX_PROXY_TOKEN={proxy_token}",
                    f"POSTGRES_PASSWORD={secrets.token_hex(32)}",
                    f"REDIS_PASSWORD={secrets.token_hex(32)}",
                    "",
                ],
            ),
            encoding="utf-8",
        )
        override_file = tmp_dir / "smoke.override.yml"
        override_file.write_text(OVERRIDE.format(external_network=external_network), encoding="utf-8")
        compose = compose_command(project_name, env_file, override_file)

        exit_code = 0
        try:
            run_command(["docker", "network", "create", external_network], capture_output=True)
            log(f"[smoke] Starting {', '.join(SERVICES)} from {image}")
            run_command([*compose, "up", "-d", "--no-build", "--wait", "--wait-timeout", "180", *SERVICES])
            run_checks(compose, project_name, proxy_token)
            log("[smoke] sandbox isolation checks passed")
        except Exception as exc:
            error(str(exc))
            for args in (["ps"], ["logs"]):
                error(f"[diagnostics] docker compose {' '.join(args)}")
                run_command([*compose, *args], check=False)
            exit_code = 1
        finally:
            run_command([*compose, "down", "-v"], check=False, capture_output=True)
            run_command(["docker", "network", "rm", external_network], check=False, capture_output=True)
        return exit_code


if __name__ == "__main__":
    sys.exit(main())
