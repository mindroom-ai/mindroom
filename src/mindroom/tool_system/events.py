"""Tool-event formatting and metadata helpers for Matrix messages."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from agno.models.response import ToolExecution  # noqa: TC002 - used in isinstance checks

if TYPE_CHECKING:
    from collections.abc import Sequence

_TOOL_TRACE_KEY = "io.mindroom.tool_trace"
_TOOL_TRACE_VERSION = 2

_MAX_TOOL_ARGS_PREVIEW_CHARS = 1200
_MAX_TOOL_ARG_VALUE_PREVIEW_CHARS = 250
_MAX_TOOL_RESULT_DISPLAY_CHARS = 500
_TRUNCATABLE_RESULT_ITEM_FIELDS = frozenset({"body_preview"})
# Keep v2 trace indexing stable (`events[N-1]`) by not truncating event slots.
# Large-message handling is responsible for payload size fallbacks.
_MAX_TOOL_TRACE_EVENTS = 120
_TOOL_REF_ICON = "🔧"
_TOOL_PENDING_MARKER = " ⏳"
_TOOL_MARKER_PATTERN = re.compile(r"🔧 `([^`]+)` \[(\d+)\]( ⏳)?")
_VISIBLE_TOOL_MARKER_LINE_PATTERN = re.compile(r"^\s*🔧 `[^`]+` \[\d+\](?: ⏳)?\s*$")
StructuredResultDict = dict[str, object]
StructuredResultList = list[object]


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


def _as_structured_result_dict(value: object) -> StructuredResultDict | None:
    if not isinstance(value, dict):
        return None
    return cast("StructuredResultDict", value)


def _as_structured_result_list(value: object) -> StructuredResultList | None:
    if not isinstance(value, list):
        return None
    return cast("StructuredResultList", value)


def _parse_structured_result(value: object) -> StructuredResultDict | None:
    parsed = _as_structured_result_dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        parsed = _as_structured_result_dict(decoded)

    if parsed is None:
        return None

    threads = _as_structured_result_list(parsed.get("threads"))
    if not threads:
        return None
    are_threads_valid = all(
        (thread_item := _as_structured_result_dict(item)) is not None
        and isinstance(thread_item.get("thread_id"), str)
        and isinstance(thread_item.get("body_preview"), str)
        for item in threads
    )
    return parsed if are_threads_valid else None


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    if limit <= 1:
        return "…", True
    return f"{text[: limit - 1]}…", True


def _truncate_result_item_field(
    item: StructuredResultDict,
    field_name: str,
    limit: int,
) -> tuple[StructuredResultDict, bool]:
    value = item.get(field_name)
    if not isinstance(value, str):
        return item, False

    truncated_value, truncated = _truncate(value, limit)
    if not truncated:
        return item, False

    updated_item = dict(item)
    updated_item[field_name] = truncated_value
    return updated_item, True


def _fit_structured_result_item(
    preview_payload: dict[str, object],
    list_key: str,
    kept_items: list[object],
    item: object,
    limit: int,
) -> tuple[object | None, bool]:
    candidate_payload = dict(preview_payload)
    candidate_payload[list_key] = [*kept_items, item]
    if len(_to_compact_text(candidate_payload)) <= limit:
        return item, False

    item_dict = _as_structured_result_dict(item)
    if item_dict is None:
        return None, False

    best_item: object | None = None
    item_truncated = False
    for field_name in _TRUNCATABLE_RESULT_ITEM_FIELDS:
        field_value = item_dict.get(field_name)
        if not isinstance(field_value, str):
            continue

        low = 0
        high = len(field_value)
        while low <= high:
            mid = (low + high) // 2
            candidate_item, field_truncated = _truncate_result_item_field(item_dict, field_name, mid)
            candidate_payload[list_key] = [*kept_items, candidate_item]
            if len(_to_compact_text(candidate_payload)) <= limit:
                best_item = candidate_item
                item_truncated = field_truncated
                low = mid + 1
            else:
                high = mid - 1

    return best_item, item_truncated


def _drop_last_structured_result_item(preview_payload: dict[str, object], list_keys: list[str]) -> bool:
    for list_key in reversed(list_keys):
        items = _as_structured_result_list(preview_payload.get(list_key))
        if items:
            items.pop()
            return True
    return False


def _shrink_last_structured_result_item(
    preview_payload: dict[str, object],
    list_keys: list[str],
    limit: int,
) -> bool:
    for list_key in reversed(list_keys):
        items = _as_structured_result_list(preview_payload.get(list_key))
        if not items:
            continue

        last_item = _as_structured_result_dict(items[-1])
        if last_item is None:
            continue

        for field_name in _TRUNCATABLE_RESULT_ITEM_FIELDS:
            field_value = last_item.get(field_name)
            if not isinstance(field_value, str):
                continue

            low = 0
            high = len(field_value)
            best_item: object | None = None
            while low <= high:
                mid = (low + high) // 2
                candidate_item, _ = _truncate_result_item_field(last_item, field_name, mid)
                if candidate_item == last_item:
                    high = mid - 1
                    continue

                candidate_payload = dict(preview_payload)
                candidate_items = list(items)
                candidate_items[-1] = candidate_item
                candidate_payload[list_key] = candidate_items
                if len(_to_compact_text(candidate_payload)) <= limit:
                    best_item = candidate_item
                    low = mid + 1
                else:
                    high = mid - 1

            if best_item is not None:
                items[-1] = best_item
                return True

    return False


def _format_structured_result_preview(result: object) -> tuple[str, bool] | None:  # noqa: C901, PLR0912
    structured_result = _parse_structured_result(result)
    if structured_result is None:
        return None

    full_text = _to_compact_text(structured_result)
    if len(full_text) <= _MAX_TOOL_RESULT_DISPLAY_CHARS:
        return full_text, False

    list_keys = [key for key, value in structured_result.items() if _as_structured_result_list(value) is not None]
    if not list_keys:
        return None

    preview_payload: StructuredResultDict = {
        key: ([] if _as_structured_result_list(value) is not None else value)
        for key, value in structured_result.items()
    }
    truncated = False
    dropped_entries = False

    for list_key in list_keys:
        items = _as_structured_result_list(structured_result[list_key])
        assert items is not None

        kept_items: list[object] = []
        for item in items:
            preview_payload[list_key] = kept_items
            fitted_item, item_truncated = _fit_structured_result_item(
                preview_payload,
                list_key,
                kept_items,
                item,
                _MAX_TOOL_RESULT_DISPLAY_CHARS,
            )
            if fitted_item is None:
                dropped_entries = True
                truncated = True
                break
            kept_items.append(fitted_item)
            if item_truncated:
                truncated = True

        preview_payload[list_key] = kept_items
        if len(kept_items) < len(items):
            dropped_entries = True

    if dropped_entries:
        preview_payload["truncated"] = True
        while len(_to_compact_text(preview_payload)) > _MAX_TOOL_RESULT_DISPLAY_CHARS:
            if _shrink_last_structured_result_item(
                preview_payload,
                list_keys,
                _MAX_TOOL_RESULT_DISPLAY_CHARS,
            ):
                continue
            if not _drop_last_structured_result_item(preview_payload, list_keys):
                return None
        truncated = True

    preview_text = _to_compact_text(preview_payload)
    if len(preview_text) > _MAX_TOOL_RESULT_DISPLAY_CHARS:
        return None

    return preview_text, truncated


def _format_tool_result_preview(result: object) -> tuple[str, bool]:
    structured_preview = _format_structured_result_preview(result)
    if structured_preview is not None:
        return structured_preview

    result_text = _to_compact_text(result)
    return _truncate(result_text, _MAX_TOOL_RESULT_DISPLAY_CHARS)


def _neutralize_mentions(text: str) -> str:
    # Avoid accidental mentions being parsed out of tool arguments/results.
    return text.replace("@", "@\u200b")


def _tool_marker_line(tool_name: str, tool_index: int | None, *, pending: bool) -> str:
    safe_tool_name = _neutralize_mentions(tool_name).replace("`", r"\`")
    suffix = f" [{tool_index}]" if tool_index is not None else ""
    pending_suffix = _TOOL_PENDING_MARKER if pending else ""
    return f"{_TOOL_REF_ICON} `{safe_tool_name}`{suffix}{pending_suffix}"


def is_visible_tool_marker_line(line: str) -> bool:
    """Return whether one plain-text line is a Matrix-visible tool marker."""
    return _VISIBLE_TOOL_MARKER_LINE_PATTERN.fullmatch(line) is not None


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return "\n"


def ensure_visible_tool_marker_spacing(text: str) -> str:
    """Ensure visible tool-marker lines cannot become setext headings."""
    lines = text.splitlines(keepends=True)
    if not lines:
        return text

    spaced_lines: list[str] = []
    for index, line in enumerate(lines):
        spaced_lines.append(line)
        line_text = line.rstrip("\r\n")
        if not is_visible_tool_marker_line(line_text):
            continue
        next_line = lines[index + 1] if index + 1 < len(lines) else None
        if next_line is not None and next_line.strip():
            spaced_lines.append(_line_ending(line) if line.endswith(("\n", "\r")) else "\n\n")
    return "".join(spaced_lines)


def _format_tool_marker(tool_name: str, tool_index: int | None, *, pending: bool) -> str:
    return f"\n\n{_tool_marker_line(tool_name, tool_index, pending=pending)}\n\n"


def _format_tool_args(tool_args: dict[str, object]) -> tuple[str, bool]:
    parts: list[str] = []
    truncated = False
    # Preserve insertion order for easier debugging of tool-call construction.
    for key, value in tool_args.items():
        value_text = _to_compact_text(value)
        # Collapse newlines so previews stay single-line.
        value_text = value_text.replace("\n", " ")
        value_preview, value_truncated = _truncate(value_text, _MAX_TOOL_ARG_VALUE_PREVIEW_CHARS)
        if value_truncated:
            truncated = True
        parts.append(f"{key}={value_preview}")

    args_preview, args_truncated = _truncate(", ".join(parts), _MAX_TOOL_ARGS_PREVIEW_CHARS)
    return args_preview, truncated or args_truncated


def _format_tool_started(
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
        result_display, result_truncated = _format_tool_result_preview(result)
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
        result_display, truncated = _format_tool_result_preview(result)

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
    text, trace = _format_tool_started(tool_name, tool_args, tool_index=tool_index)
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
        "version": _TOOL_TRACE_VERSION,
        "events": events,
    }
    if has_truncated_content:
        payload["content_truncated"] = True

    return {_TOOL_TRACE_KEY: payload}


def render_tool_trace_for_context(events: list[ToolTraceEntry]) -> str:
    """Render trace events as text for inclusion in conversation-history prompt."""
    lines: list[str] = []
    for event in events:
        status = "completed" if event.type == "tool_call_completed" else "started"
        lines.append(f"[tool:{event.tool_name} {status}]")
        if event.args_preview:
            lines.append(f"  args: {event.args_preview}")
        if event.result_preview is not None:
            lines.append(f"  result: {event.result_preview}")
        elif status == "started":
            lines.append("  result: <not yet returned>")
        if event.truncated:
            lines.append("  (truncated)")
    return "\n".join(lines)
