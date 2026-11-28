"""Tests for the versioned Matrix desktop wire protocol."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from mindroom.desktop.protocol import (
    DESKTOP_APP_CONTROL_ACTIONS,
    DESKTOP_DISCONNECT_CONFIRMED_EVENT_TYPE,
    DESKTOP_DISCONNECT_REQUEST_EVENT_TYPE,
    DESKTOP_PAIRING_CONFIRM_EVENT_TYPE,
    DESKTOP_PAIRING_CONFIRMED_EVENT_TYPE,
    DESKTOP_SETUP_OFFER_EVENT_TYPE,
    DESKTOP_SETUP_REQUEST_EVENT_TYPE,
    MAX_COMMAND_TTL_MS,
    MAX_SETTLE_MS,
    MAX_SHELL_OUTPUT_BYTES,
    SHELL_OUTPUT_MIME_TYPE,
    DesktopCommand,
    DesktopDisconnectConfirmed,
    DesktopDisconnectRequest,
    DesktopPairingAccepted,
    DesktopPairingClaim,
    DesktopPairingConfirm,
    DesktopPairingConfirmed,
    DesktopProtocolError,
    DesktopResponse,
    DesktopSetupDescriptor,
    DesktopSetupOffer,
    DesktopSetupRequest,
    EncryptedDesktopMedia,
    desktop_observation_mode,
    desktop_pairing_verification,
    desktop_settle_ms,
)


def test_native_setup_descriptor_preserves_exact_pairing_scope() -> None:
    """Native setup imports one explicit controller and requester-agent scope."""
    descriptor = DesktopSetupDescriptor(
        homeserver="https://matrix.example.org",
        user_id="@alice:example.org",
        code="pairing-code",
        controller_user_id="@computer:example.org",
        controller_device_id="CLOUD",
        controller_ed25519="fingerprint",
        requester_id="@alice:example.org",
        agent_name="computer",
        cloudflare_access=False,
    )
    content = descriptor.to_content()
    assert DesktopSetupDescriptor.from_content(content) == descriptor
    local = {**content, "homeserver": "http://localhost:8008"}
    assert DesktopSetupDescriptor.from_content(local).homeserver == "http://localhost:8008"
    for change in (
        {"v": True},
        {"kind": "another_setup"},
        {"unknown": "value"},
        {"cloudflare_access": "false"},
        {"homeserver": "https://matrix.example.org@evil.example"},
        {"homeserver": "https://m\u0430trix.example.org"},  # Cyrillic a
        {"homeserver": "http://matrix.example.org"},
    ):
        with pytest.raises(DesktopProtocolError):
            DesktopSetupDescriptor.from_content({**content, **change})


def test_pairing_claim_contains_only_protocol_version_and_token() -> None:
    """Device identity comes from the authenticated Olm envelope, not claim content."""
    claim = DesktopPairingClaim("short-lived-code")

    assert DesktopPairingClaim.from_content(claim.to_content()) == claim
    assert set(claim.to_content()) == {"v", "token"}
    assert desktop_pairing_verification(claim.token, "first-key") != desktop_pairing_verification(
        claim.token,
        "second-key",
    )


def test_pairing_acknowledgement_round_trips_without_bearer_token() -> None:
    """Controller acknowledgement correlates without echoing the code."""
    acknowledgement = DesktopPairingAccepted("VERIFY123")

    assert DesktopPairingAccepted.from_content(acknowledgement.to_content()) == acknowledgement
    assert "token" not in acknowledgement.to_content()


def _media() -> EncryptedDesktopMedia:
    return EncryptedDesktopMedia(
        url="mxc://example.org/screenshot",
        key="secret-key",
        iv="initialization-vector",
        sha256="ciphertext-hash",
        mime_type="image/jpeg",
        size=123,
    )


def _command() -> DesktopCommand:
    return DesktopCommand(
        request_id="request-1",
        session_id="session-1",
        sequence=7,
        issued_at_ms=1_000,
        expires_at_ms=2_000,
        action="click",
        requester_id="@alice:example.org",
        agent_name="computer",
        parameters={"x": 10, "y": 20, "button": "left"},
    )


def test_command_round_trip_preserves_authorization_provenance() -> None:
    """Commands retain their caller, agent, expiry, sequence, and parameters."""
    command = _command()

    assert DesktopCommand.from_content(command.to_content()) == command


@pytest.mark.parametrize("action", ["browser_observe", "browser_control"])
def test_browser_commands_share_the_pinned_desktop_wire_protocol(action: str) -> None:
    """Browser-native calls retain the same Matrix identity and replay envelope."""
    command = DesktopCommand(
        request_id="browser-request",
        session_id="browser-session",
        sequence=3,
        issued_at_ms=1_000,
        expires_at_ms=2_000,
        action=action,
        requester_id="@alice:example.org",
        agent_name="computer",
        parameters={
            "browser_action": "snapshot" if action == "browser_observe" else "navigate",
            "browser_parameters": {"targetId": "1"},
        },
    )

    assert DesktopCommand.from_content(command.to_content()) == command


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("action", "shell", "Unsupported desktop action"),
        ("sequence", -1, "non-negative"),
        ("expires_at_ms", 1_000, "TTL"),
        ("expires_at_ms", 1_000 + MAX_COMMAND_TTL_MS + 1, "TTL"),
    ],
)
def test_command_rejects_unsafe_or_unbounded_values(field: str, value: object, match: str) -> None:
    """The local bridge never receives unsupported actions or invalid replay metadata."""
    content = _command().to_content()
    content[field] = value

    with pytest.raises(DesktopProtocolError, match=match):
        DesktopCommand.from_content(content)


@pytest.mark.parametrize(
    ("parameters", "match"),
    [
        ({"text": "x" * (16 * 1024 + 1)}, "encoded bytes"),
        ({"x": float("nan")}, "finite JSON"),
        ({"nested": object()}, "finite JSON"),
    ],
)
def test_command_rejects_oversized_or_non_json_parameters(
    parameters: dict[str, object],
    match: str,
) -> None:
    """Pinned senders still cannot make the local bridge process unbounded command data."""
    content = _command().to_content()
    content["parameters"] = parameters

    with pytest.raises(DesktopProtocolError, match=match):
        DesktopCommand.from_content(content)


def test_success_response_round_trip_includes_encrypted_media() -> None:
    """Successful screenshots use the strict Matrix encrypted-file shape."""
    response = DesktopResponse(
        request_id="request-1",
        session_id="session-1",
        ok=True,
        result={"screen": {"width": 1920, "height": 1080}},
        screenshot=_media(),
    )

    assert DesktopResponse.from_content(response.to_content()) == response
    assert response.to_content()["screenshot"] == {
        "url": "mxc://example.org/screenshot",
        "key": {
            "alg": "A256CTR",
            "ext": True,
            "k": "secret-key",
            "key_ops": ["encrypt", "decrypt"],
            "kty": "oct",
        },
        "iv": "initialization-vector",
        "hashes": {"sha256": "ciphertext-hash"},
        "v": "v2",
        "mimetype": "image/jpeg",
        "size": 123,
    }


@pytest.mark.parametrize(
    "content",
    [
        DesktopResponse(request_id="r", session_id="s", ok=True, error="bad").to_content(),
        DesktopResponse(request_id="r", session_id="s", ok=False).to_content(),
        DesktopResponse(request_id="r", session_id="s", ok=False, error="bad", screenshot=_media()).to_content(),
    ],
)
def test_response_rejects_inconsistent_success_state(content: dict[str, object]) -> None:
    """Peers cannot combine success, failure, and screenshot fields ambiguously."""
    with pytest.raises(DesktopProtocolError):
        DesktopResponse.from_content(content)


def test_encrypted_media_requires_matrix_uri_and_expected_key_algorithm() -> None:
    """Only bounded Matrix media encrypted with the declared algorithm is accepted."""
    content = _media().to_content()
    content["url"] = "https://example.org/screenshot"

    with pytest.raises(DesktopProtocolError, match="mxc://"):
        EncryptedDesktopMedia.from_content(content)

    content = _media().to_content()
    assert isinstance(content["key"], dict)
    content["key"]["alg"] = "none"

    with pytest.raises(DesktopProtocolError, match="A256CTR"):
        EncryptedDesktopMedia.from_content(content)


@pytest.mark.parametrize(("key_field", "value"), [("kty", "RSA"), ("ext", False)])
def test_encrypted_media_rejects_keys_the_serializer_never_emits(key_field: str, value: object) -> None:
    """Receivers accept only the octet, extractable key description the shared serializer writes."""
    content = _media().to_content()
    assert isinstance(content["key"], dict)
    content["key"][key_field] = value

    with pytest.raises(
        DesktopProtocolError,
        match=r"^screenshot\.key must describe an extractable A256CTR octet key\.$",
    ):
        EncryptedDesktopMedia.from_content(content)


def test_encrypted_media_rejects_envelope_versions_the_serializer_never_emits() -> None:
    """Receivers accept only the encrypted-file version the shared serializer writes."""
    with pytest.raises(DesktopProtocolError, match=r"^screenshot\.v must be v2\.$"):
        EncryptedDesktopMedia.from_content({**_media().to_content(), "v": "v1"})


@pytest.mark.parametrize(
    ("action", "parameters"),
    [
        ("run_shell", {"command": "make test", "timeout_seconds": 5}),
        ("check_shell", {"handle": "shell:0123abcd"}),
        ("kill_shell", {"handle": "shell:0123abcd", "force": True}),
    ],
)
def test_shell_handle_actions_share_the_pinned_wire_protocol(action: str, parameters: dict[str, object]) -> None:
    """Starting, polling, and stopping a local command all travel as ordinary replay-protected commands."""
    command = replace(_command(), action=action, parameters=parameters)

    assert DesktopCommand.from_content(command.to_content()) == command


def test_shell_output_attachment_descriptor_is_distinct_from_screenshots() -> None:
    """Output attachments carry bounded text only, and screenshot fields still carry images only."""
    output = replace(_media(), mime_type=SHELL_OUTPUT_MIME_TYPE, size=MAX_SHELL_OUTPUT_BYTES)
    content = output.to_content()

    assert EncryptedDesktopMedia.from_content(content, kind="output_attachment") == output
    with pytest.raises(DesktopProtocolError, match=r"screenshot\.mimetype"):
        EncryptedDesktopMedia.from_content(content)
    with pytest.raises(DesktopProtocolError, match=r"output_attachment\.mimetype"):
        EncryptedDesktopMedia.from_content(_media().to_content(), kind="output_attachment")
    with pytest.raises(DesktopProtocolError, match=r"output_attachment\.size"):
        EncryptedDesktopMedia.from_content({**content, "size": MAX_SHELL_OUTPUT_BYTES + 1}, kind="output_attachment")
    with pytest.raises(DesktopProtocolError, match=r"output_attachment\.url"):
        EncryptedDesktopMedia.from_content({**content, "url": "https://example.org/x"}, kind="output_attachment")


def test_response_content_bytes_match_the_olm_plaintext_serializer() -> None:
    """The inline limit is measured with the ASCII-escaping serializer nio uses before Olm encryption."""
    response = DesktopResponse(
        request_id="request-1",
        session_id="session-1",
        ok=True,
        result={"output": "\x01é😀\n"},
    )

    expected = len(json.dumps(response.to_content(), separators=(",", ":")).encode())
    assert response.content_bytes() == expected
    assert expected > len(json.dumps(response.to_content(), ensure_ascii=False, separators=(",", ":")).encode())


def test_olm_setup_and_disconnect_requests_round_trip_and_validate() -> None:
    """Olm setup and disconnect requests enforce version 1, bounded nonces, and agent names."""
    assert DESKTOP_SETUP_REQUEST_EVENT_TYPE == "io.mindroom.desktop.setup_request.v1"
    assert DESKTOP_DISCONNECT_REQUEST_EVENT_TYPE == "io.mindroom.desktop.disconnect_request.v1"
    nonce = "nonce-0123456789"
    for cls in (DesktopSetupRequest, DesktopDisconnectRequest):
        req = cls(nonce=nonce, agent_name="computer")
        content = req.to_content()
        assert content == {"v": 1, "nonce": nonce, "agent_name": "computer"}
        assert cls.from_content(content) == req
        for change in (
            {"v": 2},
            {"v": True},
            {"nonce": "short"},
            {"nonce": " " * 20},
            {"nonce": "x" * 129},
            {"nonce": 123},
            {"agent_name": "   "},
            {"agent_name": "a" * 129},
            {"agent_name": None},
        ):
            with pytest.raises(DesktopProtocolError):
                cls.from_content({**content, **change})


def test_olm_setup_offer_round_trips_and_enforces_eligibility_invariants() -> None:
    """Setup offers strictly separate eligible pairing codes from ineligible error replies."""
    assert DESKTOP_SETUP_OFFER_EVENT_TYPE == "io.mindroom.desktop.setup_offer.v1"
    nonce = "nonce-0123456789"
    eligible = DesktopSetupOffer(
        nonce=nonce,
        agent_name="computer",
        eligible=True,
        homeserver="https://matrix.example.org",
        user_id="@alice:example.org",
        code="short-lived-code",
        cloudflare_access=True,
    )
    content = eligible.to_content()
    assert content["v"] == 1
    assert DesktopSetupOffer.from_content(content) == eligible
    assert DesktopSetupOffer.from_content({**content, "homeserver": "http://localhost:8008"}).homeserver == (
        "http://localhost:8008"
    )

    ineligible = DesktopSetupOffer(
        nonce=nonce,
        agent_name="computer",
        eligible=False,
        error="Desktop pairing is disabled.",
    )
    ineligible_content = ineligible.to_content()
    assert ineligible_content["v"] == 1
    assert DesktopSetupOffer.from_content(ineligible_content) == ineligible

    for payload in (
        {**content, "v": 2},
        {**content, "nonce": "too-short"},
        {**content, "eligible": "true"},
        {**content, "cloudflare_access": "false"},
        {**content, "error": "should not be present"},
        {**content, "code": None},
        {**content, "user_id": None},
        {**content, "homeserver": "http://matrix.example.org"},
        {**content, "homeserver": "https://matrix.example.org@evil.example"},
        {**ineligible_content, "error": None},
        {**ineligible_content, "error": "   "},
        {**ineligible_content, "code": "leaked-code"},
    ):
        with pytest.raises(DesktopProtocolError):
            DesktopSetupOffer.from_content(payload)


def test_olm_pairing_confirm_and_status_responses_round_trip_and_validate() -> None:
    """Confirmation requests and ok/error status replies enforce version 1 and strict invariants."""
    assert DESKTOP_PAIRING_CONFIRM_EVENT_TYPE == "io.mindroom.desktop.pairing_confirm.v1"
    assert DESKTOP_PAIRING_CONFIRMED_EVENT_TYPE == "io.mindroom.desktop.pairing_confirmed.v1"
    assert DESKTOP_DISCONNECT_CONFIRMED_EVENT_TYPE == "io.mindroom.desktop.disconnect_confirmed.v1"
    nonce = "nonce-0123456789"
    code = "pair-code"
    confirm = DesktopPairingConfirm(
        nonce=nonce,
        token=code,
        verification="ABCDEF0123456789",
        device_id="HELPER1",
        device_ed25519="helper-key",
    )
    confirm_content = confirm.to_content()
    assert confirm_content["v"] == 1
    assert DesktopPairingConfirm.from_content(confirm_content) == confirm
    for change in (
        {"v": 2},
        {"nonce": "   "},
        {"token": ""},
        {"token": "t" * 257},
        {"verification": "v" * 65},
        {"device_id": "d" * 257},
        {"device_ed25519": "k" * 257},
    ):
        with pytest.raises(DesktopProtocolError):
            DesktopPairingConfirm.from_content({**confirm_content, **change})

    for cls in (DesktopPairingConfirmed, DesktopDisconnectConfirmed):
        ok_reply = cls(nonce=nonce, agent_name="computer", ok=True)
        err_reply = cls(nonce=nonce, agent_name="computer", ok=False, error="rejected")
        assert ok_reply.to_content()["v"] == 1
        assert cls.from_content(ok_reply.to_content()) == ok_reply
        assert cls.from_content(err_reply.to_content()) == err_reply
        for bad in (
            {**ok_reply.to_content(), "v": 2},
            {**ok_reply.to_content(), "ok": "true"},
            {**ok_reply.to_content(), "error": "unexpected"},
            {**err_reply.to_content(), "error": None},
            {**err_reply.to_content(), "error": "   "},
            {**err_reply.to_content(), "nonce": "short"},
            {**err_reply.to_content(), "agent_name": "a" * 129},
        ):
            with pytest.raises(DesktopProtocolError):
                cls.from_content(bad)


def test_observation_mode_and_settle_ms_validation() -> None:
    """observation='none' and settle_ms are restricted to app control actions with bounded values."""
    assert "click" in DESKTOP_APP_CONTROL_ACTIONS
    assert "browser_control" not in DESKTOP_APP_CONTROL_ACTIONS
    assert desktop_observation_mode("click", "none") == "none"
    assert desktop_observation_mode("launch_app", "none") == "none"

    for action, value, pattern in (
        ("click", "invalid", "none, tree, screenshot, or both"),
        ("screenshot", "none", "screenshot action requires a screenshot"),
        ("get_app_state", "none", "get_app_state action requires an observation"),
        ("status", "none", "requires an application action"),
        ("list_folders", "none", "requires an application action"),
        ("browser_control", "none", "requires an application action"),
    ):
        with pytest.raises(DesktopProtocolError, match=pattern):
            desktop_observation_mode(action, value)

    assert desktop_settle_ms("screenshot", None, supplied=False) == 0
    assert desktop_settle_ms("click", 0, supplied=True, mode="none") == 0
    assert desktop_settle_ms("click", MAX_SETTLE_MS, supplied=True, mode="screenshot") == MAX_SETTLE_MS

    for action, value, supplied, mode, pattern in (
        ("click", None, True, "both", "integer from 0 to 2000"),
        ("click", True, True, "both", "integer from 0 to 2000"),
        ("click", -1, True, "both", "integer from 0 to 2000"),
        ("click", MAX_SETTLE_MS + 1, True, "both", "integer from 0 to 2000"),
        ("screenshot", 0, True, "screenshot", "requires an application control action"),
        ("get_app_state", 500, True, "both", "requires an application control action"),
        ("list_folders", 100, True, "both", "requires an application control action"),
        ("browser_control", 100, True, "both", "requires an application control action"),
        ("click", 500, True, "none", "cannot be positive when observation is none"),
    ):
        with pytest.raises(DesktopProtocolError, match=pattern):
            desktop_settle_ms(action, value, supplied=supplied, mode=mode)
