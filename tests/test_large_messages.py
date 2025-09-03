"""Tests for large message handling."""
# ruff: noqa: ANN201, RUF012, ANN202, ANN003, ARG002

import pytest

from mindroom.matrix.large_messages import (
    NORMAL_MESSAGE_LIMIT,
    calculate_event_size,
    create_preview,
    is_edit_message,
    prepare_large_message,
)


def test_calculate_event_size():
    """Test event size calculation."""
    # Small message
    content = {"body": "Hello", "msgtype": "m.text"}
    size = calculate_event_size(content)
    assert size < 3000  # Small message + overhead

    # Large message
    large_text = "x" * 50000
    content = {"body": large_text, "msgtype": "m.text"}
    size = calculate_event_size(content)
    assert size > 50000
    assert size < 55000  # Text + overhead


def test_is_edit_message():
    """Test edit message detection."""
    # Regular message
    regular = {"body": "Hello", "msgtype": "m.text"}
    assert not is_edit_message(regular)

    # Edit with m.new_content
    edit1 = {
        "body": "* Hello",
        "m.new_content": {"body": "Hello", "msgtype": "m.text"},
        "msgtype": "m.text",
    }
    assert is_edit_message(edit1)

    # Edit with m.relates_to replace
    edit2 = {
        "body": "* Hello",
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$123"},
        "msgtype": "m.text",
    }
    assert is_edit_message(edit2)


def test_create_preview():
    """Test preview creation."""
    # Short text - no truncation
    short_text = "Hello world"
    preview = create_preview(short_text, 1000)
    assert preview == short_text

    # Long text - should truncate
    long_text = "Hello world. " * 1000
    preview = create_preview(long_text, 1000)
    assert len(preview.encode("utf-8")) <= 1000
    assert "[Message continues...]" in preview

    # Test natural break points
    paragraph_text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph." * 100
    preview = create_preview(paragraph_text, 500)
    assert len(preview.encode("utf-8")) <= 500
    # Should break at paragraph boundary
    assert preview.count("\n\n") >= 1 or "[Message continues...]" in preview


@pytest.mark.asyncio
async def test_prepare_large_message_passthrough():
    """Test that small messages pass through unchanged."""

    # Mock client
    class MockClient:
        rooms = {}

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
async def test_prepare_large_message_truncation():
    """Test that large messages get truncated with MXC upload."""

    # Mock client with upload
    class MockClient:
        rooms = {}

        async def upload(self, **kwargs):
            class Response:
                content_uri = "mxc://server/file123"

            return Response()

    client = MockClient()

    # Large message should get processed
    large_text = "x" * 100000  # 100KB
    content = {"body": large_text, "msgtype": "m.text"}
    result = await prepare_large_message(client, "!room:server", content)

    # Should have metadata
    assert "io.mindroom.long_text" in result
    assert "mxc" in result["io.mindroom.long_text"]
    assert result["io.mindroom.long_text"]["size"] == 100000

    # Body should be truncated
    assert len(result["body"]) < len(large_text)
    assert "[Message continues...]" in result["body"]

    # Preview should fit in limit
    assert calculate_event_size(result) <= NORMAL_MESSAGE_LIMIT


@pytest.mark.asyncio
async def test_prepare_edit_message():
    """Test that edit messages use lower size threshold."""

    # Mock client with upload
    class MockClient:
        rooms = {}

        async def upload(self, **kwargs):
            class Response:
                content_uri = "mxc://server/file456"

            return Response()

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
    assert "io.mindroom.long_text" in result

    # Both body and m.new_content should be truncated
    assert len(result["body"]) < len("* " + text)
    assert len(result["m.new_content"]["body"]) < len(text)
    assert "[Message continues...]" in result["m.new_content"]["body"]
