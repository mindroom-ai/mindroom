"""Replies with tool calls as the backend builds them and as Matrix holds them, from the fixture shared with Chat."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mindroom.matrix.message_builder import markdown_to_html
from mindroom.tool_system.events import tool_markers_out_of_text

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


def _marked(body: str, events: list[Any]) -> dict[str, Any]:
    """Return a reply as the backend builds it: its markers in its text, its HTML rendered from its body."""
    return _content(body, markdown_to_html(body), 2, events)


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


def reply_with_calls(body: str, calls: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a reply as the backend builds it, its tool markers in its text, and as Matrix holds it, without them."""
    built = _marked(body, [_SHELL] * calls)
    held = tool_markers_out_of_text(built)
    assert held is not built
    return built, held
