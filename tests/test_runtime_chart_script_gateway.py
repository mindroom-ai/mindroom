"""Rendered runtime chart checks for the gateway-only background-script listener."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_helm_instance_worker_isolation import (
    _container,
    _env_by_name,
    _render_chart,
    _resource,
    _run_helm_template,
)

RUNTIME_CHART = Path("cluster/k8s/runtime")
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
BASE_VALUES = ("workers.sandbox.proxyToken.value=test-token",)
ISOLATED_WORKER_VALUES = (
    "workers.backend=kubernetes",
    f"workers.kubernetes.namespace={WORKER_NAMESPACE}",
    "workers.kubernetes.extraLabels.mindroom\\.ai/instance=demo",
    "approvedEgress.enabled=true",
    "approvedEgress.image.tag=test",
)


def _render(*set_args: str) -> list[dict[str, Any]]:
    return _render_chart(RUNTIME_CHART, *BASE_VALUES, *set_args, release_name=RELEASE, namespace=RELEASE_NAMESPACE)


def _names(docs: list[dict[str, Any]], kind: str) -> list[str]:
    return [doc["metadata"]["name"] for doc in docs if doc["kind"] == kind]


def _primary_env(docs: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    """Return the primary container, its env values, and the worker env it passes on."""
    container = _container(_resource(docs, "Deployment", FULLNAME), "mindroom")
    env = {name: entry.get("value", "") for name, entry in _env_by_name(container).items()}
    return container, env, json.loads(env["MINDROOM_KUBERNETES_WORKER_ENV_JSON"])


def test_script_gateway_is_off_by_default() -> None:
    """Without opting in, the chart renders no listener, Service, policies, or gateway env."""
    docs = _render(*ISOLATED_WORKER_VALUES, "networkPolicy.create=true")
    container, env, worker_env = _primary_env(docs)

    assert not [doc for doc in docs if doc["metadata"]["name"].startswith(GATEWAY_NAME)]
    assert [port["name"] for port in container["ports"]] == ["api"]
    assert not [name for name in env if name.startswith("MINDROOM_SCRIPT_GATEWAY_")]
    assert GATEWAY_HOST not in worker_env["NO_PROXY"]


def test_script_gateway_wires_listener_service_and_runtime_env() -> None:
    """The primary serves the gateway port and workers get its Service URL outside the egress proxy.

    Without the runtime's own NetworkPolicy, a standalone gateway ingress policy would isolate an otherwise open
    control-plane pod, so only the worker egress policy renders.
    """
    docs = _render(*ISOLATED_WORKER_VALUES, "scriptGateway.enabled=true", "scriptGateway.port=9876")
    container, env, worker_env = _primary_env(docs)

    assert {"name": "script-gateway", "containerPort": 9876, "protocol": "TCP"} in container["ports"]
    assert env["MINDROOM_SCRIPT_GATEWAY_PORT"] == "9876"
    assert env["MINDROOM_SCRIPT_GATEWAY_URL"] == f"http://{GATEWAY_HOST}:9876/api/script-gateway"
    assert env["MINDROOM_SCRIPT_GATEWAY_ISOLATED"] == "true"
    assert worker_env["NO_PROXY"].split(",") == ["localhost", "127.0.0.1", "::1", GATEWAY_HOST]
    assert worker_env["no_proxy"] == worker_env["NO_PROXY"]

    service = _resource(docs, "Service", GATEWAY_NAME)
    assert service["metadata"].get("namespace") is None
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["selector"] == CONTROL_PLANE_SELECTOR
    assert service["spec"]["ports"] == [
        {"port": 9876, "targetPort": "script-gateway", "protocol": "TCP", "name": "script-gateway"},
    ]
    assert [name for name in _names(docs, "NetworkPolicy") if name.startswith(GATEWAY_NAME)] == [
        f"{GATEWAY_NAME}-workers",
    ]


def test_script_gateway_policies_admit_workers_to_the_gateway_port_only() -> None:
    """Workers gain egress to the gateway port alone, and the control plane admits only them there."""
    docs = _render(*ISOLATED_WORKER_VALUES, "networkPolicy.create=true", "scriptGateway.enabled=true")
    gateway_port_only = [{"protocol": "TCP", "port": 8767}]

    worker_policy = _resource(docs, "NetworkPolicy", f"{GATEWAY_NAME}-workers")
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

    control_plane_policy = _resource(docs, "NetworkPolicy", GATEWAY_NAME)
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
    workers_policy = _resource(docs, "NetworkPolicy", f"{FULLNAME}-workers")
    egress_ports = {port["port"] for rule in workers_policy["spec"]["egress"] for port in rule["ports"]}
    assert 8765 not in egress_ports


def test_script_gateway_names_stay_distinct_for_the_longest_fullname() -> None:
    """Truncation never collapses the gateway resources onto each other or onto the main runtime Service."""
    fullname = "x" * 63
    docs = _render(
        "workers.backend=kubernetes",
        "approvedEgress.enabled=true",
        "approvedEgress.image.tag=test",
        "networkPolicy.create=true",
        f"fullnameOverride={fullname}",
        "scriptGateway.enabled=true",
    )
    policy_names = _names(docs, "NetworkPolicy")
    service_names = _names(docs, "Service")

    control_plane_name = f"{'x' * 48}-script-gateway"
    worker_policy_name = f"{'x' * 40}-script-gateway-workers"
    assert policy_names.count(control_plane_name) == 1
    assert policy_names.count(worker_policy_name) == 1
    assert service_names.count(control_plane_name) == 1
    assert fullname in service_names
    assert max(len(control_plane_name), len(worker_policy_name)) <= 63


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
    completed = _run_helm_template(RUNTIME_CHART, *BASE_VALUES, *set_args, release_name=RELEASE)

    assert completed.returncode != 0
    assert error in completed.stderr
