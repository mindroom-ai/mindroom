# Copyright © 2019 Damir Jelić <poljar@termina.org.uk>
#
# Permission to use, copy, modify, and/or distribute this software for
# any purpose with or without fee is hereby granted, provided that the
# above copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER
# RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF
# CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN
# CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from hashlib import sha256
from uuid import uuid4

import vodozemac

from ..api import Api
from ..event_builders import ToDeviceMessage
from ..events import KeyVerificationEvent, KeyVerificationStart
from ..exceptions import LocalProtocolError
from .device import OlmDevice


class SasState(Enum):
    """Short Authentication String enum.

    This enum tracks the current state of our verification process.
    """

    created = 0
    started = 1
    accepted = 2
    key_received = 3
    mac_received = 4
    canceled = 5


class Sas:
    """Matrix Short Authentication String class.

    This class implements a state machine to handle device verification using
    short authentication strings.

    Attributes:
        we_started_it (bool): Is true if the verification process was started
            by us, otherwise false.
        sas_accepted (bool): Is true if we accepted that the short
            authentication string matches on both devices.
        verified_devices(List[str]): The list of device ids that were verified
            during the verification process.

    Args:
        own_user (str): The user id of our own user.
        own_device (str): The device id of our own user.
        own_fp_key (str): The fingerprint key of our own device that will
            be verified by the other client.
        other_olm_device (OlmDevice): The OlmDevice which we would like to
            verify.
        transaction_id (str, optional): A string that will uniquely identify
            this verification process. A random and unique string will be
            generated if one isn't provided.
        short_auth_string (List[str], optional): A list of valid short
            authentication methods that the client would like to allow for this
            authentication session. By default the 'emoji' and 'decimal'
            methods are allowed.

    """

    _sas_method_v1 = "m.sas.v1"
    _key_agreement_v1 = "curve25519"
    _key_agreement_v2 = "curve25519-hkdf-sha256"
    _key_agreeemnt_protocols = [_key_agreement_v1, _key_agreement_v2]
    _hash_v1 = "sha256"
    _mac_normal = "hkdf-hmac-sha256"
    _mac_v1 = [_mac_normal]
    _strings_v1 = ["emoji", "decimal"]

    _user_cancel_error = ("m.user", "Canceled by user")
    _timeout_error = ("m.timeout", "Timed out")
    _txid_error = ("m.unknown_transaction", "Unknown transaction")
    _unknown_method_error = ("m.unknown_method", "Unknown method")
    _unexpected_message_error = ("m.unexpected_message", "Unexpected message")
    _key_mismatch_error = ("m.key_mismatch", "Key mismatch")
    _user_mismatch_error = ("m.user_error", "User mismatch")
    _invalid_message_error = ("m.invalid_message", "Invalid message")
    _commitment_mismatch_error = (
        "m.mismatched_commitment",
        "Mismatched commitment",
    )
    _sas_mismatch_error = (
        "m.mismatched_sas",
        "Mismatched short authentication string",
    )

    _max_age = timedelta(minutes=5)
    _max_event_timeout = timedelta(minutes=1)

    emoji = [
        ("🐶", "Dog"),
        ("🐱", "Cat"),
        ("🦁", "Lion"),
        ("🐎", "Horse"),
        ("🦄", "Unicorn"),
        ("🐷", "Pig"),
        ("🐘", "Elephant"),
        ("🐰", "Rabbit"),
        ("🐼", "Panda"),
        ("🐓", "Rooster"),
        ("🐧", "Penguin"),
        ("🐢", "Turtle"),
        ("🐟", "Fish"),
        ("🐙", "Octopus"),
        ("🦋", "Butterfly"),
        ("🌷", "Flower"),
        ("🌳", "Tree"),
        ("🌵", "Cactus"),
        ("🍄", "Mushroom"),
        ("🌏", "Globe"),
        ("🌙", "Moon"),
        ("☁️", "Cloud"),
        ("🔥", "Fire"),
        ("🍌", "Banana"),
        ("🍎", "Apple"),
        ("🍓", "Strawberry"),
        ("🌽", "Corn"),
        ("🍕", "Pizza"),
        ("🎂", "Cake"),
        ("❤️", "Heart"),
        ("😀", "Smiley"),
        ("🤖", "Robot"),
        ("🎩", "Hat"),
        ("👓", "Glasses"),
        ("🔧", "Wrench"),
        ("🎅", "Santa"),
        ("👍", "Thumbs up"),
        ("☂️", "Umbrella"),
        ("⌛", "Hourglass"),
        ("⏰", "Clock"),
        ("🎁", "Gift"),
        ("💡", "Light Bulb"),
        ("📕", "Book"),
        ("✏️", "Pencil"),
        ("📎", "Paperclip"),
        ("✂️", "Scissors"),
        ("🔒", "Lock"),
        ("🔑", "Key"),
        ("🔨", "Hammer"),
        ("☎️", "Telephone"),
        ("🏁", "Flag"),
        ("🚂", "Train"),
        ("🚲", "Bicycle"),
        ("✈️", "Airplane"),
        ("🚀", "Rocket"),
        ("🏆", "Trophy"),
        ("⚽", "Ball"),
        ("🎸", "Guitar"),
        ("🎺", "Trumpet"),
        ("🔔", "Bell"),
        ("⚓", "Anchor"),
        ("🎧", "Headphones"),
        ("📁", "Folder"),
        ("📌", "Pin"),
    ]

    def __init__(
        self,
        own_user: str,
        own_device: str,
        own_fp_key: str,
        other_olm_device: OlmDevice,
        transaction_id: str | None = None,
        short_auth_string: list[str] | None = None,
        mac_methods: list[str] | None = None,
    ):
        self._sas = vodozemac.Sas()

        self.own_user = own_user
        self.own_device = own_device
        self.own_fp_key = own_fp_key

        self.other_olm_device = other_olm_device

        self.transaction_id = transaction_id or str(uuid4())

        self.short_auth_string = short_auth_string or ["emoji", "decimal"]
        self.mac_methods = mac_methods or Sas._mac_v1
        self.chosen_mac_method = ""
        self.key_agreement_protocols = Sas._key_agreeemnt_protocols
        self.chosen_key_agreement: str | None = None
        self.state = SasState.created
        self.we_started_it = True
        self.sas_accepted = False
        self.commitment: str | None = None
        self.cancel_reason = ""
        self.cancel_code = ""

        self.established_sas: vodozemac.EstablishedSas | None = None

        self.verified_devices: list[str] = []

        self.creation_time = datetime.now()
        self._last_event_time = self.creation_time
        super().__init__()

    @classmethod
    def from_key_verification_start(
        cls,
        own_user: str,
        own_device: str,
        own_fp_key: str,
        other_olm_device: OlmDevice,
        event: KeyVerificationStart,
    ) -> Sas:
        """Create a SAS object from a KeyVerificationStart event.

        Args:
            own_user (str): The user id of our own user.
            own_device (str): The device id of our own user.
            own_fp_key (str): The fingerprint key of our own device that will
                be verified by the other client.
            other_olm_device (OlmDevice): The Olm device of the other user that
                should be verified.
            event (KeyVerificationStart): The event that we received from the
                other device to start the key verification process.

        """
        obj = cls(
            own_user,
            own_device,
            own_fp_key,
            other_olm_device,
            event.transaction_id,
            event.short_authentication_string,
            event.message_authentication_codes,
        )
        obj.we_started_it = False
        obj.state = SasState.started

        string_content = Api.to_canonical_json(event.source["content"])
        obj.commitment = sha256(
            obj.pubkey.encode() + string_content.encode()
        ).hexdigest()
        obj.key_agreement_protocols = event.key_agreement_protocols

        if (
            Sas._sas_method_v1 != event.method
            or (
                Sas._key_agreement_v1 not in event.key_agreement_protocols
                and Sas._key_agreement_v2 not in event.key_agreement_protocols
            )
            or Sas._hash_v1 not in event.hashes
            or (Sas._mac_normal not in event.message_authentication_codes)
            or (
                "emoji" not in event.short_authentication_string
                and "decimal" not in event.short_authentication_string
            )
        ):
            obj.state = SasState.canceled
            obj.cancel_code, obj.cancel_reason = obj._unknown_method_error

        return obj

    @property
    def pubkey(self) -> str:
        """Get the public key that can be used to establish a shared secret."""
        return self._sas.public_key.to_base64()

    @property
    def canceled(self) -> bool:
        """Is the verification request canceled."""
        return self.state == SasState.canceled

    @property
    def timed_out(self) -> bool:
        """Did the verification process time out."""
        if self.verified or self.canceled:
            return False

        now = datetime.now()
        if (
            now - self.creation_time >= self._max_age
            or now - self._last_event_time >= self._max_event_timeout
        ):
            self.state = SasState.canceled
            self.cancel_code, self.cancel_reason = self._timeout_error
            return True
        return False

    @property
    def verified(self) -> bool:
        """Is the device verified and the request done."""
        return self.state == SasState.mac_received and self.sas_accepted

    def establish_sas(self, their_public_key: str) -> None:
        """Prepare an EstablishedSas for byte generation."""
        self.established_sas = self.diffie_hellman(their_public_key)
        self.their_sas_key = their_public_key

    def diffie_hellman(self, their_public_key: str) -> vodozemac.EstablishedSas:
        """Establishes a SAS secret by performing a DH handshake with another public key."""
        return self._sas.diffie_hellman(
            vodozemac.Curve25519PublicKey.from_base64(their_public_key)
        )

    def cancel(self):
        """Cancel the authentication process."""
        self.state = SasState.canceled
        self.cancel_code, self.cancel_reason = self._user_cancel_error

    def _check_commitment(self, key: str):
        assert self.commitment
        calculated_commitment = sha256(
            key.encode()
            + Api.to_canonical_json(self.start_verification().content).encode()
        ).hexdigest()
        return self.commitment == calculated_commitment

    def start_verification(self) -> ToDeviceMessage:
        """Create a content dictionary to start the verification."""
        if not self.we_started_it:
            raise LocalProtocolError(
                "Verification was not started by us, "
                "can't send start verification message."
            )

        if self.state == SasState.canceled:
            raise LocalProtocolError(
                "SAS verification was canceled, can't send start verification message."
            )

        content = {
            "from_device": self.own_device,
            "method": self._sas_method_v1,
            "transaction_id": self.transaction_id,
            "key_agreement_protocols": Sas._key_agreeemnt_protocols,
            "hashes": [self._hash_v1],
            "message_authentication_codes": self._mac_v1,
            "short_authentication_string": self._strings_v1,
        }

        message = ToDeviceMessage(
            "m.key.verification.start",
            self.other_olm_device.user_id,
            self.other_olm_device.id,
            content,
        )

        return message

    def share_key(self) -> ToDeviceMessage:
        """Create a dictionary containing our public key."""
        if self.state == SasState.canceled:
            raise LocalProtocolError(
                "SAS verification was canceled, can't share our public key."
            )

        content = {
            "transaction_id": self.transaction_id,
            "key": self.pubkey,
        }

        message = ToDeviceMessage(
            "m.key.verification.key",
            self.other_olm_device.user_id,
            self.other_olm_device.id,
            content,
        )

        return message

    def get_cancellation(self) -> ToDeviceMessage:
        """Create a dictionary containing our verification cancellation."""
        if self.state != SasState.canceled:
            raise LocalProtocolError("Sas process isn't canceled.")

        assert self.cancel_code
        assert self.cancel_reason

        content = {
            "code": self.cancel_code,
            "reason": self.cancel_reason,
            "transaction_id": self.transaction_id,
        }

        message = ToDeviceMessage(
            "m.key.verification.cancel",
            self.other_olm_device.user_id,
            self.other_olm_device.id,
            content,
        )

        return message

    def _event_ok(self, event: KeyVerificationEvent):
        if self.state == SasState.canceled:
            return False

        if event.transaction_id != self.transaction_id:
            self.state = SasState.canceled
            self.cancel_code, self.cancel_reason = self._txid_error
            return False

        if self.other_olm_device.user_id != event.sender:
            self.state = SasState.canceled
            self.cancel_code, self.cancel_reason = self._user_mismatch_error
            return False

        return True

    def receive_accept_event(self, event):
        """Receive a KeyVerificationAccept event."""
        if not self._event_ok(event):
            return

        if self.state != SasState.created:
            self.state = SasState.canceled
            (
                self.cancel_code,
                self.cancel_reason,
            ) = Sas._unexpected_message_error
            return

        if (
            event.key_agreement_protocol not in Sas._key_agreeemnt_protocols
            or event.hash != Sas._hash_v1
            or event.message_authentication_code not in Sas._mac_v1
            or (
                "emoji" not in event.short_authentication_string
                and "decimal" not in event.short_authentication_string
            )
        ):
            self.state = SasState.canceled
            self.cancel_code, self.cancel_reason = Sas._unknown_method_error
            return

        self.commitment = event.commitment
        self.chosen_mac_method = event.message_authentication_code
        self.chosen_key_agreement = event.key_agreement_protocol
        self.short_auth_string = event.short_authentication_string
        self.state = SasState.accepted

    def receive_key_event(self, event):
        """Receive a KeyVerificationKey event."""
        if self.established_sas or (
            (self.state != SasState.started) and (self.state != SasState.accepted)
        ):
            self.state = SasState.canceled
            (
                self.cancel_code,
                self.cancel_reason,
            ) = self._unexpected_message_error
            return

        if not self._event_ok(event):
            return

        if self.we_started_it:
            if not self._check_commitment(event.key):
                self.state = SasState.canceled
                (
                    self.cancel_code,
                    self.cancel_reason,
                ) = self._commitment_mismatch_error
                return

        self.establish_sas(event.key)
        self.state = SasState.key_received

    def receive_mac_event(self, event):
        """Receive a KeyVerificationMac event.

        Args:
            event (KeyVerificationMac): The MAC event that was received for
                this SAS session.

        """
        if self.verified:
            return

        if not self._event_ok(event):
            return

        if self.state != SasState.key_received:
            self.state = SasState.canceled
            (
                self.cancel_code,
                self.cancel_reason,
            ) = Sas._unexpected_message_error
            return

        info = (
            f"MATRIX_KEY_VERIFICATION_MAC{self.other_olm_device.user_id}{self.other_olm_device.id}"
            f"{self.own_user}{self.own_device}{self.transaction_id}"
        )

        key_ids = ",".join(sorted(event.mac.keys()))

        assert self.established_sas
        assert self.chosen_mac_method

        calculate_mac = self.established_sas.calculate_mac

        if event.keys != calculate_mac(key_ids, info + "KEY_IDS"):
            self.state = SasState.canceled
            self.cancel_code, self.cancel_reason = self._key_mismatch_error
            return

        for key_id, key_mac in event.mac.items():
            try:
                key_type, device_id = key_id.split(":", 2)
            except ValueError:
                self.state = SasState.canceled
                (
                    self.cancel_code,
                    self.cancel_reason,
                ) = self._invalid_message_error
                return

            if key_type != "ed25519":
                self.state = SasState.canceled
                self.cancel_code, self.cancel_reason = self._key_mismatch_error
                return

            if device_id != self.other_olm_device.id:
                continue

            other_fp_key = self.other_olm_device.ed25519

            if key_mac != calculate_mac(other_fp_key, info + key_id):
                self.state = SasState.canceled
                self.cancel_code, self.cancel_reason = self._key_mismatch_error
                return

            self.verified_devices.append(device_id)

        if not self.verified_devices:
            self.state = SasState.canceled
            self.cancel_code, self.cancel_reason = self._key_mismatch_error

        self.state = SasState.mac_received
