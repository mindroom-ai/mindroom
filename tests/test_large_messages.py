"""Tests for large message handling."""

import nio
import pytest

from mindroom.matrix.large_messages import (
    NORMAL_MESSAGE_LIMIT,
    _calculate_event_size,
    _create_preview,
    _is_edit_message,
    prepare_large_message,
)


def test_calculate_event_size() -> None:
    """Test event size calculation."""
    # Small message
    content = {"body": "Hello", "msgtype": "m.text"}
    size = _calculate_event_size(content)
    assert size < 3000  # Small message + overhead

    # Large message
    large_text = "x" * 50000
    content = {"body": large_text, "msgtype": "m.text"}
    size = _calculate_event_size(content)
    assert size > 50000
    assert size < 55000  # Text + overhead


def test__is_edit_message() -> None:
    """Test edit message detection."""
    # Regular message
    regular = {"body": "Hello", "msgtype": "m.text"}
    assert not _is_edit_message(regular)

    # Edit with m.new_content
    edit1 = {
        "body": "* Hello",
        "m.new_content": {"body": "Hello", "msgtype": "m.text"},
        "msgtype": "m.text",
    }
    assert _is_edit_message(edit1)

    # Edit with m.relates_to replace
    edit2 = {
        "body": "* Hello",
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$123"},
        "msgtype": "m.text",
    }
    assert _is_edit_message(edit2)


def test__create_preview() -> None:
    """Test preview creation."""
    # Short text - no truncation
    short_text = "Hello world"
    preview = _create_preview(short_text, 1000)
    assert preview == short_text

    # Long text - should truncate
    long_text = "Hello world. " * 1000
    preview = _create_preview(long_text, 1000)
    assert len(preview.encode("utf-8")) <= 1000
    assert "[Message continues in attached file]" in preview

    # Test natural break points
    paragraph_text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph." * 100
    preview = _create_preview(paragraph_text, 500)
    assert len(preview.encode("utf-8")) <= 500
    # Should break at paragraph boundary
    assert preview.count("\n\n") >= 1 or "[Message continues in attached file]" in preview


@pytest.mark.asyncio
async def test_prepare_large_message_passthrough() -> None:
    """Test that small messages pass through unchanged."""

    # Mock client
    class MockClient:
        rooms: dict = {}  # noqa: RUF012

    client = MockClient()

    # Small message should pass through
    small_content = {"body": "Small message", "msgtype": "m.text"}
    result = await prepare_large_message(client, "!room:server", small_content)
    assert result == small_content

    # Message just under limit should pass through
    text = "x" * (NORMAL_MESSAGE_LIMIT - 3000)
    content = {"body": text, "msgtype": "m.text"}
    result = await prepare_large_message(client, "!room:server", content)
    assert result == content


@pytest.mark.asyncio
async def test_prepare_large_message_truncation() -> None:
    """Test that large messages get truncated with MXC upload."""

    # Mock client with upload - nio returns tuple
    class MockClient:
        rooms: dict = {}  # noqa: RUF012

        async def upload(self, **kwargs) -> tuple:  # noqa: ANN003, ARG002
            # Create a mock UploadResponse
            response = nio.UploadResponse.from_dict({"content_uri": "mxc://server/file123"})
            return response, None  # nio returns (response, encryption_dict)

    client = MockClient()

    # Large message should get processed
    large_text = "x" * 100000  # 100KB
    content = {"body": large_text, "msgtype": "m.text"}
    result = await prepare_large_message(client, "!room:server", content)

    # Should be an m.file message
    assert result["msgtype"] == "m.file"
    assert "filename" in result
    assert result["filename"] == "message.txt"

    # Should have file info
    assert "info" in result or "file" in result
    if "info" in result:
        assert result["info"]["mimetype"] == "text/plain"
        assert result["info"]["size"] == 100000

    # Should have URL
    assert "url" in result or "file" in result

    # Should have custom metadata
    assert "io.mindroom.long_text" in result
    assert result["io.mindroom.long_text"]["original_size"] == 100000
    assert result["io.mindroom.long_text"]["is_complete_text"] is True

    # Body should be truncated preview
    assert len(result["body"]) < len(large_text)
    assert "[Message continues in attached file]" in result["body"]

    # Preview should fit in limit
    assert _calculate_event_size(result) <= NORMAL_MESSAGE_LIMIT


@pytest.mark.asyncio
async def test_prepare_edit_message() -> None:
    """Test that edit messages use lower size threshold."""

    # Mock client with upload - nio returns tuple
    class MockClient:
        rooms: dict = {}  # noqa: RUF012

        async def upload(self, **kwargs) -> tuple:  # noqa: ANN003, ARG002
            # Create a mock UploadResponse
            response = nio.UploadResponse.from_dict({"content_uri": "mxc://server/file456"})
            return response, None  # nio returns (response, encryption_dict)

    client = MockClient()

    # Message that's under normal limit but over edit limit
    text = "y" * 30000  # 30KB
    edit_content = {
        "body": "* " + text,
        "m.new_content": {"body": text, "msgtype": "m.text"},
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$abc"},
        "msgtype": "m.text",
    }

    result = await prepare_large_message(client, "!room:server", edit_content)

    # Should be processed due to edit limit
    # For edits, the structure is different - check for m.new_content
    assert "m.new_content" in result
    assert result["m.new_content"]["msgtype"] == "m.file"
    assert "io.mindroom.long_text" in result["m.new_content"]

    # Body should have preview
    assert len(result["body"]) < len("* " + text)
    assert "[Message continues in attached file]" in result["m.new_content"]["body"]
