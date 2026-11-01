"""Tool-event formatting and metadata helpers for Matrix messages."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal, cast

from mindroom.redaction import redact_sensitive_data, redact_sensitive_text

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from mindroom.llm.events import ReportedCall, ToolCallOrigin
    from mindroom.llm.runs import ToolExecution

_TOOL_TRACE_KEY = "io.mindroom.tool_trace"
_TOOL_TRACE_VERSION = 2

_MAX_TOOL_ARGS_PREVIEW_CHARS = 1200
_MAX_TOOL_ARG_VALUE_PREVIEW_CHARS = 250
_MAX_TOOL_RESULT_DISPLAY_CHARS = 500
_TRUNCATABLE_RESULT_ITEM_FIELDS = frozenset({"body_preview"})
_TOOL_REF_ICON = "🔧"
_TOOL_PENDING_MARKER = " ⏳"
_VISIBLE_TOOL_MARKER_LINE_PATTERN = re.compile(r"^\s*🔧 `[^`]+` \[\d+\](?: ⏳)?\s*$")
# Bounded so an index in untrusted text never reaches int()'s digit limit.
_VISIBLE_TOOL_MARKER_INDEX_PATTERN = re.compile(r"(?<= \[)\d{1,9}(?=\](?: ⏳)?\s*$)")
_VISIBLE_TOOL_MARKER_SEPARATOR_PATTERN = re.compile(r"^\s{0,3}---\s*$")
_StructuredResultDict = dict[str, object]
_StructuredResultList = list[object]
_MALFORMED_TRACE = "Durable tool trace contains a malformed event"


@dataclass(slots=True)
class ToolTraceEntry:
    """Normalized representation of a tool event for message metadata."""

    type: Literal["tool_call_started", "tool_call_completed"]
    tool_name: str
    args_preview: str | None = None
    result_preview: str | None = None
    truncated: bool = False
    # Internal continuation identity; public Matrix metadata omits these fields.
    tool_call_id: str | None = field(default=None, compare=False)
    # The team member whose slot shows the call, by config name; None for a team leader's call and any call outside a
    # team.
    member_id: str | None = field(default=None, compare=False)
    parent_bash_call_id: str | None = field(default=None, compare=False)
    toolkit_name: str | None = field(default=None, compare=False)
    # Whether the completed call failed, as its run recorded it, so an interrupted replay can say so. Internal like
    # the identity above: the reply's records keep it, and public Matrix metadata omits it.
    is_error: bool = field(default=False, compare=False)
    # The call as its tool event reported it, with its full arguments and result, which an interrupted replay renders.
    # Never serialized, so Matrix metadata and the reply's records keep only the previews, and a restored entry has none.
    call: ReportedCall | None = field(default=None, compare=False, repr=False)


@dataclass(slots=True)
class StructuredStreamChunk:
    """Streaming chunk that carries fully-rendered content plus structured metadata."""

    content: str
    tool_trace: list[ToolTraceEntry] | None = None
    presentation_state: dict[str, object] | None = None
    # The latest output of each running call, by call ID, which only live frames show.
    tool_progress: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class FailedReply:
    """The last chunk of a failed response turn's stream: the text shown in place of an answer, and its failure.

    The turn driver yields it once it recorded the failure, and the two refusals before any turn runs yield it too (an
    agent turn that names a configured team, and a team turn whose members cannot be resolved), so a reader of the
    stream tells a failed turn's reply from its answer by type alone.
    """

    text: str
    error: Exception


@dataclass(frozen=True, slots=True)
class _PendingStreamingTool:
    member_id: str | None
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
        call: ReportedCall,
        *,
        member_id: str | None = None,
        tool_index: int | None = None,
        origin: ToolCallOrigin | None = None,
    ) -> tuple[str, ToolTraceEntry | None]:
        """Record one started tool call and return its visible marker, or no trace entry for a call already pending.

        ``member_id`` names the team member whose run made the call, None for a leader's or another reply's.
        """
        if any(
            pending.member_id == member_id and pending.tool_call_id == call.tool_call_id
            for pending in self.pending_tools
        ):
            return "", None
        visible_text, trace_entry = format_tool_started_event(call, tool_index=tool_index, origin=origin)
        trace_entry.member_id = member_id
        self.pending_tools.append(
            _PendingStreamingTool(
                member_id=member_id,
                tool_name=trace_entry.tool_name,
                trace_entry=trace_entry,
                tool_call_id=call.tool_call_id,
                visible_tool_index=tool_index,
                visible_text=visible_text,
            ),
        )
        return visible_text, trace_entry

    def complete(
        self,
        call: ReportedCall,
        *,
        member_id: str | None = None,
        origin: ToolCallOrigin | None = None,
    ) -> tuple[_PendingStreamingTool | None, ToolTraceEntry]:
        """Record one completed tool call; return the pending slot it completes, if any, and its completed trace."""
        pending_pos = self._find_pending_tool_index(member_id=member_id, call=call)
        pending_tool = self.pending_tools.pop(pending_pos) if pending_pos is not None else None
        completed_trace = format_tool_completed_event(call, origin=origin)
        completed_trace.member_id = member_id
        self.completed_tools.append(completed_trace)
        return pending_tool, completed_trace

    def update_visible_trace_entry(
        self,
        tool_trace: list[ToolTraceEntry],
        pending_tool: _PendingStreamingTool,
        completed_trace: ToolTraceEntry,
    ) -> bool:
        """Update the visible trace snapshot slot for a matched completion."""
        index = pending_tool.visible_tool_index
        if index is None or not 0 < index <= len(tool_trace):
            return False
        existing_entry = tool_trace[index - 1]
        existing_entry.type = "tool_call_completed"
        existing_entry.result_preview = completed_trace.result_preview
        existing_entry.truncated = existing_entry.truncated or completed_trace.truncated
        existing_entry.is_error = completed_trace.is_error
        existing_entry.call = completed_trace.call
        return True

    def restore_pending(self, tool_trace: Sequence[ToolTraceEntry]) -> None:
        """Restore only exact pending slots from a durable presentation snapshot."""
        for tool_index, trace_entry in enumerate(tool_trace, start=1):
            if trace_entry.type == "tool_call_completed":
                self.completed_tools.append(trace_entry)
                continue
            self.pending_tools.append(
                _PendingStreamingTool(
                    member_id=trace_entry.member_id,
                    tool_name=trace_entry.tool_name,
                    trace_entry=trace_entry,
                    tool_call_id=trace_entry.tool_call_id,
                    visible_tool_index=tool_index,
                ),
            )

    def sync_visible_indices(self, tool_trace: Sequence[ToolTraceEntry]) -> None:
        """Synchronize pending completion targets after visible trace reordering."""
        visible_indices = {id(trace_entry): tool_index for tool_index, trace_entry in enumerate(tool_trace, start=1)}
        synchronized: list[_PendingStreamingTool] = []
        for pending_tool in self.pending_tools:
            tool_index = visible_indices.get(id(pending_tool.trace_entry))
            if tool_index is None:
                msg = "Pending tool is missing from its visible trace"
                raise RuntimeError(msg)
            synchronized.append(replace(pending_tool, visible_tool_index=tool_index))
        self.pending_tools = synchronized

    def _find_pending_tool_index(self, *, member_id: str | None, call: ReportedCall) -> int | None:
        for pos in range(len(self.pending_tools) - 1, -1, -1):
            pending_tool = self.pending_tools[pos]
            if pending_tool.member_id == member_id and pending_tool.tool_call_id == call.tool_call_id:
                return pos
        # A slot restored from a trace that kept no call ids is matched by its tool's name.
        for pos in range(len(self.pending_tools) - 1, -1, -1):
            pending_tool = self.pending_tools[pos]
            if (
                pending_tool.member_id == member_id
                and pending_tool.tool_call_id is None
                and pending_tool.tool_name == call.tool_name
            ):
                return pos
        return None


@dataclass(slots=True)
class CollectedStreamPresentation:
    """Ordered stream presentation collected without incremental delivery."""

    show_tool_calls: bool
    response_text: str = ""
    tool_trace: list[ToolTraceEntry] = field(default_factory=list)
    track_hidden_tools: bool = False
    canonical_final_body_candidate: str | None = None
    # The structured state of a team document the stream showed whole, which a resume restores.
    document_state: dict[str, object] | None = None
    # Whether a text chunk replaces the collected text, as in a team's stream of whole documents, instead of continuing it.
    replaces_text: bool = False
    # The latest output of each running call, by call ID, which a live reply shows and nothing stores.
    tool_progress: dict[str, str] = field(default_factory=dict, init=False)
    tool_tracker: StreamingToolTracker = field(default_factory=StreamingToolTracker, init=False)
    separate_next_text: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        """Restore pending tool identity from the durable trace."""
        self.tool_tracker.restore_pending(self.tool_trace)
        self.separate_next_text = bool(self.response_text and self.tool_tracker.pending_tools)

    def show_text(self, content: str) -> None:
        """Apply one text chunk: it replaces the collected text in a stream of whole documents, else continues it."""
        if self.replaces_text:
            self.response_text = content
            return
        self.append_text(content)

    def append_text(self, content: object | None) -> None:
        """Append one provider content delta when it is non-empty."""
        self.response_text = append_stream_text(
            self.response_text,
            content,
            separate=self.separate_next_text,
        )
        if content:
            self.separate_next_text = False

    def start_tool(self, call: ReportedCall, *, origin: ToolCallOrigin | None = None) -> None:
        """Record one tool start and append its marker when visible."""
        if not self.show_tool_calls and not self.track_hidden_tools:
            return
        tool_index = len(self.tool_trace) + 1
        text, trace_entry = self.tool_tracker.start(call, tool_index=tool_index, origin=origin)
        if trace_entry is None:
            return
        self.tool_trace.append(trace_entry)
        if self.show_tool_calls:
            self.response_text += text
        elif self.response_text:
            self.separate_next_text = True

    def show_tool_progress(self, tool_call_id: str, text: str) -> None:
        """Keep a running call's latest output for the live reply until the call completes."""
        self.tool_progress[tool_call_id] = text

    def complete_tool(self, call: ReportedCall, *, origin: ToolCallOrigin | None = None) -> None:
        """Complete the exact pending slot represented by a provider event."""
        self.tool_progress.pop(call.tool_call_id, None)
        if not self.show_tool_calls and not self.track_hidden_tools:
            return
        pending_tool, completed_trace = self.tool_tracker.complete(call, origin=origin)
        if pending_tool is None or pending_tool.visible_tool_index is None:
            return
        if self.show_tool_calls:
            self.response_text = complete_pending_tool_block(
                self.response_text,
                call.tool_name,
                tool_index=pending_tool.visible_tool_index,
            )
        self.tool_tracker.update_visible_trace_entry(self.tool_trace, pending_tool, completed_trace)

    def show_document(self, chunk: StructuredStreamChunk) -> None:
        """Replace the collected presentation with a document the stream sent whole; one without a trace keeps it."""
        self.response_text = chunk.content
        if chunk.tool_trace is not None:
            self.tool_trace = list(chunk.tool_trace)
        self.document_state = chunk.presentation_state

    def final_text(self) -> str:
        """Return collected deltas, falling back to the provider's canonical final body."""
        return self.response_text or self.canonical_final_body_candidate or ""

    async def publish(self, progress: Callable[[StructuredStreamChunk], Awaitable[None]] | None) -> None:
        """Show the current ordered presentation in a live reply, when a progress publisher is attached."""
        if progress is not None:
            await progress(
                StructuredStreamChunk(
                    content=self.response_text,
                    tool_trace=self.tool_trace,
                    tool_progress=dict(self.tool_progress),
                ),
            )


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


