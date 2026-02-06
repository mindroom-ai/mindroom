"""Tool-event formatting and metadata helpers for Matrix messages."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import escape
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

TOOL_TRACE_KEY = "io.mindroom.tool_trace"
TOOL_TRACE_VERSION = 1

MAX_TOOL_ARGS_PREVIEW_CHARS = 1200
MAX_TOOL_ARG_VALUE_PREVIEW_CHARS = 250
MAX_TOOL_RESULT_DISPLAY_CHARS = 500
MAX_TOOL_TRACE_EVENTS = 120


@dataclass(slots=True)
class ToolTraceEntry:
    """Normalized representation of a tool event for message metadata."""

    type: Literal["tool_call_started", "tool_call_completed"]
    tool_name: str
    args_preview: str | None = None
    result_preview: str | None = None
    truncated: bool = False


@dataclass(slots=True)
class StructuredStreamChunk:
    """Streaming chunk that carries fully-rendered content plus structured metadata."""

    content: str
    tool_trace: list[ToolTraceEntry] | None = None


def _to_compact_text(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    if limit <= 1:
        return "…", True
    return f"{text[: limit - 1]}…", True


def _neutralize_mentions(text: str) -> str:
    # Avoid accidental mentions being parsed out of tool arguments/results.
    return text.replace("@", "@\u200b")


def _format_tool_args(tool_args: dict[str, object]) -> tuple[str, bool]:
    parts: list[str] = []
    truncated = False
    # Preserve insertion order for easier debugging of tool-call construction.
    for key, value in tool_args.items():
        value_text = _to_compact_text(value)
        # Collapse newlines so the call line stays single-line.  This is
        # critical: complete_pending_tool_block uses "\n" inside a <tool>
        # block to distinguish pending (call-only) from completed (call+result).
        value_text = value_text.replace("\n", " ")
        value_preview, value_truncated = _truncate(value_text, MAX_TOOL_ARG_VALUE_PREVIEW_CHARS)
        if value_truncated:
            truncated = True
        parts.append(f"{key}={value_preview}")

    args_preview, args_truncated = _truncate(", ".join(parts), MAX_TOOL_ARGS_PREVIEW_CHARS)
    return args_preview, truncated or args_truncated


def format_tool_started(tool_name: str, tool_args: dict[str, object]) -> tuple[str, ToolTraceEntry]:
    """Format a tool-call start marker and return associated trace metadata."""
    if tool_args:
        args_preview, truncated = _format_tool_args(tool_args)
        call_display = f"{tool_name}({args_preview})"
        trace = ToolTraceEntry(
            type="tool_call_started",
            tool_name=tool_name,
            args_preview=args_preview,
            truncated=truncated,
        )
    else:
        call_display = f"{tool_name}()"
        trace = ToolTraceEntry(type="tool_call_started", tool_name=tool_name)

    safe_display = escape(_neutralize_mentions(call_display))
    return f"\n\n<tool>{safe_display}</tool>\n", trace


def format_tool_combined(
    tool_name: str,
    tool_args: dict[str, object],
    result: object | None,
) -> tuple[str, ToolTraceEntry]:
    """Format a complete tool call (call + result) as a single <tool> block."""
    if tool_args:
        args_preview, truncated = _format_tool_args(tool_args)
        call_display = f"{tool_name}({args_preview})"
    else:
        args_preview = ""
        truncated = False
        call_display = f"{tool_name}()"

    result_display = ""
    if result is not None and result != "":
        result_text = _to_compact_text(result)
        result_display, result_truncated = _truncate(result_text, MAX_TOOL_RESULT_DISPLAY_CHARS)
        truncated = truncated or result_truncated

    safe_call = escape(_neutralize_mentions(call_display))
    if result_display:
        safe_result = escape(_neutralize_mentions(result_display))
        block = f"\n\n<tool>{safe_call}\n{safe_result}</tool>\n"
    else:
        # Trailing \n signals "completed" to the frontend (no-result tool).
        # Without it the block looks identical to a pending/in-progress block.
        block = f"\n\n<tool>{safe_call}\n</tool>\n"

    trace = ToolTraceEntry(
        type="tool_call_completed",
        tool_name=tool_name,
        args_preview=args_preview or None,
        result_preview=result_display or None,
        truncated=truncated,
    )
    return block, trace


def complete_pending_tool_block(
    accumulated_text: str,
    tool_name: str,
    result: object | None,
) -> tuple[str, ToolTraceEntry]:
    """Find the last pending <tool> block for tool_name and inject the result.

    Returns (updated_text, trace_entry).
    If no pending block is found, appends a new combined block instead.
    """
    result_display = ""
    truncated = False
    if result is not None and result != "":
        result_text = _to_compact_text(result)
        result_display, truncated = _truncate(result_text, MAX_TOOL_RESULT_DISPLAY_CHARS)

    safe_result = escape(_neutralize_mentions(result_display)) if result_display else ""

    # Search backwards for the last <tool>...tool_name(...</tool> without a newline
    # (a newline inside means it already has a result).
    safe_tool_name = re.escape(escape(_neutralize_mentions(tool_name)))
    pattern = re.compile(rf"<tool>({safe_tool_name}\([^<]*)</tool>")
    matches = list(pattern.finditer(accumulated_text))

    updated = accumulated_text
    for match in reversed(matches):
        inner = match.group(1)
        # Pending blocks have no newline (just the call). Completed blocks have \n.
        if "\n" not in inner:
            # Always inject \n to mark as completed, even when result is empty.
            replacement = f"<tool>{inner}\n{safe_result}</tool>"
            updated = updated[: match.start()] + replacement + updated[match.end() :]
            break
    else:
        # No pending block found — append a standalone completed block
        if safe_result:
            escaped_name = escape(_neutralize_mentions(tool_name))
            updated += f"<tool>{escaped_name}\n{safe_result}</tool>\n"

    trace = ToolTraceEntry(
        type="tool_call_completed",
        tool_name=tool_name,
        result_preview=result_display or None,
        truncated=truncated,
    )
    return updated, trace


def format_tool_started_event(event: object) -> tuple[str, ToolTraceEntry | None]:
    """Format an Agno tool-start event into display text and trace metadata."""
    tool = getattr(event, "tool", None)
    if not tool:
        return "", None
    tool_name = getattr(tool, "tool_name", None) or "tool"
    raw_tool_args = getattr(tool, "tool_args", None)
    tool_args = {str(k): v for k, v in raw_tool_args.items()} if isinstance(raw_tool_args, dict) else {}
    text, trace = format_tool_started(tool_name, tool_args)
    return text, trace


def extract_tool_completed_info(event: object) -> tuple[str, object | None] | None:
    """Extract tool name and result from an Agno tool-completed event.

    Returns (tool_name, result) or None if event has no tool payload.
    """
    tool = getattr(event, "tool", None)
    if not tool:
        return None
    tool_name = getattr(tool, "tool_name", None) or "tool"
    content = getattr(event, "content", None)
    result = content if content is not None else getattr(tool, "result", None)
    return tool_name, result


def build_tool_trace_content(tool_trace: Sequence[ToolTraceEntry] | None) -> dict[str, object] | None:
    """Build message content payload for tool trace metadata."""
    if not tool_trace:
        return None

    trace_list = list(tool_trace)
    overflow = max(0, len(trace_list) - MAX_TOOL_TRACE_EVENTS)
    if overflow:
        trace_list = trace_list[-MAX_TOOL_TRACE_EVENTS:]

    events: list[dict[str, object]] = []
    has_truncated_content = False
    for entry in trace_list:
        event: dict[str, object] = {
            "type": entry.type,
            "tool_name": entry.tool_name,
        }
        if entry.args_preview is not None:
            event["args_preview"] = entry.args_preview
        if entry.result_preview is not None:
            event["result_preview"] = entry.result_preview
        if entry.truncated:
            event["truncated"] = True
            has_truncated_content = True
        events.append(event)

    payload: dict[str, object] = {
        "version": TOOL_TRACE_VERSION,
        "events": events,
    }
    if overflow:
        payload["events_truncated"] = overflow
    if has_truncated_content:
        payload["content_truncated"] = True

    return {TOOL_TRACE_KEY: payload}
