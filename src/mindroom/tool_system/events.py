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


@dataclass(frozen=True, slots=True)
class _AssistantToolCall:
    call_id: str | None
    tool_name: str
    tool_args: dict[str, object]


def _assistant_tool_call(raw_call: object) -> _AssistantToolCall | None:
    """Normalize one persisted assistant tool-call payload."""
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
    raw_args = function.get("arguments") or call.get("arguments")
    parsed_args: object = raw_args
    if isinstance(raw_args, str):
        try:
            parsed_args = json.loads(raw_args)
        except json.JSONDecodeError:
            parsed_args = {}
    tool_args = {str(key): value for key, value in parsed_args.items()} if isinstance(parsed_args, Mapping) else {}
    return _AssistantToolCall(call_id=call_id, tool_name=tool_name, tool_args=tool_args)


def _execution_with_call_metadata(tool: ToolExecution, call: _AssistantToolCall) -> ToolExecution:
    """Fill provider execution gaps from its persisted assistant call."""
    updates: dict[str, object] = {}
    if tool.tool_call_id is None and call.call_id is not None:
        updates["tool_call_id"] = call.call_id
    if not tool.tool_name:
        updates["tool_name"] = call.tool_name
    if not tool.tool_args and call.tool_args:
        updates["tool_args"] = call.tool_args
    return replace(tool, **updates) if updates else tool


def _assistant_tool_calls(
    messages: Sequence[Message],
) -> list[tuple[tuple[int, int], _AssistantToolCall]]:
    """Collect normalized assistant calls with stable message positions."""
    calls: list[tuple[tuple[int, int], _AssistantToolCall]] = []
    for message_index, message in enumerate(messages):
        if message.role != "assistant" or message.from_history:
            continue
        for call_index, raw_call in enumerate(message.tool_calls or ()):
            if (call := _assistant_tool_call(raw_call)) is not None:
                calls.append(((message_index, call_index), call))
    return calls


def _reserve_exact_tool_matches(
    calls: Sequence[tuple[tuple[int, int], _AssistantToolCall]],
    tools: Sequence[ToolExecution],
) -> tuple[dict[tuple[int, int], int], set[int]]:
    """Reserve execution slots for every assistant call with an exact stable ID."""
    matched_indexes: dict[tuple[int, int], int] = {}
    used_tool_indexes: set[int] = set()
    tools_by_id = {
        tool.tool_call_id: index for index, tool in reversed(list(enumerate(tools))) if tool.tool_call_id is not None
    }
    for key, call in calls:
        if call.call_id is None:
            continue
        tool_index = tools_by_id.get(call.call_id)
        if tool_index is not None and tool_index not in used_tool_indexes:
            matched_indexes[key] = tool_index
            used_tool_indexes.add(tool_index)
    return matched_indexes, used_tool_indexes


def _assistant_tool_match_indexes(
    messages: Sequence[Message],
    tools: Sequence[ToolExecution],
) -> tuple[list[tuple[tuple[int, int], _AssistantToolCall]], dict[tuple[int, int], int]]:
    """Match exact IDs first, then assign remaining current calls by occurrence."""
    calls = _assistant_tool_calls(messages)
    matched_indexes, used_tool_indexes = _reserve_exact_tool_matches(calls, tools)

    for key, call in calls:
        if key in matched_indexes:
            continue
        for tool_index, tool in enumerate(tools):
            if tool_index in used_tool_indexes or (tool.tool_name or "tool") != call.tool_name:
                continue
            if call.call_id is not None and tool.tool_call_id is not None:
                continue
            matched_indexes[key] = tool_index
            used_tool_indexes.add(tool_index)
            break
    return calls, matched_indexes


def _match_assistant_tool_calls(
    messages: Sequence[Message],
    tools: Sequence[ToolExecution],
) -> dict[tuple[int, int], tuple[str | None, ToolExecution]]:
    """Return presentation metadata for assistant calls backed by executions."""
    calls, matched_indexes = _assistant_tool_match_indexes(messages, tools)

    matches: dict[tuple[int, int], tuple[str | None, ToolExecution]] = {}
    for key, call in calls:
        tool_index = matched_indexes.get(key)
        if tool_index is None:
            continue
        tool = tools[tool_index]
        matches[key] = (call.call_id, _execution_with_call_metadata(tool, call))
    return matches