def is_visible_tool_marker_line(line: str) -> bool:
    """Return whether one plain-text line is a Matrix-visible tool marker."""
    return _VISIBLE_TOOL_MARKER_LINE_PATTERN.fullmatch(line) is not None


def strip_visible_tool_markers(text: str) -> str:
    """Remove display-only tool marker lines before text re-enters model context.

    The text around each removed marker stays one blank line apart, as a hidden tool call leaves it; a marker that
    opens or ends the text leaves nothing. A markdown separator (``---``) after a marker goes with it. Text without
    markers is returned unchanged.
    """
    lines = text.splitlines()
    if not any(is_visible_tool_marker_line(line) for line in lines):
        return text
    pieces: list[list[str]] = [[]]
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not is_visible_tool_marker_line(line):
            pieces[-1].append(line)
            continue
        after_spacer = index
        while after_spacer < len(lines) and not lines[after_spacer].strip():
            after_spacer += 1
        if after_spacer < len(lines) and _VISIBLE_TOOL_MARKER_SEPARATOR_PATTERN.fullmatch(lines[after_spacer]):
            index = after_spacer + 1
        pieces.append([])
    segments = ("\n".join(piece).strip("\n") for piece in pieces)
    return "\n\n".join(segment for segment in segments if segment.strip()).rstrip()


