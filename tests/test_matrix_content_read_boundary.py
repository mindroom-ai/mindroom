"""A module that fetches events from the homeserver reads what they say through ``tool_markers_in_text``.

Matrix holds an agent's reply without its tool marker lines. Sync has one way in, ``ingestion_timeline_views``; a
fetch can be written anywhere, and three times a new one read the text as Matrix holds it.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).parents[1] / "src" / "mindroom"
_FETCHES = {"room_messages", "room_get_event", "room_get_event_relations", "room_context", "matrix_json"}
_CONTENT = {"source", "body", "formatted_body", "m.new_content", "io.mindroom.tool_trace", "TOOL_TRACE_CONTENT_KEY"}
# The modules that fetch events and look at them as Matrix holds them, and why no agent's text is read that way.
_RAW_READERS = {
    "approval/transport.py": "reads the approval ID of an approval card, which is no message",
    "attachments/storage.py": "reads the attachment IDs, media kind and file name of a media thread root",
    "bot_room_lifecycle.py": "looks for the router's own welcome, which has no tool calls",
    "custom_tools/chat_ui.py": "reads a canvas's metadata and the state a person's Chat shared",
    "custom_tools/matrix_conversation_operations.py": "hands its page and the agents to resolve_latest_visible_messages",
    "matrix/client_delivery.py": "the write boundary: its request sends content and answers with an event ID",
    "matrix/history_visibility.py": "walks membership events",
    "matrix/relation_lookup.py": "reads an event's relations",
    "model_selection_scope.py": "reads a thread root's relations and whether it was redacted",
}


def _fetches(node: ast.AST) -> bool:
    match node:
        case ast.Call(func=ast.Attribute(attr="_send", value=ast.Name(id="client") | ast.Attribute(attr="client"))):
            return True
        case ast.Call(func=ast.Name(id=name) | ast.Attribute(attr=name)):
            return name in _FETCHES
    return False


def _reads_content(node: ast.AST) -> bool:
    match node:
        case ast.Attribute(attr=word) | ast.Constant(value=str(word)) | ast.Name(id="TOOL_TRACE_CONTENT_KEY" as word):
            return word in _CONTENT
    return False


def _restores(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "tool_markers_in_text"


def test_a_module_that_fetches_events_reads_their_content_through_the_read_boundary() -> None:
    """Restore an agent's content with ``tool_markers_in_text``, or say in ``_RAW_READERS`` why the module need not."""
    raw_readers = set()
    for path in _SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        # Parsing every module takes seconds, and few of them name a fetch at all.
        nodes = list(ast.walk(ast.parse(text))) if any(name in text for name in (*_FETCHES, "_send")) else []
        if any(map(_fetches, nodes)) and any(map(_reads_content, nodes)) and not any(map(_restores, nodes)):
            raw_readers.add(path.relative_to(_SRC).as_posix())
    assert raw_readers == set(_RAW_READERS)
