#!/usr/bin/env python3
"""Live smoke test for MindRoom's Agent Vault bridge adapter.

This test intentionally uses the real ``infisical/agent-vault`` image and a
separate Docker worker container. It is not part of normal pytest because it
pulls images, starts containers, and calls httpbin.org with a fake credential.
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
FAKE_SECRET = "real-vault-smoke-secret"  # noqa: S105
MASTER_PASSWORD = "mindroom-agent-vault-smoke-master-password"  # noqa: S105
OWNER_PASSWORD = "mindroom-agent-vault-smoke-owner-password"  # noqa: S105


def _run(
    args: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        input=input_text,
        text=True,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if check and result.returncode != 0:
        command = " ".join(args)
        msg = f"Command failed ({result.returncode}): {command}\n{result.stdout}"
        raise RuntimeError(msg)
    return result


def _docker_port(container: str, private_port: int) -> int:
    result = _run(["docker", "port", container, f"{private_port}/tcp"])
    raw = result.stdout.strip().rsplit(":", 1)[-1]
    return int(raw)


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
            "httpbin",
            "--host",
            "httpbin.org",
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
    proxy_url = f"http://host.docker.internal:{adapter_port}"
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

with urllib.request.urlopen("http://httpbin.org/headers", timeout=20) as response:
    data = json.loads(response.read().decode("utf-8"))
print(json.dumps(data["headers"], sort_keys=True))
"""
    result = _run(
        [
            "docker",
            "run",
            "--rm",
            "--add-host=host.docker.internal:host-gateway",
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
    return json.loads(result.stdout)


def main() -> int:
    """Run the live Docker smoke and return a process exit code."""
    if shutil.which("docker") is None:
        print("docker is required for this live smoke", file=sys.stderr)
        return 1

    temp_dir = Path(tempfile.mkdtemp(prefix="mindroom-agent-vault-smoke-"))
    container = f"mindroom-agent-vault-smoke-{os.getpid()}"
    try:
        _run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
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
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
