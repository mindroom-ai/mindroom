"""Reliable local-client transport for one Desktop pairing claim."""

from __future__ import annotations

import asyncio
import secrets
from typing import TYPE_CHECKING

import nio

from mindroom.desktop.protocol import (
    DESKTOP_PAIRING_ACCEPTED_EVENT_TYPE,
    DESKTOP_PAIRING_CLAIM_EVENT_TYPE,
    DesktopPairingAccepted,
    DesktopPairingClaim,
    DesktopProtocolError,
    desktop_pairing_verification,
    event_content,
)
from mindroom.desktop.session import client_ed25519_fingerprint, prepare_desktop_client
from mindroom.matrix.olm_to_device import (
    OlmToDeviceError,
    authenticated_sender_matches,
    resolve_pinned_device,
    send_encrypted_to_device,
)
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent

if TYPE_CHECKING:
    from mindroom.matrix.device_identity import PinnedMatrixDevice

_PAIRING_ACCEPT_TIMEOUT_SECONDS = 30.0
_PAIRING_RETRY_SYNC_TIMEOUT_MS = 1_000


async def send_desktop_pairing_claim(
    client: nio.AsyncClient,
    controller: PinnedMatrixDevice,
    *,
    code: str,
    timeout_seconds: float = _PAIRING_ACCEPT_TIMEOUT_SECONDS,
) -> str:
    """Retry one claim until its pinned controller authenticates and acknowledges it."""
    verification = desktop_pairing_verification(code, client_ed25519_fingerprint(client))
    accepted = asyncio.Event()

    async def on_to_device_event(event: AuthenticatedToDeviceEvent) -> None:
        if event.type != DESKTOP_PAIRING_ACCEPTED_EVENT_TYPE:
            return
        if not authenticated_sender_matches(client, event, controller):
            return
        try:
            acknowledgement = DesktopPairingAccepted.from_content(event_content(event.source))
        except DesktopProtocolError:
            return
        if secrets.compare_digest(acknowledgement.verification, verification):
            accepted.set()

    client.add_to_device_callback(
        on_to_device_event,  # ty: ignore[invalid-argument-type]  # nio accepts async callbacks at runtime
        AuthenticatedToDeviceEvent,
    )
    await resolve_pinned_device(client, controller)
    await prepare_desktop_client(client)

    try:
        async with asyncio.timeout(timeout_seconds):
            while not accepted.is_set():
                await send_encrypted_to_device(
                    client,
                    controller,
                    event_type=DESKTOP_PAIRING_CLAIM_EVENT_TYPE,
                    content=DesktopPairingClaim(code).to_content(),
                )
                response = await client.sync(
                    timeout=_PAIRING_RETRY_SYNC_TIMEOUT_MS,
                    full_state=False,
                    set_presence="offline",
                )
                if isinstance(response, nio.SyncError):
                    msg = f"Desktop pairing sync failed: {response}"
                    raise OlmToDeviceError(msg)
    except TimeoutError as exc:
        msg = f"Cloud controller did not authenticate the Desktop pairing claim within {timeout_seconds:g} seconds."
        raise OlmToDeviceError(msg) from exc
    return verification


__all__ = ["send_desktop_pairing_claim"]
