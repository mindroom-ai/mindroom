"""What `ui` blocks in a reply become: a readable fallback block in the text and an entry in `io.mindroom.ui`."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.matrix.message_builder import fenced_code_blocks
from mindroom.ui_widgets.models import Chart, parse_widget

if TYPE_CHECKING:
    from collections.abc import Collection

logger = get_logger(__name__)

_MAX_WIDGETS = 8
_MAX_UI_BYTES = 16_384
_FALLBACK_MAX_ROWS = 40
_MARKER = "mindroom-ui-"
# A cheap test for "this text may hold a column-0 `ui` fence"; the line scan decides.
_UI_FENCE = re.compile(r"^(?:`{3,}|~{3,})[ \t]*ui(?:\s|$)", re.MULTILINE)
_MARKER_ID = re.compile(_MARKER + r"([0-9a-f]{10})")


@dataclass(frozen=True, slots=True)
class _UiRender:
    """A reply text with its widgets replaced, and the `io.mindroom.ui` value for them (`None` without an entry)."""

    text: str
    content: dict[str, object] | None


def _number(value: float) -> float | int:
    """A whole number below 2^53 without its fraction, so JSON spells it `120`."""
    return int(value) if value.is_integer() and abs(value) < 2**53 else value


def _canonical_widget_json(widget: Chart) -> str:
    """The one JSON text of a widget: no absent optional key, sorted keys, no spaces, non-ASCII kept."""
    data = widget.model_dump(exclude_none=True)
    for series in data["series"]:
        series["data"] = [_number(value) for value in series["data"]]
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _widget_id(canonical: str, taken: Collection[str] = ()) -> str:
    """The id of a widget: a hash of its canonical JSON, skipping every id a marker block in the text already uses."""
    suffix = 0
    while True:
        text = f"{canonical}#{suffix}" if suffix else canonical
        widget_id = hashlib.sha256(text.encode()).hexdigest()[:10]
        if widget_id not in taken:
            return widget_id
        suffix += 1


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _fallback_lines(widget: Chart) -> tuple[str, ...]:
    """The chart as a title line and an aligned text table."""
    rows: list[list[str] | None] = [[_one_line(widget.x or ""), *(_one_line(series.name) for series in widget.series)]]
    rows += [
        [_one_line(label), *(json.dumps(_number(series.data[index])) for series in widget.series)]
        for index, label in enumerate(widget.labels)
    ]
    hidden = len(widget.labels) - _FALLBACK_MAX_ROWS
    if hidden > 0:
        half = _FALLBACK_MAX_ROWS // 2
        rows[1 + half : -half] = [None]
    widths = [max(len(row[column]) for row in rows if row) for column in range(len(widget.series) + 1)]
    unit = f", {_one_line(widget.y)}" if widget.y else ""
    return (
        f"{_one_line(widget.title)} ({widget.kind} chart{unit})",
        *(
            "  ".join(
                [
                    row[0].ljust(widths[0]),
                    *(cell.rjust(width) for cell, width in zip(row[1:], widths[1:], strict=True)),
                ],
            ).rstrip()
            if row
            else f"... {hidden} more rows ..."
            for row in rows
        ),
    )


def _fenced(lines: tuple[str, ...], widget_id: str) -> list[str]:
    """The marker block: a fence no backtick run in the lines can close."""
    longest = max((len(run) for line in lines for run in re.findall("`+", line)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}{_MARKER}{widget_id}", *lines, fence]


@lru_cache(maxsize=64)
def _block(json_text: str) -> tuple[str, tuple[str, ...]] | list[str]:
    """The canonical JSON and fallback lines of a valid block, or its errors; kept so a live tick parses a block once."""
    widget, errors = parse_widget(json_text)
    if widget is None:
        return errors
    return _canonical_widget_json(widget), _fallback_lines(widget)


def _ui_fences(lines: list[str]) -> tuple[list[tuple[int, int]], int | None, set[str]]:
    """The column-0 `ui` fences outside any other block, where the markdown parser the HTML is made with finds them.

    Returns the opening and closing line of each closed one, the opening line of an open one, and the id of every
    marker block.
    """
    closed: list[tuple[int, int]] = []
    open_at = None
    marker_ids: set[str] = set()
    for start, end, info, top_level in fenced_code_blocks(lines):
        marker = _MARKER_ID.match(info.strip())
        if marker is not None:
            marker_ids.add(marker[1])
        opening = lines[start]
        if not top_level or opening[0] == " " or info.split()[:1] != ["ui"]:
            continue
        fence = opening[: len(opening) - len(opening.lstrip(opening[0]))]
        # An open top-level fence runs to the last line, which then is not a closing fence.
        if end - 1 > start and re.fullmatch(f" {{0,3}}{fence}{fence[0]}*[ \t]*", lines[end - 1]):
            closed.append((start, end - 1))
        else:
            open_at = start
    return closed, open_at, marker_ids


def _lines(text: str) -> list[str]:
    """The lines of a text as the markdown parser counts them."""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def without_open_ui_fence(text: str) -> str:
    """The text cut where an open column-0 `ui` fence begins; the same object when there is none."""
    if _UI_FENCE.search(text) is None:
        return text
    lines = _lines(text)
    open_at = _ui_fences(lines)[1]
    return text if open_at is None else "\n".join(lines[:open_at]).rstrip()


def _replace_blocks(
    lines: list[str],
    closed: list[tuple[int, int]],
    taken: set[str],
    *,
    warn: bool,
) -> tuple[list[str], list[tuple[str, str]]]:
    """The lines with each valid closed block replaced by its marker block, and the id and canonical JSON of each."""
    out: list[str] = []
    found: list[tuple[str, str]] = []
    position = 0
    for start, end in closed:
        block = _block("\n".join(lines[start + 1 : end]))
        if isinstance(block, list):
            if warn:
                logger.warning("ui_block_invalid", errors=block)
            continue
        canonical, fallback = block
        widget_id = _widget_id(canonical, taken)
        out += lines[position:start]
        if out and out[-1].strip():
            out.append("")
        out += _fenced(fallback, widget_id)
        position = end + 1
        if position < len(lines) and lines[position].strip():
            out.append("")
        found.append((widget_id, canonical))
    return out + lines[position:], found


def _entries(found: list[tuple[str, str]]) -> list[dict[str, str]]:
    """The entries of an `io.mindroom.ui` value: each id once, within the entry and byte limits."""
    widgets: dict[str, str] = {}
    size = 0
    for widget_id, canonical in found:
        canonical_bytes = len(canonical.encode())
        if widget_id not in widgets and len(widgets) < _MAX_WIDGETS and size + canonical_bytes <= _MAX_UI_BYTES:
            widgets[widget_id] = canonical
            size += canonical_bytes
    return [{"id": widget_id, "json": canonical} for widget_id, canonical in widgets.items()]


def _ui_value(entries: list[dict[str, str]]) -> dict[str, object] | None:
    """The `io.mindroom.ui` value of some entries; `None` without one, so the key is left out."""
    return {"version": 1, "widgets": entries} if entries else None


def _rendered(text: str, *, in_progress: bool) -> tuple[str, list[dict[str, str]]]:
    """The text with its valid closed `ui` blocks replaced, and their entries; the same text when nothing changes."""
    if _UI_FENCE.search(text) is None:
        return text, []
    lines = _lines(text)
    closed, open_at, marker_ids = _ui_fences(lines)
    cut = in_progress and open_at is not None
    if cut:
        del lines[open_at:]
    out, found = _replace_blocks(lines, closed, marker_ids, warn=not in_progress)
    if not found and not cut:
        return text, []
    rendered = "\n".join(out)
    return rendered.rstrip() if cut else rendered, _entries(found)


def render_ui_widgets(text: str, *, in_progress: bool) -> _UiRender:
    """Replace each valid closed `ui` block by its marker block and collect the widgets.

    Idempotent: a marker block is ordinary text, gets no entry, and its id is not used again.
    While the reply is in progress the text is cut at an open `ui` fence; a finished reply keeps it as written.
    Text that changes in no way is returned as the same object.
    """
    rendered, entries = _rendered(text, in_progress=in_progress)
    return _UiRender(rendered, _ui_value(entries))


def recorded_ui_widgets(shown_text: str, recorded_text: str) -> dict[str, object] | None:
    """Return the widgets of ``recorded_text`` that ``shown_text`` holds a marker block for, as an ``io.mindroom.ui`` value.

    A write that shows text a stream already rendered has no ``ui`` block left to read, while the reply's record
    still holds the text as the model wrote it. A marker block the record did not author stays without a widget.
    """
    if _MARKER not in shown_text:
        return None
    shown_ids = _ui_fences(_lines(shown_text))[2]
    entries = _rendered(recorded_text, in_progress=False)[1]
    return _ui_value([entry for entry in entries if entry["id"] in shown_ids])
