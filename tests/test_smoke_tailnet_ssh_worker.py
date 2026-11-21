"""Tests for the tailnet SSH worker smoke's choice of cluster and namespace, and what it takes from the runtime chart."""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import socket
import subprocess
import threading
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from cluster.k8s.kind import smoke_tailnet_ssh_worker as smoke
from cluster.k8s.kind import tailnet_ssh_gateway_stand_in as stand_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_KUBECTL = ["kubectl", "--context", "kind-test"]
_PRIMARY = {
    "name": "mindroom",
    "image": "mindroom:test",
    "command": ["tini", "--", "/app/.venv/bin/mindroom", "run"],
    "imagePullPolicy": "IfNotPresent",
    "args": ["--api-port", "8765"],
    "ports": [{"name": "api", "containerPort": 8765}, {"name": "tailnet-ssh", "containerPort": 8769}],
    "env": [{"name": "MINDROOM_WORKER_BACKEND", "value": "kubernetes"}],
    "envFrom": [{"secretRef": {"name": "api-key"}}],
    "startupProbe": {"httpGet": {"path": "/api/health", "port": "api"}},
    "readinessProbe": {"httpGet": {"path": "/api/ready", "port": "api"}},
    "livenessProbe": {"httpGet": {"path": "/api/health", "port": "api"}},
    "securityContext": {"runAsNonRoot": True},
    "volumeMounts": [{"name": "storage", "mountPath": "/app/agent_data"}],
}
_DEPLOYMENT = {
    "spec": {
        "template": {
            "metadata": {"labels": {"app.kubernetes.io/component": "runtime"}},
            "spec": {
                "serviceAccountName": "primary",
                "securityContext": {"fsGroup": 1000},
                "containers": [_PRIMARY],
                "volumes": [{"name": "storage", "persistentVolumeClaim": {"claimName": "storage"}}],
            },
        },
    },
}


_GATEWAY_CONTAINER = {
    "name": "tailnet-gateway",
    "image": "gateway:test",
    "env": [{"name": "TAILNET_SSH_PRIMARY_URL", "value": "http://primary.smoke.svc.cluster.local:8769"}],
    "securityContext": {"readOnlyRootFilesystem": True},
    "volumeMounts": [{"name": "state", "mountPath": "/state"}],
}
_GATEWAY = {
    "spec": {
        "template": {
            "metadata": {"labels": {"app.kubernetes.io/component": "tailnet-ssh"}},
            "spec": {
                "automountServiceAccountToken": False,
                "securityContext": {"fsGroup": 65532},
                "containers": [_GATEWAY_CONTAINER],
                "volumes": [{"name": "state", "persistentVolumeClaim": {"claimName": "gateway"}}],
            },
        },
    },
}


class _Tools:
    """Stands in for kubectl and helm: records every command, and answers those the smoke reads."""

    def __init__(self, *, provider_ids: tuple[str | None, ...] = ("kind://docker/test/node",)) -> None:
        self.commands: list[list[str]] = []
        self.namespace_exists = False
        self._provider_ids = provider_ids

    def run(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=self._answer(command), stderr="")

    def _answer(self, command: list[str]) -> str:
        if command[0] == "helm":
            return "chart"
        if "--dry-run=client" in command:
            return json.dumps(_GATEWAY)
        match command[3:5]:
            case ["get", "nodes"]:
                nodes = [
                    {"spec": {"providerID": provider_id} if provider_id else {}} for provider_id in self._provider_ids
                ]
                return json.dumps({"items": nodes})
            case ["create", "namespace"] if self.namespace_exists:
                raise subprocess.CalledProcessError(1, command, stderr="AlreadyExists")
            case ["get", "deployment"]:
                return json.dumps(_DEPLOYMENT)
            case ["get", "pod"]:
                return "Succeeded"
        return ""

    def namespaces(self, verb: str) -> list[str]:
        """Return the namespaces the smoke ran ``kubectl <verb> namespace`` on."""
        return [command[5] for command in self.commands if command[3:5] == [verb, "namespace"]]


