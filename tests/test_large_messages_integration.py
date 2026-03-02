"""Integration tests for large message handling with streaming and regular messages."""

import json
from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import nio
import pytest
from agno.models.response import ToolExecution
from agno.run.agent import ToolCallCompletedEvent, ToolCallStartedEvent

from mindroom.constants import AI_RUN_METADATA_KEY
from mindroom.matrix.client import edit_message, send_message
from mindroom.matrix.large_messages import _NORMAL_MESSAGE_LIMIT, prepare_large_message
from mindroom.streaming import (
    ReplacementStreamingResponse,
    StreamingResponse,
    _StreamInputChunk,
    send_streaming_response,
)
from mindroom.tool_system.events import _TOOL_TRACE_KEY, StructuredStreamChunk, ToolTraceEntry


class MockClient:
    """Mock Matrix client for testing."""

    def __init__(self, should_upload_succeed: bool = True) -> None:
        self.rooms = {}
        self.messages_sent = []
        self.uploads: list[dict] = []
        self.should_upload_succeed = should_upload_succeed

    async def room_send(self, room_id: str, message_type: str, content: dict) -> MagicMock:  # noqa: ARG002
        """Mock sending a message."""
        self.messages_sent.append(("send", room_id, content))

        # Create a mock that passes isinstance check
        response = MagicMock(spec=nio.RoomSendResponse)
        response.event_id = f"$event_{len(self.messages_sent)}"
        return response

    async def upload(self, **kwargs) -> tuple:  # noqa: ANN003
        """Mock file upload - returns tuple like nio."""
        if not self.should_upload_succeed:
            msg = "Upload failed"
            raise Exception(msg)  # noqa: TRY002

        # Capture the uploaded data for test inspection
        data_provider = kwargs.get("data_provider")
        data = data_provider(None, None) if data_provider else None
        self.uploads.append({"data": data, **{k: v for k, v in kwargs.items() if k != "data_provider"}})

        # Create a mock UploadResponse
        response = nio.UploadResponse.from_dict({"content_uri": f"mxc://server/file_{len(self.messages_sent)}"})
        return response, None  # nio returns (response, encryption_dict)


class MockConfig:
    """Mock config for testing."""

    def __init__(self) -> None:
        self.agents = {}


# ============================================================================
# Non-Streaming Tests
# ============================================================================


@pytest.mark.asyncio
async def test_regular_message_under_limit() -> None:
    """Test that regular messages under the limit pass through unchanged."""
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
async def test_regular_message_over_limit() -> None:
    """Test that large regular messages get uploaded to MXC."""
    client = MockClient()

    # Large message (100KB)
    large_text = "x" * 100000
    content = {"body": large_text, "msgtype": "m.text"}

    await send_message(client, "!room:server", content)

    assert len(client.messages_sent) == 1
    sent_content = client.messages_sent[0][2]

    # Should be an m.file message
    assert sent_content["msgtype"] == "m.file"
    assert sent_content["filename"] == "message-content.json"

    # Should have truncated body preview
    assert len(sent_content["body"]) < len(large_text)
    assert "[Message continues in attached file]" in sent_content["body"]

    # Should have metadata
    assert "io.mindroom.long_text" in sent_content
    assert sent_content["io.mindroom.long_text"]["version"] == 2
    assert sent_content["io.mindroom.long_text"]["encoding"] == "matrix_event_content_json"
    assert sent_content["io.mindroom.long_text"]["is_complete_content"] is True

    # Should have file URL
    assert "url" in sent_content or "file" in sent_content


