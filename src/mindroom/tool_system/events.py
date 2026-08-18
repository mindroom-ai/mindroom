"""Tool-event formatting and metadata helpers for Matrix messages."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal, cast

from agno.models.response import ToolExecution

from mindroom.redaction import redact_sensitive_data, redact_sensitive_text

if TYPE_CHECKING:
    from agno.models.message import Message

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
_StructuredResultDict = dict[str, object]
_StructuredResultList = list[object]


@dataclass(slots=True)
class ToolTraceEntry:
    """Normalized representation of a tool event for message metadata."""

    type: Literal["tool_call_started", "tool_call_completed"]
    tool_name: str
    args_preview: str | None = None
    result_preview: str | None = None
    truncated: bool = False
    tool_call_id: str | None = field(default=None, compare=False)


@dataclass(slots=True)
class StructuredStreamChunk:
    """Streaming chunk that carries fully-rendered content plus structured metadata."""

    content: str
    tool_trace: list[ToolTraceEntry] | None = None


@dataclass(frozen=True, slots=True)
class _PendingStreamingTool:
    scope_key: str
    tool_name: str
    trace_entry: ToolTraceEntry
    tool_call_id: str | None = None
    visible_tool_index: int | None = None
    visible_text: str = ""


@dataclass(slots=True)
class StreamingToolTracker:
    """Track pending and completed tool traces for one streaming response."""

    pending_tools: list[_PendingStreamingTool] = field(default_factory=list)
    completed_tools: list[ToolTraceEntry] = field(default_factory=list)

    def start(
        self,
        tool: ToolExecution | None,
        *,
        scope_key: str = "",
        tool_index: int | None = None,
    ) -> tuple[str, ToolTraceEntry | None]:
        """Record one started tool call and return its visible marker."""
        visible_text, trace_entry = format_tool_started_event(tool, tool_index=tool_index)
        if trace_entry is not None:
            self.pending_tools.append(
                _PendingStreamingTool(
                    scope_key=scope_key,
                    tool_name=trace_entry.tool_name,
                    trace_entry=trace_entry,
                    tool_call_id=_streaming_tool_call_id(tool),
                    visible_tool_index=tool_index,
                    visible_text=visible_text,
                ),
            )
        return visible_text, trace_entry

    def complete(
        self,
        tool: ToolExecution | None,
        *,
        scope_key: str = "",
    ) -> tuple[str, object | None, _PendingStreamingTool | None, ToolTraceEntry | None] | None:
        """Record one completed tool call and return the matched pending state."""
        info = extract_tool_completed_info(tool)
        if info is None:
            return None

        tool_name, result = info
        pending_pos = self._find_pending_tool_index(scope_key=scope_key, tool=tool)
        pending_tool = self.pending_tools.pop(pending_pos) if pending_pos is not None else None
        _, completed_trace = format_tool_completed_event(tool)
        if completed_trace is not None:
            self.completed_tools.append(completed_trace)
        return tool_name, result, pending_tool, completed_trace

    def update_visible_trace_entry(
        self,
        tool_trace: list[ToolTraceEntry],
        pending_tool: _PendingStreamingTool | None,
        completed_trace: ToolTraceEntry | None,
    ) -> bool:
        """Update the visible trace snapshot slot for a matched completion."""
        if pending_tool is None or pending_tool.visible_tool_index is None or completed_trace is None:
            return False
        if not 0 < pending_tool.visible_tool_index <= len(tool_trace):
            return False
        existing_entry = tool_trace[pending_tool.visible_tool_index - 1]
        existing_entry.type = "tool_call_completed"
        existing_entry.result_preview = completed_trace.result_preview
        existing_entry.truncated = existing_entry.truncated or completed_trace.truncated
        return True

    def _find_pending_tool_index(
        self,
        *,
        scope_key: str,
        tool: ToolExecution | None,
    ) -> int | None:
        call_id = _streaming_tool_call_id(tool)
        if call_id is not None:
            for pos in range(len(self.pending_tools) - 1, -1, -1):
                pending_tool = self.pending_tools[pos]
                if pending_tool.scope_key == scope_key and pending_tool.tool_call_id == call_id:
                    return pos
        info = extract_tool_completed_info(tool)
        if info is None:
            return None
        tool_name, _ = info
        for pos in range(len(self.pending_tools) - 1, -1, -1):
            pending_tool = self.pending_tools[pos]
            if (
                pending_tool.scope_key == scope_key
                and pending_tool.tool_call_id is None
                and pending_tool.tool_name == tool_name
            ):
                return pos
        return None


def _streaming_tool_call_id(tool: ToolExecution | None) -> str | None:
    if isinstance(tool, ToolExecution) and isinstance(tool.tool_call_id, str):
        return tool.tool_call_id.strip() or None
    return None


def _to_compact_text(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _as_structured_result_dict(value: object) -> _StructuredResultDict | None:
    if not isinstance(value, dict):
        return None
    return cast("_StructuredResultDict", value)


def _as_structured_result_list(value: object) -> _StructuredResultList | None:
    if not isinstance(value, list):
        return None
    return cast("_StructuredResultList", value)


def _parse_structured_result(value: object) -> _StructuredResultDict | None:
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
    item: _StructuredResultDict,
    field_name: str,
    limit: int,
) -> tuple[_StructuredResultDict, bool]:
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

    preview_payload: _StructuredResultDict = {
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
    result = redact_sensitive_data(result)
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


def _is_visible_tool_marker_line(line: str) -> bool:
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
        if not _is_visible_tool_marker_line(line_text):
            continue
        next_line = lines[index + 1] if index + 1 < len(lines) else None
        if next_line is not None and next_line.strip():
            spaced_lines.append(_line_ending(line) if line.endswith(("\n", "\r")) else "\n\n")
    return "".join(spaced_lines)


def _format_tool_marker(tool_name: str, tool_index: int | None, *, pending: bool) -> str:
    return f"\n\n{_tool_marker_line(tool_name, tool_index, pending=pending)}\n\n"


def _assistant_message_content(message: Message) -> str:
    """Return visible text without exposing multimodal blocks as a Python repr."""
    content = message.content
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    text_parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            text_parts.append(block)
        elif isinstance(block, Mapping):
            text = block.get("text")
            if isinstance(text, str):
                text_parts.append(text)
    return "".join(text_parts)


def _tool_execution_for_call(
    raw_call: object,
    tools: Sequence[ToolExecution],
    used_tool_indexes: set[int],
) -> tuple[str | None, ToolExecution] | None:
    """Resolve one persisted assistant tool call to its execution payload."""
    if not isinstance(raw_call, Mapping):
        return None
    call = cast("Mapping[str, object]", raw_call)
    call_id_value = call.get("id")
    if not isinstance(call_id_value, str):
        call_id_value = call.get("call_id")
    call_id = call_id_value if isinstance(call_id_value, str) else None
    raw_function = call.get("function")
    function = cast("Mapping[str, object]", raw_function) if isinstance(raw_function, Mapping) else {}
    raw_tool_name = function.get("name") or call.get("name")
    tool_name = raw_tool_name if isinstance(raw_tool_name, str) and raw_tool_name else "tool"
    tool: ToolExecution | None = None
    if call_id is not None:
        for index, candidate in enumerate(tools):
            if candidate.tool_call_id == call_id:
                tool = candidate
                used_tool_indexes.add(index)
                break
    if tool is None:
        for index, candidate in enumerate(tools):
            if index in used_tool_indexes or (candidate.tool_name or "tool") != tool_name:
                continue
            if call_id is not None and candidate.tool_call_id is not None:
                continue
            tool = candidate
            used_tool_indexes.add(index)
            break
    return call_id, tool or ToolExecution(tool_call_id=call_id, tool_name=tool_name)


def format_assistant_tool_transcript(
    messages: Sequence[Message],
    tools: Sequence[ToolExecution],
    *,
    pending_tool_call_ids: set[str] | frozenset[str] = frozenset(),
    start_index: int = 1,
    show_tool_calls: bool = True,
    skip_message_ids: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, list[ToolTraceEntry]]:
    """Rebuild one run's visible assistant transcript from its persisted message order."""
    used_tool_indexes: set[int] = set()
    transcript_parts: list[str] = []
    tool_trace: list[ToolTraceEntry] = []

    for message in messages:
        if message.role != "assistant":
            continue
        render_message = not message.from_history and message.id not in skip_message_ids
        if render_message:
            content_text = _assistant_message_content(message)
            if content_text.strip():
                transcript_parts.append(content_text)
        if not show_tool_calls:
            continue
        for raw_call in message.tool_calls or ():
            resolved_call = _tool_execution_for_call(raw_call, tools, used_tool_indexes)
            if resolved_call is None or not render_message:
                continue
            call_id, tool = resolved_call
            tool_index = start_index + len(tool_trace)
            if call_id is not None and call_id in pending_tool_call_ids:
                marker, trace_entry = format_tool_started_event(tool, tool_index=tool_index)
            else:
                marker, trace_entry = format_tool_completed_event(tool, tool_index=tool_index)
            transcript_parts.append(marker.strip())
            if trace_entry is not None:
                trace_entry.tool_call_id = call_id or trace_entry.tool_call_id
                tool_trace.append(trace_entry)

    return "\n\n".join(transcript_parts), tool_trace


