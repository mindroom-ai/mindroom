"""Ollama reasoning: the native ``thinking`` field streams as reasoning, and ``think`` is a typed setting."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, ReasoningDelta, TextDelta, TurnCompleted
from tests.providers.gemini_ollama_support import ollama_ndjson, run_adapter
from tests.providers.golden_support import hide_provider_environment

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.llm.provider import StreamEvent

_OLLAMA = {"provider": "ollama", "id": "qwen3.8:27b", "host": "http://ollama.test:11434"}
_REQUEST = ModelRequest(messages=[Message(role="user", content="Hi")])


@pytest.fixture(autouse=True)
def _isolated_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    hide_provider_environment(monkeypatch)


def _chunk(*, done: bool = False, **message: str) -> dict[str, Any]:
    chunk: dict[str, Any] = {
        "model": _OLLAMA["id"],
        "created_at": "2026-10-20T00:00:00Z",
        "done": done,
        "message": {"role": "assistant", "content": "", **message},
    }
    if done:
        chunk.update({"done_reason": "stop", "prompt_eval_count": 7, "eval_count": 5})
    return chunk


def _completed(events: list[StreamEvent]) -> TurnCompleted:
    completed = events[-1]
    assert isinstance(completed, TurnCompleted)
    return completed


@pytest.mark.asyncio
async def test_ollama_streams_native_thinking_as_reasoning(tmp_path: Path) -> None:
    """A thinking model's ``thinking`` field streams and is stored as reasoning, apart from the answer."""
    reply = ollama_ndjson(
        _chunk(thinking="Plan"),
        _chunk(thinking=" first."),
        _chunk(content="Hi"),
        _chunk(content=" there"),
        _chunk(done=True),
    )

    _, events = await run_adapter(_OLLAMA, _REQUEST, tmp_path, reply)

    completed = _completed(events)
    assert (completed.message.content, completed.message.reasoning_content) == ("Hi there", "Plan first.")
    assert "".join(event.text for event in events if isinstance(event, ReasoningDelta)) == "Plan first."
    assert "".join(event.text for event in events if isinstance(event, TextDelta)) == "Hi there"


@pytest.mark.asyncio
@pytest.mark.parametrize("think", [True, False, "high"])
async def test_ollama_think_setting_reaches_the_chat_request(tmp_path: Path, think: bool | str) -> None:
    """``extra_kwargs.think`` is sent as authored: on or off, or an effort level for models that take one."""
    config = {**_OLLAMA, "extra_kwargs": {"think": think}}

    request, _ = await run_adapter(config, _REQUEST, tmp_path, ollama_ndjson(_chunk(done=True)))

    assert request["body"]["think"] == think


@pytest.mark.asyncio
async def test_ollama_sends_no_think_setting_unless_one_is_set(tmp_path: Path) -> None:
    """Without ``think``, the model thinks or not as Ollama decides for it."""
    request, _ = await run_adapter(_OLLAMA, _REQUEST, tmp_path, ollama_ndjson(_chunk(done=True)))

    assert "think" not in request["body"]