def message_tool_trace(content: Mapping[str, object]) -> list[ToolTraceEntry] | None:
    """Return the tool trace stored on one Matrix message, or None when it carries none."""
    payload = content.get(_TOOL_TRACE_KEY)
    if not isinstance(payload, dict):
        return None
    events = cast("dict[str, object]", payload).get("events")
    if not isinstance(events, list):
        return []
    try:
        return deserialize_tool_trace(
            cast("list[Mapping[str, object]]", [event for event in events if isinstance(event, dict)]),
        )
    except RuntimeError:
        # Any room member can send this key, so a malformed trace reads as one without entries.
        return []


def has_tool_trace(content: Mapping[str, object]) -> bool:
    """Whether a message carries a tool trace, which proves its marker lines are MindRoom chrome."""
    return isinstance(content.get(_TOOL_TRACE_KEY), dict)


def body_without_tool_markers(body: str, content: Mapping[str, object]) -> str:
    """Remove marker lines a stored tool trace proves are MindRoom chrome; leave other text alone."""
    if not has_tool_trace(content):
        return body
    return strip_visible_tool_markers(body)


def _render_tool_tag(entry: ToolTraceEntry) -> str:
    """Render one tool call as a tag for judges, which read conversations as data.

    Values stay verbatim: any escaping changes the already redacted previews, and
    the judges' redaction check would then rewrite them and refuse the request.
    """
    attrs = [f'name="{entry.tool_name}"']
    if entry.args_preview:
        attrs.append(f'input="{entry.args_preview}"')
    if entry.result_preview:
        attrs.append(f'output="{entry.result_preview}"')
    if entry.truncated:
        attrs.append('truncated="true"')
    return f"<tool {' '.join(attrs)}/>"


