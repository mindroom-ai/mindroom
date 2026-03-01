"""Direct tests for send_file_message and _upload_file_as_mxc."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.matrix.client import _upload_file_as_mxc, send_file_message

if TYPE_CHECKING:
    from pathlib import Path


def _mock_client(*, encrypted: bool = False) -> AsyncMock:
    """Create a mock nio.AsyncClient with room state."""
    client = AsyncMock(spec=nio.AsyncClient)
    room = MagicMock()
    room.encrypted = encrypted
    client.rooms = {"!room:localhost": room}
    return client


def _upload_response(content_uri: str = "mxc://localhost/abc123") -> nio.UploadResponse:
    resp = MagicMock(spec=nio.UploadResponse)
    resp.content_uri = content_uri
    return resp


class TestUploadFileAsMxc:
    """Tests for _upload_file_as_mxc."""

    @pytest.mark.asyncio
    async def test_unencrypted_upload_returns_mxc_and_info(self, tmp_path: Path) -> None:
        """Unencrypted upload should return MXC URI and info payload without file key."""
        client = _mock_client(encrypted=False)
        client.upload.return_value = (_upload_response("mxc://localhost/plain"), {})

        file = tmp_path / "doc.txt"
        file.write_text("hello", encoding="utf-8")

        mxc_uri, payload = await _upload_file_as_mxc(
            client,
            "!room:localhost",
            file,
            mimetype="text/plain",
        )

        assert mxc_uri == "mxc://localhost/plain"
        assert payload is not None
        assert "info" in payload
        assert payload["info"]["mimetype"] == "text/plain"
        assert payload["info"]["size"] == 5
        assert "file" not in payload

    @pytest.mark.asyncio
    async def test_encrypted_upload_returns_file_payload(self, tmp_path: Path) -> None:
        """Encrypted upload should include encryption keys in the file payload."""
        client = _mock_client(encrypted=True)
        client.upload.return_value = (_upload_response("mxc://localhost/enc"), {})

        file = tmp_path / "secret.bin"
        file.write_bytes(b"\x00" * 16)

        with patch(
            "mindroom.matrix.client.crypto.attachments.encrypt_attachment",
            return_value=(
                b"encrypted_bytes",
                {
                    "key": {"k": "test_key"},
                    "iv": "test_iv",
                    "hashes": {"sha256": "test_hash"},
                },
            ),
        ):
            mxc_uri, payload = await _upload_file_as_mxc(
                client,
                "!room:localhost",
                file,
                mimetype="application/octet-stream",
            )

        assert mxc_uri == "mxc://localhost/enc"
        assert payload is not None
        assert "file" in payload
        file_payload = payload["file"]
        assert file_payload["url"] == "mxc://localhost/enc"
        assert file_payload["key"] == {"k": "test_key"}
        assert file_payload["iv"] == "test_iv"
        assert file_payload["hashes"] == {"sha256": "test_hash"}
        assert file_payload["v"] == "v2"
        assert file_payload["mimetype"] == "application/octet-stream"

        # Upload should use octet-stream content type and .enc suffix
        upload_call = client.upload.call_args
        assert upload_call.kwargs["content_type"] == "application/octet-stream"
        assert upload_call.kwargs["filename"] == "secret.bin.enc"

    @pytest.mark.asyncio
    async def test_upload_returns_none_on_read_failure(self, tmp_path: Path) -> None:
        """Should return (None, None) when the file cannot be read."""
        client = _mock_client()
        missing = tmp_path / "nonexistent.txt"

        mxc_uri, payload = await _upload_file_as_mxc(
            client,
            "!room:localhost",
            missing,
            mimetype="text/plain",
        )

        assert mxc_uri is None
        assert payload is None

    @pytest.mark.asyncio
    async def test_upload_returns_none_on_upload_error(self, tmp_path: Path) -> None:
        """Should return (None, None) when the Matrix upload fails."""
        client = _mock_client()
        error = MagicMock(spec=nio.UploadError)
        client.upload.return_value = (error, {})

        file = tmp_path / "doc.txt"
        file.write_text("content", encoding="utf-8")

        mxc_uri, payload = await _upload_file_as_mxc(
            client,
            "!room:localhost",
            file,
            mimetype="text/plain",
        )

        assert mxc_uri is None
        assert payload is None


class TestSendFileMessage:
    """Tests for send_file_message."""

    @pytest.mark.asyncio
    async def test_sends_unencrypted_file_with_url(self, tmp_path: Path) -> None:
        """Unencrypted file should produce content with 'url' and no 'file' key."""
        client = _mock_client(encrypted=False)
        client.upload.return_value = (_upload_response("mxc://localhost/f1"), {})

        sent_content: dict | None = None

        async def capture_send(_client: object, _room: str, content: dict) -> str:
            nonlocal sent_content
            sent_content = content
            return "$evt:localhost"

        file = tmp_path / "report.pdf"
        file.write_bytes(b"%PDF")

        with patch("mindroom.matrix.client.send_message", side_effect=capture_send):
            event_id = await send_file_message(client, "!room:localhost", file)

        assert event_id == "$evt:localhost"
        assert sent_content is not None
        assert sent_content["msgtype"] == "m.file"
        assert sent_content["body"] == "report.pdf"
        assert sent_content["filename"] == "report.pdf"
        assert sent_content["url"] == "mxc://localhost/f1"
        assert "file" not in sent_content
        assert "m.relates_to" not in sent_content

    @pytest.mark.asyncio
    async def test_sends_encrypted_file_with_file_key(self, tmp_path: Path) -> None:
        """Encrypted file should produce content with 'file' key and no 'url'."""
        client = _mock_client(encrypted=True)
        client.upload.return_value = (_upload_response("mxc://localhost/enc1"), {})

        sent_content: dict | None = None

        async def capture_send(_client: object, _room: str, content: dict) -> str:
            nonlocal sent_content
            sent_content = content
            return "$evt:localhost"

        file = tmp_path / "secret.bin"
        file.write_bytes(b"\x00" * 8)

        with (
            patch(
                "mindroom.matrix.client.crypto.attachments.encrypt_attachment",
                return_value=(
                    b"encrypted",
                    {
                        "key": {"k": "k1"},
                        "iv": "iv1",
                        "hashes": {"sha256": "h1"},
                    },
                ),
            ),
            patch("mindroom.matrix.client.send_message", side_effect=capture_send),
        ):
            event_id = await send_file_message(client, "!room:localhost", file)

        assert event_id == "$evt:localhost"
        assert sent_content is not None
        assert "file" in sent_content
        assert sent_content["file"]["url"] == "mxc://localhost/enc1"
        assert "url" not in sent_content

    @pytest.mark.asyncio
    async def test_thread_relation_is_set(self, tmp_path: Path) -> None:
        """When thread_id is provided, m.relates_to should be set."""
        client = _mock_client(encrypted=False)
        client.upload.return_value = (_upload_response("mxc://localhost/t1"), {})

        sent_content: dict | None = None

        async def capture_send(_client: object, _room: str, content: dict) -> str:
            nonlocal sent_content
            sent_content = content
            return "$evt:localhost"

        file = tmp_path / "data.csv"
        file.write_text("a,b,c", encoding="utf-8")

        with (
            patch("mindroom.matrix.client.send_message", side_effect=capture_send),
            patch(
                "mindroom.matrix.client._latest_thread_event_id",
                new_callable=AsyncMock,
                return_value="$latest:localhost",
            ),
        ):
            event_id = await send_file_message(
                client,
                "!room:localhost",
                file,
                thread_id="$root:localhost",
            )

        assert event_id == "$evt:localhost"
        assert sent_content is not None
        relates_to = sent_content["m.relates_to"]
        assert relates_to["rel_type"] == "m.thread"
        assert relates_to["event_id"] == "$root:localhost"
        assert relates_to["is_falling_back"] is True
        assert relates_to["m.in_reply_to"]["event_id"] == "$latest:localhost"

    @pytest.mark.asyncio
    async def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        """Should return None when the file doesn't exist."""
        client = _mock_client()
        result = await send_file_message(client, "!room:localhost", tmp_path / "gone.txt")
        assert result is None

    @pytest.mark.asyncio
    async def test_caption_overrides_body(self, tmp_path: Path) -> None:
        """When caption is set, body should use it instead of filename."""
        client = _mock_client(encrypted=False)
        client.upload.return_value = (_upload_response("mxc://localhost/c1"), {})

        sent_content: dict | None = None

        async def capture_send(_client: object, _room: str, content: dict) -> str:
            nonlocal sent_content
            sent_content = content
            return "$evt:localhost"

        file = tmp_path / "report.pdf"
        file.write_bytes(b"%PDF")

        with patch("mindroom.matrix.client.send_message", side_effect=capture_send):
            await send_file_message(
                client,
                "!room:localhost",
                file,
                caption="Q4 Report",
            )

        assert sent_content is not None
        assert sent_content["body"] == "Q4 Report"
        assert sent_content["filename"] == "report.pdf"