def _run_smoke(monkeypatch: pytest.MonkeyPatch, tools: _Tools) -> int:
    monkeypatch.setenv("TAILNET_SSH_SMOKE_CONTEXT", "kind-test")
    # The probe's pod has ended when first asked, so a smoke that asks again fails instead of waiting.
    monkeypatch.setenv("TAILNET_SSH_SMOKE_TIMEOUT_SECONDS", "0")
    monkeypatch.setattr(smoke, "run_command", tools.run)
    monkeypatch.setattr(smoke.subprocess, "run", tools.run)
    return smoke.main()


_NOT_KIND = [("aws:///eu-west-1a/i-0abc",), (None,), (), ("kind://docker/test/node", None)]


@pytest.mark.parametrize("provider_ids", _NOT_KIND)
def test_a_cluster_that_is_not_kind_is_left_untouched(
    monkeypatch: pytest.MonkeyPatch,
    provider_ids: tuple[str | None, ...],
) -> None:
    """Whatever the context points at, nothing is created or deleted unless every node is a kind node."""
    tools = _Tools(provider_ids=provider_ids)

    assert _run_smoke(monkeypatch, tools) == 1
    assert tools.commands == [[*_KUBECTL, "get", "nodes", "-o", "json"]]


@pytest.mark.parametrize("context", ["", "  "])
def test_a_context_that_names_nothing_is_refused(monkeypatch: pytest.MonkeyPatch, context: str) -> None:
    """An empty ``--context`` makes kubectl use the kubeconfig's current context, so no command is run with one."""
    tools = _Tools()
    monkeypatch.setattr(smoke, "run_command", tools.run)
    monkeypatch.setattr(smoke.subprocess, "run", tools.run)
    monkeypatch.setenv("TAILNET_SSH_SMOKE_CONTEXT", context)

    assert smoke.main() == 1
    assert tools.commands == []


def test_a_namespace_the_run_could_not_create_is_not_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A namespace that already exists is someone else's: the run fails and neither writes to it nor deletes it."""
    tools = _Tools()
    tools.namespace_exists = True

    assert _run_smoke(monkeypatch, tools) == 1
    assert [command[3] for command in tools.commands] == ["get", "create"]


def test_the_run_deletes_only_the_namespace_it_created_in_the_context_it_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each run makes a namespace of its own, deletes that one alone, and never leaves the context to kubectl."""
    tools = _Tools()

    assert _run_smoke(monkeypatch, tools) == 0
    [created] = tools.namespaces("create")
    assert re.fullmatch(r"tailnet-ssh-smoke-[0-9a-f]{8}", created)
    assert tools.namespaces("delete") == [created]
    kubectl = [command for command in tools.commands if command[0] == "kubectl"]
    assert all(command[:3] == _KUBECTL for command in kubectl)
    assert all(created in command for command in kubectl if command[3] != "get" or command[4] != "nodes")


def test_the_charts_gateway_is_read_and_never_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chart is applied without the gateway's Deployment, which is only read: no gateway joins a tailnet."""
    tools = _Tools()

    assert _run_smoke(monkeypatch, tools) == 0
    chart, gateway, pods = [command[4:] for command in tools.commands if command[3] == "apply"]
    namespace = chart[1]
    assert chart == ["-n", namespace, "-l", "app.kubernetes.io/component!=tailnet-ssh", "-f", "-"]
    assert gateway == [
        "--dry-run=client",
        "-o",
        "json",
        "-n",
        namespace,
        "-l",
        "app.kubernetes.io/component=tailnet-ssh",
        "-f",
        "-",
    ]
    assert pods == ["-n", namespace, "-f", "-"]


