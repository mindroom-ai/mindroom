"""Matrix transport receiver for requester-owned Olm Desktop pairing lifecycle events."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nio import AuthenticatedToDeviceEvent

from mindroom.constants import runtime_matrix_homeserver
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.desktop.configuration import DesktopConfigurationStatus, desktop_configuration_state
from mindroom.desktop.credentials import delete_desktop_credentials, save_desktop_credentials
from mindroom.desktop.pairing import (
    DesktopPairingError,
    claim_desktop_pairing,
    complete_desktop_pairing,
    confirm_desktop_pairing,
    create_desktop_pairing,
)
from mindroom.desktop.protocol import (
    DESKTOP_DISCONNECT_CONFIRMED_EVENT_TYPE,
    DESKTOP_DISCONNECT_REQUEST_EVENT_TYPE,
    DESKTOP_PAIRING_ACCEPTED_EVENT_TYPE,
    DESKTOP_PAIRING_CLAIM_EVENT_TYPE,
    DESKTOP_PAIRING_CONFIRM_EVENT_TYPE,
    DESKTOP_PAIRING_CONFIRMED_EVENT_TYPE,
    DESKTOP_SETUP_OFFER_EVENT_TYPE,
    DESKTOP_SETUP_REQUEST_EVENT_TYPE,
    DesktopDisconnectConfirmed,
    DesktopDisconnectRequest,
    DesktopPairingAccepted,
    DesktopPairingClaim,
    DesktopPairingConfirm,
    DesktopPairingConfirmed,
    DesktopProtocolError,
    DesktopSetupOffer,
    DesktopSetupRequest,
    desktop_pairing_verification,
    event_content,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.olm_to_device import (
    OlmToDeviceError,
    PinnedMatrixDevice,
    authenticated_sender_is_current,
    send_encrypted_to_device,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_UNAVAILABLE_ERROR = "Desktop pairing is temporarily unavailable. Please try again."
_HANDLED_EVENT_TYPES = frozenset(
    {
        DESKTOP_PAIRING_CLAIM_EVENT_TYPE,
        DESKTOP_SETUP_REQUEST_EVENT_TYPE,
        DESKTOP_PAIRING_CONFIRM_EVENT_TYPE,
        DESKTOP_DISCONNECT_REQUEST_EVENT_TYPE,
    },
)


@dataclass(frozen=True, slots=True)
class DesktopPairingReceiver:
    """Receive Olm Desktop pairing lifecycle events for one configured agent Matrix identity."""

    client: nio.AsyncClient
    agent_name: str
    runtime_paths: RuntimePaths
    config: Config | None = None
    eligibility_error: Callable[[], str | None] | None = None

    def _check_eligibility(self) -> str | None:
        if self.eligibility_error is not None:
            return self.eligibility_error()
        if self.config is None:
            return None
        if self.agent_name not in self.config.agents:
            return f"Agent '{self.agent_name}' is not configured."
        if "desktop" not in self.config.resolve_agent(self.agent_name).tools.tool_names:
            return f"Agent '{self.agent_name}' does not declare the Desktop tool."
        return None

    async def _send_reply(
        self,
        target: PinnedMatrixDevice,
        *,
        event_type: str,
        content: dict[str, object],
        failure_event: str,
    ) -> bool:
        try:
            await send_encrypted_to_device(self.client, target, event_type=event_type, content=content)
        except OlmToDeviceError:
            logger.exception(failure_event, agent=self.agent_name)
            return False
        return True

    async def _reply_setup_error(self, target: PinnedMatrixDevice, nonce: str, error: str) -> bool:
        offer = DesktopSetupOffer(nonce=nonce, agent_name=self.agent_name, eligible=False, error=error)
        return await self._send_reply(
            target,
            event_type=DESKTOP_SETUP_OFFER_EVENT_TYPE,
            content=offer.to_content(),
            failure_event="desktop_setup_offer_delivery_failed",
        )

    async def _reply_pairing_confirmed(
        self,
        target: PinnedMatrixDevice,
        nonce: str,
        *,
        ok: bool,
        error: str | None = None,
    ) -> bool:
        reply = DesktopPairingConfirmed(nonce=nonce, agent_name=self.agent_name, ok=ok, error=error)
        return await self._send_reply(
            target,
            event_type=DESKTOP_PAIRING_CONFIRMED_EVENT_TYPE,
            content=reply.to_content(),
            failure_event="desktop_pairing_confirmed_delivery_failed",
        )

    async def _reply_disconnect_confirmed(
        self,
        target: PinnedMatrixDevice,
        nonce: str,
        *,
        ok: bool,
        error: str | None = None,
    ) -> bool:
        reply = DesktopDisconnectConfirmed(nonce=nonce, agent_name=self.agent_name, ok=ok, error=error)
        return await self._send_reply(
            target,
            event_type=DESKTOP_DISCONNECT_CONFIRMED_EVENT_TYPE,
            content=reply.to_content(),
            failure_event="desktop_disconnect_confirmed_delivery_failed",
        )

    async def on_event(self, event: AuthenticatedToDeviceEvent) -> None:
        """Handle authenticated Olm Desktop pairing setup, claim, confirm, and disconnect events."""
        if event.type not in _HANDLED_EVENT_TYPES:
            return
        if not authenticated_sender_is_current(self.client, event):
            logger.warning(
                "desktop_pairing_event_rejected",
                agent=self.agent_name,
                event_type=event.type,
                reason="untrusted_device",
            )
            return
        identity = event.authenticated_sender
        target = PinnedMatrixDevice(
            user_id=event.sender,
            device_id=identity.device_id,
            ed25519=identity.ed25519,
        )
        if event.type == DESKTOP_PAIRING_CLAIM_EVENT_TYPE:
            await self._handle_pairing_claim(event, target)
        elif event.type == DESKTOP_SETUP_REQUEST_EVENT_TYPE:
            await self._handle_setup_request(event, target)
        elif event.type == DESKTOP_PAIRING_CONFIRM_EVENT_TYPE:
            await self._handle_pairing_confirm(event, target)
        elif event.type == DESKTOP_DISCONNECT_REQUEST_EVENT_TYPE:
            await self._handle_disconnect_request(event, target)

    async def _handle_pairing_claim(self, event: AuthenticatedToDeviceEvent, target: PinnedMatrixDevice) -> None:
        try:
            claim = DesktopPairingClaim.from_content(event_content(event.source))
        except DesktopProtocolError as exc:
            logger.warning("desktop_pairing_claim_malformed", agent=self.agent_name, reason=str(exc))
            return
        try:
            await asyncio.to_thread(
                claim_desktop_pairing,
                self.runtime_paths,
                token=claim.token,
                agent_name=self.agent_name,
                device_user_id=target.user_id,
                device_id=target.device_id,
                device_ed25519=target.ed25519,
            )
        except DesktopPairingError as exc:
            logger.warning("desktop_pairing_claim_rejected", agent=self.agent_name, reason=str(exc))
            return
        except sqlite3.Error:
            logger.exception("desktop_pairing_claim_db_error", agent=self.agent_name)
            return
        verification = desktop_pairing_verification(claim.token, target.ed25519)
        if await self._send_reply(
            target,
            event_type=DESKTOP_PAIRING_ACCEPTED_EVENT_TYPE,
            content=DesktopPairingAccepted(verification).to_content(),
            failure_event="desktop_pairing_ack_delivery_failed",
        ):
            logger.info("desktop_pairing_claimed", agent=self.agent_name)

    async def _handle_setup_request(self, event: AuthenticatedToDeviceEvent, target: PinnedMatrixDevice) -> None:
        try:
            request = DesktopSetupRequest.from_content(event_content(event.source))
        except DesktopProtocolError as exc:
            logger.warning("desktop_setup_request_malformed", agent=self.agent_name, reason=str(exc))
            return
        if request.agent_name != self.agent_name:
            await self._reply_setup_error(
                target,
                request.nonce,
                f"Desktop setup request targets agent '{request.agent_name}', not '{self.agent_name}'.",
            )
            return
        if (error := self._check_eligibility()) is not None:
            await self._reply_setup_error(target, request.nonce, error)
            return
        try:
            pairing = await asyncio.to_thread(
                create_desktop_pairing,
                self.runtime_paths,
                requester_id=target.user_id,
                agent_name=self.agent_name,
                initiator_device_id=target.device_id,
                initiator_ed25519=target.ed25519,
            )
        except DesktopPairingError as exc:
            logger.warning("desktop_setup_request_rejected", agent=self.agent_name, reason=str(exc))
            await self._reply_setup_error(target, request.nonce, str(exc))
            return
        except sqlite3.Error:
            logger.exception("desktop_setup_request_db_error", agent=self.agent_name)
            await self._reply_setup_error(target, request.nonce, _UNAVAILABLE_ERROR)
            return

        homeserver = self.runtime_paths.env_value("MINDROOM_DESKTOP_MATRIX_HOMESERVER") or runtime_matrix_homeserver(
            self.runtime_paths,
        )
        offer = DesktopSetupOffer(
            nonce=request.nonce,
            agent_name=self.agent_name,
            eligible=True,
            homeserver=homeserver,
            user_id=target.user_id,
            code=pairing.token,
            cloudflare_access=self.runtime_paths.env_flag("MINDROOM_DESKTOP_CLOUDFLARE_ACCESS"),
        )
        if await self._send_reply(
            target,
            event_type=DESKTOP_SETUP_OFFER_EVENT_TYPE,
            content=offer.to_content(),
            failure_event="desktop_setup_offer_delivery_failed",
        ):
            logger.info("desktop_setup_offer_sent", agent=self.agent_name)

    async def _handle_pairing_confirm(self, event: AuthenticatedToDeviceEvent, target: PinnedMatrixDevice) -> None:
        try:
            confirm = DesktopPairingConfirm.from_content(event_content(event.source))
        except DesktopProtocolError as exc:
            logger.warning("desktop_pairing_confirm_malformed", agent=self.agent_name, reason=str(exc))
            return
        if (error := self._check_eligibility()) is not None:
            await self._reply_pairing_confirmed(target, confirm.nonce, ok=False, error=error)
            return
        try:
            pending = await asyncio.to_thread(
                confirm_desktop_pairing,
                self.runtime_paths,
                token=confirm.token,
                requester_id=target.user_id,
                agent_name=self.agent_name,
                verification=confirm.verification,
                initiator_device_id=target.device_id,
                initiator_ed25519=target.ed25519,
                expected_device_id=confirm.device_id,
                expected_device_ed25519=confirm.device_ed25519,
            )
        except DesktopPairingError as exc:
            logger.warning("desktop_pairing_confirm_rejected", agent=self.agent_name, reason=str(exc))
            await self._reply_pairing_confirmed(target, confirm.nonce, ok=False, error=str(exc))
            return
        except sqlite3.Error:
            logger.exception("desktop_pairing_confirm_db_error", agent=self.agent_name)
            await self._reply_pairing_confirmed(target, confirm.nonce, ok=False, error=_UNAVAILABLE_ERROR)
            return

        assert pending.device_user_id is not None
        assert pending.device_id is not None
        assert pending.device_ed25519 is not None
        credentials: dict[str, object] = {
            "device_user_id": pending.device_user_id,
            "device_id": pending.device_id,
            "device_ed25519": pending.device_ed25519,
        }
        state = desktop_configuration_state(credentials)
        if state.status is not DesktopConfigurationStatus.READY:
            await self._reply_pairing_confirmed(
                target,
                confirm.nonce,
                ok=False,
                error=state.error or "Claimed Desktop device identity is invalid.",
            )
            return

        def _save_and_complete() -> None:
            save_desktop_credentials(
                get_runtime_credentials_manager(self.runtime_paths),
                credentials,
                requester_id=target.user_id,
                agent_name=self.agent_name,
            )
            complete_desktop_pairing(self.runtime_paths, token=confirm.token)

        try:
            await asyncio.to_thread(_save_and_complete)
        except (OSError, ValueError, sqlite3.Error):
            logger.exception("desktop_pairing_confirm_save_error", agent=self.agent_name)
            await self._reply_pairing_confirmed(target, confirm.nonce, ok=False, error=_UNAVAILABLE_ERROR)
            return
        if await self._reply_pairing_confirmed(target, confirm.nonce, ok=True):
            logger.info("desktop_pairing_confirmed", agent=self.agent_name)

    async def _handle_disconnect_request(self, event: AuthenticatedToDeviceEvent, target: PinnedMatrixDevice) -> None:
        try:
            request = DesktopDisconnectRequest.from_content(event_content(event.source))
        except DesktopProtocolError as exc:
            logger.warning("desktop_disconnect_request_malformed", agent=self.agent_name, reason=str(exc))
            return
        if request.agent_name != self.agent_name:
            await self._reply_disconnect_confirmed(
                target,
                request.nonce,
                ok=False,
                error=f"Desktop disconnect request targets agent '{request.agent_name}', not '{self.agent_name}'.",
            )
            return
        if (error := self._check_eligibility()) is not None:
            await self._reply_disconnect_confirmed(target, request.nonce, ok=False, error=error)
            return
        try:
            await asyncio.to_thread(
                delete_desktop_credentials,
                get_runtime_credentials_manager(self.runtime_paths),
                requester_id=target.user_id,
                agent_name=self.agent_name,
            )
        except (OSError, ValueError) as exc:
            logger.exception("desktop_disconnect_error", agent=self.agent_name)
            await self._reply_disconnect_confirmed(target, request.nonce, ok=False, error=str(exc))
            return
        if await self._reply_disconnect_confirmed(target, request.nonce, ok=True):
            logger.info("desktop_disconnected", agent=self.agent_name)


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
    if agent_name not in config.agents or "desktop" not in config.resolve_agent(agent_name).tools.tool_names:
        return
    receiver = DesktopPairingReceiver(
        client=client,
        agent_name=agent_name,
        runtime_paths=runtime_paths,
        config=config,
    )
    client.add_to_device_callback(
        callback_wrapper(receiver.on_event),  # ty: ignore[invalid-argument-type]  # nio types reject async wrappers
        AuthenticatedToDeviceEvent,
    )


__all__ = ["DesktopPairingReceiver", "register_desktop_pairing_receiver"]
