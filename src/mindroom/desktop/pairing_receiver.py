"""Matrix transport receiver for requester-owned Desktop pairing claims."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.desktop.pairing import DesktopPairingError, claim_desktop_pairing
from mindroom.desktop.protocol import (
    DESKTOP_PAIRING_CLAIM_EVENT_TYPE,
    DesktopPairingClaim,
    DesktopProtocolError,
    event_content,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DesktopPairingReceiver:
    """Receive claims for one exact configured agent Matrix identity."""

    client: nio.AsyncClient
    agent_name: str
    runtime_paths: RuntimePaths

    async def on_event(self, event: AuthenticatedToDeviceEvent) -> None:
        """Record a claim only from its Matrix-authenticated local device identity."""
        if event.type != DESKTOP_PAIRING_CLAIM_EVENT_TYPE:
            return
        try:
            claim = DesktopPairingClaim.from_content(event_content(event.source))
        except DesktopProtocolError as exc:
            logger.warning("desktop_pairing_claim_malformed", agent=self.agent_name, reason=str(exc))
            return
        olm = self.client.olm
        if olm is None:
            logger.warning("desktop_pairing_claim_rejected", agent=self.agent_name, reason="missing_olm")
            return
        device = olm.device_store[event.sender].get(event.authenticated_device_id)
        if device is None or device.blacklisted:
            logger.warning("desktop_pairing_claim_rejected", agent=self.agent_name, reason="untrusted_device")
            return
        try:
            await asyncio.to_thread(
                claim_desktop_pairing,
                self.runtime_paths,
                token=claim.token,
                agent_name=self.agent_name,
                device_user_id=event.sender,
                device_id=event.authenticated_device_id,
                device_ed25519=device.ed25519,
            )
        except DesktopPairingError as exc:
            logger.warning("desktop_pairing_claim_rejected", agent=self.agent_name, reason=str(exc))
            return
        except sqlite3.Error:
            logger.exception("desktop_pairing_claim_db_error", agent=self.agent_name)
            return
        logger.info("desktop_pairing_claimed", agent=self.agent_name)


def register_desktop_pairing_receiver(
    config: Config,
    *,
    client: nio.AsyncClient,
    agent_name: str,
    runtime_paths: RuntimePaths,
    callback_wrapper: Callable[
        [Callable[[AuthenticatedToDeviceEvent], Awaitable[None]]],
        Callable[..., Awaitable[None]],
    ],
) -> None:
    """Register a receiver only for concrete agents configured with Desktop."""
    if agent_name not in config.agents or "desktop" not in config.resolve_entity(agent_name).available_tools:
        return
    receiver = DesktopPairingReceiver(
        client=client,
        agent_name=agent_name,
        runtime_paths=runtime_paths,
    )
    client.add_to_device_callback(
        callback_wrapper(receiver.on_event),  # ty: ignore[invalid-argument-type]  # nio types reject async wrappers
        AuthenticatedToDeviceEvent,
    )


__all__ = ["DesktopPairingReceiver", "register_desktop_pairing_receiver"]
