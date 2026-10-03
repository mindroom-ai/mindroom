"""Rendered runtime chart checks for the gateway-only background-script listener."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

RUNTIME_CHART_DIR = Path(__file__).resolve().parents[1] / "cluster" / "k8s" / "runtime"
RELEASE = "mindroom-demo"
RELEASE_NAMESPACE = "mindroom"
WORKER_NAMESPACE = "mindroom-workers"
FULLNAME = "mindroom-demo-mindroom-runtime"
GATEWAY_NAME = f"{FULLNAME}-script-gateway"
GATEWAY_HOST = f"{GATEWAY_NAME}.{RELEASE_NAMESPACE}.svc.cluster.local"
CONTROL_PLANE_SELECTOR = {
    "app.kubernetes.io/name": "mindroom-runtime",
    "app.kubernetes.io/instance": RELEASE,
    "app.kubernetes.io/component": "runtime",
}
WORKER_SELECTOR = {
    "mindroom.ai/component": "worker",
    "app.kubernetes.io/managed-by": "mindroom",
    "app.kubernetes.io/name": "mindroom-worker",
    "mindroom.ai/instance": "demo",
}
BASE_VALUES = (
    "eventCache.postgres.auth.password=test-password",
    "workers.sandbox.proxyToken.value=test-token",
)
ISOLATED_WORKER_VALUES = (
    "workers.backend=kubernetes",
    f"workers.kubernetes.namespace={WORKER_NAMESPACE}",
    "workers.kubernetes.extraLabels.mindroom\\.ai/instance=demo",
    "approvedEgress.enabled=true",
    "approvedEgress.image.tag=test",
)


def _helm_template(*set_args: str) -> subprocess.CompletedProcess[str]:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is required for rendered chart checks")
    return subprocess.run(
        [
            helm,
            "template",
            RELEASE,
            str(RUNTIME_CHART_DIR),
            "--namespace",
            RELEASE_NAMESPACE,
            *(arg for value in (*BASE_VALUES, *set_args) for arg in ("--set", value)),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _render(*set_args: str) -> list[dict[str, Any]]:
    completed = _helm_template(*set_args)
    assert completed.returncode == 0, completed.stderr
    return [doc for doc in yaml.safe_load_all(completed.stdout) if isinstance(doc, dict)]


def _named(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any] | None:
    return next((doc for doc in docs if doc["kind"] == kind and doc["metadata"]["name"] == name), None)


def _primary_container(docs: list[dict[str, Any]]) -> dict[str, Any]:
    deployment = _named(docs, "Deployment", FULLNAME)
    assert deployment is not None
    return next(c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "mindroom")


def _env(container: dict[str, Any]) -> dict[str, str]:
    return {entry["name"]: entry.get("value", "") for entry in container["env"]}


def _worker_env(container: dict[str, Any]) -> dict[str, str]:
    return json.loads(_env(container)["MINDROOM_KUBERNETES_WORKER_ENV_JSON"])


def test_script_gateway_is_off_by_default() -> None:
    """Without opting in, the chart renders no listener, Service, policies, or gateway env."""
    docs = _render(*ISOLATED_WORKER_VALUES, "networkPolicy.create=true")
    container = _primary_container(docs)

    assert not [doc for doc in docs if doc["metadata"]["name"].startswith(GATEWAY_NAME)]
    assert [port["name"] for port in container["ports"]] == ["api"]
    assert not [name for name in _env(container) if name.startswith("MINDROOM_SCRIPT_GATEWAY_")]
    assert GATEWAY_HOST not in _worker_env(container)["NO_PROXY"]


def test_script_gateway_wires_listener_service_and_runtime_env() -> None:
    """The primary serves the gateway port and workers get its Service URL outside the egress proxy."""
    docs = _render(*ISOLATED_WORKER_VALUES, "scriptGateway.enabled=true", "scriptGateway.port=9876")
    container = _primary_container(docs)
    env = _env(container)

    assert {"name": "script-gateway", "containerPort": 9876, "protocol": "TCP"} in container["ports"]
    assert env["MINDROOM_SCRIPT_GATEWAY_PORT"] == "9876"
    assert env["MINDROOM_SCRIPT_GATEWAY_URL"] == f"http://{GATEWAY_HOST}:9876/api/script-gateway"
    assert env["MINDROOM_SCRIPT_GATEWAY_ISOLATED"] == "true"
    worker_env = _worker_env(container)
    assert worker_env["NO_PROXY"].split(",") == ["localhost", "127.0.0.1", "::1", GATEWAY_HOST]
    assert worker_env["no_proxy"] == worker_env["NO_PROXY"]

    service = _named(docs, "Service", GATEWAY_NAME)
    assert service is not None
    assert service["metadata"].get("namespace") is None
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["selector"] == CONTROL_PLANE_SELECTOR
    assert service["spec"]["ports"] == [
        {"port": 9876, "targetPort": "script-gateway", "protocol": "TCP", "name": "script-gateway"},
    ]


def test_script_gateway_policies_admit_workers_to_the_gateway_port_only() -> None:
    """Workers gain egress to the gateway port alone, and the control plane admits only them there."""
    docs = _render(*ISOLATED_WORKER_VALUES, "networkPolicy.create=true", "scriptGateway.enabled=true")
    gateway_port_only = [{"protocol": "TCP", "port": 8767}]

    worker_policy = _named(docs, "NetworkPolicy", f"{GATEWAY_NAME}-workers")
    assert worker_policy is not None
    assert worker_policy["metadata"]["namespace"] == WORKER_NAMESPACE
    assert worker_policy["spec"]["podSelector"] == {"matchLabels": WORKER_SELECTOR}
    assert worker_policy["spec"]["policyTypes"] == ["Egress"]
    assert worker_policy["spec"]["egress"] == [
        {
            "to": [
                {
                    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": RELEASE_NAMESPACE}},
                    "podSelector": {"matchLabels": CONTROL_PLANE_SELECTOR},
                },
            ],
            "ports": gateway_port_only,
        },
    ]

    control_plane_policy = _named(docs, "NetworkPolicy", GATEWAY_NAME)
    assert control_plane_policy is not None
    assert control_plane_policy["spec"]["podSelector"] == {"matchLabels": CONTROL_PLANE_SELECTOR}
    assert control_plane_policy["spec"]["policyTypes"] == ["Ingress"]
    assert control_plane_policy["spec"]["ingress"] == [
        {
            "from": [
                {
                    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": WORKER_NAMESPACE}},
                    "podSelector": {"matchLabels": WORKER_SELECTOR},
                },
            ],
            "ports": gateway_port_only,
        },
    ]

    # The chart's own worker policy still never opens the primary API port.
    workers_policy = _named(docs, "NetworkPolicy", f"{FULLNAME}-workers")
    assert workers_policy is not None
    egress_ports = {port["port"] for rule in workers_policy["spec"]["egress"] for port in rule["ports"]}
    assert 8765 not in egress_ports


def test_script_gateway_adds_no_control_plane_ingress_policy_when_the_runtime_policy_is_off() -> None:
    """A standalone ingress policy would isolate an otherwise open control-plane pod."""
    docs = _render(*ISOLATED_WORKER_VALUES, "scriptGateway.enabled=true")

    assert _named(docs, "NetworkPolicy", GATEWAY_NAME) is None
    assert _named(docs, "NetworkPolicy", f"{GATEWAY_NAME}-workers") is not None


@pytest.mark.parametrize(
    ("set_args", "error"),
    [
        (
            ("workers.backend=static_runner", "scriptGateway.enabled=true"),
            "scriptGateway.enabled requires workers.backend=kubernetes",
        ),
        (
            ("workers.backend=kubernetes", "scriptGateway.enabled=true"),
            "scriptGateway.enabled requires the worker egress NetworkPolicy",
        ),
        (
            (
                "workers.backend=kubernetes",
                "egressProxy.enabled=true",
                "egressProxy.networkPolicy.create=false",
                "scriptGateway.enabled=true",
            ),
            "scriptGateway.enabled requires the worker egress NetworkPolicy",
        ),
        (
            (*ISOLATED_WORKER_VALUES, "scriptGateway.enabled=true", "scriptGateway.port=8765"),
            "scriptGateway.port must differ from runtime.apiPort",
        ),
    ],
)
def test_script_gateway_refuses_configurations_without_an_isolated_worker_path(
    set_args: tuple[str, ...],
    error: str,
) -> None:
    """The chart only attests isolation when its own worker egress policy keeps workers off the API port."""
    completed = _helm_template(*set_args)

    assert completed.returncode != 0
    assert error in completed.stderr