@pytest.mark.asyncio
async def test_edit_message_with_lower_threshold() -> None:
    """Test that edit messages use the lower size threshold."""
    client = MockClient()

    # Message that's under normal limit but over edit limit (30KB)
    text = "y" * 30000
    content = {"body": text, "msgtype": "m.text", "formatted_body": f"<p>{text}</p>"}

    await edit_message(client, "!room:server", "$original", content, text)

    assert len(client.messages_sent) == 1
    sent_content = client.messages_sent[0][2]

    # Should be truncated due to edit limit
    # For edits, check m.new_content
    assert "m.new_content" in sent_content
    assert sent_content["m.new_content"]["msgtype"] == "m.file"
    assert "io.mindroom.long_text" in sent_content["m.new_content"]
    assert len(sent_content["m.new_content"]["body"]) < len(text)


# ============================================================================
# Streaming Tests
# ============================================================================


@pytest.mark.asyncio
async def test_streaming_initial_message_under_limit() -> None:
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
async def test_streaming_initial_message_over_limit() -> None:
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
    assert sent_content["msgtype"] == "m.file"
    assert len(sent_content["body"]) < 60000
    assert "io.mindroom.long_text" in sent_content


@pytest.mark.asyncio
async def test_streaming_edit_grows_over_limit() -> None:
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
    assert "m.new_content" in edit_content
    assert edit_content["m.new_content"]["msgtype"] == "m.file"
    assert "io.mindroom.long_text" in edit_content["m.new_content"]
    assert len(edit_content["m.new_content"]["body"]) < 35000


@pytest.mark.asyncio
async def test_streaming_multiple_edits_with_growth() -> None:
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
        # These are edits, so check m.new_content
        if "m.new_content" in content:
            assert content["m.new_content"]["msgtype"] == "m.file"
            assert "io.mindroom.long_text" in content["m.new_content"], (
                f"Message {i} should have large message handling"
            )
        else:
            assert content["msgtype"] == "m.file"
            assert "io.mindroom.long_text" in content, f"Message {i} should have large message handling"


@pytest.mark.asyncio
async def test_streaming_with_thread_context() -> None:
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
    assert sent_content["msgtype"] == "m.file"
    assert "io.mindroom.long_text" in sent_content


# ============================================================================
# Edge Cases
# ============================================================================


@pytest.mark.asyncio
async def test_message_exactly_at_limit() -> None:
    """Test message that's exactly at the size limit."""
    client = MockClient()

    # Create message exactly at normal limit
    # Account for JSON overhead (~2KB) in the calculation
    text_size = _NORMAL_MESSAGE_LIMIT - 2500
    text = "e" * text_size
    content = {"body": text, "msgtype": "m.text"}

    result = await prepare_large_message(client, "!room:server", content)

    # Should pass through unchanged (just under limit)
    assert result == content
    assert "io.mindroom.long_text" not in result


@pytest.mark.asyncio
async def test_message_with_formatted_body_no_tools() -> None:
    """Large messages upload full source content JSON sidecar."""
    client = MockClient()

    # Large message with HTML body/format fields
    large_text = "f" * 100000
    large_html = f"<p>{'f' * 100000}</p>"
    content = {
        "body": large_text,
        "formatted_body": large_html,
        "msgtype": "m.text",
        "format": "org.matrix.custom.html",
    }

    result = await prepare_large_message(client, "!room:server", content)

    # Should be an m.file message with truncated preview
    assert result["msgtype"] == "m.file"
    assert len(result["body"]) < len(large_text)
    assert "io.mindroom.long_text" in result

    assert result["info"]["mimetype"] == "application/json"
    assert result["filename"] == "message-content.json"
    assert "format" not in result
    assert "formatted_body" not in result

    uploaded_data = client.uploads[0]["data"]
    uploaded_payload = json.loads(uploaded_data.read().decode("utf-8"))
    assert uploaded_payload["formatted_body"] == large_html
    assert uploaded_payload["format"] == "org.matrix.custom.html"


