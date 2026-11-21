"""Smoke the Kubernetes worker backend's tailnet SSH worker against a kind cluster's API server.

Run via ``python -m cluster.k8s.kind.smoke_tailnet_ssh_worker`` from the repo root, once the MindRoom image is loaded
into the kind cluster. It applies the runtime chart to a namespace it creates, and runs ``tailnet_ssh_worker_probe.py``
in the pod the chart gives a primary, with the probe as its command: only that pod has the primary's access and
environment, reaches a worker's Service, and mounts the storage claim workers mount. It fails when the probe does.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from scripts.smoke_helpers import ROOT_DIR, error, getenv_int, log, run_command

_CHART = "cluster/k8s/runtime"
# The chart's release, and so the primary's Deployment.
_RELEASE = "tailnet-ssh-smoke"
_PROBE = Path(__file__).with_name("tailnet_ssh_worker_probe.py")
# The probe's pod, and the ConfigMap that holds its source.
_PROBE_NAME = "tailnet-ssh-probe"
# What only the pod the Deployment runs needs: the probe's pod serves nothing and ends by itself.
_SERVING_KEYS = {"args", "ports", "startupProbe", "readinessProbe", "livenessProbe"}


def _chart_values(image: str) -> dict[str, object]:
    """Return the runtime chart's values for a primary whose workers are Kubernetes Deployments of ``image``."""
    repository, _, tag = image.rpartition(":")
    if not repository or "/" in tag:
        msg = f"MINDROOM_IMAGE must be repository:tag, got {image!r}"
        raise ValueError(msg)
    return {
        "fullnameOverride": _RELEASE,
        # The Deployment owns the workers and runs no pod: the probe's pod stands in for it.
        "replicaCount": 0,
        # The image is loaded into kind, never pulled.
        "image": {"repository": repository, "tag": tag, "pullPolicy": "IfNotPresent"},
        # The probe syncs no Matrix events, so the chart need not run PostgreSQL.
        "eventCache": {"backend": "sqlite"},
        "workers": {
            "backend": "kubernetes",
            "sandbox": {"proxyToken": {"value": secrets.token_hex(16)}},
            "kubernetes": {"readyTimeoutSeconds": 300},
        },
    }


def _require_kind(kubectl: list[str]) -> None:
    """Raise unless every node of the context's cluster is a kind node, before anything is created or deleted."""
    nodes = json.loads(run_command([*kubectl, "get", "nodes", "-o", "json"], capture_output=True).stdout)["items"]
    if not nodes or not all(node["spec"].get("providerID", "").startswith("kind://") for node in nodes):
        msg = "The context's cluster is not a kind cluster"
        raise RuntimeError(msg)


def _apply(kubectl: list[str], namespace: str, manifests: str) -> None:
    command = [*kubectl, "apply", "-n", namespace, "-f", "-"]
    subprocess.run(command, input=manifests, text=True, check=True, cwd=ROOT_DIR)


def _install_chart(kubectl: list[str], namespace: str, image: str) -> dict[str, Any]:
    """Apply what the runtime chart renders, and return the primary's Deployment as the API server holds it."""
    with tempfile.NamedTemporaryFile("w", suffix=".json") as values:
        json.dump(_chart_values(image), values)
        values.flush()
        chart = run_command(
            ["helm", "template", _RELEASE, _CHART, "--namespace", namespace, "--values", values.name],
            capture_output=True,
        ).stdout
    _apply(kubectl, namespace, chart)
    deployment = run_command(
        [*kubectl, "get", "deployment", _RELEASE, "-n", namespace, "-o", "json"],
        capture_output=True,
    ).stdout
    return json.loads(deployment)


