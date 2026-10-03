"""Rendered Helm manifest checks for the optional MatrixRTC chart."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.test_helm_instance_worker_isolation import (
    _container,
    _env_by_name,
    _render_chart,
    _resource,
    _run_helm_template,
)

CHART = Path("cluster/k8s/matrixrtc")
REQUIRED = (
    "keys.existingSecret=matrixrtc-keys",
    "livekit.media.loadBalancerIP=203.0.113.10",
    "auth.livekitUrl=wss://matrix.example.com/livekit/sfu",
    "auth.fullAccessHomeservers[0]=example.com",
)
LIVEKIT = "matrixrtc-mindroom-matrixrtc-livekit"
AUTH = "matrixrtc-mindroom-matrixrtc-auth"


def _render(*set_args: str) -> list[dict[str, Any]]:
    return _render_chart(CHART, *REQUIRED, *set_args, release_name="matrixrtc")


def _livekit_config(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return yaml.safe_load(_resource(docs, "ConfigMap", LIVEKIT)["data"]["config.yaml"])


def test_media_address_feeds_both_the_load_balancer_and_livekit_advertisement() -> None:
    """Clients only reach media when LiveKit advertises the address and ports the load balancer serves."""
    docs = _render()
    config = _livekit_config(docs)
    media = _resource(docs, "Service", f"{LIVEKIT}-media")

    assert config["rtc"] == {"node_ip": "203.0.113.10", "tcp_port": 7881, "udp_port": 7882, "use_external_ip": False}
    assert config["room"] == {"auto_create": False}
    assert media["spec"]["type"] == "LoadBalancer"
    assert media["spec"]["loadBalancerIP"] == "203.0.113.10"
    assert [(port["protocol"], port["port"]) for port in media["spec"]["ports"]] == [("TCP", 7881), ("UDP", 7882)]


def test_both_components_share_one_secret_without_putting_keys_in_the_config() -> None:
    """LiveKit's key list is derived from the same Secret keys the authorization service reads."""
    docs = _render()
    livekit_env = _container(_resource(docs, "Deployment", LIVEKIT), "livekit")["env"]
    auth_env = _env_by_name(_container(_resource(docs, "Deployment", AUTH), "auth"))
    secret_ref = {"name": "matrixrtc-keys", "key": "LIVEKIT_KEY"}

    names = [entry["name"] for entry in livekit_env]
    assert names.index("MATRIXRTC_API_KEY") < names.index("LIVEKIT_KEYS")
    assert names.index("MATRIXRTC_API_SECRET") < names.index("LIVEKIT_KEYS")
    assert livekit_env[names.index("MATRIXRTC_API_KEY")]["valueFrom"]["secretKeyRef"] == secret_ref
    livekit_keys = livekit_env[names.index("LIVEKIT_KEYS")]["value"]
    assert livekit_keys == '"$(MATRIXRTC_API_KEY)": "$(MATRIXRTC_API_SECRET)"'
    # LiveKit keeps only string secrets, so a digits-only secret must not parse as a number.
    expanded = livekit_keys.replace("$(MATRIXRTC_API_KEY)", "12345678").replace("$(MATRIXRTC_API_SECRET)", "1" * 32)
    assert yaml.safe_load(expanded) == {"12345678": "1" * 32}
    assert auth_env["LIVEKIT_KEY"]["valueFrom"]["secretKeyRef"] == secret_ref
    assert auth_env["LIVEKIT_SECRET"]["valueFrom"]["secretKeyRef"] == {
        "name": "matrixrtc-keys",
        "key": "LIVEKIT_SECRET",
    }
    assert auth_env["LIVEKIT_URL"]["value"] == "wss://matrix.example.com/livekit/sfu"
    assert auth_env["LIVEKIT_FULL_ACCESS_HOMESERVERS"]["value"] == "example.com"
    assert "keys" not in _livekit_config(docs)
    assert "key_file" not in _livekit_config(docs)


