"""Tool markers leave the text MindRoom sends to Matrix; each call's place rides on its trace entry as ``at``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import nio
import pytest

from mindroom.judgment.conversation import message_judgment_text
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.large_messages import prepare_large_message
from mindroom.matrix.message_builder import build_matrix_edit_content
from mindroom.tool_system.events import body_with_tool_markers, html_block_spans, tool_markers_out_of_text

_TRACE_KEY = "io.mindroom.tool_trace"
_CASES = json.loads((Path(__file__).parent / "fixtures" / "tool-positions-v1.json").read_text(encoding="utf-8"))[
    "cases"
]


def _sent(case: dict[str, Any]) -> dict[str, Any]:
    sent = case["sent"]
    return {
        "msgtype": "m.text",
        "body": sent["body"],
        "format": "org.matrix.custom.html",
        "formatted_body": sent["formatted_body"],
        _TRACE_KEY: {"version": 2, "events": sent["events"]},
    }


def _wire(case: dict[str, Any]) -> dict[str, Any]:
    wire = case["wire"]
    events = [{**event, "at": at} for event, at in zip(case["sent"]["events"], wire["at"], strict=True)]
    return {
        "msgtype": "m.text",
        "body": wire["body"],
        "format": "org.matrix.custom.html",
        "formatted_body": wire["formatted_body"],
        _TRACE_KEY: {"version": 3, "events": events},
    }


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_sent_text_loses_its_markers_and_each_call_keeps_its_block(case: dict[str, Any]) -> None:
    """The shared fixture pins what Chat counts: the same HTML and positions rebuild the same markers."""
    assert tool_markers_out_of_text(_sent(case)) == _wire(case)


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_a_sent_body_reads_back_with_its_markers(case: dict[str, Any]) -> None:
    """Readers of the body (judges) put the markers back as Chat does."""
    assert body_with_tool_markers(case["wire"]["body"], _wire(case)) == case["restored"]["body"]


def test_an_edit_loses_its_markers_in_both_layers() -> None:
    """Clients read ``m.new_content``; the fallback outside it must not show markers either."""
    case = _CASES[0]
    edit = tool_markers_out_of_text(build_matrix_edit_content("$reply", _sent(case)))
    assert edit["m.new_content"] == _wire(case)
    assert edit["body"] == case["wire"]["body"]
    assert edit[_TRACE_KEY]["version"] == 3


def test_content_without_marker_metadata_is_sent_as_it_is() -> None:
    """Plain messages, sent version-3 content and a trace without HTML stay the same object."""
    plain = {"msgtype": "m.text", "body": "🔧 `shell` [1]"}
    wire = _wire(_CASES[0])
    no_html = {"msgtype": "m.text", "body": "🔧 `shell` [1]", _TRACE_KEY: {"version": 2, "events": []}}
    for content in (plain, wire, no_html):
        assert tool_markers_out_of_text(content) is content
    assert body_with_tool_markers(_CASES[0]["sent"]["body"], _sent(_CASES[0])) == _CASES[0]["sent"]["body"]


def test_a_call_without_a_usable_position_reads_back_at_the_end() -> None:
    """Positions any room member could write never lose a call."""
    content = _wire(_CASES[0])
    content[_TRACE_KEY]["events"][0]["at"] = "1"
    assert body_with_tool_markers("Let me check.\n\nFound it.", content) == (
        "Let me check.\n\n🔧 `shell` [2] ⏳\n\nFound it.\n\n🔧 `shell` [1]"
    )


def test_top_level_blocks() -> None:
    """Elements, void elements and runs of text that is not blank count; nesting and whitespace do not."""
    html = " <p>a<b>b</b></p>\n<hr>text <em>x</em><br/>\n<ul><li><p>c</p></li></ul>"
    assert [html[start : start + 4] for start, _end in html_block_spans(html)] == [
        "<p>a",
        "<hr>",
        "text",
        "<em>",
        "<br/",
        "<ul>",
    ]


def test_judges_read_a_sent_reply_with_its_tool_tags_in_place() -> None:
    """A version-3 reply reads to judges as its version-2 form did."""
    case = _CASES[0]
    sent = _sent(case)
    wire = _wire(case)

    def judged(content: dict[str, Any]) -> str | None:
        message = ResolvedVisibleMessage.synthetic(
            sender="@mindroom_code:localhost",
            body=content["body"],
            event_id="$m",
            content=content,
        )
        return message_judgment_text(message)

    assert judged(wire) == judged(sent)
    assert "<tool" in (judged(wire) or "")


@pytest.mark.asyncio
async def test_a_message_prepared_for_matrix_is_sent_without_markers() -> None:
    """Every send and edit passes ``prepare_large_message``, so a sidecar holds the converted content too."""
    case = _CASES[2]
    client = MagicMock(spec=nio.AsyncClient)
    assert await prepare_large_message(client, "!room:server", _sent(case), room_encrypted=False) == _wire(case)
    edit = build_matrix_edit_content("$reply", _sent(case))
    prepared = await prepare_large_message(client, "!room:server", edit, room_encrypted=False)
    assert prepared["m.new_content"] == _wire(case)


@pytest.mark.parametrize("at", [True, 1.0, -1, 9, None])
def test_a_position_of_the_wrong_kind_reads_back_at_the_end(at: object) -> None:
    """Booleans and floats compare equal to integers, but no position is one."""
    content = _wire(_CASES[0])
    content[_TRACE_KEY]["events"][0]["at"] = at
    assert body_with_tool_markers("Let me check.\n\nFound it.", content).endswith("Found it.\n\n🔧 `shell` [1]")
