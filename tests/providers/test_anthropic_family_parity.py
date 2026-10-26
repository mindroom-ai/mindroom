"""The owned Claude adapters reproduce every provider golden of the Anthropic wire family.

Each golden pins the request Agno 3.0.9 plus MindRoom's patches sent for ``anthropic``, ``vertexai_claude``,
or ``bedrock_claude`` and the assistant message they parsed from the canned reply; the owned adapter must
produce the same request (method, URL, kept headers, JSON body) and the same message, metrics, and stop reason,
and its output events must add up to that message.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.llm.provider import ReasoningDelta, TextDelta, ToolCallReady, TurnCompleted
from tests.agno_parity.golden_files import render
from tests.providers.anthropic_family_support import FAMILIES, run_golden
from tests.providers.golden_support import (
    FIXTURE_ROOT,
    golden_id,
    golden_paths,
    load_golden,
    regenerated_golden,
)

if TYPE_CHECKING:
    from pathlib import Path

_FIXTURES = golden_paths(FAMILIES)


def test_every_claude_route_has_goldens() -> None:
    """The fixture tree still holds the three Claude routes this suite covers."""
    assert {path.parent.name for path in _FIXTURES} == set(FAMILIES)
    assert all((FIXTURE_ROOT / family).is_dir() for family in FAMILIES)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _FIXTURES, ids=golden_id)
async def test_owned_adapter_reproduces_golden(path: Path, tmp_path: Path) -> None:
    """Replaying a golden's canned reply regenerates it byte for byte: its exact request and parsed reply."""
    fixture = load_golden(path)
    replay = await run_golden(fixture, tmp_path)
    regenerated = regenerated_golden(fixture, replay)
    assert regenerated == fixture
    assert render(regenerated) == path.read_text(encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _FIXTURES, ids=golden_id)
async def test_output_events_add_up_to_the_finished_message(path: Path, tmp_path: Path) -> None:
    """A reply's deltas and ready tool calls add up to its finished message."""
    golden = load_golden(path)
    replay = await run_golden(golden, tmp_path)
    completed = replay.events[-1]
    assert isinstance(completed, TurnCompleted)
    message = completed.message
    outputs = [event for event in replay.events if isinstance(event, TextDelta | ReasoningDelta | ToolCallReady)]
    assert "".join(event.text for event in outputs if isinstance(event, TextDelta)) == (message.content or "")
    reasoning = "".join(event.text for event in outputs if isinstance(event, ReasoningDelta))
    assert reasoning == (message.reasoning_content or "")
    assert [event.call for event in outputs if isinstance(event, ToolCallReady)] == (message.tool_calls or [])