def enrich_tool_executions_from_assistant_calls(
    messages: Sequence[Message],
    tools: Sequence[ToolExecution],
) -> list[ToolExecution]:
    """Recover missing execution identity and arguments from owning assistant calls."""
    calls, matched_indexes = _assistant_tool_match_indexes(messages, tools)
    calls_by_tool_index = {
        tool_index: call for key, call in calls if (tool_index := matched_indexes.get(key)) is not None
    }
    return [
        _execution_with_call_metadata(tool, calls_by_tool_index[index]) if index in calls_by_tool_index else tool
        for index, tool in enumerate(tools)
    ]


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
    matched_calls = _match_assistant_tool_calls(messages, tools)
    transcript_parts: list[str] = []
    tool_trace: list[ToolTraceEntry] = []

    for message_index, message in enumerate(messages):
        if message.role != "assistant":
            continue
        render_message = not message.from_history and message.id not in skip_message_ids
        if render_message:
            content_text = _assistant_message_content(message)
            if content_text.strip():
                transcript_parts.append(content_text)
        if not show_tool_calls:
            continue
        for call_index, _raw_call in enumerate(message.tool_calls or ()):
            resolved_call = matched_calls.get((message_index, call_index))
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


def _merge_presentation_tool_metadata(primary: ToolExecution, supplemental: ToolExecution) -> ToolExecution:
    """Fill display-relevant provider gaps from a second copy of the same call."""
    updates: dict[str, object] = {}
    if primary.tool_call_id is None and supplemental.tool_call_id is not None:
        updates["tool_call_id"] = supplemental.tool_call_id
    if not primary.tool_name and supplemental.tool_name:
        updates["tool_name"] = supplemental.tool_name
    if not primary.tool_args and supplemental.tool_args:
        updates["tool_args"] = supplemental.tool_args
    if primary.result is None and supplemental.result is not None:
        updates["result"] = supplemental.result
    return replace(primary, **updates) if updates else primary


def merge_tool_executions_for_presentation(
    primary_tools: Sequence[ToolExecution],
    *additional_groups: Sequence[ToolExecution],
) -> list[ToolExecution]:
    """Merge overlapping execution snapshots without losing stable call identity."""
    tools: list[ToolExecution] = []
    indexes_by_id: dict[str, int] = {}

    for group in (primary_tools, *additional_groups):
        for tool in group:
            if tool.tool_call_id is not None and (existing_index := indexes_by_id.get(tool.tool_call_id)) is not None:
                tools[existing_index] = _merge_presentation_tool_metadata(tools[existing_index], tool)
                continue
            if tool.tool_call_id is not None:
                indexes_by_id[tool.tool_call_id] = len(tools)
            tools.append(tool)
    return tools


def _exact_tool_trace_matches(
    tools: Sequence[ToolExecution],
    trace: Sequence[ToolTraceEntry],
) -> list[int | None]:
    """Match stable tool-call identities without making occurrence guesses."""
    matches: list[int | None] = [None] * len(tools)
    used_trace_indexes: set[int] = set()
    for tool_index, tool in enumerate(tools):
        if tool.tool_call_id is None:
            continue
        for index, entry in enumerate(trace):
            if index not in used_trace_indexes and entry.tool_call_id == tool.tool_call_id:
                matches[tool_index] = index
                used_trace_indexes.add(index)
                break
    return matches


def _tool_trace_has_matching_legacy_evidence(tool: ToolExecution, entry: ToolTraceEntry) -> bool:
    """Return whether preview metadata links an ID-less trace slot to one execution."""
    if (tool.tool_name or "tool") != entry.tool_name:
        return False
    if tool.tool_call_id is not None and entry.tool_call_id is not None:
        return False
    _, candidate = format_tool_completed_event(tool)
    assert candidate is not None
    compared = False
    for attribute in ("args_preview", "result_preview"):
        stored_value = getattr(entry, attribute)
        if stored_value is None:
            continue
        compared = True
        if stored_value != getattr(candidate, attribute):
            return False
    return compared


def _reserve_unambiguous_legacy_evidence_matches(
    tools: Sequence[ToolExecution],
    trace: Sequence[ToolTraceEntry],
    matches: list[int | None],
    used_trace_indexes: set[int],
) -> None:
    """Reserve one-to-one preview matches before falling back to occurrence order."""
    while True:
        candidates_by_tool: dict[int, list[int]] = {}
        candidates_by_trace: dict[int, list[int]] = {}
        for tool_index, tool in enumerate(tools):
            if matches[tool_index] is not None:
                continue
            for trace_index, entry in enumerate(trace):
                if trace_index in used_trace_indexes or not _tool_trace_has_matching_legacy_evidence(tool, entry):
                    continue
                candidates_by_tool.setdefault(tool_index, []).append(trace_index)
                candidates_by_trace.setdefault(trace_index, []).append(tool_index)

        unambiguous_pairs = [
            (tool_index, trace_indexes[0])
            for tool_index, trace_indexes in candidates_by_tool.items()
            if len(trace_indexes) == 1 and len(candidates_by_trace[trace_indexes[0]]) == 1
        ]
        if not unambiguous_pairs:
            return
        for tool_index, trace_index in unambiguous_pairs:
            matches[tool_index] = trace_index
            used_trace_indexes.add(trace_index)


