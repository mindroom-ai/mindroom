"""Tests for broker CA and trust bundle."""

from __future__ import annotations

import socket
import ssl
import threading
from pathlib import Path  # noqa: TC003 - Required at runtime for test fixtures

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from mindroom.egress_broker.ca import BrokerCA, materialize_ca_bundle


@pytest.fixture
def ca_dir(tmp_path: Path) -> Path:
    """Return the directory the CA under test is stored in (not yet created)."""
    return tmp_path / "ca"


@pytest.fixture
def ca(ca_dir: Path) -> BrokerCA:
    """Return a CA created, unencrypted, in `ca_dir`."""
    return BrokerCA.load_or_create(ca_dir, key_password=None)


def test_create_then_load_returns_same_ca(ca_dir: Path) -> None:
    """load_or_create returns same CA across calls."""
    ca1 = BrokerCA.load_or_create(ca_dir, key_password=None)
    ca2 = BrokerCA.load_or_create(ca_dir, key_password=None)
    assert ca1.cert_pem == ca2.cert_pem


def test_key_file_mode_and_encryption(ca_dir: Path) -> None:
    """Key file has mode 0600 and is encrypted when password is provided."""
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


@pytest.mark.usefixtures("ca")
def test_ca_key_that_is_not_ecdsa_is_refused(ca_dir: Path) -> None:
    """An existing CA key of another type raises TypeError instead of being used."""
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (ca_dir / "ca.key").write_bytes(
        rsa_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )

    with pytest.raises(TypeError, match="CA key is not an ECDSA key"):
        BrokerCA.load_or_create(ca_dir, key_password=None)


def _client_context(ca: BrokerCA, *, strict: bool = False, alpn: list[str] | None = None) -> ssl.SSLContext:
    """Return a client context trusting `ca`; it checks the certificate chain but not the host name."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False  # These tests are about certificate trust, not host name validation.
    context.verify_mode = ssl.CERT_REQUIRED
    if strict:
        # Strict X.509 verification (required in Python 3.13+).
        context.verify_flags = ssl.VERIFY_X509_STRICT
    context.load_verify_locations(cadata=ca.cert_pem)
    if alpn:
        context.set_alpn_protocols(alpn)
    return context


def _handshake(
    server_ctx: ssl.SSLContext,
    client_ctx: ssl.SSLContext,
    *,
    server_hostname: str | None,
) -> str | None:
    """Perform a TLS handshake over a socketpair and return the ALPN protocol the client selected."""
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
                server_hostname=server_hostname,
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
    return selected_protocol


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
def test_leaf_verifies_against_ca_for_dns_and_ip(ca: BrokerCA, *, strict: bool) -> None:
    """Leaf certificates verify against the CA for DNS names and IP addresses, also under strict X.509 checks."""
    client_ctx = _client_context(ca, strict=strict)

    # A DNS name, then an IP address (no hostname validation for IPs).
    _handshake(ca.server_context("api.github.com"), client_ctx, server_hostname="api.github.com")
    _handshake(ca.server_context("127.0.0.1"), client_ctx, server_hostname=None)


def test_server_context_is_cached(ca: BrokerCA) -> None:
    """server_context returns cached context for same host."""
    assert ca.server_context("a.test") is ca.server_context("a.test")


def test_alpn_is_http11_only(ca: BrokerCA) -> None:
    """Server context negotiates http/1.1 only, even when the client offers h2 first."""
    selected_protocol = _handshake(
        ca.server_context("test.example.com"),
        _client_context(ca, alpn=["h2", "http/1.1"]),
        server_hostname="test.example.com",
    )

    assert selected_protocol == "http/1.1"


def test_materialize_bundle_contains_system_roots_and_broker_ca(tmp_path: Path, ca: BrokerCA) -> None:
    """materialize_bundle creates combined bundle with system roots and broker CA."""
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


@pytest.mark.parametrize("which", ["combined", "broker_only"])
def test_materialize_bundle_replaces_tampered_file(tmp_path: Path, ca: BrokerCA, which: str) -> None:
    """A pre-existing bundle file with other content is rewritten instead of trusted."""
    bundle_dir = tmp_path / "bundles"
    combined, broker_only = materialize_ca_bundle(ca.cert_pem, bundle_dir)
    expected = {"combined": combined.read_text(), "broker_only": broker_only.read_text()}
    tampered = combined if which == "combined" else broker_only
    tampered.write_text("-----BEGIN CERTIFICATE-----\nplanted\n-----END CERTIFICATE-----\n")

    combined2, broker_only2 = materialize_ca_bundle(ca.cert_pem, bundle_dir)

    assert (combined2, broker_only2) == (combined, broker_only)
    assert combined.read_text() == expected["combined"]
    assert broker_only.read_text() == expected["broker_only"]
    assert sorted(path.name for path in bundle_dir.iterdir()) == sorted([combined.name, broker_only.name])


def test_materialize_bundle_replaces_symlink(tmp_path: Path, ca: BrokerCA) -> None:
    """A symlink planted at a bundle path is replaced by a regular file, never followed."""
    bundle_dir = tmp_path / "bundles"
    combined, broker_only = materialize_ca_bundle(ca.cert_pem, bundle_dir)
    planted = tmp_path / "planted.pem"
    planted.write_text(broker_only.read_text())
    broker_only.unlink()
    broker_only.symlink_to(planted)

    materialize_ca_bundle(ca.cert_pem, bundle_dir)

    assert not broker_only.is_symlink()
    assert broker_only.read_text() == ca.cert_pem
    assert combined.read_text().endswith(ca.cert_pem)
