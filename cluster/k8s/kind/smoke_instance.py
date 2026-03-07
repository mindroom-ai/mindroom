#!/usr/bin/env python3
"""Deploy and verify a MindRoom instance in kind."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO


ROOT_DIR = Path(__file__).resolve().parents[3]


def getenv_int(name: str, default: int) -> int:
    """Read an integer environment variable."""
    return int(os.getenv(name, str(default)))


def validate_port(name: str, port: int) -> None:
    """Ensure a TCP port is within the valid range."""
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535, got {port}")


def log(message: str) -> None:
    """Print a smoke log line."""
    print(message, flush=True)


def error(message: str) -> None:
    """Print an error log line."""
    print(message, file=sys.stderr, flush=True)


def run_command(command: list[str], *, check: bool = True, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a command with text output."""
    return subprocess.run(
        command,
        check=check,
        capture_output=capture_output,
        text=True,
        cwd=ROOT_DIR,
    )


def read_log_file(path: Path) -> None:
    """Print a log file if it exists."""
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8", errors="replace")
    if content:
        error(content)


def http_get_text(url: str, *, timeout: float = 2.0, headers: dict[str, str] | None = None, method: str = "GET", body: bytes | None = None) -> str:
    """Fetch a URL and return the response body as text."""
    request = urllib.request.Request(url, headers=headers or {}, method=method, data=body)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def http_contains(url: str, expected: str, *, timeout: float = 2.0) -> bool:
    """Return whether the response body contains the expected text."""
    try:
        return expected in http_get_text(url, timeout=timeout)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return False


def wait_for_http_match(url: str, expected: str, label: str, *, attempts: int = 30, sleep_seconds: float = 2.0) -> None:
    """Poll an HTTP endpoint until the response body contains the expected text."""
    for _ in range(attempts):
        if http_contains(url, expected):
            log(f"[smoke] {label} ready")
            return
        time.sleep(sleep_seconds)
    raise RuntimeError(f"[error] Timed out waiting for {label} ({url})")


def port_is_listening(local_port: int) -> bool:
    """Return whether a local TCP port is accepting connections."""
    try:
        with socket.create_connection(("127.0.0.1", local_port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_for_port_forward(local_port: int, process: subprocess.Popen[str], log_file: Path, label: str) -> None:
    """Wait until a port-forward process is listening on the local port."""
    for _ in range(30):
        if process.poll() is not None:
            error(f"[error] {label} port-forward exited early")
            read_log_file(log_file)
            raise RuntimeError(f"{label} port-forward exited early")
        if port_is_listening(local_port):
            log(f"[smoke] {label} port-forward ready")
            return
        time.sleep(1)
    error(f"[error] Timed out waiting for {label} port-forward on 127.0.0.1:{local_port}")
    read_log_file(log_file)
    raise RuntimeError(f"Timed out waiting for {label} port-forward")


def start_port_forward(
    namespace: str,
    resource: str,
    local_port: int,
    remote_port: int,
    log_file: Path,
    label: str,
    port_forwards: list[tuple[subprocess.Popen[str], IO[str]]],
) -> subprocess.Popen[str]:
    """Start a kubectl port-forward and wait for the local port to open."""
    log_handle = log_file.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            "kubectl",
            "port-forward",
            "--address",
            "127.0.0.1",
            "-n",
            namespace,
            resource,
            f"{local_port}:{remote_port}",
        ],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=ROOT_DIR,
    )
    port_forwards.append((process, log_handle))
    wait_for_port_forward(local_port, process, log_file, label)
    return process


def start_port_forward_for_http_match(
    namespace: str,
    resource: str,
    local_port: int,
    remote_port: int,
    log_file: Path,
    label: str,
    url: str,
    expected: str,
    port_forwards: list[tuple[subprocess.Popen[str], IO[str]]],
) -> subprocess.Popen[str]:
    """Start or restart a port-forward until the target HTTP endpoint is ready."""
    process: subprocess.Popen[str] | None = None
    for _ in range(30):
        if process is None or process.poll() is not None:
            process = start_port_forward(namespace, resource, local_port, remote_port, log_file, label, port_forwards)
        if http_contains(url, expected):
            log(f"[smoke] {label} ready")
            return process
        if process.poll() is not None:
            process = None
        time.sleep(1)
    error(f"[error] Timed out waiting for {label} via port-forward ({url})")
    read_log_file(log_file)
    raise RuntimeError(f"Timed out waiting for {label}")


