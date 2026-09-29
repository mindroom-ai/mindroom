"""Pure unit tests for the OpenAI streaming wire-format protocol.

Covers chunk formatting for text deltas, tool-call deltas, finish reasons,
and SSE line framing without any agent setup.
"""

from __future__ import annotations

import json
import time

from agno.models.response import ToolExecution
from agno.run.agent import (
    RunCompletedEvent,
    RunContentEvent,
    RunErrorEvent,
    ToolCallCompletedEvent,
    ToolCallStartedEvent,
)

from mindroom.api.openai_streaming_protocol import (
    SSE_DONE,
    CompletionStreamState,
    ToolStreamState,
    extract_agent_stream_failure,
    extract_stream_text,
    finalize_pending_tools,
    format_stream_tool_event,
    new_completion_id,
    sse_chunk,
)


def _parse_sse_line(line: str) -> dict:
    assert line.startswith("data: ")
    assert line.endswith("\n\n")
    return json.loads(line.removeprefix("data: ").strip())


class TestCompletionStreamState:
    """Tests for CompletionStreamState.begin()."""

    def test_begin_allocates_openai_style_identity(self) -> None:
        """begin() produces a chatcmpl id, current timestamp, and fresh tool state."""
        before = int(time.time())
        state = CompletionStreamState.begin("general")
        after = int(time.time())

        assert state.completion_id.startswith("chatcmpl-")
        assert before <= state.created <= after
        assert state.model == "general"
        assert state.tool_state.next_tool_id == 1
        assert state.tool_state.tool_ids_by_call_id == {}

    def test_new_completion_ids_are_unique(self) -> None:
        """Each completion ID is unique."""
        assert new_completion_id() != new_completion_id()
        assert new_completion_id().startswith("chatcmpl-")


class TestSSEFraming:
    """Tests for SSE chunk assembly and line framing."""

    def test_text_delta_chunk(self) -> None:
        """A content delta is framed as one data: line with the chunk schema."""
        state = CompletionStreamState(completion_id="chatcmpl-abc", created=123, model="general")
        line = sse_chunk(state, {"content": "Hello"})

        chunk = _parse_sse_line(line)
        assert chunk["id"] == "chatcmpl-abc"
        assert chunk["object"] == "chat.completion.chunk"
        assert chunk["created"] == 123
        assert chunk["model"] == "general"
        assert chunk["choices"] == [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}]

    def test_role_announcement_chunk(self) -> None:
        """The initial role announcement carries only the assistant role."""
        state = CompletionStreamState(completion_id="chatcmpl-abc", created=123, model="general")
        chunk = _parse_sse_line(sse_chunk(state, {"role": "assistant"}))
        assert chunk["choices"][0]["delta"] == {"role": "assistant"}

    def test_finish_reason_chunk(self) -> None:
        """The terminal chunk has an empty delta and finish_reason stop."""
        state = CompletionStreamState(completion_id="chatcmpl-abc", created=123, model="general")
        chunk = _parse_sse_line(sse_chunk(state, {}, finish_reason="stop"))
        assert chunk["choices"][0]["delta"] == {}
        assert chunk["choices"][0]["finish_reason"] == "stop"

    def test_done_terminator(self) -> None:
        """The stream terminator is the literal OpenAI [DONE] frame."""
        assert SSE_DONE == "data: [DONE]\n\n"