def test_the_probe_runs_in_the_charts_primary_pod_with_only_its_command_replaced() -> None:
    """The probe's access and environment are the chart's: its pod is the Deployment's, minus what serves the API."""
    manifests = smoke._pod_manifests(_DEPLOYMENT, _GATEWAY, "smoke")

    source, pod, _gateway = manifests["items"]
    [probe] = pod["spec"]["containers"]
    assert source["data"] == {
        path.name: path.read_text(encoding="utf-8") for path in (smoke._PROBE, smoke._GATEWAY_STAND_IN)
    }
    assert pod["metadata"]["labels"] == {"app.kubernetes.io/component": "runtime"}
    assert pod["spec"]["serviceAccountName"] == "primary"
    assert pod["spec"]["securityContext"] == {"fsGroup": 1000}
    assert pod["spec"]["restartPolicy"] == "Never"
    assert probe == {
        "name": "mindroom",
        "image": "mindroom:test",
        "imagePullPolicy": "IfNotPresent",
        "command": ["python", "/smoke/tailnet_ssh_worker_probe.py"],
        # The gateway's Service routes to the port named tailnet-ssh.
        "ports": _PRIMARY["ports"],
        "env": _PRIMARY["env"],
        "envFrom": _PRIMARY["envFrom"],
        "securityContext": _PRIMARY["securityContext"],
        "volumeMounts": [*_PRIMARY["volumeMounts"], {"name": "smoke", "mountPath": "/smoke", "readOnly": True}],
    }
    assert pod["spec"]["volumes"][-1] == {"name": "smoke", "configMap": {"name": source["metadata"]["name"]}}


def test_the_gateways_stand_in_is_the_charts_gateway_pod_running_the_primarys_image() -> None:
    """The stand-in has the gateway's labels, by which the chart's policies admit it, and its address of the primary."""
    manifests = smoke._pod_manifests(_DEPLOYMENT, _GATEWAY, "smoke")

    source, _probe, pod = manifests["items"]
    assert pod["metadata"]["labels"] == {"app.kubernetes.io/component": "tailnet-ssh"}
    assert pod["spec"] == {
        "automountServiceAccountToken": False,
        "securityContext": {"fsGroup": 65532},
        "restartPolicy": "Never",
        "containers": [
            {
                "name": "tailnet-gateway",
                # The gateway's image is not in kind, and the primary's has Python.
                "image": "mindroom:test",
                "imagePullPolicy": "IfNotPresent",
                "command": ["python", "/smoke/tailnet_ssh_gateway_stand_in.py"],
                "env": _GATEWAY_CONTAINER["env"],
                "securityContext": {"readOnlyRootFilesystem": True},
                "volumeMounts": [{"name": "smoke", "mountPath": "/smoke", "readOnly": True}],
            },
        ],
        # Not the claim that holds the gateway's node keys.
        "volumes": [{"name": "smoke", "configMap": {"name": source["metadata"]["name"]}}],
    }


def _serve_once(answer: bytes) -> tuple[int, Callable[[], bytes]]:
    """Serve one connection on a local port: send ``answer``, then read until the client closes.

    Returns the port, and a function that waits for that close and returns what the client sent.
    """
    received = bytearray()
    server = socket.create_server(("127.0.0.1", 0))

    def serve() -> None:
        with server, server.accept()[0] as connection:
            connection.sendall(answer)
            # A client that closes before it has read all of the answer resets the connection.
            with contextlib.suppress(ConnectionResetError):
                while data := connection.recv(1024):
                    received.extend(data)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    def sent() -> bytes:
        thread.join(timeout=10)
        return bytes(received)

    return server.getsockname()[1], sent


def _unused_port() -> int:
    with socket.create_server(("127.0.0.1", 0)) as server:
        return server.getsockname()[1]


