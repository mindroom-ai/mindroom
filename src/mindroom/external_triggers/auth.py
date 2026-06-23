"""Ed25519 signing and verification for external trigger requests."""

from __future__ import annotations

import base64
import binascii
import hashlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

if TYPE_CHECKING:
    from collections.abc import Mapping

_VERSION = "MINDROOM-TRIGGER-V1"
_KEY_ID_HEADER = "x-mindroom-trigger-key-id"
_TIMESTAMP_HEADER = "x-mindroom-trigger-timestamp"
_NONCE_HEADER = "x-mindroom-trigger-nonce"
_SIGNATURE_HEADER = "x-mindroom-trigger-signature"
_REQUIRED_HEADERS = (
    _KEY_ID_HEADER,
    _TIMESTAMP_HEADER,
    _NONCE_HEADER,
    _SIGNATURE_HEADER,
)
_ED25519_PUBLIC_KEY_BYTES = 32


class TriggerAuthError(Exception):
    """Raised when external trigger authentication fails."""


@dataclass(frozen=True)
class TriggerSignatureHeaders:
    """External trigger signature headers."""

    key_id: str
    timestamp: str
    nonce: str
    signature: str

    @classmethod
    def from_mapping(cls, headers: Mapping[str, str]) -> TriggerSignatureHeaders:
        """Build signature headers from a case-insensitive HTTP header mapping."""
        normalized_headers = {name.lower(): value for name, value in headers.items()}
        missing_headers = [name for name in _REQUIRED_HEADERS if name not in normalized_headers]
        if missing_headers:
            msg = f"missing required trigger signature header: {missing_headers[0]}"
            raise TriggerAuthError(msg)

        signature_headers = cls(
            key_id=normalized_headers[_KEY_ID_HEADER],
            timestamp=normalized_headers[_TIMESTAMP_HEADER],
            nonce=normalized_headers[_NONCE_HEADER],
            signature=normalized_headers[_SIGNATURE_HEADER],
        )
        signature_headers.validate()
        return signature_headers

    def to_mapping(self) -> dict[str, str]:
        """Return lower-case HTTP header names for this signature."""
        return {
            _KEY_ID_HEADER: self.key_id,
            _TIMESTAMP_HEADER: self.timestamp,
            _NONCE_HEADER: self.nonce,
            _SIGNATURE_HEADER: self.signature,
        }

    def validate(self) -> None:
        """Validate header values that are independent of configured key material."""
        if not self.nonce:
            msg = "trigger signature nonce must not be empty"
            raise TriggerAuthError(msg)


def canonical_trigger_signing_payload(
    *,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> bytes:
    """Return canonical payload bytes signed by external trigger clients."""
    body_hash = hashlib.sha256(body).hexdigest()
    payload = f"{_VERSION}\n{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{body_hash}"
    return payload.encode("utf-8")


def sign_trigger_request(
    *,
    method: str,
    path: str,
    body: bytes,
    key_id: str,
    timestamp: str,
    nonce: str,
    private_key: Ed25519PrivateKey,
) -> dict[str, str]:
    """Sign an external trigger request and return its signature headers."""
    signature_headers = TriggerSignatureHeaders(
        key_id=key_id,
        timestamp=timestamp,
        nonce=nonce,
        signature="",
    )
    signature_headers.validate()

    payload = canonical_trigger_signing_payload(
        method=method,
        path=path,
        timestamp=timestamp,
        nonce=nonce,
        body=body,
    )
    signature = private_key.sign(payload)
    return TriggerSignatureHeaders(
        key_id=key_id,
        timestamp=timestamp,
        nonce=nonce,
        signature=base64.b64encode(signature).decode("ascii"),
    ).to_mapping()


def verify_trigger_request(
    *,
    method: str,
    path: str,
    body: bytes,
    headers: Mapping[str, str] | TriggerSignatureHeaders,
    expected_key_id: str,
    public_key_b64: str,
    replay_window_seconds: int = 300,
    now: int | None = None,
) -> None:
    """Verify an Ed25519-signed external trigger request."""
    signature_headers = _coerce_signature_headers(headers)
    if signature_headers.key_id != expected_key_id:
        msg = "trigger signature key id does not match configured key id"
        raise TriggerAuthError(msg)

    signed_at = _parse_timestamp(signature_headers.timestamp)
    current_time = int(time.time()) if now is None else now
    if signed_at > current_time or current_time - signed_at > replay_window_seconds:
        msg = "trigger signature timestamp outside replay window"
        raise TriggerAuthError(msg)

    public_key_bytes = _decode_base64(public_key_b64, value_name="public key")
    if len(public_key_bytes) != _ED25519_PUBLIC_KEY_BYTES:
        msg = "invalid Ed25519 public key length"
        raise TriggerAuthError(msg)

    signature = _decode_base64(signature_headers.signature, value_name="signature")
    verifier = Ed25519PublicKey.from_public_bytes(public_key_bytes)
    payload = canonical_trigger_signing_payload(
        method=method,
        path=path,
        timestamp=signature_headers.timestamp,
        nonce=signature_headers.nonce,
        body=body,
    )

    try:
        verifier.verify(signature, payload)
    except (InvalidSignature, ValueError) as exc:
        msg = "trigger signature verification failed"
        raise TriggerAuthError(msg) from exc


def _coerce_signature_headers(headers: Mapping[str, str] | TriggerSignatureHeaders) -> TriggerSignatureHeaders:
    """Return normalized trigger signature headers."""
    if isinstance(headers, TriggerSignatureHeaders):
        headers.validate()
        return headers
    return TriggerSignatureHeaders.from_mapping(headers)


def _parse_timestamp(timestamp: str) -> int:
    """Parse an external trigger Unix timestamp."""
    try:
        return int(timestamp)
    except ValueError as exc:
        msg = "invalid trigger signature timestamp"
        raise TriggerAuthError(msg) from exc


def _decode_base64(value: str, *, value_name: str) -> bytes:
    """Decode strict base64 header or config values."""
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        msg = f"invalid base64 trigger {value_name}"
        raise TriggerAuthError(msg) from exc
