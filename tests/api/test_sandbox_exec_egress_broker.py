"""Tests for native broker env precedence over Agent Vault in sandbox_exec."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from mindroom.api import sandbox_exec
from mindroom.constants import resolve_runtime_paths
from mindroom.egress_broker import env as egress_env

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


@pytest.fixture
def test_ca_pem() -> str:
    """Return a valid test CA certificate in PEM format."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Egress Broker CA")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


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
    broker_env = egress_env.broker_execution_env(
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
    assert egress_env.BROKER_CA_PEM_ENV not in final_env

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

    broker_env = egress_env.broker_execution_env(
        broker_url="http://broker:8768",
        token="mrb1.token",  # noqa: S106
        ca_pem=test_ca_pem,
        placeholder_env={},
        extra_no_proxy_hosts=[],
    )

    final_env = sandbox_exec.request_execution_env("python", broker_env, runtime_paths)

    assert "HTTPS_PROXY" in final_env
    assert "mrb1.token" in final_env["HTTPS_PROXY"]
    assert egress_env.BROKER_CA_PEM_ENV not in final_env
    assert "SSL_CERT_FILE" in final_env
