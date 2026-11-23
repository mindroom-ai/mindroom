"""Check an SSH worker in kind using probe pods derived from the runtime chart."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from scripts.smoke_helpers import ROOT_DIR, error, getenv_int, log, run_command

_CHART = "cluster/k8s/runtime"
_RELEASE = "tailnet-ssh-smoke"
_PROBE = Path(__file__).with_name("tailnet_ssh_worker_probe.py")
_GATEWAY_STAND_IN = Path(__file__).with_name("tailnet_ssh_gateway_stand_in.py")
_SERVING_KEYS = {"args", "startupProbe", "readinessProbe", "livenessProbe"}


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


def _render(namespace: str, image: str) -> list[dict[str, Any]]:
    chart = subprocess.run(
        ["helm", "template", _RELEASE, _CHART, "-n", namespace, "-f", "-"],
        input=json.dumps(_chart_values(image)),
        capture_output=True,
        check=True,
        text=True,
        cwd=ROOT_DIR,
    )
    return [doc for doc in yaml.safe_load_all(chart.stdout) if doc]


def _manifest(docs: list[dict[str, Any]]) -> dict[str, object]:
    deployments = {doc["metadata"]["name"]: doc["spec"]["template"] for doc in docs if doc["kind"] == "Deployment"}
    gateway_name = f"{_RELEASE}-tailnet-ssh"
    primary, gateway = deployments[_RELEASE], deployments[gateway_name]
    # Keep the scaled-to-zero primary Deployment: it owns the worker resources.
    items = [doc for doc in docs if (doc["kind"], doc["metadata"]["name"]) != ("Deployment", gateway_name)]
    for template, script in ((primary, _PROBE), (gateway, _GATEWAY_STAND_IN)):
        spec = template["spec"]
        [original] = spec["containers"]
        container = {k: v for k, v in original.items() if k not in _SERVING_KEYS}
        container["command"] = ["python", "-c", script.read_text(encoding="utf-8")]
        volumes = spec["volumes"]
        if template is gateway:  # Retain the identity, not the gateway credentials, image or state.
            runtime = primary["spec"]["containers"][0]
            container.update(image=runtime["image"], imagePullPolicy=runtime["imagePullPolicy"])
            container["env"] = [v for v in original["env"] if v["name"] == "TAILNET_SSH_PRIMARY_URL"]
            container.pop("volumeMounts")
            volumes = []
        items.append(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": template["metadata"] | {"name": script.stem.replace("_", "-")},
                "spec": spec | {"restartPolicy": "Never", "containers": [container], "volumes": volumes},
            },
        )
    return {"apiVersion": "v1", "kind": "List", "items": items}


def main() -> int:
    """Use only the named kind context and delete only the namespace we created."""
    context = os.getenv("TAILNET_SSH_SMOKE_CONTEXT", "kind-mindroom")
    kubectl = ["kubectl", "--context", context]
    namespace = f"{_RELEASE}-{secrets.token_hex(4)}"
    ns = [*kubectl, "-n", namespace]
    if not context.strip():
        error("TAILNET_SSH_SMOKE_CONTEXT names no kubeconfig context")
        return 1
    created = False
    try:
        nodes = json.loads(run_command([*kubectl, "get", "nodes", "-o", "json"], capture_output=True).stdout)["items"]
        if not nodes or not all(node["spec"].get("providerID", "").startswith("kind://") for node in nodes):
            error("The context's cluster is not a kind cluster")
            return 1
        run_command([*kubectl, "create", "namespace", namespace])
        created = True
        image = os.getenv("MINDROOM_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
        subprocess.run(
            [*ns, "apply", "-f", "-"],
            input=json.dumps(_manifest(_render(namespace, image))),
            text=True,
            check=True,
        )
        run_command(
            [
                *ns,
                "wait",
                "--for=jsonpath={.status.phase}=Succeeded",
                "pod/tailnet-ssh-worker-probe",
                f"--timeout={getenv_int('TAILNET_SSH_SMOKE_TIMEOUT_SECONDS', 900)}s",
            ],
        )
    except (OSError, ValueError, subprocess.CalledProcessError, yaml.YAMLError) as exc:
        error(f"[error] tailnet SSH smoke failed: {exc}")
        if created:
            run_command([*ns, "describe", "pods"], check=False)
        return 1
    finally:
        if created:
            for script in (_PROBE, _GATEWAY_STAND_IN):
                run_command([*ns, "logs", script.stem.replace("_", "-")], check=False)
            run_command([*kubectl, "delete", "namespace", namespace, "--timeout=300s"], check=False)
    log("[smoke] tailnet SSH worker checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
