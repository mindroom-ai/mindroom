"""The Gemini, Ollama, and synthetic adapters reproduce their provider goldens and pinned streams.

For Gemini, Gemini on Vertex AI, and Ollama, each provider golden's request (method, URL, kept headers, JSON body)
and parsed reply (message dict, usage, stored stop reason) must match, with the request sent through the
production pipeline stages. The synthetic model has no golden, so each case pins the plan it streams for its inputs.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from mindroom.config.models import ModelConfig
from mindroom.llm.messages import Message
from mindroom.llm.provider import (
    ModelRequest,
    ReasoningDelta,
    StreamProgress,
    TextDelta,
    ToolCallReady,
    ToolSpec,
    TurnCompleted,
    UsageReported,
)
from mindroom.providers.synthetic_adapter import build_provider as build_synthetic_provider
from tests.agno_parity.golden_files import render
from tests.providers.gemini_ollama_support import (
    FAMILIES,
    Golden,
    goldens,
    replay,
    runtime_paths_for,
)
from tests.providers.golden_support import (
    comparable_metrics,
    hide_provider_environment,
    regenerated_golden,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.llm.provider import StreamEvent, ToolChoice

GOLDENS = goldens()


def test_every_route_has_goldens() -> None:
    """The parity suite iterates real fixtures for each route it covers."""
    assert {golden.family for golden in GOLDENS} == set(FAMILIES)


@pytest.mark.asyncio
@pytest.mark.parametrize("golden", GOLDENS, ids=[golden.id for golden in GOLDENS])
async def test_adapter_reproduces_golden(golden: Golden, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter sends the golden request and parses the golden reply, so the replay regenerates it byte for byte."""
    hide_provider_environment(monkeypatch)

    result = await replay(golden, tmp_path)

    regenerated = regenerated_golden(golden.data, result)
    assert regenerated == golden.data
    assert render(regenerated) == golden.path.read_text(encoding="utf-8")


def _chunks_reporting_usage(golden: Golden) -> int:
    """Count the reply's chunks that carry usage: Ollama's done chunk, and each Gemini chunk with a candidate."""
    raw = golden.data["response_raw"]
    if golden.family == "ollama":
        return sum(1 for chunk in raw["ndjson"] if chunk["done"])
    return sum(1 for event in raw["sse"] if event["data"].get("candidates") and "usageMetadata" in event["data"])


