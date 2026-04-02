"""Tests for centralized message content extraction with large message support."""

import json
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.matrix.message_content import (
    _clear_mxc_cache,
    _download_mxc_text,
    extract_and_resolve_message,
    extract_edit_body,
    resolve_event_source_content,
    visible_body_from_event_source,
)


def _make_message_event(
    *,
    body: str,
    content: dict[str, object],
    event_id: str = "$event",
    sender: str = "@alice:example.com",
    timestamp_ms: int = 1234567890,
) -> nio.RoomMessageText:
    """Create a Matrix text event for message content tests."""
    event = nio.RoomMessageText(
        source={
            "content": content,
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": timestamp_ms,
            "type": "m.room.message",
        },
        body=body,
        formatted_body=None,
        format=None,
    )
    event.sender = sender
    return event


class TestResolvedMessageExtraction:
    """Tests for coherent visible message extraction."""

    def setup_method(self) -> None:
        """Clear cache before each test."""
        _clear_mxc_cache()

    @pytest.mark.asyncio
    async def test_extract_and_resolve_message_hydrates_v2_sidecar_content(self) -> None:
        """Regular v2 sidecars should return the canonical content and body."""
        original_content = {
            "msgtype": "m.text",
            "body": "Full response body",
            "io.mindroom.tool_trace": {"version": 1, "events": [{"tool": "shell"}]},
        }
        event = _make_message_event(
            body="Preview body",
            content={
                "msgtype": "m.file",
                "body": "Preview body",
                "info": {"mimetype": "application/json"},
                "io.mindroom.long_text": {
                    "version": 2,
                    "encoding": "matrix_event_content_json",
                },
                "url": "mxc://server/sidecar",
            },
        )
        client = AsyncMock(spec=nio.AsyncClient)
        client.download = AsyncMock(
            return_value=MagicMock(
                spec=nio.DownloadResponse,
                body=json.dumps(original_content).encode("utf-8"),
            ),
        )

        resolved = await extract_and_resolve_message(event, client)

        assert resolved["body"] == "Full response body"
        assert resolved["content"] == original_content

    @pytest.mark.asyncio
    async def test_extract_and_resolve_message_hydrates_v2_edit_wrapper(self) -> None:
        """Edit-sidecar events should resolve to the canonical outer replacement payload."""
        canonical_content = {
            "msgtype": "m.text",
            "body": "* Full edit body",
            "m.new_content": {
                "msgtype": "m.text",
                "body": "Full edit body",
                "io.mindroom.tool_trace": {"version": 1, "events": [{"tool": "shell"}]},
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
        }
        event = _make_message_event(
            body="* Preview edit",
            content={
                "msgtype": "m.text",
                "body": "* Preview edit",
                "m.new_content": {
                    "msgtype": "m.file",
                    "body": "Preview edit",
                    "info": {"mimetype": "application/json"},
                    "io.mindroom.long_text": {
                        "version": 2,
                        "encoding": "matrix_event_content_json",
                    },
                    "url": "mxc://server/edit-sidecar",
                },
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
            },
        )
        client = AsyncMock(spec=nio.AsyncClient)
        client.download = AsyncMock(
            return_value=MagicMock(
                spec=nio.DownloadResponse,
                body=json.dumps(canonical_content).encode("utf-8"),
            ),
        )

        resolved = await extract_and_resolve_message(event, client)

        assert resolved["body"] == "* Full edit body"
        assert resolved["content"] == canonical_content
        assert resolved["content"]["body"] == resolved["body"]

    @pytest.mark.asyncio
    async def test_extract_edit_body_hydrates_v2_edit_sidecar(self) -> None:
        """Edit extraction should return the canonical m.new_content from a v2 sidecar."""
        canonical_content = {
            "msgtype": "m.text",
            "body": "* Full edit body",
            "m.new_content": {
                "msgtype": "m.text",
                "body": "Full edit body",
                "io.mindroom.tool_trace": {"version": 1, "events": [{"tool": "shell"}]},
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
        }
        client = AsyncMock(spec=nio.AsyncClient)
        client.download = AsyncMock(
            return_value=MagicMock(
                spec=nio.DownloadResponse,
                body=json.dumps(canonical_content).encode("utf-8"),
            ),
        )

        body, content = await extract_edit_body(
            {
                "content": {
                    "msgtype": "m.text",
                    "body": "* Preview edit",
                    "m.new_content": {
                        "msgtype": "m.file",
                        "body": "Preview edit",
                        "info": {"mimetype": "application/json"},
                        "io.mindroom.long_text": {
                            "version": 2,
                            "encoding": "matrix_event_content_json",
                        },
                        "url": "mxc://server/edit-sidecar",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
                },
            },
            client,
        )

        assert body == "Full edit body"
        assert content == canonical_content["m.new_content"]

    @pytest.mark.asyncio
    async def test_extract_and_resolve_message_leaves_legacy_v1_preview_untouched(self) -> None:
        """Unsupported v1 sidecars should stay on the preview payload without download."""
        event = _make_message_event(
            body="Preview body",
            content={
                "msgtype": "m.file",
                "body": "Preview body",
                "io.mindroom.long_text": {
                    "version": 1,
                    "original_size": 100000,
                },
                "url": "mxc://server/legacy-sidecar",
            },
        )
        client = AsyncMock(spec=nio.AsyncClient)
        client.download = AsyncMock()

        resolved = await extract_and_resolve_message(event, client)

        assert resolved["body"] == "Preview body"
        assert resolved["content"]["body"] == "Preview body"
        client.download.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_edit_body_leaves_legacy_v1_preview_untouched(self) -> None:
        """Unsupported v1 edit sidecars should keep the preview body/content coherent."""
        client = AsyncMock(spec=nio.AsyncClient)
        client.download = AsyncMock()

        body, content = await extract_edit_body(
            {
                "content": {
                    "msgtype": "m.text",
                    "body": "* Preview edit",
                    "m.new_content": {
                        "msgtype": "m.file",
                        "body": "Preview edit",
                        "io.mindroom.long_text": {
                            "version": 1,
                            "original_size": 100000,
                        },
                        "url": "mxc://server/legacy-edit-sidecar",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
                },
            },
            client,
        )

        assert body == "Preview edit"
        assert content == {
            "msgtype": "m.file",
            "body": "Preview edit",
            "io.mindroom.long_text": {
                "version": 1,
                "original_size": 100000,
            },
            "url": "mxc://server/legacy-edit-sidecar",
        }
        client.download.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_event_source_content_hydrates_v2_edit_payload(self) -> None:
        """Event-source hydration should expose canonical edit metadata for mention routing."""
        canonical_content = {
            "msgtype": "m.text",
            "body": "* @agent full edit",
            "m.new_content": {
                "msgtype": "m.text",
                "body": "@agent full edit",
                "m.mentions": {"user_ids": ["@mindroom_agent:example.com"]},
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
        }
        client = AsyncMock(spec=nio.AsyncClient)
        client.download = AsyncMock(
            return_value=MagicMock(
                spec=nio.DownloadResponse,
                body=json.dumps(canonical_content).encode("utf-8"),
            ),
        )

        event_source = await resolve_event_source_content(
            {
                "content": {
                    "msgtype": "m.text",
                    "body": "* Preview edit",
                    "m.new_content": {
                        "msgtype": "m.file",
                        "body": "Preview edit",
                        "info": {"mimetype": "application/json"},
                        "io.mindroom.long_text": {
                            "version": 2,
                            "encoding": "matrix_event_content_json",
                        },
                        "url": "mxc://server/context-edit-sidecar",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
                },
            },
            client,
        )

        assert event_source["content"] == canonical_content

    def test_visible_body_from_event_source_prefers_visible_edit_content(self) -> None:
        """Visible-body extraction should use m.new_content when present."""
        event_source = {
            "content": {
                "msgtype": "m.text",
                "body": "* Preview edit",
                "m.new_content": {
                    "msgtype": "m.text",
                    "body": "Full edit body",
                },
            },
        }

        assert visible_body_from_event_source(event_source, "* Preview edit") == "Full edit body"


class TestDownloadMxcText:
    """Tests for _download_mxc_text function."""

    def setup_method(self) -> None:
        """Clear cache before each test."""
        _clear_mxc_cache()

    @pytest.mark.asyncio
    async def test_invalid_mxc_url(self) -> None:
        """Test handling of invalid MXC URL."""
        client = AsyncMock()
        result = await _download_mxc_text(client, "http://not-mxc-url")
        assert result is None

    @pytest.mark.asyncio
    async def test_malformed_mxc_url(self) -> None:
        """Test handling of malformed MXC URL."""
        client = AsyncMock()
        result = await _download_mxc_text(client, "mxc://no-media-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_download(self) -> None:
        """Test successful text download."""
        client = AsyncMock()
        response = MagicMock(spec=nio.DownloadResponse)
        response.body = b"Downloaded text content"
        client.download.return_value = response

        result = await _download_mxc_text(client, "mxc://server/media123")
        assert result == "Downloaded text content"
        client.download.assert_called_once_with(mxc="mxc://server/media123")

    @pytest.mark.asyncio
    async def test_download_failure(self) -> None:
        """Test handling of download failure."""
        client = AsyncMock()
        client.download.return_value = MagicMock(spec=nio.DownloadError)

        result = await _download_mxc_text(client, "mxc://server/media123")
        assert result is None
