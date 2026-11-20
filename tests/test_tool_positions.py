"""Tool markers leave the text MindRoom sends to Matrix; each call's place rides on its trace entry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.constants import REASONING_CONTENT_KEY, STREAM_VISIBLE_BODY_KEY
from mindroom.custom_tools.matrix_conversation_operations import _message_payload
from mindroom.judgment.conversation import message_judgment_text
from mindroom.matrix.client_delivery import _prepare_matrix_message, build_edit_event_content
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.large_messages import calculate_event_size, fit_reasoning_to_ordinary_event, prepare_large_message
from mindroom.matrix.message_builder import markdown_to_html
from mindroom.matrix.segmented_messages import segment_matrix_content
from mindroom.tool_system.events import (
    body_with_tool_markers,
    html_block_spans,
    tool_markers_in_text,
    tool_markers_out_of_text,
)

_TRACE_KEY = "io.mindroom.tool_trace"
_CASES = json.loads((Path(__file__).parent / "fixtures" / "tool-positions-v1.json").read_text(encoding="utf-8"))[
    "cases"
]
_SENT_CASES = [case for case in _CASES if case["wire"] is not None]
_SHELL = {"type": "tool_call_completed", "tool_name": "shell"}


def _case(name: str) -> dict[str, Any]:
    return next(case for case in _CASES if case["name"] == name)


def _content(body: str, html: str, version: int, events: list[Any]) -> dict[str, Any]:
    return {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": html,
        _TRACE_KEY: {"version": version, "events": events},
    }


def _sent(case: dict[str, Any]) -> dict[str, Any]:
    sent = case["sent"]
    return _content(sent["body"], sent["formatted_body"], 2, sent["events"])


def _wire(case: dict[str, Any]) -> dict[str, Any]:
    wire = case["wire"]
    places = zip(case["sent"]["events"], wire["body_line"], wire["at"], strict=True)
    events = [
        {**event, **({} if line is None else {"body_line": line}), **({} if at is None else {"at": at})}
        for event, line, at in places
    ]
    return _content(wire["body"], wire["formatted_body"], 3, events)


def _message(content: dict[str, Any]) -> ResolvedVisibleMessage:
    return ResolvedVisibleMessage.synthetic(
        sender="@mindroom_code:localhost",
        body=content["body"],
        event_id="$m",
        content=content,
    )


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_sent_text_loses_its_markers_and_each_call_keeps_its_place(case: dict[str, Any]) -> None:
    """The shared fixture pins what Chat reads; a text that would not read back exactly is sent as it is."""
    sent = _sent(case)
    wire = tool_markers_out_of_text(sent)
    assert wire is sent if case["wire"] is None else wire == _wire(case)
    assert case["sent"]["formatted_body"] == markdown_to_html(case["sent"]["body"])


@pytest.mark.parametrize("case", _SENT_CASES, ids=[case["name"] for case in _SENT_CASES])
def test_a_sent_reply_reads_back_exactly(case: dict[str, Any]) -> None:
    """Readers get the version-2 reply back byte for byte: copy text, judges and the agent's own reads."""
    assert tool_markers_in_text(_wire(case)) == _sent(case)
    assert body_with_tool_markers(case["wire"]["body"], _wire(case)) == case["sent"]["body"]
    assert message_judgment_text(_message(_wire(case))) == message_judgment_text(_message(_sent(case)))
    wire_read = _message_payload(_message(_wire(case)))
    sent_read = _message_payload(_message(_sent(case)))
    assert json.dumps(wire_read, sort_keys=True) == json.dumps(sent_read, sort_keys=True)


def test_judges_see_a_reply_of_calls_alone_and_nothing_in_an_empty_one() -> None:
    """A blank body with its calls back is not blank; one without any is."""
    wire = _wire(_case("a reply of calls alone"))
    assert wire["body"] == ""
    assert (message_judgment_text(_message(wire)) or "").startswith('<tool name="shell"')
    empty = _content("", "", 3, [_SHELL])
    assert message_judgment_text(_message(empty)) is None


@pytest.mark.parametrize("prefix", ["", "Intro.\n\n"])
def test_an_edit_loses_its_markers_in_both_layers(prefix: str) -> None:
    """The fallback repeats the replacement after "* ", where an opening marker is no line of its own."""
    body = f"{prefix}🔧 `shell` [1]\n\nAnswer."
    new_content = _content(body, markdown_to_html(body), 2, [_SHELL])
    edit = build_edit_event_content(event_id="$reply", new_content=new_content, new_text=body)

    sent = tool_markers_out_of_text(edit)

    assert sent["m.new_content"] == tool_markers_out_of_text(new_content)
    assert sent["body"] == f"* {prefix}Answer."
    assert sent["formatted_body"] == sent["m.new_content"]["formatted_body"]
    assert "🔧" not in json.dumps(sent, ensure_ascii=False).replace(json.dumps(_SHELL), "")
    assert tool_markers_in_text(sent) == edit


def test_content_without_marker_metadata_is_sent_as_it_is() -> None:
    """Plain messages, sent version-3 content, a trace without HTML and a warm-up frame stay the same object."""
    sent = _sent(_case("calls between two paragraphs share one position"))
    plain = {"msgtype": "m.text", "body": "🔧 `shell` [1]"}
    no_html = {"msgtype": "m.text", "body": "🔧 `shell` [1]", _TRACE_KEY: {"version": 2, "events": [_SHELL]}}
    warming = {**sent, STREAM_VISIBLE_BODY_KEY: sent["body"]}
    for content in (plain, tool_markers_out_of_text(sent), no_html, warming):
        assert tool_markers_out_of_text(content) is content
    assert body_with_tool_markers(sent["body"], sent) == sent["body"]
    assert tool_markers_in_text(sent) is sent


