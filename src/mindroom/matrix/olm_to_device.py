"""Pinned Olm transport and current application authorization for to-device events."""

from __future__ import annotations

from typing import TYPE_CHECKING

import nio
from aiohttp import ClientError

from mindroom.matrix.device_identity import PinnedMatrixDevice

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nio.crypto import OlmDevice


class OlmToDeviceError(RuntimeError):
    """One pinned encrypted to-device operation failed closed."""


async def resolve_pinned_device(client: nio.AsyncClient, target: PinnedMatrixDevice) -> OlmDevice:
    """Refresh and validate one pinned identity without changing global device trust."""
    olm = client.olm
    if olm is None:
        msg = "Matrix Olm support is unavailable."
        raise OlmToDeviceError(msg)
    try:
        response = await client.keys_query(user_set={target.user_id})
    except (nio.LocalProtocolError, ClientError, TimeoutError) as exc:
        msg = f"Matrix device-key query failed for {target.user_id}: {exc}"
        raise OlmToDeviceError(msg) from exc
    if not isinstance(response, nio.KeysQueryResponse) or target.user_id.partition(":")[2] in response.failures:
        msg = f"Matrix device-key query failed for {target.user_id}: {response}"
        raise OlmToDeviceError(msg)
    payload = response.device_keys.get(target.user_id, {}).get(target.device_id)
    device = olm.device_store[target.user_id].get(target.device_id)
    if not isinstance(payload, dict) or device is None or device.deleted or device.blacklisted:
        msg = f"Pinned Matrix device {target.user_id} {target.device_id} is unavailable or blocked."
        raise OlmToDeviceError(msg)
    keys = payload.get("keys")
    if (
        not isinstance(keys, dict)
        or payload.get("user_id") != target.user_id
        or payload.get("device_id") != target.device_id
        or keys.get(f"ed25519:{target.device_id}") != target.ed25519
        or device.ed25519 != target.ed25519
        or keys.get(f"curve25519:{target.device_id}") != device.curve25519
        or not olm.verify_json(payload, target.ed25519, target.user_id, target.device_id)
    ):
        msg = f"Pinned Matrix device fingerprint mismatch for {target.user_id} {target.device_id}."
        raise OlmToDeviceError(msg)
    return device


async def send_encrypted_to_device(
    client: nio.AsyncClient,
    target: PinnedMatrixDevice,
    *,
    event_type: str,
    content: Mapping[str, object],
) -> None:
    """Let NIO own pinned encryption, crypto persistence and exact HTTP retries."""
    try:
        response = await client.encrypted_to_device(
            nio.ToDeviceMessage(
                type=event_type,
                recipient=target.user_id,
                recipient_device=target.device_id,
                content=dict(content),
            ),
            recipient_ed25519=target.ed25519,
        )
    except (nio.LocalProtocolError, nio.EncryptionError, ClientError, TimeoutError) as exc:
        msg = f"Encrypted Matrix delivery failed for {target.user_id} {target.device_id}: {exc}"
        raise OlmToDeviceError(msg) from exc
    if isinstance(response, nio.ToDeviceError):
        msg = f"Encrypted Matrix delivery failed for {target.user_id} {target.device_id}: {response}"
        raise OlmToDeviceError(msg)


def authenticated_sender_is_current(client: nio.AsyncClient, event: nio.ToDeviceEvent) -> bool:
    """Require captured Olm identity to remain active and unblocked in the current store."""
    if not isinstance(event, nio.AuthenticatedToDeviceEvent) or client.olm is None:
        return False
    identity = event.authenticated_sender
    if event.sender != identity.user_id:
        return False
    device = client.olm.device_store[identity.user_id].get(identity.device_id)
    return (
        device is not None
        and not device.deleted
        and not device.blacklisted
        and device.ed25519 == identity.ed25519
        and device.curve25519 == identity.curve25519
    )


def authenticated_sender_matches(
    client: nio.AsyncClient,
    event: nio.AuthenticatedToDeviceEvent,
    expected: PinnedMatrixDevice,
) -> bool:
    """Require both current authority and the exact pinned historical sender identity."""
    if not authenticated_sender_is_current(client, event):
        return False
    identity = event.authenticated_sender
    return (
        identity.user_id == expected.user_id
        and identity.device_id == expected.device_id
        and identity.ed25519 == expected.ed25519
    )


__all__ = [
    "OlmToDeviceError",
    "PinnedMatrixDevice",
    "authenticated_sender_is_current",
    "authenticated_sender_matches",
    "resolve_pinned_device",
    "send_encrypted_to_device",
]