def serialize_tool_trace(
    tool_trace: Sequence[ToolTraceEntry],
    *,
    include_tool_call_ids: bool = False,
) -> tuple[dict[str, object], ...]:
    """Serialize structured tool trace for an opaque durable response snapshot."""
    serialized: list[dict[str, object]] = []
    for entry in tool_trace:
        event: dict[str, object] = {
            "type": entry.type,
            "tool_name": entry.tool_name,
        }
        if entry.args_preview is not None:
            event["args_preview"] = redact_sensitive_text(entry.args_preview)
        if entry.result_preview is not None:
            event["result_preview"] = redact_sensitive_text(entry.result_preview)
        if entry.truncated:
            event["truncated"] = True
        if include_tool_call_ids and entry.tool_call_id is not None:
            event["tool_call_id"] = entry.tool_call_id
        serialized.append(event)
    return tuple(serialized)


def deserialize_tool_trace(stored: Sequence[Mapping[str, object]]) -> list[ToolTraceEntry]:
    """Restore structured tool trace from an opaque durable response snapshot."""
    tool_trace: list[ToolTraceEntry] = []
    for event in stored:
        event_type = event.get("type")
        tool_name = event.get("tool_name")
        args_preview = event.get("args_preview")
        result_preview = event.get("result_preview")
        truncated = event.get("truncated", False)
        tool_call_id = event.get("tool_call_id")
        if event_type not in {"tool_call_started", "tool_call_completed"}:
            msg = f"Invalid persisted tool trace type: {event_type!r}"
            raise ValueError(msg)
        if not isinstance(tool_name, str):
            msg = "Persisted tool trace entry is missing its tool name"
            raise TypeError(msg)
        if args_preview is not None and not isinstance(args_preview, str):
            msg = "Persisted tool trace args preview must be text"
            raise TypeError(msg)
        if result_preview is not None and not isinstance(result_preview, str):
            msg = "Persisted tool trace result preview must be text"
            raise TypeError(msg)
        if not isinstance(truncated, bool):
            msg = "Persisted tool trace truncated flag must be boolean"
            raise TypeError(msg)
        if tool_call_id is not None and not isinstance(tool_call_id, str):
            msg = "Persisted tool trace call ID must be text"
            raise TypeError(msg)
        tool_trace.append(
            ToolTraceEntry(
                type=cast("Literal['tool_call_started', 'tool_call_completed']", event_type),
                tool_name=tool_name,
                args_preview=args_preview,
                result_preview=result_preview,
                truncated=truncated,
                tool_call_id=tool_call_id,
            ),
        )
    return tool_trace


