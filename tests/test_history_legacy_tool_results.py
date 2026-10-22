"""Stored runs holding one combined tool-result message replay every result on every provider route."""
# ruff: noqa: D103

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.history.request_messages import TurnMessages, build_request, scope_history_messages
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, ToolSpec, complete
from mindroom.llm.runs import RunRecord
from mindroom.llm.usage import MessageMetrics
from tests.providers.family_support import CLAUDE_DEPRECATION_FILTER, FAMILIES, scrub_provider_env
from tests.providers.golden_support import Wire

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.filterwarnings(CLAUDE_DEPRECATION_FILTER)

_SCOPE = HistoryScope(kind="agent", scope_id="code")


def _call(call_id: str, city: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "get_weather", "arguments": f'{{"city": "{city}"}}'},
    }


# A run as Agno's Gemini adapter stored it: every result of one tool round in a single tool message, the original
# results in its content and, in each entry, the copy Agno sent, which tool-result compression could shorten.
_STORED_RUN = {
    "run_id": "run-1",
    "agent_id": "code",
    "status": "COMPLETED",
    "messages": [
        {"role": "user", "content": "What is the weather in Paris and Oslo?"},
        {"role": "assistant", "tool_calls": [_call("call_paris", "Paris"), _call("call_oslo", "Oslo")]},
        {
            "role": "tool",
            "content": ["sunny", "rainy"],
            "tool_name": "get_weather, get_weather",
            "tool_calls": [
                {"tool_call_id": "call_paris", "tool_name": "get_weather", "content": "compressed: sunny"},
                {"tool_call_id": "call_oslo", "tool_name": "get_weather", "content": "compressed: rainy"},
            ],
            "metrics": {"duration": 0.5},
        },
        {"role": "assistant", "content": "Sunny in Paris, rainy in Oslo."},
    ],
}


def _stored_run() -> RunRecord:
    return RunRecord.from_dict(copy.deepcopy(_STORED_RUN))


def _history(run: RunRecord, *, max_tool_calls: int | None = None) -> list[Message]:
    return scope_history_messages(
        [run],
        scope=_SCOPE,
        num_history_runs=None,
        num_history_messages=None,
        max_tool_calls_from_history=max_tool_calls,
    )


def test_combined_results_replay_as_one_history_message_per_result() -> None:
    run = _stored_run()
    stored = copy.deepcopy(run.messages)

    history = _history(run)

    results = [message for message in history if message.role == "tool"]
    assert [(m.tool_call_id, m.tool_name, m.content, m.tool_calls) for m in results] == [
        ("call_paris", "get_weather", "sunny", None),
        ("call_oslo", "get_weather", "rainy", None),
    ]
    assert all(message.from_history for message in history)
    assert [message.metrics for message in results] == [MessageMetrics(duration=0.5), MessageMetrics()]
    assert run.messages == stored


def test_tool_call_window_counts_each_combined_result() -> None:
    history = _history(_stored_run(), max_tool_calls=1)

    assistant = next(message for message in history if message.tool_calls)
    assert [call["id"] for call in assistant.tool_calls or []] == ["call_oslo"]
    assert [(m.tool_call_id, m.content) for m in history if m.role == "tool"] == [("call_oslo", "rainy")]


def _chat_results(body: dict[str, Any]) -> list[Any]:
    return [message["content"] for message in body["messages"] if message["role"] == "tool"]


def _claude_results(body: dict[str, Any]) -> list[Any]:
    blocks = [
        block for message in body["messages"] if isinstance(message["content"], list) for block in message["content"]
    ]
    return [block["content"] for block in blocks if block["type"] == "tool_result"]


def _gemini_results(body: dict[str, Any]) -> list[Any]:
    parts = [part for content in body["contents"] for part in content["parts"]]
    return [part["functionResponse"]["response"]["result"] for part in parts if "functionResponse" in part]


def _responses_results(body: dict[str, Any]) -> list[Any]:
    return [item["output"] for item in body["input"] if item.get("type") == "function_call_output"]


_RESULTS = {
    "anthropic": _claude_results,
    "cerebras": _chat_results,
    "gemini": _gemini_results,
    "gemini_vertexai": _gemini_results,
    "ollama": _chat_results,
    "openai_chat": _chat_results,
    "openai_responses": _responses_results,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("family", sorted(_RESULTS))
async def test_combined_results_reach_every_route(family: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scrub_provider_env(monkeypatch)
    spec = FAMILIES[family]
    wire = Wire(lambda _request: spec.reply())
    messages = build_request(
        system=[Message(role="system", content="You are Code.")],
        summary=None,
        history=_history(_stored_run()),
        turn=TurnMessages(prompt=Message(role="user", content="And tomorrow?")),
    )
    tool = ToolSpec(name="get_weather", description="Weather.", parameters={"type": "object", "properties": {}})

    await complete(spec.adapter(tmp_path, wire), ModelRequest(messages=messages, tools=[tool]))

    assert _RESULTS[family](wire.body()) == ["sunny", "rainy"]
