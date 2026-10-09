"""Tests for broker CA and trust bundle."""

from __future__ import annotations

import socket
import ssl
import threading
from pathlib import Path  # noqa: TC003 - Required at runtime for test fixtures

import pytest

from mindroom.egress_broker.ca import BrokerCA, materialize_ca_bundle


def test_create_then_load_returns_same_ca(tmp_path: Path) -> None:
    """load_or_create returns same CA across calls."""
    ca_dir = tmp_path / "ca"
    ca1 = BrokerCA.load_or_create(ca_dir, key_password=None)
    ca2 = BrokerCA.load_or_create(ca_dir, key_password=None)
    assert ca1.fingerprint == ca2.fingerprint
    assert ca1.cert_pem == ca2.cert_pem


def test_key_file_mode_and_encryption(tmp_path: Path) -> None:
    """Key file has mode 0600 and is encrypted when password is provided."""
    ca_dir = tmp_path / "ca"
    password = b"test-password"

    BrokerCA.load_or_create(ca_dir, key_password=password)
    key_path = ca_dir / "ca.key"

    # Check mode
    assert key_path.stat().st_mode & 0o777 == 0o600

    # Check encryption in PEM header
    key_pem = key_path.read_text()
    assert "ENCRYPTED PRIVATE KEY" in key_pem

    # Wrong password should raise ValueError from cryptography
    wrong_password = b"wrong"
    with pytest.raises(ValueError, match=r"Incorrect password|Could not deserialize|Bad decrypt"):
        BrokerCA.load_or_create(ca_dir, key_password=wrong_password)


def test_leaf_verifies_against_ca_for_dns_and_ip(tmp_path: Path) -> None:
    """Leaf certificates verify against CA for DNS names and IP addresses."""
    ca_dir = tmp_path / "ca"
    ca = BrokerCA.load_or_create(ca_dir, key_password=None)

    # Test DNS name
    dns_host = "api.github.com"
    server_ctx = ca.server_context(dns_host)

    # Create client context trusting the CA
    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.check_hostname = False  # We're testing cert trust, not hostname validation
    client_ctx.verify_mode = ssl.CERT_REQUIRED
    client_ctx.load_verify_locations(cadata=ca.cert_pem)

    # Test handshake with DNS name
    _test_handshake(server_ctx, client_ctx, server_hostname=dns_host)

    # Test IP address
    ip_host = "127.0.0.1"
    server_ctx_ip = ca.server_context(ip_host)

    # Test handshake with IP (no hostname validation for IPs)
    _test_handshake(server_ctx_ip, client_ctx, server_hostname=None)


def _test_handshake(
    server_ctx: ssl.SSLContext,
    client_ctx: ssl.SSLContext,
    *,
    server_hostname: str | None,
) -> None:
    """Perform TLS handshake over socketpair."""
    server_sock, client_sock = socket.socketpair()
    server_error = None
    client_error = None

    def server_side() -> None:
        nonlocal server_error
        try:
            server_ssl = server_ctx.wrap_socket(server_sock, server_side=True)
            server_ssl.close()
        except Exception as e:
            server_error = e

    def client_side() -> None:
        nonlocal client_error
        try:
            client_ssl = client_ctx.wrap_socket(
                client_sock,
                server_side=False,
                server_hostname=server_hostname,
            )
            client_ssl.close()
        except Exception as e:
            client_error = e

    server_thread = threading.Thread(target=server_side)
    client_thread = threading.Thread(target=client_side)

    server_thread.start()
    client_thread.start()

    server_thread.join(timeout=5)
    client_thread.join(timeout=5)

    server_sock.close()
    client_sock.close()

    if server_error:
        raise server_error
    if client_error:
        raise client_error


def test_server_context_is_cached(tmp_path: Path) -> None:
    """server_context returns cached context for same host."""
    ca_dir = tmp_path / "ca"
    ca = BrokerCA.load_or_create(ca_dir, key_password=None)
    assert ca.server_context("a.test") is ca.server_context("a.test")


def test_alpn_is_http11_only(tmp_path: Path) -> None:
    """Server context negotiates http/1.1 only."""
    ca_dir = tmp_path / "ca"
    ca = BrokerCA.load_or_create(ca_dir, key_password=None)
    server_ctx = ca.server_context("test.example.com")

    # Create client that offers h2 and http/1.1
    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.check_hostname = False
    client_ctx.verify_mode = ssl.CERT_REQUIRED
    client_ctx.load_verify_locations(cadata=ca.cert_pem)
    client_ctx.set_alpn_protocols(["h2", "http/1.1"])

    # Perform handshake
    server_sock, client_sock = socket.socketpair()
    server_error = None
    client_error = None
    selected_protocol = None

    def server_side() -> None:
        nonlocal server_error
        try:
            server_ssl = server_ctx.wrap_socket(server_sock, server_side=True)
            server_ssl.close()
        except Exception as e:
            server_error = e

    def client_side() -> None:
        nonlocal client_error, selected_protocol
        try:
            client_ssl = client_ctx.wrap_socket(
                client_sock,
                server_side=False,
                server_hostname="test.example.com",
            )
            selected_protocol = client_ssl.selected_alpn_protocol()
            client_ssl.close()
        except Exception as e:
            client_error = e

    server_thread = threading.Thread(target=server_side)
    client_thread = threading.Thread(target=client_side)

    server_thread.start()
    client_thread.start()

    server_thread.join(timeout=5)
    client_thread.join(timeout=5)

    server_sock.close()
    client_sock.close()

    if server_error:
        raise server_error
    if client_error:
        raise client_error

    # Check negotiated protocol
    assert selected_protocol == "http/1.1"


def test_materialize_bundle_contains_system_roots_and_broker_ca(tmp_path: Path) -> None:
    """materialize_bundle creates combined bundle with system roots and broker CA."""
    ca_dir = tmp_path / "ca"
    ca = BrokerCA.load_or_create(ca_dir, key_password=None)

    bundle_dir = tmp_path / "bundles"
    bundle_dir.mkdir()

    combined, broker_only = materialize_ca_bundle(ca.cert_pem, bundle_dir)

    # Check that files exist
    assert combined.exists()
    assert broker_only.exists()

    # Check combined bundle ends with broker CA
    combined_text = combined.read_text()
    assert combined_text.endswith(ca.cert_pem)

    # Check combined bundle starts with system roots
    # We'll check it contains at least one cert before the broker CA
    assert combined_text.count("-----BEGIN CERTIFICATE-----") >= 2

    # Check broker-only bundle is just the CA
    assert broker_only.read_text() == ca.cert_pem

    # Second call returns same paths
    combined2, broker_only2 = materialize_ca_bundle(ca.cert_pem, bundle_dir)
    assert combined == combined2
    assert broker_only == broker_only2
