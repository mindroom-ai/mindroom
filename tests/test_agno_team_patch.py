"""Integration tests for MindRoom's vendored Agno Team message patch."""
# ruff: noqa: D101, D102, D103

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from agno.models.message import Message
from agno.models.openai.chat import OpenAIChat
from agno.models.response import ModelResponse
from agno.run.base import RunContext
from agno.run.team import TeamRunOutput
from agno.session.team import TeamSession
from agno.team import Team, _messages
from pydantic import BaseModel

from mindroom.history import agno_team_patch

if TYPE_CHECKING:
    from agno.run.messages import RunMessages


@dataclass
class RecordingOpenAIChat(OpenAIChat):
    """OpenAI formatter-backed model that records provider-bound messages."""

    formatted_messages: list[dict[str, Any]] = field(default_factory=list)
    raw_messages: list[Message] = field(default_factory=list)

    def _record(self, messages: list[Message], compress_tool_results: bool) -> None:
        self.raw_messages = list(messages)
        self.formatted_messages = self._format_all_messages(messages, compress_tool_results)

    def invoke(self, messages: list[Message], *_args: object, **kwargs: object) -> ModelResponse:
        self._record(messages, bool(kwargs.get("compress_tool_results", False)))
        return ModelResponse(content="ok")

    async def ainvoke(self, messages: list[Message], *_args: object, **kwargs: object) -> ModelResponse:
        self._record(messages, bool(kwargs.get("compress_tool_results", False)))
        return ModelResponse(content="ok")


class ExampleInput(BaseModel):
    value: str


def _team(model: RecordingOpenAIChat) -> Team:
    return Team(
        name="patch-team",
        model=model,
        members=[],
        markdown=False,
        telemetry=False,
    )


async def _patched_run_messages(
    team: Team,
    input_message: list[Message],
    *,
    use_async: bool,
) -> RunMessages:
    run_response = TeamRunOutput(run_id="run", session_id="session")
    run_context = RunContext(run_id="run", session_id="session")
    session = TeamSession(session_id="session")
    if use_async:
        return await _messages._aget_run_messages(
            team,
            run_response=run_response,
            run_context=run_context,
            session=session,
            input_message=input_message,
        )
    return _messages._get_run_messages(
        team,
        run_response=run_response,
        run_context=run_context,
        session=session,
        input_message=input_message,
    )


def _conversation_messages(model: RecordingOpenAIChat) -> list[dict[str, Any]]:
    return [
        message
        for message in model.formatted_messages
        if message["role"] in {"user", "assistant"} and message.get("content") != ""
    ]


@pytest.mark.asyncio
async def test_team_list_message_patch_preserves_roleful_input_through_formatter() -> None:
    model = RecordingOpenAIChat(id="gpt-test", api_key="sk-test")
    team = _team(model)
    historical_answer = Message(role="assistant", content="persisted answer", from_history=True)

    response = await team.arun(
        [
            Message(role="user", content="stored question", from_history=True),
            historical_answer,
            Message(role="user", content="current question"),
        ],
    )

    assert response.content == "ok"
    assert _conversation_messages(model) == [
        {"role": "user", "content": "stored question"},
        {"role": "assistant", "content": "persisted answer"},
        {"role": "user", "content": "current question"},
    ]
    assert historical_answer in model.raw_messages
    assert historical_answer.from_history is True


@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.asyncio
async def test_team_list_message_patch_sets_user_message(use_async: bool) -> None:
    model = RecordingOpenAIChat(id="gpt-test", api_key="sk-test")
    team = _team(model)
    input_message = Message(role="user", content="hi")

    run_messages = await _patched_run_messages(team, [input_message], use_async=use_async)

    assert run_messages.user_message is not None
    assert run_messages.user_message.content == "hi"
    assert run_messages.user_message in run_messages.messages
    assert run_messages.extra_messages == []


@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.asyncio
async def test_team_list_message_patch_get_input_messages_includes_roleful_history(use_async: bool) -> None:
    model = RecordingOpenAIChat(id="gpt-test", api_key="sk-test")
    team = _team(model)
    old_question = Message(role="user", content="old")
    old_answer = Message(role="assistant", content="old")
    current_question = Message(role="user", content="current")

    run_messages = await _patched_run_messages(
        team,
        [old_question, old_answer, current_question],
        use_async=use_async,
    )

    assert run_messages.system_message is not None
    assert run_messages.user_message is current_question
    assert run_messages.extra_messages == [old_question, old_answer]
    assert run_messages.get_input_messages() == [
        run_messages.system_message,
        current_question,
        old_question,
        old_answer,
    ]


@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.asyncio
async def test_team_list_message_patch_preserves_additional_input_separately(use_async: bool) -> None:
    model = RecordingOpenAIChat(id="gpt-test", api_key="sk-test")
    additional_input = Message(role="user", content="older context")
    team = _team(model)
    team.additional_input = [additional_input]
    historical_input = Message(role="assistant", content="previous")
    input_message = Message(role="user", content="current")

    run_messages = await _patched_run_messages(team, [historical_input, input_message], use_async=use_async)

    assert run_messages.user_message is input_message
    assert input_message in run_messages.messages
    assert input_message not in run_messages.extra_messages
    assert run_messages.extra_messages == [historical_input, additional_input]


def test_team_list_message_patch_is_idempotent() -> None:
    patched_sync = _messages._get_run_messages
    patched_async = _messages._aget_run_messages

    agno_team_patch.apply_patch()
    agno_team_patch.apply_patch()

    assert _messages._get_run_messages is patched_sync
    assert _messages._aget_run_messages is patched_async


@pytest.mark.parametrize(
    ("input_message", "expected_content"),
    [
        ("plain text", "plain text"),
        ({"role": "user", "content": "dict text"}, "dict text"),
        (Message(role="user", content="message text"), "message text"),
        (ExampleInput(value="model text"), '{\n  "value": "model text"\n}'),
        ([{"type": "text", "text": "multipart text"}], [{"type": "text", "text": "multipart text"}]),
    ],
)
@pytest.mark.asyncio
async def test_team_patch_keeps_non_roleful_inputs_on_original_path(
    input_message: object,
    expected_content: object,
) -> None:
    model = RecordingOpenAIChat(id="gpt-test", api_key="sk-test")
    team = _team(model)

    await team.arun(input_message)

    conversation = _conversation_messages(model)
    assert len(conversation) == 1
    assert conversation[0] == {"role": "user", "content": expected_content}


@pytest.mark.asyncio
async def test_team_patch_produces_separate_provider_blocks_not_flattened_text() -> None:
    model = RecordingOpenAIChat(id="gpt-test", api_key="sk-test")
    team = _team(model)

    await team.arun(
        [
            Message(role="user", content="first"),
            Message(role="assistant", content="second"),
        ],
    )

    conversation = _conversation_messages(model)
    assert len(conversation) == 2
    assert conversation[0]["role"] == "user"
    assert conversation[1]["role"] == "assistant"
    assert "second" not in conversation[0]["content"]