def provision_via_platform_api(platform_backend_local_port: int, provisioner_api_key: str, account_id: str, tmp_dir: Path) -> str:
    """Provision an instance via the platform backend API and return the customer id."""
    response_body = http_get_text(
        f"http://127.0.0.1:{platform_backend_local_port}/system/provision",
        timeout=30.0,
        headers={
            "Authorization": f"Bearer {provisioner_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
        body=json.dumps(
            {
                "subscription_id": "smoke-subscription",
                "account_id": account_id,
                "tier": "starter",
            }
        ).encode("utf-8"),
    )
    response_file = tmp_dir / "provision-response.json"
    response_file.write_text(response_body, encoding="utf-8")
    payload = json.loads(response_body)
    customer_id = payload.get("customer_id")
    if customer_id in (None, ""):
        raise RuntimeError("missing customer_id in provisioner response")
    return str(customer_id)


def deploy_instance_directly(
    *,
    instance_id: str,
    instance_namespace: str,
    base_domain: str,
    account_id: str,
    mindroom_image: str,
    mindroom_image_pull_policy: str,
    synapse_image: str,
    synapse_image_pull_policy: str,
) -> None:
    """Deploy the instance chart directly with smoke-safe overrides."""
    namespace_status = run_command(
        ["kubectl", "get", "namespace", instance_namespace],
        check=False,
        capture_output=True,
    )
    if namespace_status.returncode != 0:
        run_command(["kubectl", "create", "namespace", instance_namespace])

    log(f"[helm] Deploying instance {instance_id} directly...")
    run_command(
        [
            "helm",
            "upgrade",
            "--install",
            f"instance-{instance_id}",
            str(ROOT_DIR / "cluster" / "k8s" / "instance"),
            "--namespace",
            instance_namespace,
            "--create-namespace",
            "--set",
            f"customer={instance_id}",
            "--set",
            f"baseDomain={base_domain}",
            "--set",
            f"accountId={account_id}",
            "--set",
            "storageClassName=standard",
            "--set",
            f"mindroom_image={mindroom_image}",
            "--set",
            f"mindroom_image_pull_policy={mindroom_image_pull_policy}",
            "--set",
            f"synapse_image={synapse_image}",
            "--set",
            f"synapse_image_pull_policy={synapse_image_pull_policy}",
            "--set",
            "disableAiRoomTopics=true",
            "--set-json",
            "authorizationGlobalUsers=[]",
            "--set",
            "openai_key=test-openai",
            "--set",
            "anthropic_key=test-anthropic",
            "--set",
            "google_key=test-google",
            "--set",
            "openrouter_key=test-openrouter",
            "--set",
            "deepseek_key=test-deepseek",
            "--set",
            "sandbox_proxy_token=test-sandbox-token",
        ]
    )


def dump_instance_diagnostics(instance_namespace: str, instance_id: str) -> None:
    """Best-effort Kubernetes diagnostics for a failed smoke deploy."""
    error(f"[diagnostics] Dumping Kubernetes state for namespace {instance_namespace}")
    commands = [
        ["kubectl", "get", "pods", "-n", instance_namespace, "-o", "wide"],
        ["kubectl", "get", "svc", "-n", instance_namespace],
        ["kubectl", "get", "deployment", "-n", instance_namespace],
        ["kubectl", "describe", "deployment", f"mindroom-{instance_id}", "-n", instance_namespace],
        ["kubectl", "describe", "deployment", f"synapse-{instance_id}", "-n", instance_namespace],
        ["kubectl", "describe", "pod", "-n", instance_namespace, "-l", "app=mindroom"],
        ["kubectl", "describe", "pod", "-n", instance_namespace, "-l", "app=synapse"],
        ["kubectl", "logs", f"deployment/mindroom-{instance_id}", "-n", instance_namespace, "-c", "mindroom", "--tail=200"],
        ["kubectl", "logs", f"deployment/mindroom-{instance_id}", "-n", instance_namespace, "-c", "sandbox-runner", "--tail=200"],
        ["kubectl", "logs", f"deployment/synapse-{instance_id}", "-n", instance_namespace, "-c", "synapse", "--tail=200"],
        ["kubectl", "get", "events", "-n", instance_namespace, "--sort-by=.lastTimestamp"],
        [
            "kubectl",
            "exec",
            "-n",
            instance_namespace,
            f"deployment/mindroom-{instance_id}",
            "-c",
            "mindroom",
            "--",
            "curl",
            "-fsS",
            "localhost:8765/api/ready",
        ],
    ]
    for command in commands:
        error(f"[diagnostics] $ {' '.join(command)}")
        run_command(command, check=False)


def cleanup_port_forwards(port_forwards: list[tuple[subprocess.Popen[str], IO[str]]]) -> None:
    """Terminate any active port-forward processes."""
    for process, log_handle in port_forwards:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log_handle.close()


def main() -> int:
    """Run the smoke deploy."""
    instance_id = os.getenv("INSTANCE_ID", "1")
    instance_namespace = os.getenv("INSTANCE_NAMESPACE", "mindroom-instances")
    platform_namespace = os.getenv("PLATFORM_NAMESPACE", "mindroom-staging")
    base_domain = os.getenv("BASE_DOMAIN", "local")
    account_id = os.getenv("ACCOUNT_ID", "acct-kindtest")
    mindroom_image = os.getenv("MINDROOM_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
    mindroom_image_pull_policy = os.getenv("MINDROOM_IMAGE_PULL_POLICY", "IfNotPresent")
    synapse_image = os.getenv("SYNAPSE_IMAGE", "matrixdotorg/synapse:latest")
    synapse_image_pull_policy = os.getenv("SYNAPSE_IMAGE_PULL_POLICY", "IfNotPresent")
    platform_backend_local_port = getenv_int("PLATFORM_BACKEND_LOCAL_PORT", 18000)
    platform_frontend_local_port = getenv_int("PLATFORM_FRONTEND_LOCAL_PORT", 13000)
    mindroom_local_port = getenv_int("MINDROOM_LOCAL_PORT", 18765)
    synapse_local_port = getenv_int("SYNAPSE_LOCAL_PORT", 18008)
    provisioner_api_key = os.getenv("PROVISIONER_API_KEY", "kind-provisioner-key")
    smoke_require_platform_provisioning = os.getenv("SMOKE_REQUIRE_PLATFORM_PROVISIONING", "0")
    deployment_rollout_timeout = os.getenv("DEPLOYMENT_ROLLOUT_TIMEOUT", "600s")
    platform_health_url = f"http://127.0.0.1:{platform_backend_local_port}/health"
    platform_ui_url = f"http://127.0.0.1:{platform_frontend_local_port}/"
    mindroom_ready_url = f"http://127.0.0.1:{mindroom_local_port}/api/ready"
    mindroom_ui_url = f"http://127.0.0.1:{mindroom_local_port}/"
    synapse_url = f"http://127.0.0.1:{synapse_local_port}/_matrix/client/versions"

    validate_port("PLATFORM_BACKEND_LOCAL_PORT", platform_backend_local_port)
    validate_port("PLATFORM_FRONTEND_LOCAL_PORT", platform_frontend_local_port)
    validate_port("MINDROOM_LOCAL_PORT", mindroom_local_port)
    validate_port("SYNAPSE_LOCAL_PORT", synapse_local_port)

    port_forwards: list[tuple[subprocess.Popen[str], IO[str]]] = []

    try:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            tmp_dir = Path(tmp_dir_name)
            start_port_forward_for_http_match(
                platform_namespace,
                "svc/platform-backend",
                platform_backend_local_port,
                8000,
                tmp_dir / "pf-platform-backend.log",
                "platform backend health",
                platform_health_url,
                '"status"',
                port_forwards,
            )
            start_port_forward_for_http_match(
                platform_namespace,
                "svc/platform-frontend",
                platform_frontend_local_port,
                3000,
                tmp_dir / "pf-platform-frontend.log",
                "platform frontend",
                platform_ui_url,
                "MindRoom",
                port_forwards,
            )

            platform_health = http_get_text(platform_health_url, timeout=5.0)
            if '"supabase":true' in platform_health:
                log("[smoke] Provisioning instance through live platform API")
                instance_id = provision_via_platform_api(platform_backend_local_port, provisioner_api_key, account_id, tmp_dir)
            elif smoke_require_platform_provisioning == "1":
                raise RuntimeError("[error] Platform provisioning smoke requires Supabase-configured platform backend")
            else:
                log("[smoke] Platform backend has no Supabase; falling back to direct Helm instance deploy")
                deploy_instance_directly(
                    instance_id=instance_id,
                    instance_namespace=instance_namespace,
                    base_domain=base_domain,
                    account_id=account_id,
                    mindroom_image=mindroom_image,
                    mindroom_image_pull_policy=mindroom_image_pull_policy,
                    synapse_image=synapse_image,
                    synapse_image_pull_policy=synapse_image_pull_policy,
                )

            run_command(
                [
                    "kubectl",
                    "rollout",
                    "status",
                    f"deployment/mindroom-{instance_id}",
                    "-n",
                    instance_namespace,
                    f"--timeout={deployment_rollout_timeout}",
                ]
            )
            run_command(
                [
                    "kubectl",
                    "rollout",
                    "status",
                    f"deployment/synapse-{instance_id}",
                    "-n",
                    instance_namespace,
                    f"--timeout={deployment_rollout_timeout}",
                ]
            )

            start_port_forward_for_http_match(
                instance_namespace,
                f"svc/mindroom-{instance_id}",
                mindroom_local_port,
                8765,
                tmp_dir / "pf-mindroom.log",
                "MindRoom readiness",
                mindroom_ready_url,
                '"ready"',
                port_forwards,
            )
            wait_for_http_match(mindroom_ui_url, "MindRoom", "MindRoom dashboard")
            start_port_forward_for_http_match(
                instance_namespace,
                f"svc/synapse-{instance_id}",
                synapse_local_port,
                8008,
                tmp_dir / "pf-synapse.log",
                "instance Synapse",
                synapse_url,
                '"versions"',
                port_forwards,
            )
            log("[smoke] kind platform + instance checks passed")
        return 0
    except Exception as exc:
        error(f"[error] smoke_instance.py failed: {exc}")
        traceback.print_exc()
        dump_instance_diagnostics(instance_namespace, instance_id)
        return 1
    finally:
        cleanup_port_forwards(port_forwards)


if __name__ == "__main__":
    sys.exit(main())
