"""Rendered runtime chart checks that every chart-managed pod gets the configured image pull secrets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.test_helm_instance_worker_isolation import _render_chart, _values_files

POD_TEMPLATE_KINDS = {"Deployment", "StatefulSet", "Job"}

ALL_PODS_VALUES: dict[str, Any] = {
    "approvedEgress": {
        "enabled": True,
        "image": {"tag": "v0.1.0"},
        "parentProxy": {"enabled": True, "host": "agent-vault", "port": 14322},
    },
    "eventCache": {"postgres": {"auth": {"password": "test-password"}}},
    "workers": {
        "backend": "kubernetes",
        "sandbox": {"proxyToken": {"value": "test-token"}},
        "kubernetes": {
            "agentVault": {
                "enabled": True,
                "cliImage": "example.test/vault:test",
                "ownerEmail": "owner@example.test",
                "workerCaConfigMapName": "agent-vault-ca",
                "server": {"enabled": True},
                "bootstrap": {"enabled": True, "kubectlImage": "example.test/kubectl:test"},
                "accessGrants": {
                    "enabled": True,
                    "grants": [{"email": "maintainer@example.test", "workerScope": "shared", "agent": "helper"}],
                },
            },
        },
    },
}


def _pod_specs(tmp_path: Path, *values: dict[str, Any]) -> dict[str, dict[str, Any]]:
    docs = _render_chart(
        Path("cluster/k8s/runtime"),
        values_files=_values_files(tmp_path, ALL_PODS_VALUES, *values),
        release_name="mindroom-runtime",
    )
    return {
        f"{doc['kind']}/{doc['metadata']['name']}": doc["spec"]["template"]["spec"]
        for doc in docs
        if doc["kind"] in POD_TEMPLATE_KINDS
    }


def test_every_runtime_chart_pod_uses_the_image_pull_secrets(tmp_path: Path) -> None:
    """Pods pulling from a private registry need the credentials, not only the primary Deployment."""
    pull_secrets = [{"name": "private-registry-pull"}]
    pod_specs = _pod_specs(tmp_path, {"imagePullSecrets": pull_secrets})

    assert set(pod_specs) == {
        "Deployment/mindroom-runtime",
        "Deployment/agent-vault",
        "Deployment/mindroom-runtime-egress-proxy",
        "StatefulSet/mindroom-runtime-event-cache-postgres",
        "Job/agent-vault-bootstrap",
        "Job/agent-vault-access-grants",
    }
    assert {name: spec.get("imagePullSecrets") for name, spec in pod_specs.items()} == dict.fromkeys(
        pod_specs,
        pull_secrets,
    )


def test_runtime_chart_pods_omit_image_pull_secrets_by_default(tmp_path: Path) -> None:
    """Without configured pull secrets, no pod spec renders an empty imagePullSecrets field."""
    assert not [name for name, spec in _pod_specs(tmp_path).items() if "imagePullSecrets" in spec]
