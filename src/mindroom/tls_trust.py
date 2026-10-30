"""TLS trust for outbound connections whose client trusts only Python's default certificate store."""

from __future__ import annotations

import ssl
from typing import TYPE_CHECKING

import certifi

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["verifying_ssl_context"]


def verifying_ssl_context(env: Mapping[str, str]) -> ssl.SSLContext:
    """Trust the system store plus certifi's roots, which HTTPS through httpx already uses.

    Some Python builds (for example uv-managed CPython on NixOS) look for CA files the host does not have,
    so the system store alone can be empty and every certificate check fails.
    An explicit SSL_CERT_FILE or SSL_CERT_DIR in ``env`` is the operator's trust choice and is left to OpenSSL unchanged.
    """
    context = ssl.create_default_context()
    if not any(name in env for name in ("SSL_CERT_FILE", "SSL_CERT_DIR")):
        context.load_verify_locations(cafile=certifi.where())
    return context