def visible_text_without_tool_markers(text: str) -> str:
    """Return presentation prose while ignoring display-only tool marker lines."""
    lines = text.splitlines()
    if not any(_is_visible_tool_marker_line(line) for line in lines):
        return text
    return "\n".join(line for line in lines if not _is_visible_tool_marker_line(line)).strip()


def _trace_entry_for_tool(
    tool: ToolExecution,
    *,
    pending_tool_call_ids: set[str] | frozenset[str],
    tool_index: int,
) -> tuple[str, ToolTraceEntry]:
    pending = tool.tool_call_id is not None and tool.tool_call_id in pending_tool_call_ids
    if pending:
        marker, trace_entry = format_tool_started_event(tool, tool_index=tool_index)
    else:
        marker, trace_entry = format_tool_completed_event(tool, tool_index=tool_index)
    assert trace_entry is not None
    return marker.strip(), trace_entry


def _append_presentation_part(body: str, part: str) -> str:
    """Append one known-new presentation part without rewriting durable bytes."""
    return f"{body}\n\n{part}" if body and part else body or part


def _matching_trace_index(
    trace: Sequence[ToolTraceEntry],
    tool: ToolExecution,
    used_indexes: set[int],
) -> int | None:
    """Match one execution to one trace entry by ID, then legacy name and occurrence."""
    if tool.tool_call_id is not None:
        for index, entry in enumerate(trace):
            if index not in used_indexes and entry.tool_call_id == tool.tool_call_id:
                return index
    tool_name = tool.tool_name or "tool"
    for index, entry in enumerate(trace):
        if (
            index not in used_indexes
            and entry.tool_name == tool_name
            and (tool.tool_call_id is None or entry.tool_call_id is None)
        ):
            return index
    return None