def body_with_tool_tags(body: str, trace: Sequence[ToolTraceEntry]) -> str:
    """Replace each marker with its trace entry's tag; a marker without an entry, or repeating one, is dropped."""
    lines: list[str] = []
    rendered: set[int] = set()
    for line in body.splitlines():
        if not is_visible_tool_marker_line(line):
            lines.append(line)
            continue
        index_match = _VISIBLE_TOOL_MARKER_INDEX_PATTERN.search(line)
        index = int(index_match.group()) if index_match is not None else 0
        # Each entry renders once, so repeated markers cannot multiply a large preview.
        if 1 <= index <= len(trace) and index not in rendered:
            rendered.add(index)
            lines.append(_render_tool_tag(trace[index - 1]))
    return "\n".join(lines)


def _line_ending(line: str) -> str:
    return "\r\n" if line.endswith("\r\n") else "\n"


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


def remap_visible_tool_marker_indices(text: str, index_map: Mapping[int, int]) -> str:
    """Remap standalone visible marker indices while preserving all other text."""
    remapped_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        line_text = line.rstrip("\r\n")
        if not is_visible_tool_marker_line(line_text):
            remapped_lines.append(line)
            continue

        match = _VISIBLE_TOOL_MARKER_INDEX_PATTERN.search(line_text)
        if match is None:
            remapped_lines.append(line)
            continue
        old_index = int(match.group())
        new_index = index_map.get(old_index, old_index)
        remapped_lines.append(f"{line[: match.start()]}{new_index}{line[match.end() :]}")
    return "".join(remapped_lines)


