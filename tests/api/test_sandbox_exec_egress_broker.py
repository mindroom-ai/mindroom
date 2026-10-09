"""Tests for native broker env precedence over Agent Vault in sandbox_exec."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING

from mindroom.api import sandbox_exec
from mindroom.constants import resolve_runtime_paths
from mindroom.egress_broker.env import BROKER_CA_PEM_ENV, broker_execution_env

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def test_native_broker_env_wins_over_agent_vault(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    test_ca_pem: str,
) -> None:
    """When execution_env carries broker env, Agent Vault overlay is skipped."""
    # Set up Agent Vault token file
    token_path = tmp_path / "av-token"
    token_path.write_text("av_sess_worker_token\n", encoding="utf-8")

    monkeypatch.setenv("MINDROOM_WORKER_EGRESS_PROXY_URL", "http://agent-vault:14322")
    monkeypatch.setenv("MINDROOM_WORKER_EGRESS_PROXY_TOKEN_FILE", str(token_path))
    monkeypatch.setenv("MINDROOM_WORKER_EGRESS_PROXY_VAULT", "agent-vault-worker")
    monkeypatch.setenv("MINDROOM_WORKER_EGRESS_PROXY_CA_FILE", "/etc/agent-vault/ca.pem")

    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={},
    )

    # Compose broker execution env
    broker_env = broker_execution_env(
        broker_url="http://host.docker.internal:8768",
        token="mrb1.worker1token",  # noqa: S106
        ca_pem=test_ca_pem,
        placeholder_env={},
        extra_no_proxy_hosts=[],
    )

    # request_execution_env should apply the broker CA bundle and skip Agent Vault overlay
    final_env = sandbox_exec.request_execution_env("shell", broker_env, runtime_paths)

    # The broker's HTTPS_PROXY should win
    assert "HTTPS_PROXY" in final_env
    assert "mrb1.worker1token" in final_env["HTTPS_PROXY"]
    assert "agent-vault" not in final_env["HTTPS_PROXY"]

    # The broker CA env var should be removed (materialized into files)
    assert BROKER_CA_PEM_ENV not in final_env

    # SSL_CERT_FILE and friends should point to materialized bundles
    assert "SSL_CERT_FILE" in final_env
    assert "REQUESTS_CA_BUNDLE" in final_env
    assert "NODE_EXTRA_CA_CERTS" in final_env
    # Agent Vault's static CA mount should NOT appear
    assert final_env["REQUESTS_CA_BUNDLE"] != "/etc/agent-vault/ca.pem"


def test_broker_env_without_agent_vault_config(tmp_path: Path, test_ca_pem: str) -> None:
    """Broker env applies even when no Agent Vault is configured."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={},
    )

    broker_env = broker_execution_env(
        broker_url="http://broker:8768",
        token="mrb1.token",  # noqa: S106
        ca_pem=test_ca_pem,
        placeholder_env={},
        extra_no_proxy_hosts=[],
    )

    final_env = sandbox_exec.request_execution_env("python", broker_env, runtime_paths)

    assert "HTTPS_PROXY" in final_env
    assert "mrb1.token" in final_env["HTTPS_PROXY"]
    assert BROKER_CA_PEM_ENV not in final_env
    assert "SSL_CERT_FILE" in final_env
