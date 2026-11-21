"""Tests for the shared verifying TLS context."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom import tls_trust

if TYPE_CHECKING:
    import pytest


def test_verifying_context_is_built_once_per_trust_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loading every CA is slow, so each trust choice loads them once; a new context is still available on request."""
    loaded: list[str] = []
    real_where = tls_trust.certifi.where
    monkeypatch.setattr(tls_trust.certifi, "where", lambda: loaded.append("certifi") or real_where())

    shared = tls_trust.verifying_ssl_context({})
    operator = tls_trust.verifying_ssl_context({"SSL_CERT_DIR": "/etc/ssl/certs"})

    assert tls_trust.verifying_ssl_context({"PATH": "/bin"}) is shared
    assert tls_trust.verifying_ssl_context({"SSL_CERT_FILE": "/etc/ssl/cert.pem"}) is operator
    assert operator is not shared
    assert loaded == ["certifi"]
    assert tls_trust.verifying_ssl_context({}, shared=False) is not shared
    assert loaded == ["certifi", "certifi"]
