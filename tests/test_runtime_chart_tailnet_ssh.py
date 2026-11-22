"""Rendered runtime chart checks for the tailnet SSH gateway, its token, and its NetworkPolicies."""

from __future__ import annotations

import base64
import hashlib
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
NAME = f"{FULLNAME}-tailnet-ssh"
CONTROL_PLANE_SELECTOR = {
    "app.kubernetes.io/name": "mindroom-runtime",
    "app.kubernetes.io/instance": RELEASE,
    "app.kubernetes.io/component": "runtime",
}
GATEWAY_SELECTOR = {
    "app.kubernetes.io/name": "mindroom-runtime",
    "app.kubernetes.io/instance": RELEASE,
    "app.kubernetes.io/component": "tailnet-ssh",
}
WORKER_SELECTOR = {
    "mindroom.ai/component": "worker",
    "app.kubernetes.io/managed-by": "mindroom",
    "app.kubernetes.io/name": "mindroom-worker",
    "mindroom.ai/instance": "demo",
}
RELEASE_ID = hashlib.sha256(f"{RELEASE_NAMESPACE}/{RELEASE}".encode()).hexdigest()[:32]
RELEASE_ID_LABEL = "mindroom.ai/tailnet-ssh-release"
RELEASE_ID_ENV = "MINDROOM_KUBERNETES_WORKER_TAILNET_SSH_RELEASE_ID"
WORKER_POLICY = f"mindroom-tailnet-ssh-workers-{RELEASE_ID}"
SSH_WORKER_SELECTOR = {**WORKER_SELECTOR, "mindroom.ai/tailnet-ssh": "true", RELEASE_ID_LABEL: RELEASE_ID}
PRIVATE_RANGES = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16"]
# The gateway's egress before extraEgress and the public 443 rule: DNS, the primary's listener, and marked workers.
BASE_GATEWAY_EGRESS = [
    {
        "to": [
            {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
            },
        ],
        "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
    },
    {"to": [{"podSelector": {"matchLabels": CONTROL_PLANE_SELECTOR}}], "ports": [{"protocol": "TCP", "port": 8769}]},
    {
        "to": [
            {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": WORKER_NAMESPACE}},
                "podSelector": {"matchLabels": SSH_WORKER_SELECTOR},
            },
        ],
        "ports": [{"protocol": "TCP", "port": 2222}],
    },
]
PUBLIC_443_EGRESS = {
    "to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": PRIVATE_RANGES}}],
    "ports": [{"protocol": "TCP", "port": 443}],
}
PROXY_EGRESS_RULE = {"to": [{"ipBlock": {"cidr": "192.0.2.10/32"}}], "ports": [{"protocol": "TCP", "port": 3128}]}
BASE_VALUES = ("workers.sandbox.proxyToken.value=test-token",)
WORKER_VALUES = (
    "workers.backend=kubernetes",
    f"workers.kubernetes.namespace={WORKER_NAMESPACE}",
    "workers.kubernetes.extraLabels.mindroom\\.ai/instance=demo",
)
AUTH_KEY = "tailnetSsh.authKeySecret=tailnet-key"
ENABLED = (*WORKER_VALUES, "tailnetSsh.enabled=true", AUTH_KEY)
PROXY_EGRESS = (
    "tailnetSsh.httpsProxy=http://proxy.example:3128",
    "tailnetSsh.extraEgress[0].to[0].ipBlock.cidr=192.0.2.10/32",
    "tailnetSsh.extraEgress[0].ports[0].protocol=TCP",
    "tailnetSsh.extraEgress[0].ports[0].port=3128",
)


def _render(*set_args: str) -> list[dict[str, Any]]:
    return _render_chart(RUNTIME_CHART, *BASE_VALUES, *set_args, release_name=RELEASE, namespace=RELEASE_NAMESPACE)


