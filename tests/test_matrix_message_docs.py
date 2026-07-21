"""Tests for matrix_message tool documentation extraction."""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path
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

    assert "default `ignore_mentions=True`" in description
    assert "com.mindroom.skip_mentions" in description
    assert "com.mindroom.original_sender" in description
    assert "Set `False` ONLY" in description
    assert "handoff" in description
    assert "self-trigger" in description

    assert "only `send`, `reply`, and `thread-reply`" in description
    assert "context-scoped `att_*` IDs or local file paths" in description
    assert "combined maximum 5" in description
    assert "text, attachments, or both, but not neither" in description
    assert "Relative paths resolve from the agent workspace" in description

    assert "`message_extras` adds collapsible sections" in description
    assert "`text/plain`" in description
    assert "`text/markdown`" in description
    assert "`text/html`" in description
    assert "Full semantics: `docs/tools/matrix-message.md`" in description


def test_matrix_message_docstring_stays_within_hard_cap() -> None:
    """The complete cleaned method docstring should stay within the issue's hard cap."""
    docstring = inspect.getdoc(MatrixMessageTools.matrix_message)

    assert docstring is not None
    assert len(docstring) <= 2_500


def test_matrix_message_long_form_docstring_was_relocated_verbatim() -> None:
    """The dedicated docs page should preserve the exact former cleaned docstring."""
    page = (Path(__file__).parents[1] / "docs/tools/matrix-message.md").read_text()
    start = "<!-- matrix-message-docstring:start -->\n"
    end = "\n<!-- matrix-message-docstring:end -->"
    relocated = page.split(start, maxsplit=1)[1].split(end, maxsplit=1)[0]

    assert len(relocated) == 7_281
    assert hashlib.sha256(relocated.encode()).hexdigest() == (
        "acef1649f73f923e2bcf96a9f773d41ae276b69cc8c1bce1dc61c94cd871dcdc"
    )


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

    assert action_description.endswith("Action listed above.")

    assert "emoji" in message_description

    assert "att_*" in attachment_ids_description
    assert "combined maximum 5" in attachment_ids_description
    assert attachment_paths_description.endswith("Local paths with the same restrictions.")

    assert "defaults to current" in room_id_description
    assert "react/edit" in target_description

    assert '"room"' in thread_id_description
    assert "room scope" in thread_id_description

    assert "Keep `True`" in ignore_mentions_description
    assert "handoffs/self-triggers" in ignore_mentions_description

    assert "default 20" in limit_description
    assert "maximum 50" in limit_description
