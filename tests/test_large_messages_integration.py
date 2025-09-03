"""Integration tests for large message handling with streaming and regular messages."""
# ruff: noqa: ANN201, ANN001, ANN003, ANN204, ARG002, PLC0415, TRY002, TRY003, EM101

from unittest.mock import MagicMock

import nio
import pytest

from mindroom.matrix.large_messages import NORMAL_MESSAGE_LIMIT, prepare_large_message
from mindroom.streaming import StreamingResponse


class MockClient:
    """Mock Matrix client for testing."""

    def __init__(self, should_upload_succeed=True):
        self.rooms = {}
        self.messages_sent = []
        self.should_upload_succeed = should_upload_succeed

    async def room_send(self, room_id, message_type, content):
        """Mock sending a message."""
        self.messages_sent.append(("send", room_id, content))

        # Create a mock that passes isinstance check
        response = MagicMock(spec=nio.RoomSendResponse)
        response.event_id = f"$event_{len(self.messages_sent)}"
        return response

    async def upload(self, **kwargs):
        """Mock file upload."""
        if not self.should_upload_succeed:
            raise Exception("Upload failed")

        class Response:
            content_uri = f"mxc://server/file_{len(self.messages_sent)}"

        return Response()


class MockConfig:
    """Mock config for testing."""

    def __init__(self):
        self.agents = {}


# ============================================================================
# Non-Streaming Tests
# ============================================================================


@pytest.mark.asyncio
async def test_regular_message_under_limit():
    """Test that regular messages under the limit pass through unchanged."""
    from mindroom.matrix.client import send_message

    client = MockClient()

    # Small message
    content = {"body": "Hello world", "msgtype": "m.text"}

    # Should pass through unchanged
    await send_message(client, "!room:server", content)

    assert len(client.messages_sent) == 1
    sent_content = client.messages_sent[0][2]
    assert sent_content["body"] == "Hello world"
    assert "io.mindroom.long_text" not in sent_content


@pytest.mark.asyncio
async def test_regular_message_over_limit():
    """Test that large regular messages get uploaded to MXC."""
    from mindroom.matrix.client import send_message

    client = MockClient()

    # Large message (100KB)
    large_text = "x" * 100000
    content = {"body": large_text, "msgtype": "m.text"}

    await send_message(client, "!room:server", content)

    assert len(client.messages_sent) == 1
    sent_content = client.messages_sent[0][2]

    # Should have truncated body
    assert len(sent_content["body"]) < len(large_text)
    assert "[Message continues...]" in sent_content["body"]

    # Should have metadata
    assert "io.mindroom.long_text" in sent_content
    assert "mxc" in sent_content["io.mindroom.long_text"]
    assert sent_content["io.mindroom.long_text"]["size"] == 100000


@pytest.mark.asyncio
async def test_edit_message_with_lower_threshold():
    """Test that edit messages use the lower size threshold."""
    from mindroom.matrix.client import edit_message

    client = MockClient()

    # Message that's under normal limit but over edit limit (30KB)
    text = "y" * 30000
    content = {"body": text, "msgtype": "m.text", "formatted_body": f"<p>{text}</p>"}

    await edit_message(client, "!room:server", "$original", content, text)

    assert len(client.messages_sent) == 1
    sent_content = client.messages_sent[0][2]

    # Should be truncated due to edit limit
    assert "io.mindroom.long_text" in sent_content
    assert len(sent_content["m.new_content"]["body"]) < len(text)


@pytest.mark.asyncio
async def test_upload_failure_fallback():
    """Test that when upload fails, we fall back to truncated message."""
    client = MockClient(should_upload_succeed=False)

    # Large message
    large_text = "z" * 100000
    content = {"body": large_text, "msgtype": "m.text"}

    # Prepare should handle the failure gracefully
    result = await prepare_large_message(client, "!room:server", content)

    # Should have truncated but no metadata
    assert len(result["body"]) < len(large_text)
    assert "[Message continues...]" in result["body"]
    assert "io.mindroom.long_text" not in result


# ============================================================================
# Streaming Tests
# ============================================================================


@pytest.mark.asyncio
async def test_streaming_initial_message_under_limit():
    """Test streaming with initial message under limit."""
    client = MockClient()
    config = MockConfig()

    streaming = StreamingResponse(
        room_id="!test:room",
        reply_to_event_id=None,
        thread_id=None,
        sender_domain="example.com",
        config=config,
    )

    # Small initial content
    await streaming.update_content("Hello streaming world", client)

    # Should trigger initial send
    assert len(client.messages_sent) == 1
    sent_content = client.messages_sent[0][2]
    assert "Hello streaming world" in sent_content["body"]
    assert "io.mindroom.long_text" not in sent_content


@pytest.mark.asyncio
async def test_streaming_initial_message_over_limit():
    """Test streaming with initial message over limit."""
    client = MockClient()
    config = MockConfig()

    streaming = StreamingResponse(
        room_id="!test:room",
        reply_to_event_id=None,
        thread_id=None,
        sender_domain="example.com",
        config=config,
    )

    # Large initial content (60KB - over normal limit)
    large_text = "a" * 60000
    streaming.accumulated_text = large_text
    streaming.last_update = float("-inf")  # Force immediate send

    await streaming._send_or_edit_message(client, is_final=True)

    # Should have sent with large message handling
    assert len(client.messages_sent) == 1
    sent_content = client.messages_sent[0][2]
    assert len(sent_content["body"]) < 60000
    assert "io.mindroom.long_text" in sent_content


