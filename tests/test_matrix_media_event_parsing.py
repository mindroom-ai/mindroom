"""Regression tests for safe nio parsing of encrypted Matrix media sources."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import nio
import pytest

import mindroom.matrix.media as media_module
from mindroom.matrix.cache import ConversationEventCache
from mindroom.matrix.client_thread_history import _parse_room_message_event
from mindroom.matrix.conversation_cache import _cached_room_get_event_response

_SYNTHETIC_FILE_KEY = "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s"
_SYNTHETIC_FILE_IV = "aWlpaWlpaWlpaWlpaWlpaQ"
_SYNTHETIC_FILE_HASH = "aGhoaGhoaGhoaGhoaGhoaGhoaGhoaGhoaGhoaGhoaGg"
_SYNTHETIC_FILE_MXC = "mxc://example.test/synthetic-encrypted-media"
_SYNTHETIC_THUMBNAIL_KEY = "SYNTHETIC_THUMBNAIL_JWK_KEY_DO_NOT_USE"
_SYNTHETIC_THUMBNAIL_IV = "SYNTHETIC_THUMBNAIL_IV_DO_NOT_USE"
_SYNTHETIC_THUMBNAIL_HASH = "SYNTHETIC_THUMBNAIL_HASH_DO_NOT_USE"
_SYNTHETIC_ACCESS_TOKEN = "SYNTHETIC_ACCESS_TOKEN_DO_NOT_USE"  # noqa: S105
_SYNTHETIC_SECRET_VALUES = (
    _SYNTHETIC_FILE_KEY,
    _SYNTHETIC_FILE_IV,
    _SYNTHETIC_FILE_HASH,
    _SYNTHETIC_FILE_MXC,
    _SYNTHETIC_THUMBNAIL_KEY,
    _SYNTHETIC_THUMBNAIL_IV,
    _SYNTHETIC_THUMBNAIL_HASH,
    _SYNTHETIC_ACCESS_TOKEN,
)


def _encrypted_media_source(msgtype: str = "m.image") -> dict[str, object]:
    return {
        "type": "m.room.message",
        "event_id": "$synthetic-media:example.test",
        "sender": "@synthetic:example.test",
        "origin_server_ts": 1,
        "content": {
            "msgtype": msgtype,
            "body": "synthetic-media.bin",
            "file": {
                "url": _SYNTHETIC_FILE_MXC,
                "key": {
                    "alg": "A256CTR",
                    "ext": True,
                    "key_ops": ["encrypt", "decrypt"],
                    "kty": "oct",
                    "k": _SYNTHETIC_FILE_KEY,
                },
                "iv": _SYNTHETIC_FILE_IV,
                "hashes": {"sha256": _SYNTHETIC_FILE_HASH},
                "v": "v2",
            },
            "info": {
                "mimetype": "application/octet-stream",
                "thumbnail_file": {
                    "url": f"https://example.test/media?access_token={_SYNTHETIC_ACCESS_TOKEN}",
                    "key": {"alg": "A256CTR", "k": _SYNTHETIC_THUMBNAIL_KEY},
                    "iv": _SYNTHETIC_THUMBNAIL_IV,
                    "hashes": {"sha256": _SYNTHETIC_THUMBNAIL_HASH},
                },
            },
        },
    }


def _assert_synthetic_secrets_absent(caplog: pytest.LogCaptureFixture) -> None:
    captured_logs = "\n".join(record.getMessage() for record in caplog.records)
    for secret in _SYNTHETIC_SECRET_VALUES:
        assert secret not in captured_logs


@pytest.mark.parametrize(
    ("msgtype", "expected_type"),
    [
        ("m.image", nio.RoomEncryptedImage),
        ("m.audio", nio.RoomEncryptedAudio),
        ("m.video", nio.RoomEncryptedVideo),
        ("m.file", nio.RoomEncryptedFile),
    ],
)
def test_encrypted_media_parser_uses_nio_decrypted_event_path_without_secret_logs(
    msgtype: str,
    expected_type: type[nio.RoomEncryptedMedia],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every supported encrypted media type should parse without plaintext-schema warnings."""
    with caplog.at_level(logging.WARNING, logger="nio.events.misc"):
        parsed_event = media_module.parse_matrix_media_event_source(_encrypted_media_source(msgtype))

    assert isinstance(parsed_event, expected_type)
    assert parsed_event.url == _SYNTHETIC_FILE_MXC
    assert parsed_event.key["k"] == _SYNTHETIC_FILE_KEY
    _assert_synthetic_secrets_absent(caplog)
    assert not any("Error validating event" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_parsed_encrypted_media_still_downloads_and_decrypts() -> None:
    """The safe parser should retain every field required for encrypted media delivery."""
    parsed_event = media_module.parse_matrix_media_event_source(_encrypted_media_source())
    assert isinstance(parsed_event, nio.RoomEncryptedImage)
    client = AsyncMock(spec=nio.AsyncClient)
    client.download.return_value = nio.DownloadResponse(
        body=b"synthetic-ciphertext",
        content_type="application/octet-stream",
        filename=None,
    )

    with patch(
        "mindroom.matrix.media.crypto.attachments.decrypt_attachment",
        return_value=b"synthetic-plaintext",
    ) as decrypt_attachment:
        media_bytes = await media_module.download_media_bytes(client, parsed_event)

    assert media_bytes == b"synthetic-plaintext"
    client.download.assert_awaited_once_with(_SYNTHETIC_FILE_MXC)
    decrypt_attachment.assert_called_once_with(
        b"synthetic-ciphertext",
        _SYNTHETIC_FILE_KEY,
        _SYNTHETIC_FILE_HASH,
        _SYNTHETIC_FILE_IV,
    )


def test_thread_history_reparse_uses_encrypted_media_boundary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Thread reconstruction should preserve encrypted media without logging its content."""
    with caplog.at_level(logging.WARNING, logger="nio.events.misc"):
        parsed_event = _parse_room_message_event(_encrypted_media_source())

    assert isinstance(parsed_event, nio.RoomEncryptedImage)
    assert parsed_event.url == _SYNTHETIC_FILE_MXC
    _assert_synthetic_secrets_absent(caplog)
    assert not any("Error validating event" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_cached_event_reconstruction_uses_encrypted_media_boundary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cached point lookups should rebuild encrypted media without logging its content."""
    client = AsyncMock(spec=nio.AsyncClient)
    event_cache = AsyncMock(spec=ConversationEventCache)
    event_cache.get_latest_edit.return_value = None

    with caplog.at_level(logging.WARNING, logger="nio.events.misc"):
        response = await _cached_room_get_event_response(
            client,
            event_cache,
            room_id="!synthetic:example.test",
            event_source=_encrypted_media_source(),
        )

    assert isinstance(response, nio.RoomGetEventResponse)
    assert isinstance(response.event, nio.RoomEncryptedImage)
    assert response.event.url == _SYNTHETIC_FILE_MXC
    _assert_synthetic_secrets_absent(caplog)
    assert not any("Error validating event" in record.getMessage() for record in caplog.records)


def test_text_message_with_file_extension_is_not_dropped_from_history() -> None:
    """A non-media extension field should not select the encrypted-media parser."""
    event_source = {
        "type": "m.room.message",
        "event_id": "$synthetic-text:example.test",
        "sender": "@synthetic:example.test",
        "origin_server_ts": 2,
        "content": {
            "msgtype": "m.text",
            "body": "harmless text with extension metadata",
            "file": "harmless extension metadata",
        },
    }

    parsed_event = _parse_room_message_event(event_source)

    assert isinstance(parsed_event, nio.RoomMessageText)
    assert parsed_event.body == "harmless text with extension metadata"


def test_custom_message_with_encrypted_file_is_not_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A custom message type should keep its normal nio representation."""
    with caplog.at_level(logging.WARNING, logger="nio.events.misc"):
        parsed_event = _parse_room_message_event(_encrypted_media_source("io.synthetic.custom"))

    assert isinstance(parsed_event, nio.RoomMessageUnknown)
    _assert_synthetic_secrets_absent(caplog)


@pytest.mark.asyncio
async def test_text_message_with_file_extension_uses_cached_response() -> None:
    """A non-media extension field should not bypass cached point reconstruction."""
    event_source = {
        "type": "m.room.message",
        "event_id": "$synthetic-cached-text:example.test",
        "sender": "@synthetic:example.test",
        "origin_server_ts": 3,
        "content": {
            "msgtype": "m.text",
            "body": "harmless cached text",
            "file": "harmless extension metadata",
        },
    }
    client = AsyncMock(spec=nio.AsyncClient)
    event_cache = AsyncMock(spec=ConversationEventCache)
    event_cache.get_latest_edit.return_value = None

    response = await _cached_room_get_event_response(
        client,
        event_cache,
        room_id="!synthetic:example.test",
        event_source=event_source,
    )

    assert isinstance(response, nio.RoomGetEventResponse)
    assert isinstance(response.event, nio.RoomMessageText)
    assert response.event.body == "harmless cached text"


@pytest.mark.asyncio
async def test_non_message_event_with_media_extensions_keeps_event_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Media-shaped extension fields should not retype a non-message event."""
    event_source = _encrypted_media_source()
    event_source["type"] = "m.reaction"
    content = event_source["content"]
    assert isinstance(content, dict)
    content["m.relates_to"] = {
        "rel_type": "m.annotation",
        "event_id": "$synthetic-target:example.test",
        "key": "harmless-reaction",
    }
    client = AsyncMock(spec=nio.AsyncClient)
    event_cache = AsyncMock(spec=ConversationEventCache)
    event_cache.get_latest_edit.return_value = None

    with caplog.at_level(logging.WARNING, logger="nio.events.misc"):
        response = await _cached_room_get_event_response(
            client,
            event_cache,
            room_id="!synthetic:example.test",
            event_source=event_source,
        )

    assert isinstance(response, nio.RoomGetEventResponse)
    assert isinstance(response.event, nio.ReactionEvent)
    _assert_synthetic_secrets_absent(caplog)


@pytest.mark.asyncio
async def test_malformed_encrypted_media_is_rejected_with_diagnostic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed encrypted media should be rejected after nio logs a safe diagnostic."""
    event_source = {
        "type": "m.room.message",
        "event_id": "$harmless-invalid-encrypted:example.test",
        "sender": "@synthetic:example.test",
        "origin_server_ts": 5,
        "content": {
            "msgtype": "m.image",
            "body": "harmless-invalid-encrypted.png",
            "file": {},
        },
    }
    client = AsyncMock(spec=nio.AsyncClient)
    event_cache = AsyncMock(spec=ConversationEventCache)
    event_cache.get_latest_edit.return_value = None

    with caplog.at_level(logging.WARNING, logger="nio.events.misc"):
        parsed_event = _parse_room_message_event(event_source)
        response = await _cached_room_get_event_response(
            client,
            event_cache,
            room_id="!synthetic:example.test",
            event_source=event_source,
        )

    captured_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert parsed_event is None
    assert response is None
    assert "'url' is a required property" in captured_logs
    assert "instance['content']['file']" in captured_logs


def test_plain_media_validation_warning_rejects_bad_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed plaintext media should be rejected without erasing nio's warning."""
    event_source = {
        "type": "m.room.message",
        "event_id": "$harmless-invalid:example.test",
        "sender": "@synthetic:example.test",
        "origin_server_ts": 2,
        "content": {
            "msgtype": "m.image",
            "body": "harmless-missing-url.png",
        },
    }

    with caplog.at_level(logging.WARNING, logger="nio.events.misc"):
        parsed_event = _parse_room_message_event(event_source)

    captured_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert parsed_event is None
    assert "'url' is a required property" in captured_logs
    assert "harmless-missing-url.png" in captured_logs
