#!/usr/bin/env python3
"""Live smoke test for MindRoom's Agent Vault bridge adapter.

This test intentionally uses the real ``infisical/agent-vault`` image, a local
echo container, and a separate Docker worker container. It is not part of normal
pytest because it pulls images and starts containers.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from mindroom.egress.agent_vault_bridge import start_adapter

AGENT_VAULT_IMAGE = os.environ.get("AGENT_VAULT_IMAGE", "infisical/agent-vault:latest")
WORKER_IMAGE = os.environ.get("MINDROOM_AGENT_VAULT_SMOKE_WORKER_IMAGE", "python:3.13-alpine")
DOCKER_HOST_GATEWAY = "host.docker.internal"
ECHO_HOST = "local-echo.test"
ECHO_PORT = 80
ECHO_SUBNET = "203.0.113.0/24"
ECHO_IPV4 = "203.0.113.10"
FAKE_SECRET = "real-vault-smoke-secret"  # noqa: S105
MASTER_PASSWORD = "mindroom-agent-vault-smoke-master-password"  # noqa: S105
OWNER_PASSWORD = "mindroom-agent-vault-smoke-owner-password"  # noqa: S105
ECHO_SERVER_CODE = r"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class HeaderEchoHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path != "/headers":
            self.send_error(404, "Not Found")
            return
        payload = json.dumps(
            {"headers": {key: value for key, value in self.headers.items()}},
            sort_keys=True,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

ThreadingHTTPServer(("0.0.0.0", __ECHO_PORT__), HeaderEchoHandler).serve_forever()
""".replace("__ECHO_PORT__", str(ECHO_PORT))


def _run(
    args: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
    timeout_seconds: float = 300,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            input=input_text,
            text=True,
            check=False,
            timeout=timeout_seconds,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except subprocess.TimeoutExpired as exc:
        command = " ".join(args)
        output = exc.stdout or ""
        msg = f"Command timed out after {timeout_seconds}s: {command}\n{output}"
        raise TimeoutError(msg) from exc
    if check and result.returncode != 0:
        command = " ".join(args)
        msg = f"Command failed ({result.returncode}): {command}\n{result.stdout}"
        raise RuntimeError(msg)
    return result


def _docker_port(container: str, private_port: int) -> int:
    result = _run(["docker", "port", container, f"{private_port}/tcp"])
    raw = result.stdout.strip().rsplit(":", 1)[-1]
    return int(raw)


def _start_echo_container(container: str, network: str) -> None:
    _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "--network",
            network,
            "--ip",
            ECHO_IPV4,
            "--network-alias",
            ECHO_HOST,
            WORKER_IMAGE,
            "python",
            "-c",
            ECHO_SERVER_CODE,
        ],
    )
    _wait_for_echo(container)


def _wait_for_echo(container: str) -> None:
    probe_code = f"""
import urllib.request
urllib.request.urlopen("http://127.0.0.1:{ECHO_PORT}/headers", timeout=2).read()
"""
    deadline = time.monotonic() + 45
    last_output = ""
    while time.monotonic() < deadline:
        result = _run(
            ["docker", "exec", container, "python", "-c", probe_code],
            check=False,
            timeout_seconds=5,
        )
        if result.returncode == 0:
            return
        last_output = result.stdout
        time.sleep(1)
    logs = _run(["docker", "logs", container], check=False).stdout
    msg = f"Echo container did not become healthy:\n{last_output}\n{logs}"
    raise TimeoutError(msg)


def _worker_target_url() -> str:
    return f"http://{ECHO_HOST}/headers"


def _parse_worker_headers(output: str) -> dict[str, str]:
    for line in reversed(output.splitlines()):
        raw_line = line.strip()
        if not raw_line:
            continue
        try:
            data = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        headers: dict[str, str] = {}
        for key, value in data.items():
            if not isinstance(key, str) or not isinstance(value, str):
                msg = f"Worker emitted non-string header data: {data}"
                raise TypeError(msg)
            headers[key] = value
        return headers
    msg = f"Worker did not emit JSON headers:\n{output}"
    raise ValueError(msg)


def _wait_for_health(api_port: int) -> None:
    url = f"http://127.0.0.1:{api_port}/health"
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
                if response.status == 200:
                    return
        except OSError:
            time.sleep(1)
    msg = f"Agent Vault did not become healthy at {url}"
    raise TimeoutError(msg)


