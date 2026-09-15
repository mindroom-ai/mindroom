"""Verify the model receives a complete, compact Matrix message schema."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from mindroom.custom_tools.matrix_message import MatrixMessageTools

if TYPE_CHECKING:
    from agno.tools.function import Function


def _matrix_message_function() -> Function:
    function = MatrixMessageTools().async_functions["matrix_message"]
    function.process_entrypoint(strict=False)
    return function


def test_matrix_message_schema_exposes_four_actions_and_ten_arguments() -> None:
    """Agents should only see supported actions and the consolidated targeting contract."""
    function = _matrix_message_function()
    properties = function.parameters["properties"]
    assert set(properties) == {
        "action",
        "message",
        "recipient",
        "room_id",
        "thread_id",
        "new_thread",
        "event_id",
        "attachments",
        "message_extras",
        "limit",
    }
    assert properties["action"]["enum"] == ["send", "read", "edit", "react"]
    assert function.parameters["required"] == []
    assert all(field["description"] for field in properties.values())


def test_matrix_message_extras_schema_exposes_section_fields() -> None:
    """An agent must be able to construct a valid extra section from its JSON schema."""
    properties = _matrix_message_function().parameters["properties"]
    array = next(item for item in properties["message_extras"]["anyOf"] if item["type"] == "array")
    section = array["items"]
    assert set(section["properties"]) == {"title", "content", "content_type", "collapsed"}
    assert set(section["required"]) == {"title", "content"}
    assert section["properties"]["content_type"]["enum"] == ["text/plain", "text/markdown", "text/html"]


def test_matrix_message_description_stays_compact() -> None:
    """The tool should leave room in the context for the task itself."""
    description = _matrix_message_function().description
    docstring = inspect.getdoc(MatrixMessageTools.matrix_message)
    assert description is not None
    assert len(description) <= 2_000
    assert docstring is not None
    assert len(docstring) <= 2_500