def _tool_trace_matches(
    tools: Sequence[ToolExecution],
    trace: Sequence[ToolTraceEntry],
    *,
    prefer_latest_tools: bool = False,
) -> list[int | None]:
    """Match exact IDs first, then remaining legacy entries by name and occurrence."""
    matches = _exact_tool_trace_matches(tools, trace)
    used_trace_indexes = {match for match in matches if match is not None}
    _reserve_unambiguous_legacy_evidence_matches(tools, trace, matches, used_trace_indexes)

    tool_indexes = range(len(tools) - 1, -1, -1) if prefer_latest_tools else range(len(tools))
    trace_indexes = range(len(trace) - 1, -1, -1) if prefer_latest_tools else range(len(trace))
    for tool_index in tool_indexes:
        if matches[tool_index] is not None:
            continue
        tool = tools[tool_index]
        tool_name = tool.tool_name or "tool"
        for index in trace_indexes:
            entry = trace[index]
            if index in used_trace_indexes or entry.tool_name != tool_name:
                continue
            if tool.tool_call_id is not None and entry.tool_call_id is not None:
                continue
            matches[tool_index] = index
            used_trace_indexes.add(index)
            break
    return matches


def partition_tools_by_trace(
    tools: Sequence[ToolExecution],
    trace: Sequence[ToolTraceEntry],
) -> tuple[list[ToolExecution], list[ToolExecution]]:
    """Partition executions into one-to-one represented and missing groups."""
    matches = _tool_trace_matches(tools, trace)
    represented = [tool for tool, match in zip(tools, matches, strict=True) if match is not None]
    missing = [tool for tool, match in zip(tools, matches, strict=True) if match is None]
    return represented, missing


def tools_not_represented_in_trace(
    tools: Sequence[ToolExecution],
    trace: Sequence[ToolTraceEntry],
) -> list[ToolExecution]:
    """Return executions without a one-to-one stable or legacy trace match."""
    return partition_tools_by_trace(tools, trace)[1]


def _filter_tool_trace_presentation(
    text: str,
    trace: Sequence[ToolTraceEntry],
    *,
    excluded_indexes: set[int],
    start_index: int,
) -> tuple[str, list[ToolTraceEntry]]:
    """Remove replayed markers and compact the remaining marker indexes."""
    filtered_trace: list[ToolTraceEntry] = []
    for offset, entry in enumerate(trace):
        pending = entry.type == "tool_call_started"
        old_marker = _tool_marker_line(entry.tool_name, start_index + offset, pending=pending)
        if offset in excluded_indexes:
            text = text.replace(old_marker, "", 1)
            continue
        new_marker = _tool_marker_line(entry.tool_name, start_index + len(filtered_trace), pending=pending)
        text = text.replace(old_marker, new_marker, 1)
        filtered_trace.append(replace(entry))
    text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text).strip("\n")
    return text, filtered_trace


def _merge_current_tool_presentation(
    text: str,
    trace: Sequence[ToolTraceEntry],
    tools: Sequence[ToolExecution],
    matches: Sequence[int | None],
    *,
    pending_tool_call_ids: set[str] | frozenset[str],
    start_index: int,
) -> tuple[str, list[ToolTraceEntry]]:
    """Insert recovery-only markers beside anchored calls in execution order."""
    token_entries: dict[str, ToolTraceEntry] = {}
    trace_tokens: list[str] = []
    for trace_index, entry in enumerate(trace):
        token = f"\x00mindroom-tool-{trace_index}\x00"
        marker = _tool_marker_line(
            entry.tool_name,
            start_index + trace_index,
            pending=entry.type == "tool_call_started",
        )
        text = text.replace(marker, token, 1)
        token_entries[token] = replace(entry)
        trace_tokens.append(token)

    tool_tokens = [trace_tokens[match] if match is not None else None for match in matches]
    for tool_index, tool in enumerate(tools):
        if tool_tokens[tool_index] is not None:
            continue
        token = f"\x00mindroom-recovery-{tool_index}\x00"
        _, trace_entry = _trace_entry_for_tool(
            tool,
            pending_tool_call_ids=pending_tool_call_ids,
            tool_index=1,
        )
        token_entries[token] = trace_entry
        next_token = next((candidate for candidate in tool_tokens[tool_index + 1 :] if candidate), None)
        previous_token = next((candidate for candidate in reversed(tool_tokens[:tool_index]) if candidate), None)
        if next_token is not None:
            text = text.replace(next_token, f"{token}\n\n{next_token}", 1)
        elif tool.tool_call_id is not None and tool.tool_call_id in pending_tool_call_ids:
            text = _append_presentation_part(text, token)
        elif previous_token is not None:
            text = text.replace(previous_token, f"{previous_token}\n\n{token}", 1)
        else:
            text = f"{token}\n\n{text}" if text else token
        tool_tokens[tool_index] = token

    ordered_tokens = sorted(token_entries, key=text.index)
    merged_trace: list[ToolTraceEntry] = []
    for token in ordered_tokens:
        entry = token_entries[token]
        marker = _tool_marker_line(
            entry.tool_name,
            start_index + len(merged_trace),
            pending=entry.type == "tool_call_started",
        )
        text = text.replace(token, marker, 1)
        merged_trace.append(entry)
    return text, merged_trace


