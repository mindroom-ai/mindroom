"""Tests for external trigger Ed25519 request authentication."""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from mindroom.external_triggers.auth import (
    TriggerAuthError,
    TriggerSignatureHeaders,
    canonical_trigger_signing_payload,
    sign_trigger_request,
    verify_trigger_request,
)


def _private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _public_key_b64(private_key: Ed25519PrivateKey) -> str:
    public_key_bytes = private_key.public_key().public_bytes(
        encoding=Encoding.Raw,
        format=PublicFormat.Raw,
    )
    return base64.b64encode(public_key_bytes).decode("ascii")


def test_exact_signed_request_verifies() -> None:
    """A request signed over the exact canonical payload verifies."""
    private_key = _private_key()
    body = b'{"kind":"campground.availability","message":"open"}'
    headers = sign_trigger_request(
        method="post",
        path="/api/external-triggers/campground",
        body=body,
        key_id="campground-main",
        timestamp="1710000000",
        nonce="nonce-1",
        private_key=private_key,
    )
    assert isinstance(headers, dict)

    verify_trigger_request(
        method="POST",
        path="/api/external-triggers/campground",
        body=body,
        headers=TriggerSignatureHeaders.from_mapping(headers),
        expected_key_id="campground-main",
        public_key_b64=_public_key_b64(private_key),
        now=1710000000,
    )


def test_canonical_payload_covers_method_path_timestamp_nonce_and_body_hash() -> None:
    """Canonical signing payload includes all request identity fields."""
    payload = canonical_trigger_signing_payload(
        method="post",
        path="/hooks/campground?dry_run=1",
        timestamp="1710000000",
        nonce="nonce-1",
        body=b'{"kind":"campground.availability"}',
    )

    assert payload == (
        b"MINDROOM-TRIGGER-V1\n"
        b"POST\n"
        b"/hooks/campground?dry_run=1\n"
        b"1710000000\n"
        b"nonce-1\n"
        b"9fb75c04b76d051778ffb4b0c941e9128f7185301da4a303ff022284814dac45"
    )


def test_changed_body_fails_with_signature_error() -> None:
    """Body mutations after signing invalidate the signature."""
    private_key = _private_key()
    headers = sign_trigger_request(
        method="POST",
        path="/api/external-triggers/campground",
        body=b'{"message":"open"}',
        key_id="campground-main",
        timestamp="1710000000",
        nonce="nonce-1",
        private_key=private_key,
    )

    with pytest.raises(TriggerAuthError, match="signature"):
        verify_trigger_request(
            method="POST",
            path="/api/external-triggers/campground",
            body=b'{"message":"closed"}',
            headers=TriggerSignatureHeaders.from_mapping(headers),
            expected_key_id="campground-main",
            public_key_b64=_public_key_b64(private_key),
            now=1710000000,
        )


def test_non_ascii_signature_header_fails_with_trigger_auth_error() -> None:
    """Non-ASCII signature header values are reported as auth failures."""
    private_key = _private_key()
    headers = sign_trigger_request(
        method="POST",
        path="/api/external-triggers/campground",
        body=b"{}",
        key_id="campground-main",
        timestamp="1710000000",
        nonce="nonce-1",
        private_key=private_key,
    )
    headers["x-mindroom-trigger-signature"] = "not-ascii-\u2603"

    with pytest.raises(TriggerAuthError, match=r"base64|signature"):
        verify_trigger_request(
            method="POST",
            path="/api/external-triggers/campground",
            body=b"{}",
            headers=TriggerSignatureHeaders.from_mapping(headers),
            expected_key_id="campground-main",
            public_key_b64=_public_key_b64(private_key),
            now=1710000000,
        )


def test_wrong_key_id_fails() -> None:
    """Key id must match the configured trigger key id."""
    private_key = _private_key()
    headers = sign_trigger_request(
        method="POST",
        path="/api/external-triggers/campground",
        body=b"{}",
        key_id="old-key",
        timestamp="1710000000",
        nonce="nonce-1",
        private_key=private_key,
    )

    with pytest.raises(TriggerAuthError, match="key id"):
        verify_trigger_request(
            method="POST",
            path="/api/external-triggers/campground",
            body=b"{}",
            headers=TriggerSignatureHeaders.from_mapping(headers),
            expected_key_id="new-key",
            public_key_b64=_public_key_b64(private_key),
            now=1710000000,
        )


def test_timestamp_outside_replay_window_fails() -> None:
    """Stale timestamps are rejected before signature verification succeeds."""
    private_key = _private_key()
    headers = sign_trigger_request(
        method="POST",
        path="/api/external-triggers/campground",
        body=b"{}",
        key_id="campground-main",
        timestamp="1710000000",
        nonce="nonce-1",
        private_key=private_key,
    )

    with pytest.raises(TriggerAuthError, match="timestamp"):
        verify_trigger_request(
            method="POST",
            path="/api/external-triggers/campground",
            body=b"{}",
            headers=TriggerSignatureHeaders.from_mapping(headers),
            expected_key_id="campground-main",
            public_key_b64=_public_key_b64(private_key),
            replay_window_seconds=300,
            now=1710000301,
        )


def test_missing_required_header_fails() -> None:
    """All signature headers are required."""
    private_key = _private_key()
    headers = sign_trigger_request(
        method="POST",
        path="/api/external-triggers/campground",
        body=b"{}",
        key_id="campground-main",
        timestamp="1710000000",
        nonce="nonce-1",
        private_key=private_key,
    )
    del headers["x-mindroom-trigger-signature"]

    with pytest.raises(TriggerAuthError, match="x-mindroom-trigger-signature"):
        verify_trigger_request(
            method="POST",
            path="/api/external-triggers/campground",
            body=b"{}",
            headers=TriggerSignatureHeaders.from_mapping(headers),
            expected_key_id="campground-main",
            public_key_b64=_public_key_b64(private_key),
            now=1710000000,
        )