def renumbered_tool_trace(body: str, trace: Sequence[ToolTraceEntry]) -> tuple[str, list[ToolTraceEntry]]:
    """Number one body's markers from 1 and return the trace entries they now point at."""
    index_map: dict[int, int] = {}
    entries: list[ToolTraceEntry] = []
    for line in body.splitlines():
        index_match = _VISIBLE_TOOL_MARKER_INDEX_PATTERN.search(line) if is_visible_tool_marker_line(line) else None
        if index_match is None:
            continue
        old_index = int(index_match.group())
        if old_index in index_map or not 1 <= old_index <= len(trace):
            continue
        entries.append(trace[old_index - 1])
        index_map[old_index] = len(entries)
    return remap_visible_tool_marker_indices(body, index_map), entries


def append_stream_text(existing_text: str, content: object | None, *, separate: bool = False) -> str:
    """Append one delta while preserving explicit and visible-tool boundaries."""
    if not content:
        return existing_text
    delta = str(content)
    last_line = existing_text.rsplit("\n", maxsplit=1)[-1].rstrip("\r")
    needs_separator = separate or is_visible_tool_marker_line(last_line)
    separator = ""
    if existing_text and not delta.startswith(("\n", "\r")) and needs_separator:
        if existing_text.endswith(("\n\n", "\r\n\r\n")):
            separator = ""
        elif existing_text.endswith(("\n", "\r")):
            separator = "\n"
        else:
            separator = "\n\n"
    return f"{existing_text}{separator}{delta}"


def tool_markers_match_trace(text: str, tool_trace: Sequence[ToolTraceEntry]) -> bool:
    """Return whether visible marker lines exactly match the ordered trace slots."""
    visible_markers = tuple(line.strip() for line in text.splitlines() if is_visible_tool_marker_line(line))
    expected_markers = tuple(
        _tool_marker_line(entry.tool_name, index, pending=entry.type == "tool_call_started")
        for index, entry in enumerate(tool_trace, start=1)
    )
    return visible_markers == expected_markers


def _format_tool_marker(tool_name: str, tool_index: int | None, *, pending: bool) -> str:
    return f"\n\n{_tool_marker_line(tool_name, tool_index, pending=pending)}\n\n"


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
    args_preview, truncated = _format_tool_args(tool_args)
    trace = ToolTraceEntry(
        type="tool_call_started",
        tool_name=tool_name,
        args_preview=args_preview or None,
        truncated=truncated,
    )
    return _format_tool_marker(tool_name, tool_index, pending=True), trace


def format_tool_combined(
    tool_name: str,
    tool_args: dict[str, object],
    result: object | None,
    tool_index: int | None = None,
    *,
    is_error: bool = False,
) -> tuple[str, ToolTraceEntry]:
    """Format a complete tool call marker and associated trace metadata, marked failed when ``is_error``."""
    args_preview, truncated = _format_tool_args(tool_args)
    result_display = ""
    if result is not None and result != "":
        result_display, result_truncated = _format_tool_result_preview(result)
        truncated = truncated or result_truncated

    trace = ToolTraceEntry(
        type="tool_call_completed",
        tool_name=tool_name,
        args_preview=args_preview or None,
        result_preview=result_display or None,
        truncated=truncated,
        is_error=is_error,
    )
    return _format_tool_marker(tool_name, tool_index, pending=False), trace