def test_pods_disable_service_links_that_livekit_would_parse_as_flags() -> None:
    """A Service named livekit injects LIVEKIT_PORT=tcp://..., which LiveKit rejects as its port flag."""
    docs = _render()

    for name in (LIVEKIT, AUTH):
        pod_spec = _resource(docs, "Deployment", name)["spec"]["template"]["spec"]
        assert pod_spec["enableServiceLinks"] is False
        assert pod_spec["automountServiceAccountToken"] is False
        assert pod_spec["securityContext"]["runAsNonRoot"] is True


def test_extra_livekit_config_merges_without_replacing_the_media_address() -> None:
    """Operators can add LiveKit options while the chart keeps owning the advertised media address."""
    docs = _render(
        "livekit.extraConfig.webhook.urls[0]=http://hooks.example.com",
        "livekit.extraConfig.rtc.allow_tcp_fallback=true",
    )
    config = _livekit_config(docs)

    assert config["webhook"] == {"urls": ["http://hooks.example.com"]}
    assert config["rtc"]["allow_tcp_fallback"] is True
    assert config["rtc"]["node_ip"] == "203.0.113.10"


def test_network_policy_admits_selected_proxies_and_open_media() -> None:
    """Only the public proxy reaches the HTTP ports, while media stays reachable from any client."""
    assert not [doc for doc in _render() if doc["kind"] == "NetworkPolicy"]

    docs = _render(
        "networkPolicy.enabled=true",
        "networkPolicy.clientPodSelector.matchLabels.app=client",
        "networkPolicy.extraFrom[0].namespaceSelector.matchLabels.team=ingress",
    )
    livekit_rules = _resource(docs, "NetworkPolicy", LIVEKIT)["spec"]["ingress"]
    auth_rules = _resource(docs, "NetworkPolicy", AUTH)["spec"]["ingress"]
    proxy_peers = [
        {"podSelector": {"matchLabels": {"app": "client"}}},
        {"namespaceSelector": {"matchLabels": {"team": "ingress"}}},
    ]

    assert livekit_rules[0]["from"][:2] == proxy_peers
    assert livekit_rules[0]["from"][2]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "auth"
    assert livekit_rules[0]["ports"] == [{"protocol": "TCP", "port": 7880}]
    assert livekit_rules[1] == {"ports": [{"protocol": "TCP", "port": 7881}, {"protocol": "UDP", "port": 7882}]}
    assert auth_rules == [{"from": proxy_peers, "ports": [{"protocol": "TCP", "port": 8080}]}]


@pytest.mark.parametrize(
    ("dropped", "extra", "message"),
    [
        ("keys.existingSecret", (), "keys.existingSecret is required"),
        ("livekit.media.loadBalancerIP", (), "livekit.media.loadBalancerIP is required"),
        ("auth.livekitUrl", (), "auth.livekitUrl is required"),
        ("auth.fullAccessHomeservers", (), "auth.fullAccessHomeservers must list at least one Matrix server name"),
        (
            None,
            ("auth.livekitUrl=https://matrix.example.com/livekit/sfu",),
            "auth.livekitUrl must be a ws:// or wss:// URL",
        ),
        (None, ("livekit.extraConfig.keys.leaked=secret",), "livekit.extraConfig must not set keys;"),
        (None, ("livekit.extraConfig.rtc.node_ip=198.51.100.7",), "livekit.extraConfig must not set rtc.node_ip;"),
        (None, ("livekit.extraConfig.room.auto_create=true",), "livekit.extraConfig must not set room.auto_create;"),
        (
            None,
            ("livekit.extraConfig.rtc.port_range_start=50000", "livekit.extraConfig.rtc.port_range_end=60000"),
            "livekit.extraConfig must not set rtc.port_range_",
        ),
        (None, ("livekit.extraConfig.rtc=replaced",), "livekit.extraConfig.rtc must be a map"),
        (None, ("networkPolicy.enabled=true",), "networkPolicy.enabled requires networkPolicy.clientPodSelector"),
    ],
)
def test_invalid_values_fail_rendering(dropped: str | None, extra: tuple[str, ...], message: str) -> None:
    """Misconfigured calls fail at render time instead of producing a backend clients cannot use."""
    set_args = [arg for arg in REQUIRED if dropped is None or not arg.startswith(dropped)]
    completed = _run_helm_template(CHART, *set_args, *extra, release_name="matrixrtc")

    assert completed.returncode != 0
    assert message in completed.stderr
