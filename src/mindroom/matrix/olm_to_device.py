"""Pinned Olm-encrypted transport for custom Matrix to-device events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import nio

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nio.crypto import OlmDevice

    from mindroom.matrix.to_device import AuthenticatedToDeviceEvent


class OlmToDeviceError(RuntimeError):
    """One pinned encrypted to-device operation failed closed."""


@dataclass(frozen=True, slots=True)
class PinnedMatrixDevice:
    """Exact Matrix device identity trusted for one encrypted channel."""

    user_id: str
    device_id: str
    ed25519: str

    def __post_init__(self) -> None:
        """Reject incomplete pins before any network or trust-store mutation."""
        if not self.user_id.startswith("@") or ":" not in self.user_id:
            msg = "Pinned Matrix user_id must use @user:server form."
            raise ValueError(msg)
        if not self.device_id.strip():
            msg = "Pinned Matrix device_id must not be empty."
            raise ValueError(msg)
        if not self.ed25519.strip():
            msg = "Pinned Matrix ed25519 fingerprint must not be empty."
            raise ValueError(msg)


async def resolve_pinned_device(
    client: nio.AsyncClient,
    target: PinnedMatrixDevice,
) -> OlmDevice:
    """Resolve and locally verify one exact device, querying fresh keys when needed."""
    olm = client.olm
    if olm is None:
        msg = "Matrix Olm support is unavailable."
        raise OlmToDeviceError(msg)

    device = olm.device_store[target.user_id].get(target.device_id)
    if device is None:
        olm.users_for_key_query.add(target.user_id)
        response = await client.keys_query()
        if isinstance(response, nio.KeysQueryError):
            msg = f"Matrix device-key query failed for {target.user_id}: {response}"
            raise OlmToDeviceError(msg)
        device = olm.device_store[target.user_id].get(target.device_id)

    if device is None:
        msg = f"Pinned Matrix device {target.user_id} {target.device_id} is unknown."
        raise OlmToDeviceError(msg)
    if device.ed25519 != target.ed25519:
        msg = f"Pinned Matrix device fingerprint mismatch for {target.user_id} {target.device_id}."
        raise OlmToDeviceError(msg)
    if device.blacklisted:
        msg = f"Pinned Matrix device {target.user_id} {target.device_id} is blocked."
        raise OlmToDeviceError(msg)
    if not device.verified:
        client.verify_device(device)
    return device


async def send_encrypted_to_device(
    client: nio.AsyncClient,
    target: PinnedMatrixDevice,
    *,
    event_type: str,
    content: Mapping[str, object],
) -> None:
    """Olm-encrypt and send one custom event to an exact pinned device."""
    olm = client.olm
    if olm is None:
        msg = "Matrix Olm support is unavailable."
        raise OlmToDeviceError(msg)

    device = await resolve_pinned_device(client, target)
    session = olm.session_store.get(device.curve25519)
    if session is None:
        missing_devices = olm.get_missing_sessions([target.user_id]).get(target.user_id, [])
        if target.device_id in missing_devices:
            claim_response = await client.keys_claim({target.user_id: [target.device_id]})
            if isinstance(claim_response, nio.KeysClaimError):
                msg = f"Matrix one-time-key claim failed for {target.user_id}: {claim_response}"
                raise OlmToDeviceError(msg)
        session = olm.session_store.get(device.curve25519)
    if session is None:
        msg = f"Could not establish an Olm session with {target.user_id} {target.device_id}."
        raise OlmToDeviceError(msg)

    encrypted_content = olm._olm_encrypt(session, device, event_type, dict(content))
    response = await client.to_device(
        nio.ToDeviceMessage(
            type="m.room.encrypted",
            recipient=target.user_id,
            recipient_device=target.device_id,
            content=encrypted_content,
        ),
    )
    if isinstance(response, nio.ToDeviceError):
        msg = f"Encrypted Matrix delivery failed for {target.user_id} {target.device_id}: {response}"
        raise OlmToDeviceError(msg)


def authenticated_sender_matches(
    client: nio.AsyncClient,
    event: AuthenticatedToDeviceEvent,
    expected: PinnedMatrixDevice,
) -> bool:
    """Return whether an authenticated Olm event matches one exact pinned sender."""
    if event.sender != expected.user_id or event.authenticated_device_id != expected.device_id:
        return False
    olm = client.olm
    if olm is None:
        return False
    device = olm.device_store[event.sender].get(event.authenticated_device_id)
    return device is not None and not device.blacklisted and device.ed25519 == expected.ed25519


__all__ = [
    "OlmToDeviceError",
    "PinnedMatrixDevice",
    "authenticated_sender_matches",
    "resolve_pinned_device",
    "send_encrypted_to_device",
]