def complete_pending_tool_block(accumulated_text: str, tool_name: str, tool_index: int) -> str:
    """Return ``accumulated_text`` with the pending marker of tool ``tool_index`` marked completed.

    The hourglass is removed; text without that pending marker is returned unchanged.
    """
    if tool_index < 1:
        msg = "tool_index must be >= 1 for v2 tool markers"
        raise ValueError(msg)

    pending_line = _tool_marker_line(tool_name, tool_index, pending=True)
    completed_line = _tool_marker_line(tool_name, tool_index, pending=False)
    pending_pos = accumulated_text.rfind(pending_line)
    # A duplicate completion finds no pending marker and leaves the text unchanged.
    if pending_pos < 0:
        return accumulated_text
    return accumulated_text[:pending_pos] + completed_line + accumulated_text[pending_pos + len(pending_line) :]


def _tool_args(call: ReportedCall) -> dict[str, object]:
    return {str(k): v for k, v in call.tool_args.items()} if isinstance(call.tool_args, dict) else {}


def _identified(trace: ToolTraceEntry, call: ReportedCall, origin: ToolCallOrigin | None) -> ToolTraceEntry:
    """Stamp a trace entry with its call, and with where a hidden CLI call ran."""
    trace.call = call
    trace.tool_call_id = call.tool_call_id
    if origin is not None:
        trace.parent_bash_call_id = origin.parent_bash_call_id
        trace.toolkit_name = origin.toolkit_name
    return trace


def with_stored_calls(trace: Sequence[ToolTraceEntry], tools: Sequence[ToolExecution]) -> list[ToolTraceEntry]:
    """Return ``trace`` with each entry whose call ``tools`` stores carrying that call whole, as its live entry did.

    A trace restored from a reply's records keeps only previews, while the run that made the calls stores each one.
    Entries and calls pair from the newest, so an entry of an earlier run, or a call id a provider reused, keeps its own.
    """
    stored: dict[str, list[ReportedCall]] = {}
    for tool in tools:
        stored.setdefault(tool.tool_call_id, []).append(tool.call)
    restored = list(trace)
    for index in reversed(range(len(restored))):
        calls = stored.get(restored[index].tool_call_id or "")
        if calls:
            restored[index] = replace(restored[index], call=calls.pop())
    return restored


def format_tool_started_event(
    call: ReportedCall,
    tool_index: int | None = None,
    *,
    origin: ToolCallOrigin | None = None,
) -> tuple[str, ToolTraceEntry]:
    """Format a tool-call start into display text and trace metadata."""
    text, trace = _format_tool_started(call.tool_name, _tool_args(call), tool_index=tool_index)
    return text, _identified(trace, call, origin)


def format_tool_completed_event(call: ReportedCall, *, origin: ToolCallOrigin | None = None) -> ToolTraceEntry:
    """Return a tool-call completion's trace metadata."""
    _, trace = format_tool_combined(call.tool_name, _tool_args(call), call.result, is_error=call.is_error is True)
    return _identified(trace, call, origin)


def _internal_fields(entry: ToolTraceEntry) -> dict[str, object]:
    """Return the restart-only fields an entry sets: its identity, where a hidden CLI call ran, and its failure."""
    fields: dict[str, object] = {
        "parent_bash_call_id": entry.parent_bash_call_id,
        "toolkit_name": entry.toolkit_name,
        "tool_call_id": entry.tool_call_id,
        "member_id": entry.member_id,
        "is_error": entry.is_error or None,
    }
    return {key: value for key, value in fields.items() if value is not None}


def serialize_tool_trace(
    tool_trace: Sequence[ToolTraceEntry],
    *,
    include_internal: bool = False,
) -> tuple[dict[str, object], ...]:
    """Serialize trace entries, optionally retaining restart-only identity and failure."""
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
        if include_internal:
            event.update(_internal_fields(entry))
        serialized.append(event)
    return tuple(serialized)


