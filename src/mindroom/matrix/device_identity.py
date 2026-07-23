"""Leaf identity value objects for pinned Matrix devices."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PinnedMatrixDevice:
    """Exact Matrix device identity trusted for one encrypted channel."""

    user_id: str
    device_id: str
    ed25519: str

    def __post_init__(self) -> None:
        """Reject incomplete pins before any network or trust-store mutation."""
        localpart, separator, server_name = self.user_id.removeprefix("@").partition(":")
        if not self.user_id.startswith("@") or not localpart or not separator or not server_name:
            msg = "Pinned Matrix user_id must use @user:server form."
            raise ValueError(msg)
        if not self.device_id.strip():
            msg = "Pinned Matrix device_id must not be empty."
            raise ValueError(msg)
        if not self.ed25519.strip():
            msg = "Pinned Matrix ed25519 fingerprint must not be empty."
            raise ValueError(msg)


__all__ = ["PinnedMatrixDevice"]