@pytest.mark.parametrize("place", [True, 1.0, -1, 9, None, "1"])
def test_a_position_of_the_wrong_kind_reads_back_at_the_end(place: object) -> None:
    """Positions any room member could write never lose a call; booleans and floats compare equal to integers."""
    content = _wire(_case("calls between two paragraphs share one position"))
    content[_TRACE_KEY]["events"][0].update(body_line=place, at=place)
    restored = tool_markers_in_text(content)
    assert restored["body"] == "Let me check.\n\n🔧 `shell` [2] ⏳\n\nFound it.\n\n🔧 `shell` [1]"
    assert restored["formatted_body"].endswith("<p>Found it.</p>\n<p>🔧 <code>shell</code> [1]</p>\n")


@pytest.mark.parametrize(
    "trace",
    [
        {"version": 3, "events": 5},
        {"version": 3, "events": "events"},
        {"version": 3, "events": {"at": 0}},
        {"version": 3, "events": [1, None, {"tool_name": 3, "at": 0}, {"tool_name": "", "body_line": 0}]},
        {"version": 3},
        [{"tool_name": "shell", "at": 0}],
    ],
)
def test_a_malformed_trace_reads_as_its_text(trace: object) -> None:
    """Readers tolerate any value under the trace key and add nothing for it."""
    content = {**_content("Found it.", "<p>Found it.</p>\n", 3, []), _TRACE_KEY: trace}
    assert body_with_tool_markers("Found it.", content) == "Found it."
    assert message_judgment_text(_message(content)) == "Found it."
    restored = tool_markers_in_text(content)
    assert (restored["body"], restored["formatted_body"]) == ("Found it.", "<p>Found it.</p>\n")
    assert tool_markers_out_of_text(content) is content


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


def _large(calls: int, prose: int) -> dict[str, Any]:
    body = "\n\n".join([*(f"🔧 `shell` [{index}]" for index in range(1, calls + 1)), "x" * prose])
    return _content(body, markdown_to_html(body), 2, [_SHELL] * calls)


@pytest.mark.asyncio
async def test_a_message_too_large_for_one_event_keeps_its_markers_for_its_preview() -> None:
    """Chat draws a long message's calls from its preview's markers until the sidecar loads, so both keep them."""
    content = _large(3, 80_000)
    client = MagicMock(spec=nio.AsyncClient)
    client.upload = AsyncMock(return_value=(nio.UploadResponse.from_dict({"content_uri": "mxc://server/full"}), None))
    small = _sent(_case("calls between two paragraphs share one position"))
    assert await prepare_large_message(client, "!room:server", small, room_encrypted=False) == _wire(
        _case("calls between two paragraphs share one position"),
    )

    with patch("mindroom.matrix.large_messages.upload_json_sidecar", new=AsyncMock(return_value=("mxc://s/f", {}))):
        preview = await prepare_large_message(client, "!room:server", content, room_encrypted=False)

    assert preview["body"].startswith("🔧 `shell` [1]\n\n🔧 `shell` [2]")


def test_sizing_counts_the_event_as_sent() -> None:
    """Reasoning and splitting are fitted to the event without its markers, which is what Matrix gets."""
    content = _large(400, 200)
    marked, sent = calculate_event_size(content), calculate_event_size(tool_markers_out_of_text(content))
    assert sent < marked
    target = (marked + sent) // 2
    with patch("mindroom.matrix.segmented_messages._segment_target_bytes", return_value=target):
        assert segment_matrix_content(content, room_encrypted=False) is None
    # Room for the reasoning beside the text as sent, but not beside its markers too.
    reasoning = {"text": "r" * (marked - sent)}
    fitted = fit_reasoning_to_ordinary_event(
        MagicMock(spec=nio.AsyncClient),
        "!room:server",
        {**content, REASONING_CONTENT_KEY: reasoning},
        room_encrypted=False,
        size_limit=marked + 200,
    )
    assert fitted[REASONING_CONTENT_KEY] == reasoning


@pytest.mark.asyncio
async def test_preparing_a_message_that_fits_checks_room_encryption_no_more() -> None:
    """Taking markers out is no await, so a message that fits needs no second look at the room's encryption."""
    resolve = AsyncMock(return_value=False)
    client = MagicMock(spec=nio.AsyncClient)
    client.rooms = {}
    with patch("mindroom.matrix.client_delivery.resolve_room_encryption_outcome", new=resolve):
        prepared = await _prepare_matrix_message(
            client,
            "!room:server",
            _sent(_case("calls between two paragraphs share one position")),
            room_encrypted=False,
            operation="send",
            content_is_prepared=False,
        )
    assert prepared.content == _wire(_case("calls between two paragraphs share one position"))
    resolve.assert_not_awaited()


def test_a_trace_with_an_entry_that_is_no_call_names_no_marker() -> None:
    """Marker [n] names entry n; skipping a broken entry would give marker [1] the next entry's call."""
    content = _content("A\n\n🔧 `read_file` [1]", "", 2, [1, {**_SHELL, "args_preview": "cmd=rm"}])
    assert "shell" not in (message_judgment_text(_message(content)) or "")
