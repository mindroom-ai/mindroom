"""Tests for broker env composition and runner CA bundle materialization."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import pytest

from mindroom.constants import resolve_runtime_paths
from mindroom.egress_broker.env import (
    BROKER_CA_PEM_ENV,
    apply_runner_ca_bundle,
    broker_execution_env,
    egress_broker_enabled,
    primary_callback_hosts,
)


def test_broker_env_sets_proxy_with_token_userinfo(test_ca_pem: str) -> None:
    """HTTPS_PROXY includes the token as userinfo with a fixed password, and the git proxy entries are present."""
    result = broker_execution_env(
        broker_url="http://host.docker.internal:8768",
        token="mrb1.eyJjbGFpbXMiOnsic2NvcGUiOiJ3b3JrZXItMSJ9LCJleHAiOjE3MDAwMDAwMDB9.abcdef",  # noqa: S106
        ca_pem=test_ca_pem,
        placeholder_env={"PLACEHOLDER": "mindroom-brokered"},
        extra_no_proxy_hosts=["example.com"],
    )

    expected_proxy = "http://mrb1.eyJjbGFpbXMiOnsic2NvcGUiOiJ3b3JrZXItMSJ9LCJleHAiOjE3MDAwMDAwMDB9.abcdef:mindroom@host.docker.internal:8768"
    assert result["HTTP_PROXY"] == expected_proxy
    assert result["HTTPS_PROXY"] == expected_proxy
    assert result["http_proxy"] == expected_proxy
    assert result["https_proxy"] == expected_proxy

    # Git config entries for proxy
    git_count = int(result["GIT_CONFIG_COUNT"])
    assert git_count == 2
    git_config = {result[f"GIT_CONFIG_KEY_{i}"]: result[f"GIT_CONFIG_VALUE_{i}"] for i in range(git_count)}
    assert git_config["http.proxy"] == expected_proxy
    assert git_config["http.proxyAuthMethod"] == "basic"


def test_proxy_url_carries_a_non_empty_password(test_ca_pem: str) -> None:
    """The password is never empty: Python urllib and Node fetch send proxy credentials only when both parts are set.

    The token stays the user name, percent-encoded, which is where the broker reads it.
    """
    token = "mrb1.e30.sig/+=:@"  # noqa: S105 - URL-significant characters must be quoted
    result = broker_execution_env(
        broker_url="http://broker:8768",
        token=token,
        ca_pem=test_ca_pem,
        placeholder_env={},
        extra_no_proxy_hosts=[],
    )

    assert result["HTTPS_PROXY"] == f"http://{quote(token, safe='')}:mindroom@broker:8768"
    parts = urlsplit(result["HTTPS_PROXY"])
    assert (unquote(parts.username or ""), parts.password) == (token, "mindroom")


def test_no_proxy_includes_primary_callback_hosts(test_ca_pem: str) -> None:
    """NO_PROXY includes parsed hosts from primary callback URLs plus extra_no_proxy_hosts."""
    result = broker_execution_env(
        broker_url="http://broker:8768",
        token="mrb1.token",  # noqa: S106
        ca_pem=test_ca_pem,
        placeholder_env={},
        extra_no_proxy_hosts=["host.docker.internal", "another.local"],
    )

    # Default NO_PROXY: localhost,127.0.0.1,::1,.svc,.cluster.local
    # Plus extra_no_proxy_hosts
    no_proxy = result["NO_PROXY"]
    assert "localhost" in no_proxy
    assert "127.0.0.1" in no_proxy
    assert "::1" in no_proxy
    assert ".svc" in no_proxy
    assert ".cluster.local" in no_proxy
    assert "host.docker.internal" in no_proxy
    assert "another.local" in no_proxy
    # Verify duplicates are removed
    assert no_proxy == result["no_proxy"]


def test_placeholder_env_and_node_proxy_flag(test_ca_pem: str) -> None:
    """Placeholder env vars and NODE_USE_ENV_PROXY=1 are included."""
    result = broker_execution_env(
        broker_url="http://broker:8768",
        token="mrb1.token",  # noqa: S106
        ca_pem=test_ca_pem,
        placeholder_env={"GITHUB_TOKEN": "mindroom-brokered", "PYPI_TOKEN": "mindroom-brokered"},
        extra_no_proxy_hosts=[],
    )

    assert result["GITHUB_TOKEN"] == "mindroom-brokered"  # noqa: S105
    assert result["PYPI_TOKEN"] == "mindroom-brokered"  # noqa: S105
    assert result["NODE_USE_ENV_PROXY"] == "1"
    assert BROKER_CA_PEM_ENV in result
    assert result[BROKER_CA_PEM_ENV] == test_ca_pem


def test_apply_runner_ca_bundle_sets_ca_vars_and_pops_pem(tmp_path: Path, test_ca_pem: str) -> None:
    """apply_runner_ca_bundle creates bundle files, sets CA env vars, and removes the PEM env."""
    test_env = {
        BROKER_CA_PEM_ENV: test_ca_pem,
        "EXISTING_VAR": "kept",
    }

    result = apply_runner_ca_bundle(test_env, tmp_path)

    assert result is True
    assert BROKER_CA_PEM_ENV not in test_env
    assert test_env["EXISTING_VAR"] == "kept"

    # SSL_CERT_FILE points to combined bundle
    assert "SSL_CERT_FILE" in test_env
    ssl_cert_file = Path(test_env["SSL_CERT_FILE"])
    assert ssl_cert_file.exists()
    assert ssl_cert_file.parent == tmp_path
    bundle_content = ssl_cert_file.read_text()
    # Verify test CA PEM is in the bundle (ends with the broker CA)
    assert bundle_content.endswith(test_ca_pem)

    # REQUESTS_CA_BUNDLE, CURL_CA_BUNDLE, GIT_SSL_CAINFO point to same combined bundle
    assert test_env["REQUESTS_CA_BUNDLE"] == str(ssl_cert_file)
    assert test_env["CURL_CA_BUNDLE"] == str(ssl_cert_file)
    assert test_env["GIT_SSL_CAINFO"] == str(ssl_cert_file)

    # NODE_EXTRA_CA_CERTS points to broker-only file
    assert "NODE_EXTRA_CA_CERTS" in test_env
    node_ca_file = Path(test_env["NODE_EXTRA_CA_CERTS"])
    assert node_ca_file.exists()
    assert node_ca_file.parent == tmp_path
    assert node_ca_file.read_text() == test_ca_pem


def test_apply_runner_ca_bundle_noop_without_pem(tmp_path: Path) -> None:
    """apply_runner_ca_bundle returns False when BROKER_CA_PEM_ENV is absent."""
    test_env = {"OTHER_VAR": "value"}
    result = apply_runner_ca_bundle(test_env, tmp_path)

    assert result is False
    assert test_env == {"OTHER_VAR": "value"}


@pytest.mark.parametrize(
    ("process_env", "expected_hosts"),
    [
        pytest.param(
            {
                "MINDROOM_EGRESS_BROKER_URL": "http://broker.local:8768",
                "MINDROOM_SCRIPT_GATEWAY_URL": "http://gateway.local:9000",
                "MINDROOM_AGENT_CLI_PRIMARY_URL": "http://host.docker.internal:8766",
                "MINDROOM_AGENT_CLI_URL": "http://api.mindroom.chat",
            },
            ["broker.local", "gateway.local", "host.docker.internal", "api.mindroom.chat"],
            id="parses-urls",
        ),
        pytest.param(
            {
                "MINDROOM_EGRESS_BROKER_URL": "http://broker.local:8768",
                "MINDROOM_SCRIPT_GATEWAY_URL": "   ",
                "MINDROOM_AGENT_CLI_PRIMARY_URL": "not-a-url",
                # MINDROOM_AGENT_CLI_URL unset
            },
            ["broker.local"],
            id="skips-unset-blank-and-unparsable",
        ),
        # urlsplit raises ValueError for these, such as an unclosed IPv6 bracket; they are skipped like other bad ones.
        pytest.param(
            {
                "MINDROOM_EGRESS_BROKER_URL": "http://[::1",
                "MINDROOM_SCRIPT_GATEWAY_URL": "http://gateway.local:9000",
            },
            ["gateway.local"],
            id="skips-urls-that-urlsplit-rejects",
        ),
        # host.docker.internal appears twice (broker and primary), other.local appears twice (gateway and cli);
        # only the first occurrence of each stays.
        pytest.param(
            {
                "MINDROOM_EGRESS_BROKER_URL": "http://host.docker.internal:8768",
                "MINDROOM_SCRIPT_GATEWAY_URL": "http://other.local:9000",
                "MINDROOM_AGENT_CLI_PRIMARY_URL": "http://host.docker.internal:8766",
                "MINDROOM_AGENT_CLI_URL": "http://other.local",
            },
            ["host.docker.internal", "other.local"],
            id="deduplicates-preserving-order",
        ),
    ],
)
def test_primary_callback_hosts(tmp_path: Path, process_env: dict[str, str], expected_hosts: list[str]) -> None:
    """primary_callback_hosts extracts hostnames from the configured URLs, skipping bad ones and keeping first-seen order."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env=process_env,
    )

    assert primary_callback_hosts(runtime_paths) == expected_hosts


@pytest.mark.parametrize(
    ("process_env", "enabled"),
    [
        pytest.param({}, False, id="unset"),
        pytest.param({"MINDROOM_EGRESS_BROKER_PORT": "   "}, False, id="blank"),
        pytest.param({"MINDROOM_EGRESS_BROKER_PORT": "8768"}, True, id="set"),
        # An invalid port still counts, so startup refuses it instead of the feature silently disappearing.
        pytest.param({"MINDROOM_EGRESS_BROKER_PORT": "http"}, True, id="invalid"),
        # The worker-facing URL alone does not start the broker.
        pytest.param({"MINDROOM_EGRESS_BROKER_URL": "http://broker.local:8768"}, False, id="url-only"),
    ],
)
def test_egress_broker_enabled_follows_the_port_setting(
    tmp_path: Path,
    process_env: dict[str, str],
    *,
    enabled: bool,
) -> None:
    """The broker, and with it every Agent Keys surface, is enabled exactly when its port setting is set."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env=process_env,
    )

    assert egress_broker_enabled(runtime_paths) is enabled