def _tailnet_docs(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [doc for doc in docs if doc["metadata"]["name"].startswith(NAME)]


def _primary(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return _container(_resource(docs, "Deployment", FULLNAME), "mindroom")


def _gateway_pod(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return _resource(docs, "Deployment", NAME)["spec"]["template"]


def _gateway_env(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return _env_by_name(_container(_resource(docs, "Deployment", NAME), "tailnet-gateway"))


def _rule_ports(rules: list[dict[str, Any]]) -> set[tuple[int, str]]:
    return {(port["port"], port["protocol"]) for rule in rules for port in rule["ports"]}


def test_disabled_renders_nothing_and_no_primary_env() -> None:
    """Without opting in, the chart renders no gateway resource and the primary keeps the feature off."""
    docs = _render(*WORKER_VALUES, "networkPolicy.create=true")
    primary = _primary(docs)

    assert _tailnet_docs(docs) == []
    assert [port["name"] for port in primary["ports"]] == ["api"]
    assert not [name for name in _env_by_name(primary) if name.startswith("MINDROOM_TAILNET_SSH_")]


@pytest.mark.parametrize(
    ("image_values", "tag"),
    [
        ((), "latest"),
        (("image.tag=v2026.11.21",), "v2026.11.21"),
        (("image.tag=v2026.11.21", "tailnetSsh.image.tag=1.2.3"), "1.2.3"),
    ],
)
def test_gateway_tag_follows_primary_unless_overridden(image_values: tuple[str, ...], tag: str) -> None:
    """A chart from checkout defaults to latest; pinning the runtime pins the gateway unless overridden."""
    docs = _render(*ENABLED, *image_values)
    container = _container(_resource(docs, "Deployment", NAME), "tailnet-gateway")

    assert container["image"] == f"ghcr.io/mindroom-ai/mindroom-tailnet-gateway:{tag}"


def test_gateway_deployment_is_single_recreate_and_hardened() -> None:
    """One replica that never overlaps its successor, non-root on a read-only root, with no service account token."""
    docs = _render(
        *ENABLED,
        "nodeSelector.pool=control",
        "tolerations[0].key=dedicated",
        "tolerations[0].operator=Exists",
    )
    deployment = _resource(docs, "Deployment", NAME)
    pod = deployment["spec"]["template"]
    container = _container(deployment, "tailnet-gateway")

    assert deployment["metadata"].get("namespace") is None
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    assert deployment["spec"]["selector"] == {"matchLabels": GATEWAY_SELECTOR}
    assert pod["metadata"]["labels"] == GATEWAY_SELECTOR
    assert pod["spec"]["automountServiceAccountToken"] is False
    assert pod["spec"]["nodeSelector"] == {"pool": "control"}
    assert pod["spec"]["tolerations"] == [{"key": "dedicated", "operator": "Exists"}]
    assert pod["spec"]["securityContext"] == {
        "runAsNonRoot": True,
        "fsGroup": 65532,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert container["securityContext"] == {
        "runAsNonRoot": True,
        "readOnlyRootFilesystem": True,
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert container["volumeMounts"] == [
        {"name": "state", "mountPath": "/state"},
        {"name": "tmp", "mountPath": "/tmp"},  # noqa: S108 - an emptyDir, the pod's own
    ]
    assert pod["spec"]["volumes"] == [
        {"name": "state", "persistentVolumeClaim": {"claimName": NAME}},
        {"name": "tmp", "emptyDir": {}},
    ]
    claim = _resource(docs, "PersistentVolumeClaim", NAME)
    assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert claim["spec"]["resources"] == {"requests": {"storage": "1Gi"}}
    assert "storageClassName" not in claim["spec"]


def test_gateway_env_points_at_primary() -> None:
    """The gateway reaches the primary's listener by its Service and holds the token from the shared Secret."""
    docs = _render(*ENABLED, "tailnetSsh.port=9100")
    env = _gateway_env(docs)

    assert env == {
        "TAILNET_SSH_PRIMARY_URL": {
            "name": "TAILNET_SSH_PRIMARY_URL",
            "value": f"http://{NAME}.{RELEASE_NAMESPACE}.svc.cluster.local:9100",
        },
        "TAILNET_SSH_GATEWAY_TOKEN": {
            "name": "TAILNET_SSH_GATEWAY_TOKEN",
            "valueFrom": {"secretKeyRef": {"name": NAME, "key": "token"}},
        },
        "TAILNET_SSH_AUTH_KEY": {
            "name": "TAILNET_SSH_AUTH_KEY",
            "valueFrom": {"secretKeyRef": {"name": "tailnet-key", "key": "auth-key"}},
        },
    }
    service = _resource(docs, "Service", NAME)
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["selector"] == CONTROL_PLANE_SELECTOR
    assert service["spec"]["ports"] == [
        {"port": 9100, "targetPort": "tailnet-ssh", "protocol": "TCP", "name": "tailnet-ssh"},
    ]


@pytest.mark.parametrize(("key_args", "key"), [((), "token"), (("tailnetSsh.key=listener-token",), "listener-token")])
def test_existing_listener_token_secret_replaces_generated_token(key_args: tuple[str, ...], key: str) -> None:
    """Both deployments read the same operator-managed token, with no generated token Secret."""
    docs = _render(*ENABLED, "tailnetSsh.existingSecret=operator-listener", *key_args)

    assert all(doc["kind"] != "Secret" or doc["metadata"]["name"] != NAME for doc in docs)
    expected = {"secretKeyRef": {"name": "operator-listener", "key": key}}
    assert _env_by_name(_primary(docs))["MINDROOM_TAILNET_SSH_GATEWAY_TOKEN"]["valueFrom"] == expected
    assert _gateway_env(docs)["TAILNET_SSH_GATEWAY_TOKEN"]["valueFrom"] == expected


def test_login_server_tags_and_proxy_are_optional_env() -> None:
    """Headscale, tags, and a proxy each add their variable only when set."""
    docs = _render(
        *ENABLED,
        *PROXY_EGRESS,
        "tailnetSsh.loginServer=https://headscale.example",
        "tailnetSsh.tags={tag:mindroom,tag:ssh}",
        "tailnetSsh.persistence.storageClassName=fast",
    )
    env = _gateway_env(docs)

    assert env["TAILNET_SSH_LOGIN_SERVER"]["value"] == "https://headscale.example"
    assert env["TAILNET_SSH_TAGS"]["value"] == "tag:mindroom,tag:ssh"
    assert env["HTTPS_PROXY"]["value"] == "http://proxy.example:3128"
    assert _resource(docs, "PersistentVolumeClaim", NAME)["spec"]["storageClassName"] == "fast"


def test_primary_gets_port_and_token_from_the_secret() -> None:
    """The primary serves the listener port and reads the same generated token as the gateway."""
    docs = _render(*ENABLED, "tailnetSsh.port=9100")
    primary = _primary(docs)
    env = _env_by_name(primary)

    assert {"name": "tailnet-ssh", "containerPort": 9100, "protocol": "TCP"} in primary["ports"]
    assert env["MINDROOM_TAILNET_SSH_PORT"]["value"] == "9100"
    assert env["MINDROOM_TAILNET_SSH_GATEWAY_TOKEN"]["valueFrom"] == {"secretKeyRef": {"name": NAME, "key": "token"}}
    secret = _resource(docs, "Secret", NAME)
    token = base64.b64decode(secret["data"]["token"]).decode()
    assert len(token) == 48
    assert token.isalnum()
    # Only the primary and the gateway hold the token; no worker manifest or env names it.
    holders = {
        (doc["metadata"]["name"], container["name"])
        for doc in docs
        if doc["kind"] == "Deployment"
        for container in doc["spec"]["template"]["spec"]["containers"]
        if NAME in json.dumps(container.get("env", []))
    }
    assert holders == {(FULLNAME, "mindroom"), (NAME, "tailnet-gateway")}


def test_worker_policy_admits_gateway_only_to_labelled_pods_on_2222() -> None:
    """Only workers with the tailnet SSH mark gain one ingress rule: gateway pods of the release namespace on 2222.

    The rule is additive to the chart's worker policy; alone it would cut the primary off from marked workers.
    """
    docs = _render(*ENABLED)
    policy = _resource(docs, "NetworkPolicy", WORKER_POLICY)

    assert policy["metadata"]["namespace"] == WORKER_NAMESPACE
    assert policy["spec"]["podSelector"] == {"matchLabels": SSH_WORKER_SELECTOR}
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    assert policy["spec"]["ingress"] == [
        {
            "from": [
                {
                    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": RELEASE_NAMESPACE}},
                    "podSelector": {"matchLabels": GATEWAY_SELECTOR},
                },
            ],
            "ports": [{"protocol": "TCP", "port": 2222}],
        },
    ]
    without_worker_policy = _render(*ENABLED, "workers.kubernetes.networkPolicy.create=false")
    assert WORKER_POLICY not in [doc["metadata"]["name"] for doc in without_worker_policy]


@pytest.mark.parametrize(("release", "namespace"), [("another", RELEASE_NAMESPACE), (RELEASE, "another")])
def test_ssh_policies_isolate_releases_in_a_shared_worker_namespace(release: str, namespace: str) -> None:
    """Even with the same extra labels, only each release's gateway can reach its marked workers."""
    first = _render(*ENABLED)
    second = _render_chart(RUNTIME_CHART, *BASE_VALUES, *ENABLED, release_name=release, namespace=namespace)
    identities = set()
    ingress_names = set()
    for docs in (first, second):
        [primary] = [
            doc for doc in docs if doc["kind"] == "Deployment" and not doc["metadata"]["name"].endswith("-tailnet-ssh")
        ]
        release_id = _env_by_name(_container(primary, "mindroom"))[RELEASE_ID_ENV]["value"]
        [ingress] = [
            doc
            for doc in docs
            if doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"].startswith("mindroom-tailnet-ssh-workers-")
        ]
        [gateway] = [
            doc for doc in docs if doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"].endswith("-tailnet-ssh")
        ]
        [egress] = [rule for rule in gateway["spec"]["egress"] if (2222, "TCP") in _rule_ports([rule])]
        assert ingress["metadata"]["namespace"] == WORKER_NAMESPACE
        worker_selector = {"matchLabels": {**SSH_WORKER_SELECTOR, RELEASE_ID_LABEL: release_id}}
        assert ingress["spec"]["podSelector"] == worker_selector
        assert egress["to"][0]["podSelector"] == worker_selector
        identities.add(release_id)
        ingress_names.add(ingress["metadata"]["name"])
    assert len(identities) == len(ingress_names) == 2


def test_primary_policy_admits_gateway_on_the_listener_port() -> None:
    """With the control-plane NetworkPolicy, gateway pods are admitted on the listener port; without it none renders.

    A standalone ingress policy would otherwise isolate an open control-plane pod.
    """
    docs = _render(*ENABLED, "networkPolicy.create=true")
    policy = _resource(docs, "NetworkPolicy", f"{NAME}-primary")

    assert policy["spec"]["podSelector"] == {"matchLabels": CONTROL_PLANE_SELECTOR}
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    assert policy["spec"]["ingress"] == [
        {"from": [{"podSelector": {"matchLabels": GATEWAY_SELECTOR}}], "ports": [{"protocol": "TCP", "port": 8769}]},
    ]
    assert f"{NAME}-primary" not in [doc["metadata"]["name"] for doc in _render(*ENABLED)]


def test_gateway_policy_has_no_ingress_and_only_named_egress() -> None:
    """The gateway admits nothing and reaches DNS, the listener, marked workers, and public TCP 443 only."""
    docs = _render(*ENABLED)
    spec = _resource(docs, "NetworkPolicy", NAME)["spec"]
    egress = spec["egress"]

    assert spec["podSelector"] == {"matchLabels": GATEWAY_SELECTOR}
    assert spec["policyTypes"] == ["Ingress", "Egress"]
    assert not spec.get("ingress")
    assert _rule_ports(egress) == {(53, "UDP"), (53, "TCP"), (8769, "TCP"), (2222, "TCP"), (443, "TCP")}
    assert egress == [*BASE_GATEWAY_EGRESS, PUBLIC_443_EGRESS]


def test_https_proxy_drops_public_443_and_requires_extra_egress() -> None:
    """With a proxy the public 443 rule is gone and only the operator's rule admits the proxy."""
    egress = _resource(_render(*ENABLED, *PROXY_EGRESS), "NetworkPolicy", NAME)["spec"]["egress"]

    assert egress == [*BASE_GATEWAY_EGRESS, PROXY_EGRESS_RULE]

    completed = _run_helm_template(
        RUNTIME_CHART,
        *BASE_VALUES,
        *ENABLED,
        "tailnetSsh.httpsProxy=http://proxy.example:3128",
        release_name=RELEASE,
    )
    assert completed.returncode != 0
    assert "tailnetSsh.httpsProxy requires tailnetSsh.extraEgress to admit the proxy" in completed.stderr


@pytest.mark.parametrize(
    "value",
    ["workers.backend=static_runner", "tailnetSsh.port=8765", "tailnetSsh.port=8767", "tailnetSsh.port=8768"],
)
def test_validation_fails_with_a_sentence(value: str) -> None:
    """A gateway without Kubernetes workers, or a listener on the API, script gateway, or egress broker port, fails."""
    completed = _run_helm_template(RUNTIME_CHART, *BASE_VALUES, *ENABLED, value, release_name=RELEASE)

    assert completed.returncode != 0
    if value.startswith("workers."):
        assert "tailnetSsh.enabled requires workers.backend=kubernetes" in completed.stderr
    else:
        assert (
            "tailnetSsh.port must differ from runtime.apiPort, scriptGateway.port, and egressBroker.port"
            in completed.stderr
        )


@pytest.mark.parametrize("secret", [(), ("tailnetSsh.authKeySecret=  ",)])
def test_enabled_requires_a_nonblank_auth_key_secret(secret: tuple[str, ...]) -> None:
    """An enabled gateway cannot render without an operator's auth-key Secret reference."""
    values = (*BASE_VALUES, *WORKER_VALUES, "tailnetSsh.enabled=true", *secret)
    completed = _run_helm_template(RUNTIME_CHART, *values, release_name=RELEASE)
    assert completed.returncode != 0
    assert "tailnetSsh.enabled requires tailnetSsh.authKeySecret" in completed.stderr


@pytest.mark.parametrize(
    ("set_args", "error"),
    [
        (("tailnetSsh.enabled=false",), "tailnetSsh.existingSecret cannot be set when tailnetSsh.enabled=false"),
        (("tailnetSsh.key=",), "tailnetSsh.key is required when tailnetSsh.existingSecret is set"),
        (
            (f"tailnetSsh.existingSecret={NAME}",),
            f"tailnetSsh.existingSecret must name a Secret the chart does not manage, not {NAME}",
        ),
        (
            ("env.extra[0].name=MINDROOM_TAILNET_SSH_GATEWAY_TOKEN", "env.extra[0].value=duplicate"),
            "tailnetSsh.existingSecret and env.extra both set MINDROOM_TAILNET_SSH_GATEWAY_TOKEN; remove one of them",
        ),
    ],
)
def test_rejects_conflicting_existing_listener_token_secret(set_args: tuple[str, ...], error: str) -> None:
    """An existing token Secret must be the only token source and used with the gateway enabled."""
    completed = _run_helm_template(
        RUNTIME_CHART,
        *BASE_VALUES,
        *ENABLED,
        "tailnetSsh.existingSecret=operator-listener",
        *set_args,
        release_name=RELEASE,
    )

    assert completed.returncode != 0
    assert error in completed.stderr


def test_worker_egress_policy_and_squid_are_unchanged() -> None:
    """Workers gain one ingress rule and no egress: their policy and the approved-egress proxy render identically."""
    approved_egress = ("approvedEgress.enabled=true", "approvedEgress.image.tag=test")

    def worker_side(*extra: str) -> list[dict[str, Any]]:
        docs = _render(*WORKER_VALUES, *approved_egress, "networkPolicy.create=true", *extra)
        return [
            doc
            for doc in docs
            if doc["metadata"]["name"] == f"{FULLNAME}-workers"
            or doc["metadata"]["name"].startswith(f"{FULLNAME}-egress-proxy")
        ]

    off = worker_side()
    assert {doc["kind"] for doc in off} >= {"NetworkPolicy", "Deployment", "Service"}
    assert worker_side("tailnetSsh.enabled=true", AUTH_KEY) == off
