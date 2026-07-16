"""Tests for the versioned Matrix desktop wire protocol."""

from __future__ import annotations

import pytest

from mindroom.desktop.protocol import (
    MAX_COMMAND_TTL_MS,
    DesktopCommand,
    DesktopProtocolError,
    DesktopResponse,
    EncryptedDesktopMedia,
)


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
