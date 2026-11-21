"""Smoke the Kubernetes worker backend's tailnet SSH worker against a kind cluster's API server.

Run via ``python -m cluster.k8s.kind.smoke_tailnet_ssh_worker`` from the repo root, once the MindRoom image is loaded
into the kind cluster. It applies the runtime chart to a namespace it creates, and runs ``tailnet_ssh_worker_probe.py``
in the pod the chart gives a primary, with the probe as its command: only that pod has the primary's access and
environment, reaches a worker's Service, and mounts the storage claim workers mount. It fails when the probe does.
The chart's gateway is not run. ``tailnet_ssh_gateway_stand_in.py`` runs in the pod the chart gives it, and reads
there what a worker's sshd sends through its Service, as the chart's network policies let the gateway alone.
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
# What only the pod the Deployment runs needs: the probe's pod serves no API and ends by itself. Its ports stay,
# since the Service the gateway dials routes to the one named for it.
_SERVING_KEYS = {"args", "startupProbe", "readinessProbe", "livenessProbe"}
_GATEWAY_STAND_IN = Path(__file__).with_name("tailnet_ssh_gateway_stand_in.py")
_GATEWAY_STAND_IN_NAME = "tailnet-ssh-gateway-stand-in"
# Of what the chart renders, the gateway's Deployment alone.
_GATEWAY_SELECTOR = "app.kubernetes.io/component=tailnet-ssh"


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
        # With the gateway's Deployment, and the policy that admits its pods to a marked worker's sshd.
        "tailnetSsh": {"enabled": True},
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


def _apply(kubectl: list[str], namespace: str, manifests: str, *selector: str) -> None:
    command = [*kubectl, "apply", "-n", namespace, *selector, "-f", "-"]
    subprocess.run(command, input=manifests, text=True, check=True, cwd=ROOT_DIR)


def _install_chart(kubectl: list[str], namespace: str, image: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply what the runtime chart renders but for the gateway, and return the primary's Deployment and the gateway's.

    The primary's is as the API server holds it. The gateway's is as kubectl would send it: a gateway that ran would
    ask to join a tailnet, and its image is not in kind.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".json") as values:
        json.dump(_chart_values(image), values)
        values.flush()
        chart = run_command(
            ["helm", "template", _RELEASE, _CHART, "--namespace", namespace, "--values", values.name],
            capture_output=True,
        ).stdout
    _apply(kubectl, namespace, chart, "-l", _GATEWAY_SELECTOR.replace("=", "!="))
    gateway = subprocess.run(
        [*kubectl, "apply", "--dry-run=client", "-o", "json", "-n", namespace, "-l", _GATEWAY_SELECTOR, "-f", "-"],
        input=chart,
        text=True,
        check=True,
        capture_output=True,
        cwd=ROOT_DIR,
    ).stdout
    deployment = run_command(
        [*kubectl, "get", "deployment", _RELEASE, "-n", namespace, "-o", "json"],
        capture_output=True,
    ).stdout
    return json.loads(deployment), json.loads(gateway)


def _pod_manifests(deployment: dict[str, Any], gateway: dict[str, Any], namespace: str) -> dict[str, object]:
    """Return the two sources and their pods: the primary's pod of ``deployment`` and the gateway's pod of ``gateway``.

    Each pod is the chart's with the source as its command. The probe's service account, environment, mounts and
    security context are the primary's, so it has what the chart gives a primary and nothing else. Both pods keep the
    chart's labels: by them the chart's network policies admit the primary to a worker's runner, and the gateway to
    a marked worker's sshd and to the primary.
    """
    source = {"name": "smoke", "configMap": {"name": _PROBE_NAME}}
    source_mount = {"name": "smoke", "mountPath": "/smoke", "readOnly": True}
    template = deployment["spec"]["template"]
    [primary] = template["spec"]["containers"]
    probe = {key: value for key, value in primary.items() if key not in _SERVING_KEYS} | {
        "command": ["python", f"/smoke/{_PROBE.name}"],
        "volumeMounts": [*primary["volumeMounts"], source_mount],
    }
    spec = template["spec"] | {
        "restartPolicy": "Never",
        "containers": [probe],
        "volumes": [*template["spec"]["volumes"], source],
    }
    gateway_template = gateway["spec"]["template"]
    [gateway_container] = gateway_template["spec"]["containers"]
    stand_in = gateway_container | {
        # The gateway's image is not in kind, and the primary's has Python.
        "image": primary["image"],
        "imagePullPolicy": primary["imagePullPolicy"],
        "command": ["python", f"/smoke/{_GATEWAY_STAND_IN.name}"],
        "volumeMounts": [source_mount],
    }
    # Without the gateway's volumes: the claim that holds its node keys is not the stand-in's to write.
    gateway_spec = gateway_template["spec"] | {"restartPolicy": "Never", "containers": [stand_in], "volumes": [source]}
    items = [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": _PROBE_NAME, "namespace": namespace},
            "data": {path.name: path.read_text(encoding="utf-8") for path in (_PROBE, _GATEWAY_STAND_IN)},
        },
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": _PROBE_NAME, "namespace": namespace, "labels": template["metadata"]["labels"]},
            "spec": spec,
        },
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": _GATEWAY_STAND_IN_NAME,
                "namespace": namespace,
                "labels": gateway_template["metadata"]["labels"],
            },
            "spec": gateway_spec,
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
        ["get", "networkpolicies,endpoints", "-n", namespace, "-o", "wide"],
        ["get", "events", "-n", namespace, "--sort-by=.lastTimestamp"],
    ]
    for command in commands:
        error(f"[diagnostics] $ kubectl {' '.join(command)}")
        run_command([*kubectl, *command], check=False)


def main() -> int:
    """Run the smoke."""
    # Named in every kubectl call, so the kubeconfig's current context is never the target.
    context = os.getenv("TAILNET_SSH_SMOKE_CONTEXT", "kind-mindroom")
    if not context.strip():
        # kubectl takes an empty --context for none given, and uses the current one.
        error("[error] smoke_tailnet_ssh_worker.py failed: TAILNET_SSH_SMOKE_CONTEXT names no kubeconfig context")
        return 1
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
        deployment, gateway = _install_chart(kubectl, namespace, image)
        _apply(kubectl, namespace, json.dumps(_pod_manifests(deployment, gateway, namespace)))
        try:
            _wait_for_probe(kubectl, namespace, timeout_seconds)
        finally:
            for pod in (_PROBE_NAME, _GATEWAY_STAND_IN_NAME):
                run_command([*kubectl, "logs", pod, "-n", namespace], check=False)
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
