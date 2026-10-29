"""The owned Claude adapters reproduce every provider golden of the Anthropic wire family.

Each golden pins the request Agno 3.0.9 plus MindRoom's patches sent for ``anthropic``, ``vertexai_claude``,
or ``bedrock_claude`` and the assistant message they parsed from the canned reply; the owned adapter must
produce the same request (method, URL, kept headers, JSON body) and the same message, metrics, and stop reason,
and its output events must add up to that message. The histories the goldens replay hold Claude turns as earlier
releases stored them, so a turn this release stores must replay the same request bytes in their place.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

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
# The ``provider_data`` keys that hold a stored Claude turn's blocks, in this release's shape and in earlier ones'.
_STORED_BLOCK_KEYS = frozenset({"claude_blocks", "content_blocks", "server_tool_blocks", "signature"})


def _turn_without_blocks(message: dict[str, Any]) -> dict[str, Any]:
    """Return what a stored turn records apart from its usage and the keys that hold its blocks."""
    turn = {key: value for key, value in message.items() if key not in {"metrics", "provider_data"}}
    provider_data = message.get("provider_data") or {}
    return {
        **turn,
        "provider_data": {key: value for key, value in provider_data.items() if key not in _STORED_BLOCK_KEYS},
    }


def _earlier_turns_stored_anew() -> list[tuple[Path, int, Path]]:
    """Pair each history turn stored in an earlier shape with a golden whose reply this release stores as that turn."""
    replies = {path: load_golden(path)["parsed"]["message"] for path in _FIXTURES}
    pairs = []
    for path in _FIXTURES:
        for index, message in enumerate(load_golden(path)["input"]["messages"]):
            if message["role"] != "assistant" or "content_blocks" not in (message.get("provider_data") or {}):
                continue
            pairs.extend(
                (path, index, reply_path)
                for reply_path, reply in replies.items()
                if reply_path.parent == path.parent and _turn_without_blocks(reply) == _turn_without_blocks(message)
            )
    return pairs


_EARLIER_TURNS_STORED_ANEW = _earlier_turns_stored_anew()


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


def test_every_reshaped_reply_replays_in_place_of_its_earlier_shape() -> None:
    """Each reply whose stored shape changed most, cited, server-tool, signed, and compacted, is paired below."""
    paired = {(path.parent.name, reply.stem) for path, _, reply in _EARLIER_TURNS_STORED_ANEW}
    for family in FAMILIES:
        assert {(family, "claude_citations"), (family, "reasoning"), (family, "reasoning_tool_call")} <= paired
    for family in ("anthropic", "vertexai_claude"):
        assert {(family, "claude_server_tool_blocks"), (family, "native_compaction")} <= paired


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "index", "reply_path"),
    _EARLIER_TURNS_STORED_ANEW,
    ids=[f"{golden_id(path)}[{index}]<-{reply.stem}" for path, index, reply in _EARLIER_TURNS_STORED_ANEW],
)
async def test_a_turn_stored_now_replays_like_the_same_turn_stored_earlier(
    path: Path,
    index: int,
    reply_path: Path,
    tmp_path: Path,
) -> None:
    """A history turn this release stores sends the request bytes the golden pins for the same turn stored earlier."""
    golden = load_golden(path)
    earlier = golden["input"]["messages"][index]
    golden["input"]["messages"][index] = {**load_golden(reply_path)["parsed"]["message"], "metrics": earlier["metrics"]}

    replay = await run_golden(golden, tmp_path)

    assert json.dumps(replay.request["body"]) == json.dumps(golden["request"]["body"])
