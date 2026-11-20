"""Smoke the Kubernetes worker backend's tailnet SSH worker against the kind API server.

Run via ``python -m cluster.k8s.kind.smoke_tailnet_ssh_worker`` from the repo root, once the MindRoom image is loaded
into the cluster ``kubectl`` talks to. It runs ``tailnet_ssh_worker_probe.py`` in a pod set up as a primary's, since
only a pod reaches a worker's Service and the storage claim workers mount, and it fails when the probe does.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from scripts.smoke_helpers import error, getenv_int, log, run_command

_PROBE = Path(__file__).with_name("tailnet_ssh_worker_probe.py")
_NAME = "tailnet-ssh-smoke"
# Where the runtime chart mounts the storage claim, in the primary and in its workers.
_STORAGE_PATH = "/app/agent_data"
# The verbs of the runtime chart's worker manager Role (cluster/k8s/runtime/templates/rbac.yaml).
_ALL_VERBS = ["create", "delete", "get", "list", "patch", "update", "watch"]
_ROLE_RULES = [
    {"apiGroups": [""], "resources": ["services"], "verbs": _ALL_VERBS},
    {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create", "delete", "get", "patch"]},
    {"apiGroups": [""], "resources": ["pods", "pods/log", "events"], "verbs": ["get", "list", "watch"]},
    {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": _ALL_VERBS},
    {"apiGroups": ["apps"], "resources": ["replicasets"], "verbs": ["list"]},
]


def _primary_env(namespace: str, image: str) -> list[dict[str, str]]:
    """Return the environment the runtime chart gives a primary whose workers are Kubernetes Deployments."""
    env = {
        "MINDROOM_STORAGE_PATH": _STORAGE_PATH,
        "MINDROOM_WORKER_BACKEND": "kubernetes",
        "MINDROOM_SANDBOX_EXECUTION_MODE": "selective",
        "MINDROOM_SANDBOX_PROXY_TOOLS": "shell,file,python,coding",
        "MINDROOM_SANDBOX_PROXY_TOKEN": secrets.token_hex(16),
        "MINDROOM_KUBERNETES_WORKER_NAMESPACE": namespace,
        "MINDROOM_KUBERNETES_WORKER_IMAGE": image,
        # The image is loaded into kind, never pulled.
        "MINDROOM_KUBERNETES_WORKER_IMAGE_PULL_POLICY": "IfNotPresent",
        "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME": _NAME,
        "MINDROOM_KUBERNETES_WORKER_STORAGE_MOUNT_PATH": _STORAGE_PATH,
        "MINDROOM_KUBERNETES_WORKER_READY_TIMEOUT_SECONDS": "300",
    }
    return [{"name": name, "value": value} for name, value in env.items()]


def _manifests(namespace: str, image: str) -> dict[str, object]:
    """Return the namespace and, in it, a primary's access, storage and pod, whose command is the probe."""

    def metadata() -> dict[str, str]:
        return {"name": _NAME, "namespace": namespace}

    pod_spec = {
        "serviceAccountName": _NAME,
        "restartPolicy": "Never",
        "securityContext": {"fsGroup": 1000, "fsGroupChangePolicy": "OnRootMismatch"},
        "containers": [
            {
                "name": "probe",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["python", f"/smoke/{_PROBE.name}"],
                "env": _primary_env(namespace, image),
                "securityContext": {
                    "runAsNonRoot": True,
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                },
                "volumeMounts": [
                    {"name": "storage", "mountPath": _STORAGE_PATH},
                    {"name": "probe", "mountPath": "/smoke", "readOnly": True},
                ],
            },
        ],
        "volumes": [
            {"name": "storage", "persistentVolumeClaim": {"claimName": _NAME}},
            {"name": "probe", "configMap": {"name": _NAME}},
        ],
    }
    items = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}},
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": metadata()},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": metadata(), "rules": _ROLE_RULES},
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": metadata(),
            "subjects": [{"kind": "ServiceAccount", "name": _NAME, "namespace": namespace}],
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": _NAME},
        },
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": metadata(),
            "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}},
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": metadata(),
            "data": {_PROBE.name: _PROBE.read_text(encoding="utf-8")},
        },
        {"apiVersion": "v1", "kind": "Pod", "metadata": metadata(), "spec": pod_spec},
    ]
    return {"apiVersion": "v1", "kind": "List", "items": items}


def _delete_namespace(namespace: str, *, check: bool) -> None:
    """Delete the namespace with every worker in it, and wait until it is gone."""
    run_command(["kubectl", "delete", "namespace", namespace, "--ignore-not-found", "--timeout=300s"], check=check)


def _probe_phase(namespace: str) -> str:
    result = run_command(
        ["kubectl", "get", "pod", _NAME, "-n", namespace, "-o", "jsonpath={.status.phase}"],
        capture_output=True,
    )
    return result.stdout.strip()


def _wait_for_probe(namespace: str, timeout_seconds: int) -> None:
    """Wait until the probe's pod has ended, and raise unless it succeeded."""
    deadline = time.monotonic() + timeout_seconds
    while (phase := _probe_phase(namespace)) not in {"Succeeded", "Failed"}:
        if time.monotonic() >= deadline:
            msg = f"The probe pod was still {phase or 'unscheduled'} after {timeout_seconds}s"
            raise RuntimeError(msg)
        time.sleep(5)
    if phase != "Succeeded":
        msg = "The probe failed"
        raise RuntimeError(msg)


def _dump_diagnostics(namespace: str) -> None:
    """Best-effort Kubernetes diagnostics for a failed smoke."""
    error(f"[diagnostics] Dumping Kubernetes state for namespace {namespace}")
    commands = [
        ["kubectl", "get", "pods,services,deployments,persistentvolumeclaims", "-n", namespace, "-o", "wide"],
        ["kubectl", "describe", "pods", "-n", namespace],
        ["kubectl", "logs", "-n", namespace, "-l", "mindroom.ai/component=worker", "--tail=200"],
        ["kubectl", "get", "events", "-n", namespace, "--sort-by=.lastTimestamp"],
    ]
    for command in commands:
        error(f"[diagnostics] $ {' '.join(command)}")
        run_command(command, check=False)


def main() -> int:
    """Run the smoke."""
    namespace = os.getenv("TAILNET_SSH_SMOKE_NAMESPACE", "mindroom-tailnet-ssh-smoke")
    image = os.getenv("MINDROOM_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
    timeout_seconds = getenv_int("TAILNET_SSH_SMOKE_TIMEOUT_SECONDS", 900)
    try:
        # A pod an earlier run left would be read as this run's.
        _delete_namespace(namespace, check=True)
        log(f"[smoke] Running the tailnet SSH worker probe in namespace {namespace} with {image}")
        subprocess.run(
            ["kubectl", "apply", "-f", "-"],
            input=json.dumps(_manifests(namespace, image)),
            text=True,
            check=True,
        )
        try:
            _wait_for_probe(namespace, timeout_seconds)
        finally:
            run_command(["kubectl", "logs", _NAME, "-n", namespace], check=False)
    except Exception as exc:
        error(f"[error] smoke_tailnet_ssh_worker.py failed: {exc}")
        _dump_diagnostics(namespace)
        return 1
    finally:
        _delete_namespace(namespace, check=False)
    log("[smoke] tailnet SSH worker checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
