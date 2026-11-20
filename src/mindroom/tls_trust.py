"""TLS trust for outbound connections whose client trusts only Python's default certificate store."""

from __future__ import annotations

import functools
import ssl
from typing import TYPE_CHECKING

import certifi

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["verifying_ssl_context"]


def verifying_ssl_context(env: Mapping[str, str], *, shared: bool = True) -> ssl.SSLContext:
    """Trust the system store plus certifi's roots, which HTTPS through httpx already uses.

    Some Python builds (for example uv-managed CPython on NixOS) look for CA files the host does not have,
    so the system store alone can be empty and every certificate check fails.
    An explicit SSL_CERT_FILE or SSL_CERT_DIR in ``env`` is the operator's trust choice and is left to OpenSSL unchanged.
    Loading every CA takes CPU time, so each trust choice shares one context and the CA store is read once per process.
    Callers must not change a shared context; one that does passes ``shared=False`` for its own.
    """
    operator_trust = any(name in env for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"))
    return (_shared_context if shared else _new_context)(operator_trust=operator_trust)


def _new_context(*, operator_trust: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not operator_trust:
        context.load_verify_locations(cafile=certifi.where())
    return context


_shared_context = functools.cache(_new_context)
