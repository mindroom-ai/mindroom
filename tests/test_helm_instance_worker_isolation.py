"""Rendered Helm manifest checks for Kubernetes worker isolation defaults."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml


def _render_chart(chart_dir: Path, *set_args: str, release_name: str = "mindroom-demo") -> list[dict[str, Any]]:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is required for rendered chart checks")
    completed = subprocess.run(
        [
            helm,
            "template",
            release_name,
            str(chart_dir),
            *(arg for value in set_args for arg in ("--set", value)),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [doc for doc in yaml.safe_load_all(completed.stdout) if isinstance(doc, dict)]


def _render_instance_chart() -> list[dict[str, Any]]:
    return _render_chart(
        Path("cluster/k8s/instance"),
        "workerBackend=kubernetes",
        "storageAccessMode=ReadWriteMany",
    )


def _render_runtime_chart() -> list[dict[str, Any]]:
    return _render_chart(
        Path("cluster/k8s/runtime"),
        "workers.backend=kubernetes",
        "workers.sandbox.proxyToken.value=test-token",
        "eventCache.postgres.auth.password=test-password",
        release_name="mindroom-runtime",
    )


def _render_runtime_chart_with_separate_worker_namespace() -> list[dict[str, Any]]:
    return _render_chart(
        Path("cluster/k8s/runtime"),
        "workers.backend=kubernetes",
        "workers.kubernetes.namespace=mindroom-workers",
        "workers.sandbox.proxyToken.value=test-token",
        "eventCache.postgres.auth.password=test-password",
        release_name="mindroom-runtime",
    )


def _resource(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    for doc in docs:
        metadata = doc.get("metadata")
        if doc.get("kind") == kind and isinstance(metadata, dict) and metadata.get("name") == name:
            return doc
    msg = f"{kind}/{name} was not rendered"
    raise AssertionError(msg)


def test_instance_chart_worker_network_policy_allows_runner_ingress_only_from_control_plane() -> None:
    """Worker runner ingress should not allow every pod carrying the instance label."""
    docs = _render_instance_chart()
    policy = _resource(docs, "NetworkPolicy", "instance-traffic-controls-demo")
    worker_rule = next(
        rule for rule in policy["spec"]["ingress"] if any(port.get("port") == 8766 for port in rule.get("ports", []))
    )

    assert worker_rule["from"] == [{"podSelector": {"matchLabels": {"app": "mindroom", "customer": "demo"}}}]


def test_instance_chart_disables_service_links_for_dynamic_worker_pods_by_default() -> None:
    """The control plane should configure generated worker pod specs with service links disabled."""
    docs = _render_instance_chart()
    deployment = _resource(docs, "Deployment", "mindroom-demo")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_values = {env["name"]: env.get("value") for env in container["env"]}

    assert env_values["MINDROOM_KUBERNETES_WORKER_ENABLE_SERVICE_LINKS"] == "false"


def test_runtime_chart_worker_network_policy_selects_dynamic_worker_labels() -> None:
    """The runtime chart worker NetworkPolicy selector should match generated worker pod labels."""
    docs = _render_runtime_chart()
    policy = _resource(docs, "NetworkPolicy", "mindroom-runtime-workers")

    assert policy["spec"]["podSelector"]["matchLabels"] == {
        "mindroom.ai/component": "worker",
        "app.kubernetes.io/managed-by": "mindroom",
        "app.kubernetes.io/name": "mindroom-worker",
    }


def test_runtime_chart_disables_service_links_for_dynamic_worker_pods_by_default() -> None:
    """The runtime chart should pass the default service-link setting to generated workers."""
    docs = _render_runtime_chart()
    deployment = _resource(docs, "Deployment", "mindroom-runtime")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_values = {env["name"]: env.get("value") for env in container["env"]}

    assert env_values["MINDROOM_KUBERNETES_WORKER_ENABLE_SERVICE_LINKS"] == "false"


def test_runtime_chart_does_not_copy_shared_proxy_token_to_worker_namespace() -> None:
    """Dedicated workers receive derived tokens, so their namespace should not get the shared token Secret."""
    docs = _render_runtime_chart_with_separate_worker_namespace()

    runtime_secret = _resource(docs, "Secret", "mindroom-runtime-sandbox-proxy")
    assert runtime_secret["stringData"] == {"MINDROOM_SANDBOX_PROXY_TOKEN": "test-token"}

    worker_namespace_secrets = [
        doc
        for doc in docs
        if doc.get("kind") == "Secret" and doc.get("metadata", {}).get("namespace") == "mindroom-workers"
    ]

    assert worker_namespace_secrets == []
