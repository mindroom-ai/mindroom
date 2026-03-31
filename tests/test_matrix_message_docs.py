"""Tests for matrix_message tool documentation extraction."""

from __future__ import annotations

from agno.tools.function import Function

from mindroom.custom_tools.matrix_message import MatrixMessageTools


def _matrix_message_function() -> Function:
    tools = MatrixMessageTools()
    function = tools.async_functions["matrix_message"]
    function.process_entrypoint(strict=False)
    return function


def test_matrix_message_description_covers_critical_behavior() -> None:
    """Processed description should explain the key matrix_message mechanics."""
    function = _matrix_message_function()
    description = function.description

    assert description is not None
    assert "ignore_mentions" in description
    assert "com.mindroom.skip_mentions" in description
    assert "com.mindroom.original_sender" in description
    assert "self-trigger" in description
    assert 'thread_id="room"' in description
    assert "combined limit" in description
    assert "5 per call" in description


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

    assert "send" in action_description
    assert "thread-reply" in action_description
    assert "context" in action_description

    assert "emoji" in message_description
    assert "None" in message_description

    assert "att_*" in attachment_ids_description
    assert "cannot exceed 5" in attachment_ids_description
    assert "cannot exceed 5" in attachment_paths_description

    assert "current room context" in room_id_description
    assert "react" in target_description
    assert "edit" in target_description

    assert 'thread_id="room"' in thread_id_description
    assert "room-level scope" in thread_id_description

    assert "com.mindroom.skip_mentions" in ignore_mentions_description
    assert "com.mindroom.original_sender" in ignore_mentions_description
    assert "default `True`" in ignore_mentions_description

    assert "defaults to 20" in limit_description
    assert "capped at 50" in limit_description