class TestToolCallDeltas:
    """Tests for tool-call trace encoding with stable per-stream IDs."""

    def test_started_and_completed_share_one_tool_id(self) -> None:
        """A started/completed pair for the same call ID reuses the same tool ID."""
        tool_state = ToolStreamState()
        started = ToolExecution(tool_name="search", tool_args={"query": "X"}, tool_call_id="tc-1")
        completed = ToolExecution(tool_name="search", tool_args={"query": "X"}, tool_call_id="tc-1", result="3 results")

        start_text = format_stream_tool_event(ToolCallStartedEvent(tool=started), tool_state)
        done_text = format_stream_tool_event(ToolCallCompletedEvent(tool=completed), tool_state)

        assert start_text == '<tool id="1" state="start">search(query=X)</tool>'
        assert done_text == '<tool id="1" state="done">search(query=X)\n3 results</tool>'
        assert tool_state.tool_ids_by_call_id == {}

    def test_parallel_tools_get_distinct_ids(self) -> None:
        """Distinct call IDs allocate increasing tool IDs."""
        tool_state = ToolStreamState()
        first = ToolExecution(tool_name="search", tool_args={}, tool_call_id="tc-1")
        second = ToolExecution(tool_name="fetch", tool_args={}, tool_call_id="tc-2")

        first_text = format_stream_tool_event(ToolCallStartedEvent(tool=first), tool_state)
        second_text = format_stream_tool_event(ToolCallStartedEvent(tool=second), tool_state)

        assert first_text is not None
        assert second_text is not None
        assert '<tool id="1" state="start">' in first_text
        assert '<tool id="2" state="start">' in second_text

    def test_completed_without_start_allocates_new_id(self) -> None:
        """A completion that was never announced still gets a usable tool ID."""
        tool_state = ToolStreamState()
        completed = ToolExecution(tool_name="search", tool_args={}, tool_call_id="tc-9", result="ok")
        text = format_stream_tool_event(ToolCallCompletedEvent(tool=completed), tool_state)
        assert text is not None
        assert '<tool id="1" state="done">' in text

    def test_tool_payload_is_escaped(self) -> None:
        """Tool names, args, and results are XML-escaped in the inline trace."""
        tool_state = ToolStreamState()
        completed = ToolExecution(
            tool_name="search",
            tool_args={"query": "</tool><b>pwn</b>"},
            tool_call_id="tc-1",
            result="</tool><i>boom</i>",
        )
        text = format_stream_tool_event(ToolCallCompletedEvent(tool=completed), tool_state)
        assert text is not None
        assert "</tool><b>pwn</b>" not in text
        assert "&lt;/tool&gt;&lt;b&gt;pwn&lt;/b&gt;" in text
        assert "&lt;/tool&gt;&lt;i&gt;boom&lt;/i&gt;" in text

    def test_non_tool_event_is_skipped(self) -> None:
        """Events that are not tool calls produce no tool trace."""
        assert format_stream_tool_event(RunCompletedEvent(content="done"), ToolStreamState()) is None

    def test_finalize_pending_tools_closes_interrupted_calls(self) -> None:
        """Started-but-never-completed calls are closed with an interrupted done tag."""
        tool_state = ToolStreamState()
        started = ToolExecution(tool_name="search", tool_args={}, tool_call_id="tc-1")
        format_stream_tool_event(ToolCallStartedEvent(tool=started), tool_state)

        pending = finalize_pending_tools(tool_state)
        assert pending == '<tool id="1" state="done">(interrupted)</tool>'
        assert tool_state.tool_ids_by_call_id == {}
        assert finalize_pending_tools(tool_state) is None


class TestStreamTextExtraction:
    """Tests for extracting wire text from core stream chunk events."""

    def test_content_event_text(self) -> None:
        """RunContentEvent content streams through as text."""
        assert extract_stream_text(RunContentEvent(content="Hello"), ToolStreamState()) == "Hello"

    def test_plain_string_passthrough(self) -> None:
        """Cached full responses (plain strings) pass through unchanged."""
        assert extract_stream_text("cached response", ToolStreamState()) == "cached response"

    def test_tool_event_becomes_tool_trace(self) -> None:
        """Tool events are encoded as inline tool traces."""
        started = ToolExecution(tool_name="search", tool_args={}, tool_call_id="tc-1")
        text = extract_stream_text(ToolCallStartedEvent(tool=started), ToolStreamState())
        assert text == '<tool id="1" state="start">search()</tool>'

    def test_completed_event_yields_nothing(self) -> None:
        """RunCompletedEvent is not a text delta."""
        assert extract_stream_text(RunCompletedEvent(content="final"), ToolStreamState()) is None


class TestAgentStreamFailure:
    """Tests for terminal failure detection on agent stream chunks."""

    def test_run_error_event(self) -> None:
        """RunErrorEvent surfaces its content as the failure text."""
        assert extract_agent_stream_failure(RunErrorEvent(content="boom")) == "boom"

    def test_run_error_event_without_content(self) -> None:
        """RunErrorEvent without content falls back to a generic message."""
        assert extract_agent_stream_failure(RunErrorEvent(content=None)) == "Agent execution failed."

    def test_error_string_chunk(self) -> None:
        """Error-formatted string chunks are treated as terminal failures."""
        assert extract_agent_stream_failure("❌ Something broke") == "❌ Something broke"

    def test_normal_chunks_are_not_failures(self) -> None:
        """Ordinary content never reads as a failure."""
        assert extract_agent_stream_failure("All good") is None
        assert extract_agent_stream_failure(RunContentEvent(content="hi")) is None
