"""The owned OpenAI-family adapters send the requests and parse the replies the Agno-backed layer did."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.llm.provider import TextDelta, ToolCallReady, TurnCompleted
from tests.providers.golden_support import (
    expected_body,
    expected_stop_reason,
    golden_id,
    golden_paths,
    load_golden,
    ordered_json,
)
from tests.providers.openai_family_support import OPENAI_FAMILIES, replay_golden

if TYPE_CHECKING:
    from pathlib import Path

GOLDENS = golden_paths(OPENAI_FAMILIES)


def test_every_openai_family_golden_is_replayed() -> None:
    """The parity suite covers every golden of the families the owned adapters serve."""
    assert {path.parent.name for path in GOLDENS} == set(OPENAI_FAMILIES)
    assert len(GOLDENS) == 255


@pytest.mark.asyncio
@pytest.mark.parametrize("path", GOLDENS, ids=golden_id)
async def test_owned_adapter_reproduces_golden(path: Path, tmp_path: Path) -> None:
    """Request method, URL, kept headers, and body match, and the parsed reply matches field for field."""
    golden = load_golden(path)
    replay = await replay_golden(golden, tmp_path)
    assert replay.request == golden["request"]
    assert ordered_json(replay.request["body"]) == ordered_json(expected_body(golden, replay.request["body"]))
    assert replay.parsed == golden["parsed"]
    assert replay.stop_reason == expected_stop_reason(golden)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", GOLDENS, ids=golden_id)
async def test_a_streamed_reply_yields_its_text_and_tool_calls_and_a_whole_reply_yields_none(
    path: Path,
    tmp_path: Path,
) -> None:
    """A stream's text deltas and ready tool calls add up to its finished message; Codex streams every request."""
    golden = load_golden(path)
    replay = await replay_golden(golden, tmp_path)
    completed = replay.events[-1]
    assert isinstance(completed, TurnCompleted)
    text = "".join(event.text for event in replay.events if isinstance(event, TextDelta))
    calls = [event.call for event in replay.events if isinstance(event, ToolCallReady)]
    if golden["input"]["stream"] or path.parent.name == "codex":
        assert (text, calls) == (completed.message.content or "", completed.message.tool_calls or [])
    else:
        assert (text, calls) == ("", [])
