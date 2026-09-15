"""Reliable local-client transport for one Desktop pairing claim."""

from __future__ import annotations

import asyncio
import secrets
from functools import partial
from typing import TYPE_CHECKING

from nio import AuthenticatedToDeviceEvent

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
from mindroom.desktop.transport import DesktopTransport
from mindroom.matrix.olm_to_device import (
    OlmToDeviceError,
    authenticated_sender_matches,
    resolve_pinned_device,
    send_encrypted_to_device,
)

if TYPE_CHECKING:
    import nio

    from mindroom.desktop.session import DesktopOwnedSession
    from mindroom.matrix.device_identity import PinnedMatrixDevice

_PAIRING_ACCEPT_TIMEOUT_SECONDS = 30.0
_PAIRING_RETRY_SECONDS = 1.0


async def send_desktop_pairing_claim(
    owner: DesktopOwnedSession,
    controller: PinnedMatrixDevice,
    *,
    code: str,
    timeout_seconds: float = _PAIRING_ACCEPT_TIMEOUT_SECONDS,
) -> str:
    """Retry one claim until its pinned controller authenticates and acknowledges it."""
    client = owner.client
    verification = desktop_pairing_verification(code, client_ed25519_fingerprint(client))
    received = asyncio.Event()
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
        if secrets.compare_digest(acknowledgement.verification.encode(), verification.encode()):
            received.set()

    client.add_to_device_callback(on_to_device_event, AuthenticatedToDeviceEvent)
    registration = client.to_device_callbacks[-1]
    tasks: set[asyncio.Task[None]] = set()
    try:
        await resolve_pinned_device(client, controller)
        await prepare_desktop_client(client)
        async with asyncio.timeout(timeout_seconds):
            transport = DesktopTransport(
                owner.source,
                reject_commands=True,
                on_batch_acknowledged=partial(_acknowledge_pairing, received, accepted),
            )
            transport_task = asyncio.create_task(transport.run())
            claim_task = asyncio.create_task(_retry_claim(client, controller, code, accepted))
            tasks.update((transport_task, claim_task))
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if transport_task in done:
                await transport_task
                msg = "Desktop pairing transport stopped before acknowledgement."
                raise OlmToDeviceError(msg)
            await claim_task
    except TimeoutError as exc:
        msg = f"Cloud controller did not authenticate the Desktop pairing claim within {timeout_seconds:g} seconds."
        raise OlmToDeviceError(msg) from exc
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        client.to_device_callbacks.remove(registration)
    return verification


def _acknowledge_pairing(received: asyncio.Event, accepted: asyncio.Event) -> None:
    if received.is_set():
        accepted.set()


async def _retry_claim(
    client: nio.AsyncClient,
    controller: PinnedMatrixDevice,
    code: str,
    accepted: asyncio.Event,
) -> None:
    while not accepted.is_set():
        await send_encrypted_to_device(
            client,
            controller,
            event_type=DESKTOP_PAIRING_CLAIM_EVENT_TYPE,
            content=DesktopPairingClaim(code).to_content(),
        )
        try:
            async with asyncio.timeout(_PAIRING_RETRY_SECONDS):
                await accepted.wait()
        except TimeoutError:
            continue


__all__ = ["send_desktop_pairing_claim"]
