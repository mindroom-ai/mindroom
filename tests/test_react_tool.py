"""The ``react`` toolkit: reaction-only access to the current room, with matrix_message's rate limit."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest

import mindroom.tools  # noqa: F401
from mindroom.custom_tools.matrix_message import MatrixMessageTools
from mindroom.custom_tools.react import ReactTools
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.chat_ui_contract_fixture import ROOM_ID
from tests.chat_ui_contract_fixture import make_chat_ui_context as _context

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _fresh_rate_limit() -> None:
    """Start each test with no recent actions, as the limiter is shared with matrix_message."""
    MatrixMessageTools._recent_actions.clear()


def test_react_registered_with_one_tool() -> None:
    """The toolkit needs room context and offers only ``react(event_id, emoji)``."""
    metadata = builtin_tool_registry().entries["react"]

    assert metadata.requires_room_context
    assert metadata.function_names == ("react",)
    assert tuple(inspect.signature(ReactTools.react).parameters) == ("self", "event_id", "emoji")
    assert sorted(ReactTools().functions) == ["react"]


async def test_react_sends_annotation(tmp_path: Path) -> None:
    """A reaction is one ``m.annotation`` on the event, sent to the current room."""
    context = _context(tmp_path)
    with tool_runtime_context(context):
        result = await ReactTools().react("$target", "🎉")

    assert '"status": "ok"' in result
    context.client.room_send.assert_awaited_once()
    kwargs = context.client.room_send.await_args.kwargs
    assert kwargs["room_id"] == ROOM_ID
    assert kwargs["message_type"] == "m.reaction"
    assert kwargs["content"]["m.relates_to"] == {"rel_type": "m.annotation", "event_id": "$target", "key": "🎉"}


async def test_react_other_room_refused(tmp_path: Path) -> None:
    """An event the current room does not hold is refused and nothing is sent."""
    context = _context(tmp_path)
    context.client.room_get_event = AsyncMock(
        return_value=nio.RoomGetEventError("M_NOT_FOUND", status_code="M_NOT_FOUND"),
    )
    with tool_runtime_context(context):
        result = await ReactTools().react("$elsewhere", "👍")

    assert '"status": "error"' in result
    assert "not in this room" in result
    context.client.room_get_event.assert_awaited_once_with(ROOM_ID, "$elsewhere")
    context.client.room_send.assert_not_awaited()


@pytest.mark.parametrize(
    "failure",
    [nio.RoomGetEventError("M_LIMIT_EXCEEDED", status_code="429"), nio.RoomGetEventError("timeout")],
)
async def test_react_unverifiable_event_is_retryable(tmp_path: Path, failure: nio.RoomGetEventError) -> None:
    """A failed lookup that is not a definite not-found is not reported as another room, so a retry stays sensible."""
    context = _context(tmp_path)
    context.client.room_get_event = AsyncMock(return_value=failure)
    with tool_runtime_context(context):
        result = await ReactTools().react("$target", "👍")

    assert "try again" in result
    assert "not in this room" not in result
    context.client.room_send.assert_not_awaited()


@pytest.mark.parametrize("emoji", ["", "   "])
async def test_react_blank_emoji_refused(tmp_path: Path, emoji: str) -> None:
    """A blank emoji is an error, not the 👍 that matrix_message would default to."""
    context = _context(tmp_path)
    with tool_runtime_context(context):
        result = await ReactTools().react("$target", emoji)

    assert '"status": "error"' in result
    assert "emoji" in result
    context.client.room_send.assert_not_awaited()


@pytest.mark.parametrize("emoji", ["thanks a lot", "123456789", "👍\u200a👍"])
async def test_react_text_key_refused(tmp_path: Path, emoji: str) -> None:
    """A key longer than 8 characters or with whitespace inside is refused, so a reaction never carries text."""
    context = _context(tmp_path)
    with tool_runtime_context(context):
        result = await ReactTools().react("$target", emoji)

    assert '"status": "error"' in result
    assert "at most 8 characters and without spaces" in result
    context.client.room_send.assert_not_awaited()


@pytest.mark.parametrize("emoji", ["👨\u200d👩\u200d👧\u200d👦", "🇳🇱", "👍🏽", "12345678"])
async def test_react_emoji_sequences_allowed(tmp_path: Path, emoji: str) -> None:
    """Emoji sequences such as a family, a flag, or a skin tone stay within the limit and are sent."""
    context = _context(tmp_path)
    with tool_runtime_context(context):
        result = await ReactTools().react("$target", emoji)

    assert '"status": "ok"' in result
    assert context.client.room_send.await_args.kwargs["content"]["m.relates_to"]["key"] == emoji


async def test_react_rate_limited(tmp_path: Path) -> None:
    """The room's matrix_message budget (12 per 30 seconds) is shared, so the 13th action is refused."""
    context = _context(tmp_path)
    with tool_runtime_context(context):
        for _ in range(MatrixMessageTools._RATE_LIMIT_MAX_ACTIONS):
            assert '"status": "ok"' in await ReactTools().react("$target", "👍")
        result = await ReactTools().react("$target", "👍")

    assert "Rate limit exceeded" in result
    assert context.client.room_send.await_count == MatrixMessageTools._RATE_LIMIT_MAX_ACTIONS