@pytest.mark.asyncio
async def test_large_message_with_plain_tool_markers_uploads_full_content_json() -> None:
    """Large-message sidecar stores full source content including tool trace metadata."""
    client = MockClient()

    body = "Here is the result:\n\n🔧 `web_search` [1]\n"
    body = body * 500  # Make it large enough to trigger long text
    formatted_body = "<p>Here is the result:</p>\n<p>🔧 <code>web_search</code> [1]</p>\n"
    formatted_body = formatted_body * 500

    content = {
        "body": body,
        "formatted_body": formatted_body,
        "msgtype": "m.text",
        "format": "org.matrix.custom.html",
        _TOOL_TRACE_KEY: {"version": 2, "events": [{"type": "tool_call_started", "tool_name": "web_search"}]},
    }

    result = await prepare_large_message(client, "!room:server", content)

    assert result["msgtype"] == "m.file"
    assert result["info"]["mimetype"] == "application/json"
    assert "format" not in result
    assert "formatted_body" not in result
    assert _TOOL_TRACE_KEY not in result

    # Uploaded sidecar should preserve full original content.
    uploaded_data = client.uploads[0]["data"]
    uploaded_payload = json.loads(uploaded_data.read().decode("utf-8"))
    assert uploaded_payload["formatted_body"] == formatted_body
    assert uploaded_payload[_TOOL_TRACE_KEY]["events"][0]["tool_name"] == "web_search"


@pytest.mark.asyncio
async def test_large_message_preview_uses_generic_truncation_with_plain_markers() -> None:
    """Plain-marker messages use generic truncation (no special tool-block shrinking)."""
    client = MockClient()

    # Non-tool text that should survive in full
    intro = "Important analysis result:\n" * 20  # ~520 bytes
    conclusion = "\nFinal conclusion here.\n"

    # A single huge plain-text body section that includes a visible tool marker
    tool_result = "x" * 80000
    body = f"{intro}🔧 `search` [1]\n{tool_result}\n{conclusion}"
    content = {
        "body": body,
        "msgtype": "m.text",
    }

    result = await prepare_large_message(client, "!room:server", content)

    assert result["msgtype"] == "m.file"

    # Intro text and the visible tool marker should survive in the body preview.
    assert "Important analysis result:" in result["body"]
    assert "[Message continues in attached file]" in result["body"]
    assert "🔧 `search` [1]" in result["body"]

    # Generic truncation is used now.
    assert "formatted_body" not in result


@pytest.mark.asyncio
async def test_streaming_finalize() -> None:
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
    assert sent_content["msgtype"] == "m.file"
    assert "io.mindroom.long_text" in sent_content
    # Should not have in-progress marker in final
    assert "⋯" not in sent_content["body"]


@pytest.mark.asyncio
async def test_structured_stream_chunk_adds_tool_trace_metadata() -> None:
    """Structured streaming chunks should preserve tool trace metadata in sent content."""
    client = MockClient()
    config = MockConfig()

    async def stream() -> AsyncIterator[_StreamInputChunk]:
        trace = [ToolTraceEntry(type="tool_call_started", tool_name="save_file", args_preview="file_name=a.py")]
        yield StructuredStreamChunk(content="🔧 `save_file` [1] ⏳", tool_trace=trace)

    event_id, _ = await send_streaming_response(
        client=client,
        room_id="!test:room",
        reply_to_event_id=None,
        thread_id=None,
        sender_domain="example.com",
        config=config,
        response_stream=stream(),
        streaming_cls=ReplacementStreamingResponse,
    )

    assert event_id is not None
    assert len(client.messages_sent) >= 1
    last_content = client.messages_sent[-1][2]
    target_content = last_content.get("m.new_content", last_content)
    assert _TOOL_TRACE_KEY in target_content
    assert target_content[_TOOL_TRACE_KEY]["events"][0]["tool_name"] == "save_file"


