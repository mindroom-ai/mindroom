"""Verify the model receives a complete, compact Matrix message schema."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from mindroom.custom_tools.matrix_message import MatrixMessageTools
from tests.agent_tool_helpers import toolkit_offers

if TYPE_CHECKING:
    from mindroom.tool_system.tool_function import ToolFunction


def _matrix_message_function() -> ToolFunction:
    return toolkit_offers(MatrixMessageTools(), "matrix_message")


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
        "limit",
        "idempotency_key",
    }
    assert properties["action"]["enum"] == ["send", "read", "edit", "react"]
    assert function.parameters["required"] == []
    assert all(field["description"] for field in properties.values())


def test_matrix_message_description_stays_compact() -> None:
    """The tool should leave room in the context for the task itself."""
    description = _matrix_message_function().description
    docstring = inspect.getdoc(MatrixMessageTools.matrix_message)
    assert description is not None
    assert len(description) <= 2_000
    assert docstring is not None
    assert len(docstring) <= 2_500