def _configure_agent_vault(container: str) -> str:
    _run(
        [
            "docker",
            "exec",
            "-i",
            container,
            "agent-vault",
            "auth",
            "register",
            "--address",
            "http://127.0.0.1:14321",
            "--email",
            "owner@example.test",
            "--password-stdin",
        ],
        input_text=f"{OWNER_PASSWORD}\n",
    )
    _run(
        [
            "docker",
            "exec",
            container,
            "agent-vault",
            "vault",
            "credential",
            "set",
            f"TEST_TOKEN={FAKE_SECRET}",
            "--vault",
            "default",
        ],
    )
    _run(
        [
            "docker",
            "exec",
            container,
            "agent-vault",
            "vault",
            "service",
            "add",
            "--vault",
            "default",
            "--name",
            "local-echo",
            "--host",
            ECHO_HOST,
            "--auth-type",
            "bearer",
            "--token-key",
            "TEST_TOKEN",
        ],
    )
    result = _run(
        [
            "docker",
            "exec",
            container,
            "agent-vault",
            "vault",
            "token",
            "--vault",
            "default",
            "--ttl",
            "3600",
        ],
    )
    return result.stdout.strip()


def _run_worker(adapter_port: int) -> dict[str, str]:
    proxy_url = f"http://{DOCKER_HOST_GATEWAY}:{adapter_port}"
    target_url = _worker_target_url()
    worker_code = r"""
import json
import os
import urllib.request

leaked = {
    key: value
    for key, value in os.environ.items()
    if "AGENT_VAULT" in key or "TOKEN" in key or "SECRET" in key
}
if leaked:
    raise SystemExit(f"worker env leaked secret-like names: {sorted(leaked)}")

with urllib.request.urlopen("__TARGET_URL__", timeout=20) as response:
    data = json.loads(response.read().decode("utf-8"))
print(json.dumps(data["headers"], sort_keys=True))
""".replace("__TARGET_URL__", target_url)
    result = _run(
        [
            "docker",
            "run",
            "--rm",
            f"--add-host={DOCKER_HOST_GATEWAY}:host-gateway",
            "-e",
            f"HTTP_PROXY={proxy_url}",
            "-e",
            f"HTTPS_PROXY={proxy_url}",
            "-e",
            f"http_proxy={proxy_url}",
            "-e",
            f"https_proxy={proxy_url}",
            WORKER_IMAGE,
            "python",
            "-c",
            worker_code,
        ],
    )
    return _parse_worker_headers(result.stdout)


def main() -> int:
    """Run the live Docker smoke and return a process exit code."""
    if shutil.which("docker") is None:
        print("docker is required for this live smoke", file=sys.stderr)
        return 1

    temp_dir = Path(tempfile.mkdtemp(prefix="mindroom-agent-vault-smoke-"))
    network = f"mindroom-agent-vault-smoke-{os.getpid()}"
    container = f"mindroom-agent-vault-smoke-vault-{os.getpid()}"
    echo_container = f"mindroom-agent-vault-smoke-echo-{os.getpid()}"
    try:
        # Agent Vault refuses local/private proxy targets, so this uses a TEST-NET bridge.
        _run(["docker", "network", "create", "--subnet", ECHO_SUBNET, network])
        _start_echo_container(echo_container, network)
        _run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "--network",
                network,
                "-p",
                "127.0.0.1::14321",
                "-p",
                "127.0.0.1::14322",
                "-e",
                f"AGENT_VAULT_MASTER_PASSWORD={MASTER_PASSWORD}",
                "-v",
                f"{temp_dir}:/data",
                AGENT_VAULT_IMAGE,
                "server",
                "--host",
                "0.0.0.0",  # noqa: S104
                "--port",
                "14321",
                "--mitm-port",
                "14322",
            ],
        )
        api_port = _docker_port(container, 14321)
        proxy_port = _docker_port(container, 14322)
        _wait_for_health(api_port)
        session_token = _configure_agent_vault(container)

        with start_adapter(
            host="0.0.0.0",  # noqa: S104
            port=0,
            upstream_proxy_url=f"http://127.0.0.1:{proxy_port}",
            session_token=session_token,
        ) as adapter:
            headers = _run_worker(adapter.port)

        authorization = headers.get("Authorization")
        if authorization != f"Bearer {FAKE_SECRET}":
            msg = f"Agent Vault did not inject credential: {headers}"
            raise AssertionError(msg)
        print(
            json.dumps(
                {
                    "agent_vault_image": AGENT_VAULT_IMAGE,
                    "api_port": api_port,
                    "proxy_port": proxy_port,
                    "worker_authorization": authorization,
                    "worker_received_agent_vault_token": False,
                },
                sort_keys=True,
            ),
        )
        return 0
    finally:
        _run(["docker", "rm", "-f", container], check=False)
        _run(["docker", "rm", "-f", echo_container], check=False)
        _run(["docker", "network", "rm", network], check=False)
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
