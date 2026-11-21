"""Check an SSH worker in kind using probe pods derived from the runtime chart."""

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
_RELEASE = "tailnet-ssh-smoke"
_PROBE = Path(__file__).with_name("tailnet_ssh_worker_probe.py")
_PROBE_NAME = "tailnet-ssh-probe"
_SERVING_KEYS = {"args", "startupProbe", "readinessProbe", "livenessProbe"}
_GATEWAY_STAND_IN = Path(__file__).with_name("tailnet_ssh_gateway_stand_in.py")
_GATEWAY_STAND_IN_NAME = "tailnet-ssh-gateway-stand-in"
_GATEWAY_SELECTOR = "app.kubernetes.io/component=tailnet-ssh"


def _chart_values(image: str) -> dict[str, object]:
    repository, _, tag = image.rpartition(":")
    if not repository or not tag or "/" in tag:
        msg = f"MINDROOM_IMAGE must be repository:tag, got {image!r}"
        raise ValueError(msg)
    return {
        "fullnameOverride": _RELEASE,
        "replicaCount": 0,
        "image": {"repository": repository, "tag": tag, "pullPolicy": "IfNotPresent"},
        "eventCache": {"backend": "sqlite"},
        "tailnetSsh": {"enabled": True, "authKeySecret": "render-only"},
        "workers": {
            "backend": "kubernetes",
            "sandbox": {"proxyToken": {"value": secrets.token_hex(16)}},
            "kubernetes": {"readyTimeoutSeconds": 300},
        },
    }


def _apply(kubectl: list[str], namespace: str, manifests: str, *flags: str) -> str:
    command = [*kubectl, "apply", *flags, "-n", namespace, "-f", "-"]
    return subprocess.run(command, input=manifests, text=True, check=True, cwd=ROOT_DIR, stdout=subprocess.PIPE).stdout


def _install_chart(kubectl: list[str], namespace: str, image: str) -> tuple[dict[str, Any], dict[str, Any]]:
    with tempfile.NamedTemporaryFile("w", suffix=".json") as values:
        json.dump(_chart_values(image), values)
        values.flush()
        chart = run_command(
            ["helm", "template", _RELEASE, _CHART, "--namespace", namespace, "--values", values.name],
            capture_output=True,
        ).stdout
    # Never run a real gateway or require a real tailnet key.
    log(_apply(kubectl, namespace, chart, "-l", _GATEWAY_SELECTOR.replace("=", "!=")))
    gateway = _apply(kubectl, namespace, chart, "--dry-run=client", "-o", "json", "-l", _GATEWAY_SELECTOR)
    primary = run_command([*kubectl, "get", "deployment", _RELEASE, "-n", namespace, "-o", "json"], capture_output=True)
    return json.loads(primary.stdout), json.loads(gateway)


def _pod_manifests(deployment: dict[str, Any], gateway: dict[str, Any], namespace: str) -> dict[str, object]:
    source = {"name": "smoke", "configMap": {"name": _PROBE_NAME}}
    mount = {"name": "smoke", "mountPath": "/smoke", "readOnly": True}
    primary = deployment["spec"]["template"]["spec"]["containers"][0]

    def pod(owner: dict[str, Any], name: str, script: Path, *, stand_in: bool = False) -> dict[str, Any]:
        template = owner["spec"]["template"]
        spec = template["spec"]
        [container] = spec["containers"]
        command = {key: value for key, value in container.items() if key not in _SERVING_KEYS}
        command["command"] = ["python", f"/smoke/{script.name}"]
        if stand_in:
            command.update(image=primary["image"], imagePullPolicy=primary["imagePullPolicy"])
            command["env"] = [env for env in container["env"] if env["name"] != "TAILNET_SSH_AUTH_KEY"]
        # The stand-in doesn't mount the node-key PVC. Both pods retain chart labels and security context.
        command["volumeMounts"] = [*([] if stand_in else container["volumeMounts"]), mount]
        volumes = [] if stand_in else spec["volumes"]
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace, "labels": template["metadata"]["labels"]},
            "spec": spec | {"restartPolicy": "Never", "containers": [command], "volumes": [*volumes, source]},
        }

    config = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": _PROBE_NAME, "namespace": namespace},
        "data": {file.name: file.read_text(encoding="utf-8") for file in (_PROBE, _GATEWAY_STAND_IN)},
    }
    probe = pod(deployment, _PROBE_NAME, _PROBE)
    stand_in = pod(gateway, _GATEWAY_STAND_IN_NAME, _GATEWAY_STAND_IN, stand_in=True)
    return {"apiVersion": "v1", "kind": "List", "items": [config, probe, stand_in]}


def _wait_for_probe(kubectl: list[str], namespace: str, seconds: int) -> None:
    deadline = time.monotonic() + seconds
    command = [*kubectl, "get", "pod", _PROBE_NAME, "-n", namespace, "-o", "jsonpath={.status.phase}"]
    while (phase := run_command(command, capture_output=True).stdout.strip()) != "Succeeded":
        if phase == "Failed" or time.monotonic() >= deadline:
            msg = f"Probe did not pass: {phase or 'unscheduled'} (timeout {seconds}s)"
            raise RuntimeError(msg)
        time.sleep(5)


def _require_kind(kubectl: list[str], context: str) -> None:
    if not context.strip():
        msg = "TAILNET_SSH_SMOKE_CONTEXT names no kubeconfig context"
        raise ValueError(msg)
    nodes = json.loads(run_command([*kubectl, "get", "nodes", "-o", "json"], capture_output=True).stdout)["items"]
    if not nodes or not all(node["spec"].get("providerID", "").startswith("kind://") for node in nodes):
        msg = "The context's cluster is not a kind cluster"
        raise RuntimeError(msg)


def main() -> int:
    """Use only the named kind context and delete only the namespace we created."""
    context = os.getenv("TAILNET_SSH_SMOKE_CONTEXT", "kind-mindroom")
    image = os.getenv("MINDROOM_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
    kubectl = ["kubectl", "--context", context]
    namespace = f"{_RELEASE}-{secrets.token_hex(4)}"
    created = False
    try:
        _require_kind(kubectl, context)
        run_command([*kubectl, "create", "namespace", namespace])
        created = True
        log(f"[smoke] Running {namespace} in {context} with {image}")
        deployment, gateway = _install_chart(kubectl, namespace, image)
        log(_apply(kubectl, namespace, json.dumps(_pod_manifests(deployment, gateway, namespace))))
        _wait_for_probe(kubectl, namespace, getenv_int("TAILNET_SSH_SMOKE_TIMEOUT_SECONDS", 900))
    except Exception as exc:
        error(f"[error] smoke_tailnet_ssh_worker.py failed: {exc}")
        if created:
            run_command([*kubectl, "describe", "pods", "-n", namespace], check=False)
            run_command([*kubectl, "get", "services,endpoints,networkpolicies,pvc", "-n", namespace], check=False)
            worker_logs = [*kubectl, "logs", "-n", namespace, "-l", "mindroom.ai/component=worker", "--tail=200"]
            run_command(worker_logs, check=False)
        return 1
    finally:
        if created:
            for pod in (_PROBE_NAME, _GATEWAY_STAND_IN_NAME):
                run_command([*kubectl, "logs", pod, "-n", namespace], check=False)
            run_command([*kubectl, "delete", "namespace", namespace, "--timeout=300s"], check=False)
    log("[smoke] tailnet SSH worker checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
