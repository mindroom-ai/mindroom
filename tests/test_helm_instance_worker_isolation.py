"""Rendered Helm manifest checks for Kubernetes worker isolation defaults."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml


def _render_chart(chart_dir: Path, *set_args: str, release_name: str = "mindroom-demo") -> list[dict[str, Any]]:
    completed = _run_helm_template(chart_dir, *set_args, release_name=release_name)
    completed.check_returncode()
    return [doc for doc in yaml.safe_load_all(completed.stdout) if isinstance(doc, dict)]


def _run_helm_template(
    chart_dir: Path,
    *set_args: str,
    release_name: str = "mindroom-demo",
    set_string_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is required for rendered chart checks")
    return subprocess.run(
        [
            helm,
            "template",
            release_name,
            str(chart_dir),
            *(arg for value in set_args for arg in ("--set", value)),
            *(arg for value in set_string_args for arg in ("--set-string", value)),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


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


def test_instance_chart_worker_manager_can_only_patch_own_worker_auth_secret() -> None:
    """Shared-namespace instances must not get cross-tenant Secret permissions."""
    docs = _render_instance_chart()
    role = _resource(docs, "Role", "mindroom-worker-manager-demo")

    secret_rules = [rule for rule in role["rules"] if "secrets" in rule.get("resources", [])]
    assert secret_rules == [
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": ["mindroom-worker-auth-demo"],
            "verbs": ["get", "patch"],
        },
    ]


def test_instance_chart_uses_tenant_worker_auth_secret() -> None:
    """Shared-namespace instances should reference a pre-created tenant token Secret."""
    docs = _render_instance_chart()
    deployment = _resource(docs, "Deployment", "mindroom-demo")
    worker_auth_secret = _resource(docs, "Secret", "mindroom-worker-auth-demo")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_values = {env["name"]: env.get("value") for env in container["env"]}

    assert env_values["MINDROOM_KUBERNETES_WORKER_AUTH_SECRET_NAME"] == "mindroom-worker-auth-demo"  # noqa: S105
    assert worker_auth_secret["metadata"]["namespace"] == "mindroom-instances"
    assert "stringData" not in worker_auth_secret
    assert "data" not in worker_auth_secret


def test_instance_chart_rejects_email_template_without_email_header() -> None:
    """Email-to-Matrix derivation requires the trusted email header name."""
    completed = _run_helm_template(
        Path("cluster/k8s/instance"),
        "trustedUpstreamAuth.enabled=true",
        "trustedUpstreamAuth.userIdHeader=X-Trusted-User",
        set_string_args=("trustedUpstreamAuth.emailToMatrixUserIdTemplate=@{localpart}:example.org",),
    )

    assert completed.returncode != 0
    assert (
        "trustedUpstreamAuth.emailHeader is required when trustedUpstreamAuth.emailToMatrixUserIdTemplate is set"
        in completed.stderr
    )


def test_platform_chart_rejects_email_template_without_email_header() -> None:
    """The platform chart should fail before provisioning invalid instance auth config."""
    completed = _run_helm_template(
        Path("cluster/k8s/platform"),
        "provisioner.trustedUpstreamAuth.enabled=true",
        "provisioner.trustedUpstreamAuth.userIdHeader=X-Trusted-User",
        release_name="mindroom-platform",
        set_string_args=("provisioner.trustedUpstreamAuth.emailToMatrixUserIdTemplate=@{localpart}:example.org",),
    )

    assert completed.returncode != 0
    assert (
        "provisioner.trustedUpstreamAuth.emailHeader is required when "
        "provisioner.trustedUpstreamAuth.emailToMatrixUserIdTemplate is set"
    ) in completed.stderr


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


def test_runtime_chart_worker_manager_can_only_patch_default_worker_auth_secret() -> None:
    """Default same-namespace runtime workers should not get broad Secret permissions."""
    docs = _render_runtime_chart()
    role = _resource(docs, "Role", "mindroom-runtime-worker-manager")

    secret_rules = [rule for rule in role["rules"] if "secrets" in rule.get("resources", [])]
    assert secret_rules == [
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": ["mindroom-runtime-worker-auth"],
            "verbs": ["get", "patch"],
        },
    ]


def test_runtime_chart_uses_default_worker_auth_secret() -> None:
    """The runtime chart should use one scoped auth Secret in its release namespace by default."""
    docs = _render_runtime_chart()
    deployment = _resource(docs, "Deployment", "mindroom-runtime")
    worker_auth_secret = _resource(docs, "Secret", "mindroom-runtime-worker-auth")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_values = {env["name"]: env.get("value") for env in container["env"]}

    assert env_values["MINDROOM_KUBERNETES_WORKER_AUTH_SECRET_NAME"] == "mindroom-runtime-worker-auth"  # noqa: S105
    assert worker_auth_secret["metadata"]["namespace"] == "default"
    assert "stringData" not in worker_auth_secret
    assert "data" not in worker_auth_secret


def test_runtime_chart_separate_worker_namespace_can_manage_per_worker_auth_secrets() -> None:
    """Explicit worker namespaces may use per-worker Secrets in that namespace."""
    docs = _render_runtime_chart_with_separate_worker_namespace()
    role = _resource(docs, "Role", "mindroom-runtime-worker-manager")
    deployment = _resource(docs, "Deployment", "mindroom-runtime")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_names = {env["name"] for env in container["env"]}

    assert "MINDROOM_KUBERNETES_WORKER_AUTH_SECRET_NAME" not in env_names
    assert role["metadata"]["namespace"] == "mindroom-workers"
    assert {
        "apiGroups": [""],
        "resources": ["secrets"],
        "verbs": ["create", "delete", "get", "patch"],
    } in role["rules"]


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
