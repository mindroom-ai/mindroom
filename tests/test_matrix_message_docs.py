"""Tests for matrix_message tool documentation extraction."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from mindroom.custom_tools.matrix_message import MatrixMessageTools

if TYPE_CHECKING:
    from agno.tools.function import Function


def _matrix_message_function() -> Function:
    tools = MatrixMessageTools()
    function = tools.async_functions["matrix_message"]
    function.process_entrypoint(strict=False)
    return function


def test_matrix_message_description_covers_critical_behavior() -> None:
    """The compact processed description should retain every model-facing safety rule."""
    function = _matrix_message_function()
    description = function.description

    assert description is not None
    assert len(description) <= 2_000
    for action in ("send", "reply", "thread-reply", "react", "read", "room-threads", "thread-list", "edit", "context"):
        assert f"`{action}`" in description

    assert "`send` is room-level even inside a thread" in description
    assert "`reply` and `thread-reply` inherit the current thread" in description
    assert 'thread_id="room"' in description

    assert "Mention safety for text send/reply/thread-reply" in description
    assert "default `ignore_mentions=True`" in description
    assert "com.mindroom.skip_mentions" in description
    assert "com.mindroom.original_sender" in description
    assert "Set `False` ONLY" in description
    assert "handoff" in description
    assert "self-trigger" in description
    assert "then human requesters use `com.mindroom.original_sender`" in description

    assert "only `send`, `reply`, and `thread-reply`" in description
    assert "context-scoped `att_*` IDs or local file paths" in description
    assert "combined maximum 5" in description
    assert "text, attachments, or both, but not neither" in description
    assert "Relative paths resolve from the agent workspace" in description

    assert "`message_extras` adds collapsible sections" in description
    assert "`text/plain`" in description
    assert "`text/markdown`" in description
    assert "`text/html`" in description
    assert "`text/html`; basic fragments only" in description
    assert "no scripts/styles/forms/media/SVG/math/interactive elements" in description
    assert "links only `http`/`https`/`mailto`" in description
    assert "Full semantics: https://docs.mindroom.chat/tools/matrix-message/" in description


def test_matrix_message_docstring_stays_within_hard_cap() -> None:
    """The complete cleaned method docstring should stay within the issue's hard cap."""
    docstring = inspect.getdoc(MatrixMessageTools.matrix_message)

    assert docstring is not None
    assert len(docstring) <= 2_500


def test_matrix_message_parameter_descriptions_are_exposed() -> None:
    """Docstring Args should populate the tool parameter schema."""
    function = _matrix_message_function()
    properties = function.parameters["properties"]

    action_description = properties["action"]["description"]
    message_description = properties["message"]["description"]
    attachment_ids_description = properties["attachment_ids"]["description"]
    attachment_paths_description = properties["attachment_file_paths"]["description"]
    room_id_description = properties["room_id"]["description"]
    target_description = properties["target"]["description"]
    thread_id_description = properties["thread_id"]["description"]
    ignore_mentions_description = properties["ignore_mentions"]["description"]
    limit_description = properties["limit"]["description"]

    assert "Action" in action_description

    assert "emoji" in message_description

    assert "att_*" in attachment_ids_description
    assert "combined max 5" in attachment_ids_description
    assert "Local paths" in attachment_paths_description

    assert "current by default" in room_id_description
    assert "react/edit" in target_description

    assert '"room"' in thread_id_description
    assert "room scope" in thread_id_description

    assert "`True`" in ignore_mentions_description
    assert "intentional dispatch" in ignore_mentions_description

    assert "1-50" in limit_description
    assert "default 20" in limit_description