def _complete_prior_tool_presentation(
    body: str,
    trace: list[ToolTraceEntry],
    tools: Sequence[ToolExecution],
    matches: Sequence[int | None],
    pending_tool_call_ids: set[str] | frozenset[str],
) -> tuple[str, list[ToolTraceEntry]]:
    """Complete tools already represented in the durable snapshot."""
    for tool, prior_index in zip(tools, matches, strict=True):
        if prior_index is None:
            continue
        previous_entry = trace[prior_index]
        matched_call_id = tool.tool_call_id or previous_entry.tool_call_id
        if matched_call_id is not None and matched_call_id in pending_tool_call_ids:
            continue
        marker_index = prior_index + 1
        body, _ = complete_pending_tool_block(
            body,
            previous_entry.tool_name,
            tool.result,
            marker_index,
        )
        _, completed_entry = format_tool_completed_event(tool, tool_index=marker_index)
        assert completed_entry is not None
        trace[prior_index] = replace(
            completed_entry,
            tool_call_id=completed_entry.tool_call_id or previous_entry.tool_call_id,
            tool_name=previous_entry.tool_name,
            args_preview=completed_entry.args_preview or previous_entry.args_preview,
            result_preview=completed_entry.result_preview or previous_entry.result_preview,
            truncated=completed_entry.truncated or previous_entry.truncated,
        )
    return body, trace


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
    if current_start_index < len(prior_tool_trace) + 1:
        msg = "Current tool marker index overlaps the prior trace"
        raise ValueError(msg)
    exact_prior_matches = _exact_tool_trace_matches(tools, trace)
    original_current_matches = _tool_trace_matches(tools, current_tool_trace, prefer_latest_tools=True)
    current_owned_tool_indexes = {
        tool_index
        for tool_index, current_match in enumerate(original_current_matches)
        if current_match is not None and exact_prior_matches[tool_index] is None
    }
    prior_candidate_indexes = [index for index in range(len(tools)) if index not in current_owned_tool_indexes]
    prior_candidate_matches = _tool_trace_matches([tools[index] for index in prior_candidate_indexes], trace)
    prior_matches: list[int | None] = [None] * len(tools)
    for tool_index, prior_match in zip(prior_candidate_indexes, prior_candidate_matches, strict=True):
        prior_matches[tool_index] = prior_match
    body, trace = _complete_prior_tool_presentation(body, trace, tools, prior_matches, pending_tool_call_ids)
    replayed_current_indexes = {
        current_match
        for tool, prior_match, current_match in zip(tools, prior_matches, original_current_matches, strict=True)
        if prior_match is not None
        and current_match is not None
        and tool.tool_call_id is not None
        and trace[prior_match].tool_call_id == tool.tool_call_id
    }
    current_text, filtered_current_trace = _filter_tool_trace_presentation(
        current_text,
        current_tool_trace,
        excluded_indexes=replayed_current_indexes,
        start_index=current_start_index,
    )
    current_tools = [tool for tool, prior_match in zip(tools, prior_matches, strict=True) if prior_match is None]
    current_matches = _tool_trace_matches(current_tools, filtered_current_trace, prefer_latest_tools=True)
    current_text, merged_current_trace = _merge_current_tool_presentation(
        current_text,
        filtered_current_trace,
        current_tools,
        current_matches,
        pending_tool_call_ids=pending_tool_call_ids,
        start_index=current_start_index,
    )
    body = _append_presentation_part(body, current_text)
    trace.extend(merged_current_trace)

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