@pytest.mark.asyncio
async def test_streaming_with_extra_content_metadata() -> None:
    """Streaming sender should merge custom metadata into final event content."""
    client = MockClient()
    config = MockConfig()
    extra_content: dict[str, object] = {}

    async def stream() -> AsyncIterator[_StreamInputChunk]:
        yield "hello"
        extra_content[AI_RUN_METADATA_KEY] = {"version": 1, "usage": {"total_tokens": 10}}

    event_id, _ = await send_streaming_response(
        client=client,
        room_id="!test:room",
        reply_to_event_id=None,
        thread_id=None,
        sender_domain="example.com",
        config=config,
        response_stream=stream(),
        streaming_cls=ReplacementStreamingResponse,
        extra_content=extra_content,
    )

    assert event_id is not None
    target_content = client.messages_sent[-1][2].get("m.new_content", client.messages_sent[-1][2])
    assert target_content[AI_RUN_METADATA_KEY]["usage"]["total_tokens"] == 10


@pytest.mark.asyncio
async def test_structured_stream_chunk_does_not_drop_trace_on_stale_snapshot() -> None:
    """Older structured snapshots should not remove already-seen tool trace entries."""
    client = MockClient()
    config = MockConfig()

    trace_full = [
        ToolTraceEntry(type="tool_call_started", tool_name="save_file"),
        ToolTraceEntry(type="tool_call_completed", tool_name="save_file"),
    ]
    trace_stale = [ToolTraceEntry(type="tool_call_started", tool_name="save_file")]

    async def stream() -> AsyncIterator[_StreamInputChunk]:
        yield StructuredStreamChunk(content="🔧 `save_file` [1]", tool_trace=trace_full)
        yield StructuredStreamChunk(content="🔧 `save_file` [1]", tool_trace=trace_stale)

    event_id, _ = await send_streaming_response(
        client=client,
        room_id="!test:room",
        reply_to_event_id=None,
        thread_id=None,
        sender_domain="example.com",
        config=config,
        response_stream=stream(),
        streaming_cls=ReplacementStreamingResponse,
    )

    assert event_id is not None
    target_content = client.messages_sent[-1][2].get("m.new_content", client.messages_sent[-1][2])
    assert _TOOL_TRACE_KEY in target_content
    assert len(target_content[_TOOL_TRACE_KEY]["events"]) == 2


@pytest.mark.asyncio
async def test_replacement_streaming_preserves_text_on_tool_completion() -> None:
    """ToolCallCompletedEvent through ReplacementStreamingResponse must not wipe accumulated_text."""
    client = MockClient()
    config = MockConfig()

    tool = ToolExecution(tool_name="save_file", tool_args={"file": "a.py"}, result="ok")

    async def stream() -> AsyncIterator[_StreamInputChunk]:
        yield ToolCallStartedEvent(tool=ToolExecution(tool_name="save_file", tool_args={"file": "a.py"}))
        yield ToolCallCompletedEvent(tool=tool, content="ok")

    event_id, accumulated = await send_streaming_response(
        client=client,
        room_id="!test:room",
        reply_to_event_id=None,
        thread_id=None,
        sender_domain="example.com",
        config=config,
        response_stream=stream(),
        streaming_cls=ReplacementStreamingResponse,
    )

    assert event_id is not None
    # The accumulated text must still contain the tool marker, not be empty
    assert "save_file" in accumulated
    assert accumulated.strip() != ""


@pytest.mark.asyncio
async def test_hidden_tool_calls_coalesce_placeholder_spacing() -> None:
    """Hidden tool calls should not stack repeated blank-line placeholders."""
    client = MockClient()
    config = MockConfig()

    async def stream() -> AsyncIterator[_StreamInputChunk]:
        yield ToolCallStartedEvent(tool=ToolExecution(tool_name="first_tool", tool_args={}))
        yield ToolCallStartedEvent(tool=ToolExecution(tool_name="second_tool", tool_args={}))
        yield "Done"

    event_id, accumulated = await send_streaming_response(
        client=client,
        room_id="!test:room",
        reply_to_event_id=None,
        thread_id=None,
        sender_domain="example.com",
        config=config,
        response_stream=stream(),
        show_tool_calls=False,
    )

    assert event_id is not None
    assert accumulated == "\n\nDone"