@pytest.mark.asyncio
@pytest.mark.parametrize("golden", GOLDENS, ids=[golden.id for golden in GOLDENS])
async def test_stream_events_match_the_finished_message(
    golden: Golden,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream's deltas add up to the finished message, and usage is final once, as the message's own metrics.

    Every chunk that carries usage reports it as a partial report, and the final report replaces the last of them.
    """
    hide_provider_environment(monkeypatch)

    result = await replay(golden, tmp_path)

    completed = result.events[-1]
    assert isinstance(completed, TurnCompleted)
    message = completed.message
    text = "".join(event.text for event in result.events if isinstance(event, TextDelta))
    reasoning = "".join(event.text for event in result.events if isinstance(event, ReasoningDelta))
    calls = [event.call for event in result.events if isinstance(event, ToolCallReady)]
    reports = [event for event in result.events if isinstance(event, UsageReported)]
    final = [report.usage for report in reports if report.final]
    assert text == (message.content or "")
    assert reasoning == (message.reasoning_content or "")
    assert calls == (message.tool_calls or [])
    assert len(final) == 1
    assert final[0] is message.metrics
    assert comparable_metrics(final[0]) == golden.data["parsed"]["metrics"]
    partial = [comparable_metrics(report.usage) for report in reports if not report.final]
    assert len(partial) == _chunks_reporting_usage(golden)
    assert partial[-1:] in ([], [comparable_metrics(final[0])])
    # Every chunk carries output, usage, or a hosted tool's start, so none shows up as bare provider activity.
    assert not [event for event in result.events if isinstance(event, StreamProgress) and not event.commits]


# ---------------------------------------------------------------------------
# Synthetic: no provider golden exists, so each case pins what the synthetic model streams for its settings and
# messages: chunk count, text length, a sha256 prefix of the text, and the shell call id.
# ---------------------------------------------------------------------------

SHELL = ToolSpec(
    name="run_shell_command",
    description="Run a shell command.",
    parameters={"type": "object"},
    strict=None,
)
FORCED_SHELL = {"seed": 17, "min_response_chars": 128, "max_response_chars": 128, "chunk_chars": 17}
FORCED_CALL_ID = "synthetic-11187ef3211f722f"


def _shell_call(call_id: str) -> dict[str, object]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "run_shell_command", "arguments": '{"args":["echo","hi"]}'},
    }


@dataclass(frozen=True)
class SyntheticCase:
    """One synthetic request and what the synthetic model streams for it."""

    extra_kwargs: dict[str, object]
    messages: list[Message]
    chunks: int
    characters: int
    text_sha256: str
    tools: tuple[ToolSpec, ...] = ()
    tool_choice: ToolChoice | None = None
    call_id: str | None = None


SYNTHETIC_CASES = {
    "defaults": SyntheticCase(
        extra_kwargs={},
        messages=[Message(role="system", content="sys"), Message(role="user", content="hello")],
        chunks=19,
        characters=732,
        text_sha256="03d81634ba23a29c",
    ),
    "forced_shell_call": SyntheticCase(
        extra_kwargs={**FORCED_SHELL, "tool_call_probability": 1},
        messages=[Message(role="user", content="exercise shell")],
        tools=(SHELL,),
        chunks=7,
        characters=103,
        text_sha256="9ff02d9a6cbebad2",
        call_id=FORCED_CALL_ID,
    ),
    "continuation_after_shell_result": SyntheticCase(
        extra_kwargs={**FORCED_SHELL, "tool_call_probability": 1},
        messages=[
            Message(role="user", content="exercise shell"),
            Message(role="assistant", tool_calls=[_shell_call(FORCED_CALL_ID)]),
            Message(role="tool", tool_call_id=FORCED_CALL_ID, tool_name="run_shell_command", content="hi"),
        ],
        tools=(SHELL,),
        chunks=2,
        characters=25,
        text_sha256="213dd68abb669072",
    ),
    "tool_choice_none": SyntheticCase(
        extra_kwargs={**FORCED_SHELL, "tool_call_probability": 1},
        messages=[Message(role="user", content="exercise shell")],
        tools=(SHELL,),
        tool_choice="none",
        chunks=8,
        characters=128,
        text_sha256="9003d29453d1d516",
    ),
    "user_history": SyntheticCase(
        extra_kwargs={"seed": 3},
        messages=[
            Message(role="user", content="first"),
            Message(role="assistant", content="x"),
            Message(role="user", content="second"),
        ],
        tools=(SHELL,),
        chunks=22,
        characters=870,
        text_sha256="b040c628ca6ada9c",
    ),
}


async def _synthetic_events(case: SyntheticCase, root: Path) -> list[StreamEvent]:
    config = ModelConfig(
        provider="synthetic",
        id="lorem-ipsum",
        extra_kwargs={"chars_per_second": 0, **case.extra_kwargs},
    )
    provider = build_synthetic_provider(config, runtime_paths_for(root))
    request = ModelRequest(messages=case.messages, tools=case.tools, tool_choice=case.tool_choice)
    return [event async for event in provider.stream(request)]


def _text(events: list[StreamEvent]) -> str:
    return "".join(event.text for event in events if isinstance(event, TextDelta))


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(SYNTHETIC_CASES))
async def test_synthetic_streams_the_pinned_plan_of_each_case(name: str, tmp_path: Path) -> None:
    """The same seed, settings, and user messages produce the same chunks, text, and shell call."""
    case = SYNTHETIC_CASES[name]

    events = await _synthetic_events(case, tmp_path)

    chunks = [event.text for event in events if isinstance(event, TextDelta)]
    text = _text(events)
    calls = [event.call for event in events if isinstance(event, ToolCallReady)]
    completed = events[-1]
    assert isinstance(completed, TurnCompleted)
    chunk_chars = case.extra_kwargs.get("chunk_chars", 40)
    assert all(len(chunk) == chunk_chars for chunk in chunks[:-1])
    assert (len(chunks), len(text), hashlib.sha256(text.encode()).hexdigest()[:16]) == (
        case.chunks,
        case.characters,
        case.text_sha256,
    )
    assert calls == ([] if case.call_id is None else [_shell_call(case.call_id)])
    assert completed.message.content == text
    assert completed.message.tool_calls == (calls or None)
    assert completed.message.provider_data == {"mindroom_model": {"id": "lorem-ipsum", "provider": "Synthetic"}}
    assert completed.stop_reason == ("tool_use" if calls else "end")


@pytest.mark.asyncio
async def test_synthetic_continuation_finishes_the_interrupted_response(tmp_path: Path) -> None:
    """The text after the shell result completes the Lorem Ipsum the shell call interrupted."""
    texts = {
        name: _text(await _synthetic_events(SYNTHETIC_CASES[name], tmp_path))
        for name in ("forced_shell_call", "continuation_after_shell_result", "tool_choice_none")
    }

    assert texts["forced_shell_call"] + texts["continuation_after_shell_result"] == texts["tool_choice_none"]
    assert texts["tool_choice_none"].startswith("Lorem ipsum")