def tools_not_represented_in_trace(
    tools: Sequence[ToolExecution],
    trace: Sequence[ToolTraceEntry],
) -> list[ToolExecution]:
    """Return executions without a one-to-one stable or legacy trace match."""
    used_indexes: set[int] = set()
    missing: list[ToolExecution] = []
    for tool in tools:
        trace_index = _matching_trace_index(trace, tool, used_indexes)
        if trace_index is None:
            missing.append(tool)
        else:
            used_indexes.add(trace_index)
    return missing


def _reindex_tool_markers(
    text: str,
    trace: Sequence[ToolTraceEntry],
    *,
    old_start_index: int,
    new_start_index: int,
) -> str:
    """Shift marker indices when recovery-only tools precede ordered messages."""
    if old_start_index == new_start_index or not trace:
        return text
    replacements: dict[str, str] = {}
    for offset, entry in enumerate(trace):
        pending = entry.type == "tool_call_started"
        old_marker = _tool_marker_line(entry.tool_name, old_start_index + offset, pending=pending)
        new_marker = _tool_marker_line(entry.tool_name, new_start_index + offset, pending=pending)
        replacements[old_marker] = new_marker
    marker_pattern = re.compile("|".join(re.escape(marker) for marker in replacements))
    return marker_pattern.sub(lambda match: replacements[match.group(0)], text)