@pytest.mark.asyncio
async def test_streaming_edit_grows_over_limit():
    """Test streaming where edit grows beyond limit."""
    client = MockClient()
    config = MockConfig()

    streaming = StreamingResponse(
        room_id="!test:room",
        reply_to_event_id=None,
        thread_id=None,
        sender_domain="example.com",
        config=config,
    )

    # Start with small message
    streaming.accumulated_text = "Small start"
    streaming.last_update = float("-inf")
    await streaming._send_or_edit_message(client, is_final=False)

    # Should have an event ID now
    assert streaming.event_id is not None
    assert len(client.messages_sent) == 1

    # Now grow to large message (35KB - over edit limit)
    large_text = "b" * 35000
    streaming.accumulated_text = large_text

    # This should trigger edit with large message handling
    await streaming._send_or_edit_message(client, is_final=True)

    # Should have sent an edit
    assert len(client.messages_sent) == 2
    edit_content = client.messages_sent[1][2]

    # Edit should have large message handling
    assert "io.mindroom.long_text" in edit_content
    assert len(edit_content["m.new_content"]["body"]) < 35000


@pytest.mark.asyncio
async def test_streaming_multiple_edits_with_growth():
    """Test streaming with multiple edits as message grows."""
    client = MockClient()
    config = MockConfig()

    streaming = StreamingResponse(
        room_id="!test:room",
        reply_to_event_id=None,
        thread_id=None,
        sender_domain="example.com",
        config=config,
    )

    # Simulate progressive growth
    sizes = [
        ("Initial", 100),
        ("Growing", 10000),
        ("Large", 28000),  # Over edit limit
        ("Larger", 35000),  # Way over edit limit
    ]

    for label, size in sizes:
        streaming.accumulated_text = "x" * size
        streaming.last_update = float("-inf")
        is_final = label == "Larger"

        await streaming._send_or_edit_message(client, is_final=is_final)

        # After first, should have event_id
        if label != "Initial":
            assert streaming.event_id is not None

    # Check final state
    assert len(client.messages_sent) == len(sizes)

    # Last two should have large message handling
    for i in [-2, -1]:
        content = client.messages_sent[i][2]
        assert "io.mindroom.long_text" in content, f"Message {i} should have large message handling"


@pytest.mark.asyncio
async def test_streaming_with_thread_context():
    """Test that streaming preserves thread context with large messages."""
    client = MockClient()
    config = MockConfig()

    streaming = StreamingResponse(
        room_id="!test:room",
        reply_to_event_id="$reply_to",
        thread_id="$thread_root",
        sender_domain="example.com",
        config=config,
    )

    # Large message
    large_text = "t" * 60000
    streaming.accumulated_text = large_text
    streaming.last_update = float("-inf")

    await streaming._send_or_edit_message(client, is_final=True)

    sent_content = client.messages_sent[0][2]

    # Should preserve thread context
    assert "m.relates_to" in sent_content
    # Thread relationship should be preserved
    relates_to = sent_content.get("m.relates_to", {})
    assert relates_to.get("event_id") == "$thread_root" or relates_to.get("rel_type") == "m.thread"

    # Should have large message handling
    assert "io.mindroom.long_text" in sent_content


# ============================================================================
# Edge Cases
# ============================================================================


@pytest.mark.asyncio
async def test_message_exactly_at_limit():
    """Test message that's exactly at the size limit."""
    client = MockClient()

    # Create message exactly at normal limit
    # Account for JSON overhead (~2KB) in the calculation
    text_size = NORMAL_MESSAGE_LIMIT - 2500
    text = "e" * text_size
    content = {"body": text, "msgtype": "m.text"}

    result = await prepare_large_message(client, "!room:server", content)

    # Should pass through unchanged (just under limit)
    assert result == content
    assert "io.mindroom.long_text" not in result


@pytest.mark.asyncio
async def test_message_with_formatted_body():
    """Test that formatted_body is handled correctly."""
    client = MockClient()

    # Large message with HTML
    large_text = "f" * 100000
    large_html = f"<p>{'f' * 100000}</p>"
    content = {
        "body": large_text,
        "formatted_body": large_html,
        "msgtype": "m.text",
        "format": "org.matrix.custom.html",
    }

    result = await prepare_large_message(client, "!room:server", content)

    # Should truncate both
    assert len(result["body"]) < len(large_text)
    assert len(result["formatted_body"]) < len(large_html)
    assert "io.mindroom.long_text" in result


@pytest.mark.asyncio
async def test_streaming_finalize():
    """Test that streaming finalize properly handles large messages."""
    client = MockClient()
    config = MockConfig()

    streaming = StreamingResponse(
        room_id="!test:room",
        reply_to_event_id=None,
        thread_id=None,
        sender_domain="example.com",
        config=config,
    )

    # Large content
    streaming.accumulated_text = "g" * 60000

    # Use finalize which should remove the in-progress marker
    await streaming.finalize(client)

    sent_content = client.messages_sent[0][2]

    # Should have large message handling
    assert "io.mindroom.long_text" in sent_content
    # Should not have in-progress marker in final
    assert "⋯" not in sent_content["body"]
