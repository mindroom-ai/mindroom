"""Actionable local recovery for desktop bridge startup failures."""

from __future__ import annotations

import ssl

import aiohttp

from mindroom.desktop.native_protocol import NativeProtocolError
from mindroom.desktop.session import DesktopSessionNotFoundError


def desktop_startup_error(error: Exception) -> NativeProtocolError:
    """Keep transport failures distinct from saved-session repair, including wrapped NIO errors."""
    causes: list[BaseException] = []
    cause: BaseException | None = error
    while cause is not None and cause not in causes:
        causes.append(cause)
        cause = cause.__cause__ or (None if cause.__suppress_context__ else cause.__context__)
    if any(isinstance(item, ssl.SSLCertVerificationError | aiohttp.ClientConnectorCertificateError) for item in causes):
        return NativeProtocolError(
            "tls_certificate_error",
            "The Matrix server's TLS certificate could not be verified.",
            recovery=(
                "Update MindRoom, then retry Start Access. If this continues, check your network's certificate "
                "settings with your administrator. Your saved connection has been kept."
            ),
            retryable=True,
        )
    if any(
        isinstance(item, aiohttp.ClientConnectionError | aiohttp.ClientPayloadError | TimeoutError) for item in causes
    ):
        return NativeProtocolError(
            "connection_failed",
            str(error),
            recovery="Check your network and server availability, then retry Start Access. Your saved connection has been kept.",
            retryable=True,
        )
    if isinstance(error, DesktopSessionNotFoundError):
        return NativeProtocolError(
            "session_missing",
            str(error),
            recovery="Review Connect and repair the saved sign-in before retrying.",
            retryable=True,
        )
    return NativeProtocolError(
        "internal_error",
        str(error) or "Desktop bridge start failed.",
        recovery="Retry Start Access. If this continues, open View Details and copy the diagnostics for support.",
        retryable=True,
    )
