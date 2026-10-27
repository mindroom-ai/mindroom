"""Stored runs holding one combined tool-result message convert to one result per call, for replay and compaction."""
# ruff: noqa: D103

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.history.request_messages import TurnMessages, build_request, scope_history_messages
from mindroom.history.summary_input import messages_for_runs
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, ToolSpec, complete
from mindroom.llm.runs import RunRecord, TeamRunRecord
from mindroom.llm.usage import MessageMetrics
from mindroom.media_inputs import MediaInputs
from mindroom.session_storage.schema import TableNames
from tests.history_helpers import replay_settings
from tests.providers.family_support import CLAUDE_DEPRECATION_FILTER, FAMILIES
from tests.providers.golden_support import Wire, hide_provider_environment
from tests.session_storage.agno_layout import converted_store

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


def _stored_run(tmp_path: Path) -> RunRecord:
    run = converted_store(TableNames("code").database_path(tmp_path), _STORED_RUN).get_run("run-1")
    assert isinstance(run, RunRecord)
    return run


def _history(run: RunRecord, *, max_tool_calls: int | None = None) -> list[Message]:
    return scope_history_messages(
        [run],
        scope=_SCOPE,
        num_history_runs=None,
        num_history_messages=None,
        max_tool_calls_from_history=max_tool_calls,
    )


def test_combined_results_replay_as_one_history_message_per_result(tmp_path: Path) -> None:
    run = _stored_run(tmp_path)
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


def test_tool_call_window_counts_each_combined_result(tmp_path: Path) -> None:
    history = _history(_stored_run(tmp_path), max_tool_calls=1)

    assistant = next(message for message in history if message.tool_calls)
    assert [call["id"] for call in assistant.tool_calls or []] == ["call_oslo"]
    assert [(m.tool_call_id, m.content) for m in history if m.role == "tool"] == [("call_oslo", "rainy")]


def test_a_paused_team_runs_members_store_each_combined_result(tmp_path: Path) -> None:
    """A resume read the members a paused team run embedded, so their combined results are split as they convert."""
    member = {**copy.deepcopy(_STORED_RUN), "run_id": "member-1", "status": "PAUSED"}
    team_run = {
        "run_id": "team-1",
        "team_id": "squad",
        "status": "PAUSED",
        "messages": [],
        "member_responses": [member],
    }

    store = converted_store(TableNames("squad").database_path(tmp_path), team_run, kind="team", owner_id="squad")

    assert isinstance(store.get_run("team-1"), TeamRunRecord)
    stored = store.get_run("member-1")
    assert stored is not None
    assert stored.parent_run_id == "team-1"
    results = [(m.tool_call_id, m.content) for m in stored.messages if m.role == "tool"]
    assert results == [("call_paris", "sunny"), ("call_oslo", "rainy")]


def test_compaction_reads_each_combined_result_as_its_own_tool_result(tmp_path: Path) -> None:
    settings = replay_settings(max_tool_calls_from_history=1)

    messages = messages_for_runs([_stored_run(tmp_path)], settings)

    assistant = next(message for message in messages if message.tool_calls)
    assert [call["id"] for call in assistant.tool_calls or []] == ["call_oslo"]
    assert [(m.tool_call_id, m.content) for m in messages if m.role == "tool"] == [("call_oslo", "rainy")]


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
    "groq": _chat_results,
    "ollama": _chat_results,
    "openai_chat": _chat_results,
    "openai_responses": _responses_results,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("family", sorted(FAMILIES))
async def test_combined_results_reach_every_route(family: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hide_provider_environment(monkeypatch)
    spec = FAMILIES[family]
    wire = Wire(lambda _request: spec.reply())
    messages = [
        Message(role="system", content="You are Code."),
        *build_request(
            summary=None,
            history=_history(_stored_run(tmp_path / "store")),
            turn=TurnMessages(prompt=Message(role="user", content="And tomorrow?")),
            media=MediaInputs(),
        ),
    ]
    tool = ToolSpec(
        name="get_weather",
        description="Weather.",
        parameters={"type": "object", "properties": {}},
        strict=None,
    )

    await complete(spec.adapter(tmp_path, wire), ModelRequest(messages=messages, tools=[tool]))

    assert _RESULTS[family](wire.body()) == ["sunny", "rainy"]