# LEGACY_COMPAT: Tool trace entries that name their team slot as a ``scope_key`` string.
# Legacy format: an entry of the internal tool trace a reply record stores (``serialize_tool_trace(...,
# include_internal=True)``) with ``scope_key`` ``team`` for a team leader's call or ``agent:<id>`` for a member's,
# where ``<id>`` is the member's id in the version-2 team snapshot stored beside the trace.
# Last legacy release: v2026.10.677, whose ``_internal_fields`` (``tool_system/events.py``) stored the scope the team
# presentation (``teams/presentation.py``) gave each call; replacement: the next release, which stores a member's call
# with ``member_id``, the member's config name, and a leader's call with neither key.
# Handling: ``team`` reads as no member and ``agent:<id>`` as member ``<id>``, which the version-2 snapshot's reader
# (``teams/legacy_team_presentation.py``) maps to the member's config name; any other value is a malformed entry, as
# that release never wrote one.
# Coverage: tests/test_tool_events.py::test_a_trace_entry_stored_with_a_scope_key_reads_as_its_member,
#   tests/test_tool_events.py::test_durable_tool_trace_rejects_malformed_events,
#   tests/teams/test_legacy_team_presentation.py::test_a_v2_reply_record_restores_with_its_members_keyed_by_config_name
def _legacy_member_id(scope_key: object) -> str | None:
    """Return the member a stored ``scope_key`` names, None for ``team``."""
    if scope_key == "team":
        return None
    if isinstance(scope_key, str) and scope_key.startswith("agent:"):
        return scope_key.removeprefix("agent:")
    raise RuntimeError(_MALFORMED_TRACE)


def deserialize_tool_trace(stored: Sequence[Mapping[str, object]]) -> list[ToolTraceEntry]:
    """Restore a trace snapshot while rejecting malformed event records."""
    restored: list[ToolTraceEntry] = []
    for event in stored:
        event_type = event.get("type")
        tool_name = event.get("tool_name")
        args_preview = event.get("args_preview")
        result_preview = event.get("result_preview")
        tool_call_id = event.get("tool_call_id")
        legacy_scope = event.get("scope_key")
        member_id = event.get("member_id") if legacy_scope is None else _legacy_member_id(legacy_scope)
        parent_bash_call_id = event.get("parent_bash_call_id")
        toolkit_name = event.get("toolkit_name")
        truncated = event.get("truncated")
        is_error = event.get("is_error")
        if (
            not isinstance(event_type, str)
            or event_type not in {"tool_call_started", "tool_call_completed"}
            or not isinstance(tool_name, str)
            or not tool_name
            or (args_preview is not None and not isinstance(args_preview, str))
            or (result_preview is not None and not isinstance(result_preview, str))
            or (tool_call_id is not None and (not isinstance(tool_call_id, str) or not tool_call_id))
            or (member_id is not None and (not isinstance(member_id, str) or not member_id))
            or (parent_bash_call_id is not None and not isinstance(parent_bash_call_id, str))
            or (toolkit_name is not None and not isinstance(toolkit_name, str))
            or (truncated is not None and not isinstance(truncated, bool))
            or (is_error is not None and not isinstance(is_error, bool))
        ):
            raise RuntimeError(_MALFORMED_TRACE)
        restored.append(
            ToolTraceEntry(
                type=cast("Literal['tool_call_started', 'tool_call_completed']", event_type),
                tool_name=tool_name,
                args_preview=args_preview,
                result_preview=result_preview,
                truncated=truncated is True,
                tool_call_id=cast("str | None", tool_call_id),
                member_id=cast("str | None", member_id),
                parent_bash_call_id=parent_bash_call_id,
                toolkit_name=toolkit_name,
                is_error=is_error is True,
            ),
        )
    return restored


def build_tool_trace_content(tool_trace: Sequence[ToolTraceEntry] | None) -> dict[str, object] | None:
    """Build message content payload for tool trace metadata."""
    if not tool_trace:
        return None
    payload: dict[str, object] = {"version": _TOOL_TRACE_VERSION, "events": list(serialize_tool_trace(tool_trace))}
    if any(entry.truncated for entry in tool_trace):
        payload["content_truncated"] = True
    return {_TOOL_TRACE_KEY: payload}
