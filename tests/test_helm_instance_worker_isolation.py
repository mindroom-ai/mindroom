"""Rendered Helm manifest checks for Kubernetes worker isolation defaults."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml


def _render_chart(
    chart_dir: Path,
    *set_args: str,
    release_name: str = "mindroom-demo",
    set_string_args: tuple[str, ...] = (),
    values_files: tuple[Path, ...] = (),
) -> list[dict[str, Any]]:
    completed = _run_helm_template(
        chart_dir,
        *set_args,
        release_name=release_name,
        set_string_args=set_string_args,
        values_files=values_files,
    )
    completed.check_returncode()
    return [doc for doc in yaml.safe_load_all(completed.stdout) if isinstance(doc, dict)]


def _run_helm_template(
    chart_dir: Path,
    *set_args: str,
    release_name: str = "mindroom-demo",
    set_string_args: tuple[str, ...] = (),
    values_files: tuple[Path, ...] = (),
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
            *(arg for value in values_files for arg in ("--values", str(value))),
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


def _container(deployment: dict[str, Any], name: str) -> dict[str, Any]:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    for container in containers:
        if container["name"] == name:
            return container
    msg = f"container {name} was not rendered"
    raise AssertionError(msg)


def _env_by_name(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {env["name"]: env for env in container["env"]}


def _instance_secret_hash(**overrides: str) -> str:
    secret_data = {
        "openai_key": "",
        "anthropic_key": "",
        "openrouter_key": "",
        "google_key": "",
        "deepseek_key": "",
        "supabase_service_key": "",
        "sandbox_proxy_token": "",
        "credentials_encryption_key": "",
        "matrix_oidc_client_secret": "",
    }
    secret_data.update(overrides)
    ordered_values = [
        secret_data["openai_key"],
        secret_data["anthropic_key"],
        secret_data["openrouter_key"],
        secret_data["google_key"],
        secret_data["deepseek_key"],
        secret_data["supabase_service_key"],
        secret_data["sandbox_proxy_token"],
        secret_data["credentials_encryption_key"],
        secret_data["matrix_oidc_client_secret"],
    ]
    return hashlib.sha256("|".join(ordered_values).encode("utf-8")).hexdigest()


def test_instance_chart_worker_network_policy_allows_runner_ingress_only_from_control_plane() -> None:
    """Worker runner ingress should not allow every pod carrying the instance label."""
    docs = _render_instance_chart()
    policy = _resource(docs, "NetworkPolicy", "instance-traffic-controls-demo")
    worker_rule = next(
        rule for rule in policy["spec"]["ingress"] if any(port.get("port") == 8766 for port in rule.get("ports", []))
    )

    assert worker_rule["from"] == [{"podSelector": {"matchLabels": {"app": "mindroom", "customer": "demo"}}}]


def test_instance_chart_network_policy_limits_public_ports_to_ingress_and_instance_pods() -> None:
    """Instance service ports should not be reachable from unrelated namespace pods."""
    docs = _render_instance_chart()
    policy = _resource(docs, "NetworkPolicy", "instance-traffic-controls-demo")
    service_rules = [
        rule
        for rule in policy["spec"]["ingress"]
        if {port.get("port") for port in rule.get("ports", [])} == {8765, 8008}
    ]

    assert service_rules == [
        {
            "from": [
                {
                    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "ingress-nginx"}},
                    "podSelector": {
                        "matchLabels": {
                            "app.kubernetes.io/component": "controller",
                            "app.kubernetes.io/name": "ingress-nginx",
                        },
                    },
                },
                {"podSelector": {"matchLabels": {"customer": "demo"}}},
            ],
            "ports": [{"port": 8765}, {"port": 8008}],
        },
    ]


def test_instance_chart_disables_service_links_for_dynamic_worker_pods_by_default() -> None:
    """The control plane should configure generated worker pod specs with service links disabled."""
    docs = _render_instance_chart()
    deployment = _resource(docs, "Deployment", "mindroom-demo")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_values = {env["name"]: env.get("value") for env in container["env"]}

    assert env_values["MINDROOM_KUBERNETES_WORKER_ENABLE_SERVICE_LINKS"] == "false"


def test_instance_chart_sets_public_url_for_oauth_redirects() -> None:
    """Hosted instances should derive OAuth callbacks from their public dashboard origin."""
    docs = _render_chart(
        Path("cluster/k8s/instance"),
        "customer=tenant42",
        "baseDomain=example.test",
    )
    deployment = _resource(docs, "Deployment", "mindroom-tenant42")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_values = {env["name"]: env.get("value") for env in container["env"]}

    assert env_values["MINDROOM_PUBLIC_URL"] == "https://tenant42.example.test"


def test_instance_chart_wires_image_pull_secrets_to_control_plane_pods() -> None:
    """Private registry credentials should be available before pulling instance images."""
    docs = _render_chart(
        Path("cluster/k8s/instance"),
        "workerBackend=kubernetes",
        "storageAccessMode=ReadWriteMany",
        "imagePullSecrets[0].name=ghcr-pull",
    )
    deployment = _resource(docs, "Deployment", "mindroom-demo")
    worker_manager_account = _resource(docs, "ServiceAccount", "mindroom-worker-manager-demo")

    assert deployment["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "ghcr-pull"}]
    assert worker_manager_account["imagePullSecrets"] == [{"name": "ghcr-pull"}]


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


def test_instance_chart_static_runner_uses_shared_credentials_encryption_key_secret() -> None:
    """Static runner mode should give both runtime containers the same Secret-backed credential key."""
    credentials_encryption_key = "test-encryption-key"
    docs = _render_chart(
        Path("cluster/k8s/instance"),
        "credentials_encryption_key=test-encryption-key",
    )
    deployment = _resource(docs, "Deployment", "mindroom-demo")
    api_keys_secret = _resource(docs, "Secret", "mindroom-api-keys-demo")
    mindroom_container = _container(deployment, "mindroom")
    runner_container = _container(deployment, "sandbox-runner")
    annotations = deployment["spec"]["template"]["metadata"]["annotations"]

    assert api_keys_secret["stringData"]["credentials_encryption_key"] == credentials_encryption_key
    assert credentials_encryption_key not in json.dumps(deployment)
    assert annotations["mindroom.ai/instance-secret-hash"] == _instance_secret_hash(
        credentials_encryption_key=credentials_encryption_key,
    )
    assert _env_by_name(mindroom_container)["MINDROOM_CREDENTIALS_ENCRYPTION_KEY"] == {
        "name": "MINDROOM_CREDENTIALS_ENCRYPTION_KEY",
        "valueFrom": {
            "secretKeyRef": {
                "name": "mindroom-api-keys-demo",
                "key": "credentials_encryption_key",
            },
        },
    }
    assert _env_by_name(runner_container)["MINDROOM_CREDENTIALS_ENCRYPTION_KEY"] == {
        "name": "MINDROOM_CREDENTIALS_ENCRYPTION_KEY",
        "valueFrom": {
            "secretKeyRef": {
                "name": "mindroom-api-keys-demo",
                "key": "credentials_encryption_key",
            },
        },
    }


def test_instance_chart_wires_credentials_encryption_env_when_key_is_unset() -> None:
    """Instance runtime containers should consistently read the key from the shared Secret."""
    docs = _render_chart(Path("cluster/k8s/instance"))
    deployment = _resource(docs, "Deployment", "mindroom-demo")
    mindroom_container = _container(deployment, "mindroom")
    runner_container = _container(deployment, "sandbox-runner")
    annotations = deployment["spec"]["template"]["metadata"]["annotations"]

    expected_env = {
        "name": "MINDROOM_CREDENTIALS_ENCRYPTION_KEY",
        "valueFrom": {
            "secretKeyRef": {
                "name": "mindroom-api-keys-demo",
                "key": "credentials_encryption_key",
            },
        },
    }
    assert _env_by_name(mindroom_container)["MINDROOM_CREDENTIALS_ENCRYPTION_KEY"] == expected_env
    assert _env_by_name(runner_container)["MINDROOM_CREDENTIALS_ENCRYPTION_KEY"] == expected_env
    assert annotations["mindroom.ai/instance-secret-hash"] == _instance_secret_hash()


def test_instance_chart_credentials_encryption_key_rotation_changes_pod_template() -> None:
    """Changing the Secret-backed credential key should render a new pod template hash."""
    first_docs = _render_chart(
        Path("cluster/k8s/instance"),
        "credentials_encryption_key=first-key",
    )
    second_docs = _render_chart(
        Path("cluster/k8s/instance"),
        "credentials_encryption_key=second-key",
    )
    first_deployment = _resource(first_docs, "Deployment", "mindroom-demo")
    second_deployment = _resource(second_docs, "Deployment", "mindroom-demo")

    assert (
        first_deployment["spec"]["template"]["metadata"]["annotations"]["mindroom.ai/instance-secret-hash"]
        != second_deployment["spec"]["template"]["metadata"]["annotations"]["mindroom.ai/instance-secret-hash"]
    )


def test_instance_chart_can_use_existing_secret_for_sensitive_values() -> None:
    """Production instance deploys should keep secret material out of Helm manifests."""
    docs = _render_chart(
        Path("cluster/k8s/instance"),
        "instanceSecrets.create=false",
        "instanceSecrets.name=tenant-runtime-secrets",
        "instanceSecrets.hash=abc123",
        "credentials_encryption_key=must-not-render",
        "matrixOidc.enabled=true",
        "matrixOidc.issuer=https://api.mindroom.chat/matrix-oidc",
        "matrixOidc.clientId=mindroom-synapse",
        "matrixOidc.clientSecret=must-not-render-oidc",
    )
    mindroom = _resource(docs, "Deployment", "mindroom-demo")
    synapse = _resource(docs, "Deployment", "synapse-demo")
    rendered = json.dumps(docs)

    assert not any(doc["kind"] == "Secret" and doc["metadata"]["name"] == "tenant-runtime-secrets" for doc in docs)
    assert "must-not-render" not in rendered
    assert "must-not-render-oidc" not in rendered
    assert mindroom["spec"]["template"]["spec"]["volumes"][2]["secret"]["secretName"] == "tenant-runtime-secrets"
    assert synapse["spec"]["template"]["spec"]["volumes"][2]["secret"]["secretName"] == "tenant-runtime-secrets"
    assert mindroom["spec"]["template"]["metadata"]["annotations"]["mindroom.ai/instance-secret-hash"] == "abc123"
    assert synapse["spec"]["template"]["metadata"]["annotations"]["mindroom.ai/instance-secret-hash"] == "abc123"


def test_instance_chart_numeric_customer_uses_valid_instance_secret_name() -> None:
    """CI and production instance IDs are numeric and must still render valid Secret names."""
    docs = _render_chart(
        Path("cluster/k8s/instance"),
        "customer=1",
    )
    secret = _resource(docs, "Secret", "mindroom-api-keys-1")
    deployment = _resource(docs, "Deployment", "mindroom-1")

    assert secret["metadata"]["name"] == "mindroom-api-keys-1"
    assert deployment["spec"]["template"]["spec"]["volumes"][2]["secret"]["secretName"] == "mindroom-api-keys-1"


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


def test_instance_chart_renders_strict_trusted_upstream_jwt_env() -> None:
    """Strict trusted upstream settings should render to MindRoom runtime env vars."""
    docs = _render_chart(
        Path("cluster/k8s/instance"),
        "trustedUpstreamAuth.enabled=true",
        "trustedUpstreamAuth.userIdHeader=X-Trusted-User",
        "trustedUpstreamAuth.emailHeader=X-Trusted-Email",
        "trustedUpstreamAuth.requireJwt=true",
        "trustedUpstreamAuth.jwtHeader=X-Trusted-Jwt",
        "trustedUpstreamAuth.jwtAudience=mindroom-dashboard",
        "trustedUpstreamAuth.jwtIssuer=https://issuer.example",
        "trustedUpstreamAuth.jwtEmailClaim=email",
        "trustedUpstreamAuth.jwtUserIdClaim=sub",
        "trustedUpstreamAuth.jwtMatrixUserIdClaim=matrix_user_id",
        set_string_args=("trustedUpstreamAuth.jwksUrl=https://issuer.example/jwks",),
    )
    deployment = _resource(docs, "Deployment", "mindroom-demo")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_values = {env["name"]: env.get("value") for env in container["env"]}

    assert env_values["MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT"] == "true"
    assert env_values["MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER"] == "X-Trusted-Jwt"
    assert env_values["MINDROOM_TRUSTED_UPSTREAM_JWKS_URL"] == "https://issuer.example/jwks"
    assert env_values["MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE"] == "mindroom-dashboard"
    assert env_values["MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER"] == "https://issuer.example"
    assert env_values["MINDROOM_TRUSTED_UPSTREAM_JWT_EMAIL_CLAIM"] == "email"
    assert env_values["MINDROOM_TRUSTED_UPSTREAM_JWT_USER_ID_CLAIM"] == "sub"
    assert env_values["MINDROOM_TRUSTED_UPSTREAM_JWT_MATRIX_USER_ID_CLAIM"] == "matrix_user_id"


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


def test_platform_chart_rejects_trusted_upstream_without_user_id_header() -> None:
    """The platform chart should fail before provisioning instances that cannot authenticate users."""
    completed = _run_helm_template(
        Path("cluster/k8s/platform"),
        "provisioner.trustedUpstreamAuth.enabled=true",
        release_name="mindroom-platform",
    )

    assert completed.returncode != 0
    assert (
        "provisioner.trustedUpstreamAuth.userIdHeader is required when provisioner.trustedUpstreamAuth.enabled=true"
    ) in completed.stderr


def test_platform_chart_wires_instance_credentials_encryption_secret() -> None:
    """The platform chart should mount the stable instance credential key derivation secret."""
    docs = _render_chart(
        Path("cluster/k8s/platform"),
        "provisioner.apiKey=test-api-key",
        release_name="mindroom-platform",
        set_string_args=("provisioner.instanceCredentialsEncryptionSecret=abc: def",),
    )
    secret = _resource(docs, "Secret", "platform-secrets")
    deployment = _resource(docs, "Deployment", "platform-backend")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_values = {env["name"]: env.get("value") for env in container["env"]}

    assert secret["stringData"]["instance_credentials_encryption_secret"] == "abc: def"  # noqa: S105
    assert env_values["INSTANCE_CREDENTIALS_ENCRYPTION_SECRET_FILE"] == (
        "/etc/secrets/instance_credentials_encryption_secret"  # noqa: S105
    )


def test_platform_chart_can_use_existing_secret_for_sensitive_values() -> None:
    """Production deploys should keep secret material out of Helm release values."""
    docs = _render_chart(
        Path("cluster/k8s/platform"),
        "platformSecrets.create=false",
        "platformSecrets.name=mindroom-platform-secrets",
        "matrixOidc.enabled=true",
        release_name="mindroom-platform",
    )
    deployment = _resource(docs, "Deployment", "platform-backend")
    volume = deployment["spec"]["template"]["spec"]["volumes"][0]

    assert not any(doc["kind"] == "Secret" and doc["metadata"]["name"] == "platform-secrets" for doc in docs)
    assert volume["secret"]["secretName"] == "mindroom-platform-secrets"


def test_platform_chart_exposes_instance_image_pull_secret_names() -> None:
    """Provisioner config should forward registry pull secret names to instance Helm releases."""
    docs = _render_chart(
        Path("cluster/k8s/platform"),
        "provisioner.instanceImagePullSecretNames[0]=ghcr-pull",
        "provisioner.instanceImagePullSecretNames[1]=backup-pull",
        release_name="mindroom-platform",
    )
    config = _resource(docs, "ConfigMap", "platform-config")

    assert config["data"]["INSTANCE_IMAGE_PULL_SECRET_NAMES"] == "ghcr-pull,backup-pull"  # noqa: S105


def test_platform_chart_can_pin_frontend_and_backend_images_separately() -> None:
    """Platform services should be deployable without forcing identical image tags."""
    docs = _render_chart(
        Path("cluster/k8s/platform"),
        "frontendImageTag=frontend-tag",
        "backendImageTag=backend-tag",
        release_name="mindroom-platform",
    )
    frontend = _resource(docs, "Deployment", "platform-frontend")
    backend = _resource(docs, "Deployment", "platform-backend")

    assert _container(frontend, "app")["image"] == "ghcr.io/mindroom-ai/platform-frontend:frontend-tag"
    assert _container(backend, "app")["image"] == "ghcr.io/mindroom-ai/platform-backend:backend-tag"


def test_instance_chart_renders_configurable_control_plane_resources() -> None:
    """Tenant MindRoom and Synapse resources should be configurable per release."""
    docs = _render_chart(
        Path("cluster/k8s/instance"),
        "mindroomResources.requests.cpu=300m",
        "mindroomResources.requests.memory=768Mi",
        "mindroomResources.limits.cpu=1500m",
        "mindroomResources.limits.memory=3Gi",
        "synapseResources.requests.cpu=350m",
        "synapseResources.requests.memory=1Gi",
        "synapseResources.limits.memory=4Gi",
        set_string_args=("synapseResources.limits.cpu=2",),
    )
    mindroom = _resource(docs, "Deployment", "mindroom-demo")
    synapse = _resource(docs, "Deployment", "synapse-demo")

    assert _container(mindroom, "mindroom")["resources"] == {
        "requests": {"cpu": "300m", "memory": "768Mi"},
        "limits": {"cpu": "1500m", "memory": "3Gi"},
    }
    assert _container(synapse, "synapse")["resources"] == {
        "requests": {"cpu": "350m", "memory": "1Gi"},
        "limits": {"cpu": "2", "memory": "4Gi"},
    }


def test_instance_chart_renders_with_pre_resource_release_values(tmp_path: Path) -> None:
    """Older release values should not break chart upgrades before resources are set."""
    values_path = tmp_path / "old-instance-values.yaml"
    values_path.write_text(
        "customer: demo\nbaseDomain: mindroom.chat\nmindroomResources:\nsynapseResources:\nsandboxRunnerResources:\n",
        encoding="utf-8",
    )

    docs = _render_chart(Path("cluster/k8s/instance"), values_files=(values_path,))
    mindroom = _resource(docs, "Deployment", "mindroom-demo")
    synapse = _resource(docs, "Deployment", "synapse-demo")

    assert _container(mindroom, "mindroom")["resources"] == {}
    assert _container(mindroom, "sandbox-runner")["resources"] == {}
    assert _container(synapse, "synapse")["resources"] == {}


def test_platform_chart_renders_default_resources_for_stateless_services() -> None:
    """Platform pods need requests and limits so scheduling and HPA decisions are meaningful."""
    docs = _render_chart(Path("cluster/k8s/platform"), release_name="mindroom-platform")
    frontend = _resource(docs, "Deployment", "platform-frontend")
    backend = _resource(docs, "Deployment", "platform-backend")

    assert _container(frontend, "app")["resources"] == {
        "requests": {"cpu": "100m", "memory": "256Mi"},
        "limits": {"cpu": "500m", "memory": "512Mi"},
    }
    assert _container(backend, "app")["resources"] == {
        "requests": {"cpu": "250m", "memory": "512Mi"},
        "limits": {"cpu": "1000m", "memory": "1Gi"},
    }


def test_platform_chart_renders_with_pre_resource_release_values(tmp_path: Path) -> None:
    """Older release values should not break chart upgrades before resources are set."""
    values_path = tmp_path / "old-platform-values.yaml"
    values_path.write_text(
        "environment: production\ndomain: mindroom.chat\nresources:\n",
        encoding="utf-8",
    )

    docs = _render_chart(
        Path("cluster/k8s/platform"),
        release_name="mindroom-platform",
        values_files=(values_path,),
    )
    frontend = _resource(docs, "Deployment", "platform-frontend")
    backend = _resource(docs, "Deployment", "platform-backend")

    assert _container(frontend, "app")["resources"] == {}
    assert _container(backend, "app")["resources"] == {}


def test_platform_chart_can_render_hpa_for_stateless_services() -> None:
    """Horizontal autoscaling should be opt-in for platform frontend and backend."""
    docs = _render_chart(
        Path("cluster/k8s/platform"),
        "autoscaling.enabled=true",
        "autoscaling.frontend.minReplicas=1",
        "autoscaling.frontend.maxReplicas=3",
        "autoscaling.frontend.targetCPUUtilizationPercentage=70",
        "autoscaling.backend.minReplicas=1",
        "autoscaling.backend.maxReplicas=4",
        "autoscaling.backend.targetCPUUtilizationPercentage=65",
        release_name="mindroom-platform",
    )
    frontend_hpa = _resource(docs, "HorizontalPodAutoscaler", "platform-frontend")
    backend_hpa = _resource(docs, "HorizontalPodAutoscaler", "platform-backend")

    assert frontend_hpa["spec"]["scaleTargetRef"] == {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "name": "platform-frontend",
    }
    assert frontend_hpa["spec"]["minReplicas"] == 1
    assert frontend_hpa["spec"]["maxReplicas"] == 3
    assert frontend_hpa["spec"]["metrics"][0]["resource"]["target"]["averageUtilization"] == 70

    assert backend_hpa["spec"]["scaleTargetRef"] == {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "name": "platform-backend",
    }
    assert backend_hpa["spec"]["minReplicas"] == 1
    assert backend_hpa["spec"]["maxReplicas"] == 4
    assert backend_hpa["spec"]["metrics"][0]["resource"]["target"]["averageUtilization"] == 65


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


def test_runtime_chart_static_runner_uses_credentials_encryption_key_secret() -> None:
    """Static runner mode should wire the optional credential key Secret into both containers."""
    docs = _render_chart(
        Path("cluster/k8s/runtime"),
        "workers.sandbox.proxyToken.value=test-token",
        "workers.sandbox.credentialsEncryptionKey.existingSecret=runtime-credentials",
        "eventCache.postgres.auth.password=test-password",
        release_name="mindroom-runtime",
    )
    deployment = _resource(docs, "Deployment", "mindroom-runtime")
    mindroom_container = _container(deployment, "mindroom")
    runner_container = _container(deployment, "sandbox-runner")
    expected_env = {
        "name": "MINDROOM_CREDENTIALS_ENCRYPTION_KEY",
        "valueFrom": {
            "secretKeyRef": {
                "name": "runtime-credentials",
                "key": "MINDROOM_CREDENTIALS_ENCRYPTION_KEY",
            },
        },
    }

    assert _env_by_name(mindroom_container)["MINDROOM_CREDENTIALS_ENCRYPTION_KEY"] == expected_env
    assert _env_by_name(runner_container)["MINDROOM_CREDENTIALS_ENCRYPTION_KEY"] == expected_env


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