def test_the_stand_in_sends_the_primary_what_the_worker_sent_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """It dials the primary where the chart says, is told where sshd is, and answers with sshd's first eight bytes."""
    sshd_port, _ = _serve_once(b"SSH-2.0-OpenSSH_10.0\r\n")
    primary_port, told = _serve_once(f"127.0.0.1 {sshd_port}\n".encode())
    monkeypatch.setenv("TAILNET_SSH_PRIMARY_URL", f"http://127.0.0.1:{primary_port}")

    stand_in.main()

    assert told() == b"SSH-2.0-"


def test_the_stand_in_tells_the_primary_why_it_could_not_reach_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker port that never answers is reported to the primary, which then fails with the reason."""
    primary_port, told = _serve_once(f"127.0.0.1 {_unused_port()}\n".encode())
    monkeypatch.setenv("TAILNET_SSH_PRIMARY_URL", f"http://127.0.0.1:{primary_port}")
    monkeypatch.setattr(stand_in, "_WORKER_SECONDS", 0)

    stand_in.main()

    assert told().startswith(b"ConnectionRefusedError")


def test_the_runtime_chart_renders_a_kubernetes_primary_from_the_smokes_values(tmp_path: Path) -> None:
    """The chart takes the smoke's values and renders the primary's access, with a Deployment that runs no pod."""
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is required for rendered chart checks")
    values = tmp_path / "values.json"
    values.write_text(json.dumps(smoke._chart_values("ghcr.io/mindroom-ai/mindroom:latest")), encoding="utf-8")
    rendered = subprocess.run(
        [helm, "template", smoke._RELEASE, smoke._CHART, "--namespace", "smoke", "--values", str(values)],
        check=True,
        capture_output=True,
        text=True,
    )
    docs: list[dict[str, Any]] = [doc for doc in yaml.safe_load_all(rendered.stdout) if isinstance(doc, dict)]

    kinds = {doc["kind"] for doc in docs}
    assert {"Role", "RoleBinding", "ServiceAccount", "PersistentVolumeClaim"} <= kinds
    assert "StatefulSet" not in kinds
    # The label the smoke leaves out of what it applies is on the gateway's Deployment and on nothing else.
    key, _, value = smoke._GATEWAY_SELECTOR.partition("=")
    [gateway] = [doc for doc in docs if doc["metadata"].get("labels", {}).get(key) == value]
    assert gateway["kind"] == "Deployment"
    assert gateway["spec"]["template"]["metadata"]["labels"][key] == value
    names = {(doc["kind"], doc["metadata"]["name"]) for doc in docs}
    # The policy that admits the gateway to a marked worker's sshd, and the gateway's own, which lets it dial there.
    assert ("NetworkPolicy", f"{smoke._RELEASE}-tailnet-ssh-workers") in names
    assert ("NetworkPolicy", f"{smoke._RELEASE}-tailnet-ssh") in names
    [deployment] = [doc for doc in docs if doc["kind"] == "Deployment" and doc is not gateway]
    assert deployment["metadata"]["name"] == smoke._RELEASE
    assert deployment["spec"]["replicas"] == 0
    [primary] = deployment["spec"]["template"]["spec"]["containers"]
    assert "tailnet-ssh" in {port["name"] for port in primary["ports"]}
    env = {entry["name"]: entry.get("value") for entry in primary["env"]}
    assert env["MINDROOM_TAILNET_SSH_PORT"] == "8769"
    assert env["MINDROOM_WORKER_BACKEND"] == "kubernetes"
    assert env["MINDROOM_KUBERNETES_WORKER_NAMESPACE"] == "smoke"
    assert env["MINDROOM_KUBERNETES_WORKER_IMAGE"] == "ghcr.io/mindroom-ai/mindroom:latest"


@pytest.mark.parametrize("image", ["mindroom", "localhost:5000/mindroom", ":latest"])
def test_an_image_without_a_tag_is_refused(image: str) -> None:
    """The chart takes the repository and the tag apart, so the smoke needs both."""
    with pytest.raises(ValueError, match="repository:tag"):
        smoke._chart_values(image)