def _probe_manifests(deployment: dict[str, Any], namespace: str) -> dict[str, object]:
    """Return the probe's source and its pod, which is the primary's pod of ``deployment`` with the probe as its command.

    The service account, environment, mounts and security context are the chart's, so the probe has what the chart
    gives a primary and nothing else. The pod's labels are the chart's too: by them the workers' NetworkPolicy admits it.
    """
    template = deployment["spec"]["template"]
    [primary] = template["spec"]["containers"]
    probe = {key: value for key, value in primary.items() if key not in _SERVING_KEYS} | {
        "command": ["python", f"/smoke/{_PROBE.name}"],
        "volumeMounts": [*primary["volumeMounts"], {"name": "smoke", "mountPath": "/smoke", "readOnly": True}],
    }
    spec = template["spec"] | {
        "restartPolicy": "Never",
        "containers": [probe],
        "volumes": [*template["spec"]["volumes"], {"name": "smoke", "configMap": {"name": _PROBE_NAME}}],
    }
    metadata = {"name": _PROBE_NAME, "namespace": namespace}
    items = [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": metadata,
            "data": {_PROBE.name: _PROBE.read_text(encoding="utf-8")},
        },
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": metadata | {"labels": template["metadata"]["labels"]},
            "spec": spec,
        },
    ]
    return {"apiVersion": "v1", "kind": "List", "items": items}


def _probe_phase(kubectl: list[str], namespace: str) -> str:
    result = run_command(
        [*kubectl, "get", "pod", _PROBE_NAME, "-n", namespace, "-o", "jsonpath={.status.phase}"],
        capture_output=True,
    )
    return result.stdout.strip()


def _wait_for_probe(kubectl: list[str], namespace: str, timeout_seconds: int) -> None:
    """Wait until the probe's pod has ended, and raise unless it succeeded."""
    deadline = time.monotonic() + timeout_seconds
    while (phase := _probe_phase(kubectl, namespace)) not in {"Succeeded", "Failed"}:
        if time.monotonic() >= deadline:
            msg = f"The probe pod was still {phase or 'unscheduled'} after {timeout_seconds}s"
            raise RuntimeError(msg)
        time.sleep(5)
    if phase != "Succeeded":
        msg = "The probe failed"
        raise RuntimeError(msg)


def _report(exc: Exception) -> None:
    error(f"[error] smoke_tailnet_ssh_worker.py failed: {exc}")
    if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
        error(exc.stderr)


def _dump_diagnostics(kubectl: list[str], namespace: str) -> None:
    """Best-effort Kubernetes diagnostics for a failed smoke."""
    error(f"[diagnostics] Dumping Kubernetes state for namespace {namespace}")
    commands = [
        ["get", "pods,services,deployments,persistentvolumeclaims", "-n", namespace, "-o", "wide"],
        ["describe", "pods", "-n", namespace],
        ["logs", "-n", namespace, "-l", "mindroom.ai/component=worker", "--tail=200"],
        ["get", "events", "-n", namespace, "--sort-by=.lastTimestamp"],
    ]
    for command in commands:
        error(f"[diagnostics] $ kubectl {' '.join(command)}")
        run_command([*kubectl, *command], check=False)


def main() -> int:
    """Run the smoke."""
    # Named in every kubectl call, so the kubeconfig's current context is never the target.
    context = os.getenv("TAILNET_SSH_SMOKE_CONTEXT", "kind-mindroom")
    image = os.getenv("MINDROOM_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
    timeout_seconds = getenv_int("TAILNET_SSH_SMOKE_TIMEOUT_SECONDS", 900)
    kubectl = ["kubectl", "--context", context]
    # This run's own: creating it fails if it exists, and it is deleted only once this run has created it.
    namespace = f"{_RELEASE}-{secrets.token_hex(4)}"
    try:
        _require_kind(kubectl)
        run_command([*kubectl, "create", "namespace", namespace])
    except Exception as exc:
        _report(exc)
        return 1
    try:
        log(f"[smoke] Running the tailnet SSH worker probe in namespace {namespace} of {context} with {image}")
        deployment = _install_chart(kubectl, namespace, image)
        _apply(kubectl, namespace, json.dumps(_probe_manifests(deployment, namespace)))
        try:
            _wait_for_probe(kubectl, namespace, timeout_seconds)
        finally:
            run_command([*kubectl, "logs", _PROBE_NAME, "-n", namespace], check=False)
    except Exception as exc:
        _report(exc)
        _dump_diagnostics(kubectl, namespace)
        return 1
    finally:
        run_command([*kubectl, "delete", "namespace", namespace, "--timeout=300s"], check=False)
    log("[smoke] tailnet SSH worker checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
