"""Tool-event formatting and metadata helpers for Matrix messages."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agno.models.response import ToolExecution  # noqa: TC002 - used in isinstance checks

if TYPE_CHECKING:
    from collections.abc import Sequence

TOOL_TRACE_KEY = "io.mindroom.tool_trace"
TOOL_TRACE_VERSION = 2

MAX_TOOL_ARGS_PREVIEW_CHARS = 1200
MAX_TOOL_ARG_VALUE_PREVIEW_CHARS = 250
MAX_TOOL_RESULT_DISPLAY_CHARS = 500
MAX_TOOL_TRACE_EVENTS = 120
TOOL_REF_ICON = "🔧"
TOOL_PENDING_MARKER = " ⏳"
TOOL_MARKER_PATTERN = re.compile(r"🔧 `([^`]+)` \[(\d+)\]( ⏳)?")


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


def _tool_marker_line(tool_name: str, tool_index: int | None, *, pending: bool) -> str:
    safe_tool_name = _neutralize_mentions(tool_name).replace("`", r"\`")
    suffix = f" [{tool_index}]" if tool_index is not None else ""
    pending_suffix = TOOL_PENDING_MARKER if pending else ""
    return f"{TOOL_REF_ICON} `{safe_tool_name}`{suffix}{pending_suffix}"


def _format_tool_marker(tool_name: str, tool_index: int | None, *, pending: bool) -> str:
    return f"\n\n{_tool_marker_line(tool_name, tool_index, pending=pending)}\n"


def _format_tool_args(tool_args: dict[str, object]) -> tuple[str, bool]:
    parts: list[str] = []
    truncated = False
    # Preserve insertion order for easier debugging of tool-call construction.
    for key, value in tool_args.items():
        value_text = _to_compact_text(value)
        # Collapse newlines so previews stay single-line.
        value_text = value_text.replace("\n", " ")
        value_preview, value_truncated = _truncate(value_text, MAX_TOOL_ARG_VALUE_PREVIEW_CHARS)
        if value_truncated:
            truncated = True
        parts.append(f"{key}={value_preview}")

    args_preview, args_truncated = _truncate(", ".join(parts), MAX_TOOL_ARGS_PREVIEW_CHARS)
    return args_preview, truncated or args_truncated


def format_tool_started(
    tool_name: str,
    tool_args: dict[str, object],
    tool_index: int | None = None,
) -> tuple[str, ToolTraceEntry]:
    """Format a tool-call start marker and return associated trace metadata."""
    if tool_args:
        args_preview, truncated = _format_tool_args(tool_args)
        trace = ToolTraceEntry(
            type="tool_call_started",
            tool_name=tool_name,
            args_preview=args_preview,
            truncated=truncated,
        )
    else:
        trace = ToolTraceEntry(type="tool_call_started", tool_name=tool_name)
    return _format_tool_marker(tool_name, tool_index, pending=True), trace


def format_tool_combined(
    tool_name: str,
    tool_args: dict[str, object],
    result: object | None,
    tool_index: int | None = None,
) -> tuple[str, ToolTraceEntry]:
    """Format a complete tool call marker and associated trace metadata."""
    if tool_args:
        args_preview, truncated = _format_tool_args(tool_args)
    else:
        args_preview = ""
        truncated = False

    result_display = ""
    if result is not None and result != "":
        result_text = _to_compact_text(result)
        result_display, result_truncated = _truncate(result_text, MAX_TOOL_RESULT_DISPLAY_CHARS)
        truncated = truncated or result_truncated

    block = _format_tool_marker(tool_name, tool_index, pending=False)

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
    tool_index: int,
) -> tuple[str, ToolTraceEntry]:
    """Find a pending tool marker by index and mark it completed by removing the hourglass.

    Returns (updated_text, trace_entry).
    If no pending block is found, leaves text unchanged.
    """
    if tool_index < 1:
        msg = "tool_index must be >= 1 for v2 tool markers"
        raise ValueError(msg)

    result_display = ""
    truncated = False
    if result is not None and result != "":
        result_text = _to_compact_text(result)
        result_display, truncated = _truncate(result_text, MAX_TOOL_RESULT_DISPLAY_CHARS)

    updated = accumulated_text
    pending_line = _tool_marker_line(tool_name, tool_index, pending=True)
    completed_line = _tool_marker_line(tool_name, tool_index, pending=False)
    pending_pos = updated.rfind(pending_line)
    if pending_pos >= 0:
        updated = updated[:pending_pos] + completed_line + updated[pending_pos + len(pending_line) :]
    elif completed_line in updated:
        # Duplicate completion event for the same marker; leave text unchanged.
        pass

    trace = ToolTraceEntry(
        type="tool_call_completed",
        tool_name=tool_name,
        result_preview=result_display or None,
        truncated=truncated,
    )
    return updated, trace


def format_tool_started_event(
    tool: ToolExecution | None,
    tool_index: int | None = None,
) -> tuple[str, ToolTraceEntry | None]:
    """Format an Agno tool-call start into display text and trace metadata."""
    if tool is None:
        return "", None
    tool_name = tool.tool_name or "tool"
    tool_args = {str(k): v for k, v in tool.tool_args.items()} if isinstance(tool.tool_args, dict) else {}
    text, trace = format_tool_started(tool_name, tool_args, tool_index=tool_index)
    return text, trace


def format_tool_completed_event(
    tool: ToolExecution | None,
    tool_index: int | None = None,
) -> tuple[str, ToolTraceEntry | None]:
    """Format an Agno tool-call completion into display text and trace metadata."""
    if tool is None:
        return "", None
    tool_name = tool.tool_name or "tool"
    tool_args = {str(k): v for k, v in tool.tool_args.items()} if isinstance(tool.tool_args, dict) else {}
    text, trace = format_tool_combined(tool_name, tool_args, tool.result, tool_index=tool_index)
    return text, trace


def extract_tool_completed_info(tool: ToolExecution | None) -> tuple[str, object | None] | None:
    """Extract tool name and result from a ToolExecution.

    Returns (tool_name, result) or None if tool is absent.
    Uses ``tool.result`` (actual tool output), not ``event.content``
    which Agno sets to a timing string like ``"tool() completed in 0.12s"``.
    """
    if tool is None:
        return None
    tool_name = tool.tool_name or "tool"
    return tool_name, tool.result


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
