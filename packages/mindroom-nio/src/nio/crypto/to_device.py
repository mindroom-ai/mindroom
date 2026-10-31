"""Shared validation for pinned custom Olm sends."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..event_builders import ToDeviceMessage
from ..exceptions import LocalProtocolError
from ..responses import KeysQueryResponse

if TYPE_CHECKING:
    from .device import OlmDevice
    from .olm_machine import Olm


@dataclass(frozen=True)
class EncryptedToDevice:
    message: ToDeviceMessage
    recipient_ed25519: str
    fingerprint: str

    @classmethod
    def prepare(
        cls, message: ToDeviceMessage, pin: str, user_id: str, device_id: str
    ) -> EncryptedToDevice:
        if (
            not isinstance(message, ToDeviceMessage)
            or any(
                not isinstance(value, str) or not value
                for value in (
                    message.type,
                    message.recipient,
                    message.recipient_device,
                    pin,
                )
            )
            or message.recipient_device == "*"
            or message.type == "m.room.encrypted"
            or not message.recipient.startswith("@")
            or ":" not in message.recipient
            or not isinstance(message.content, dict)
        ):
            raise LocalProtocolError(
                "encrypted to-device send requires one pinned device and clear event"
            )
        try:
            body = json.dumps(
                [
                    user_id,
                    device_id,
                    message.type,
                    message.recipient,
                    message.recipient_device,
                    pin,
                    message.content,
                ],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise LocalProtocolError(
                "encrypted to-device content must be JSON"
            ) from error
        clear = ToDeviceMessage(
            message.type,
            message.recipient,
            message.recipient_device,
            json.loads(body)[-1],
        )
        return cls(clear, pin, hashlib.sha256(body.encode()).hexdigest())

    def resolve(self, olm: Olm, response: object) -> OlmDevice:
        """Require fresh signed identity as well as the current accepted cache."""
        user, device_id = self.message.recipient, self.message.recipient_device
        if not isinstance(response, KeysQueryResponse):
            raise LocalProtocolError("encrypted to-device key query failed")
        if user.partition(":")[2] in response.failures:
            raise LocalProtocolError(
                "encrypted to-device key query failed for recipient"
            )
        payload = response.device_keys.get(user, {}).get(device_id)
        device = olm.device_store[user].get(device_id)
        if not payload or device is None or device.deleted or device.blacklisted:
            raise LocalProtocolError(
                "encrypted to-device recipient is unavailable or blocked"
            )
        keys = payload.get("keys", {})
        if (
            payload.get("user_id") != user
            or payload.get("device_id") != device_id
            or keys.get(f"ed25519:{device_id}") != self.recipient_ed25519
            or device.ed25519 != self.recipient_ed25519
            or keys.get(f"curve25519:{device_id}") != device.curve25519
            or not olm.verify_json(payload, self.recipient_ed25519, user, device_id)
        ):
            raise LocalProtocolError(
                "encrypted to-device recipient identity does not match pin"
            )
        return device

    def encrypt(self, olm: Olm, device: OlmDevice) -> ToDeviceMessage:
        session = olm.session_store.get(device.curve25519)
        if session is None:
            raise LocalProtocolError("encrypted to-device recipient has no Olm session")
        return ToDeviceMessage(
            "m.room.encrypted",
            device.user_id,
            device.id,
            olm._olm_encrypt(session, device, self.message.type, self.message.content),
        )