def reconcile_tool_presentation(
    *,
    prior_text: str,
    prior_tool_trace: Sequence[ToolTraceEntry],
    current_text: str,
    current_tool_trace: Sequence[ToolTraceEntry],
    tools: Sequence[ToolExecution],
    pending_tool_call_ids: set[str] | frozenset[str] = frozenset(),
    show_tool_calls: bool = True,
    current_start_index: int | None = None,
) -> tuple[str, list[ToolTraceEntry]]:
    """Reconcile a continued run against the last transport-committed presentation."""
    if not show_tool_calls:
        body = visible_text_without_tool_markers(prior_text)
        return _append_presentation_part(body, current_text), []

    body = prior_text
    trace = [replace(entry) for entry in prior_tool_trace]
    current_start_index = current_start_index or len(prior_tool_trace) + 1
    marker_index_offset = current_start_index - len(prior_tool_trace) - 1
    if marker_index_offset < 0:
        msg = "Current tool marker index overlaps the prior trace"
        raise ValueError(msg)
    used_prior_indexes: set[int] = set()
    tools_without_prior: list[ToolExecution] = []

    for tool in tools:
        prior_index = _matching_trace_index(trace, tool, used_prior_indexes)
        if prior_index is None:
            tools_without_prior.append(tool)
            continue

        used_prior_indexes.add(prior_index)
        matched_call_id = tool.tool_call_id or trace[prior_index].tool_call_id
        if matched_call_id is not None and matched_call_id in pending_tool_call_ids:
            continue
        marker_index = prior_index + 1
        tool_name = tool.tool_name or trace[prior_index].tool_name
        body, _ = complete_pending_tool_block(body, tool_name, tool.result, marker_index)
        _, completed_entry = format_tool_completed_event(tool, tool_index=marker_index)
        assert completed_entry is not None
        previous_entry = trace[prior_index]
        trace[prior_index] = replace(
            completed_entry,
            tool_call_id=completed_entry.tool_call_id or previous_entry.tool_call_id,
            tool_name=tool.tool_name or previous_entry.tool_name,
            args_preview=completed_entry.args_preview or previous_entry.args_preview,
            result_preview=completed_entry.result_preview or previous_entry.result_preview,
            truncated=completed_entry.truncated or previous_entry.truncated,
        )

    used_current_indexes: set[int] = set()
    missing_tools: list[ToolExecution] = []
    for tool in reversed(tools_without_prior):
        current_index = _matching_trace_index(current_tool_trace, tool, used_current_indexes)
        if current_index is None:
            missing_tools.append(tool)
        else:
            used_current_indexes.add(current_index)
    missing_tools.reverse()

    missing_completed = [
        tool for tool in missing_tools if tool.tool_call_id is None or tool.tool_call_id not in pending_tool_call_ids
    ]
    missing_pending = [
        tool for tool in missing_tools if tool.tool_call_id is not None and tool.tool_call_id in pending_tool_call_ids
    ]
    for tool in missing_completed:
        marker, trace_entry = _trace_entry_for_tool(
            tool,
            pending_tool_call_ids=pending_tool_call_ids,
            tool_index=len(trace) + marker_index_offset + 1,
        )
        body = _append_presentation_part(body, marker)
        trace.append(trace_entry)

    current_text = _reindex_tool_markers(
        current_text,
        current_tool_trace,
        old_start_index=current_start_index,
        new_start_index=len(trace) + marker_index_offset + 1,
    )
    body = _append_presentation_part(body, current_text)
    trace.extend(replace(entry) for entry in current_tool_trace)

    for tool in missing_pending:
        marker, trace_entry = _trace_entry_for_tool(
            tool,
            pending_tool_call_ids=pending_tool_call_ids,
            tool_index=len(trace) + marker_index_offset + 1,
        )
        body = _append_presentation_part(body, marker)
        trace.append(trace_entry)

    return body, trace


def _format_tool_args(tool_args: dict[str, object]) -> tuple[str, bool]:
    parts: list[str] = []
    truncated = False
    redacted_args = redact_sensitive_data(tool_args)
    assert isinstance(redacted_args, dict)
    # Preserve insertion order for easier debugging of tool-call construction.
    for key, value in redacted_args.items():
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
    trace.tool_call_id = _streaming_tool_call_id(tool)
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
    trace.tool_call_id = _streaming_tool_call_id(tool)
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

    events = list(serialize_tool_trace(trace_list))
    has_truncated_content = any(event.get("truncated") is True for event in events)

    payload: dict[str, object] = {
        "version": _TOOL_TRACE_VERSION,
        "events": events,
    }
    if has_truncated_content:
        payload["content_truncated"] = True

    return {_TOOL_TRACE_KEY: payload}
