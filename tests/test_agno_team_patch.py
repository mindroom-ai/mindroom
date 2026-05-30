"""Integration tests for MindRoom's vendored Agno Team message patch."""
# ruff: noqa: D101, D102, D103

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from agno.agent import _messages as agent_messages
from agno.media import Image
from agno.models.message import Message
from agno.models.openai.chat import OpenAIChat
from agno.models.response import ModelResponse
from agno.run.base import RunContext
from agno.run.team import TeamRunOutput
from agno.session.team import TeamSession
from agno.team import Team, _messages
from pydantic import BaseModel

from mindroom.attachment_media import (
    _INLINE_MEDIA_RECORDS_BY_ID,
    _INLINE_MEDIA_RECORDS_BY_PATH,
    resolve_attachment_media,
)
from mindroom.attachments import AttachmentRecord, register_local_attachment
from mindroom.history import agno_team_patch

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from agno.run.messages import RunMessages


@pytest.fixture(autouse=True)
def _clear_inline_media_caches() -> Iterator[None]:
    _INLINE_MEDIA_RECORDS_BY_ID.clear()
    _INLINE_MEDIA_RECORDS_BY_PATH.clear()
    yield
    _INLINE_MEDIA_RECORDS_BY_ID.clear()
    _INLINE_MEDIA_RECORDS_BY_PATH.clear()


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


def _register_image_attachment(
    tmp_path: Path,
    attachment_id: str,
    payload: bytes,
) -> AttachmentRecord:
    image_path = tmp_path / f"{attachment_id}.png"
    image_path.write_bytes(payload)
    record = register_local_attachment(
        tmp_path,
        image_path,
        kind="image",
        attachment_id=attachment_id,
        filename=image_path.name,
        mime_type="image/png",
    )
    assert record is not None
    return record


def _image(record: AttachmentRecord, *, image_id: str | None = None) -> Image:
    return Image(
        id=image_id if image_id is not None else record.attachment_id,
        filepath=record.local_path,
        mime_type=record.mime_type,
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


async def _patched_history_run_messages(
    tmp_path: Path,
    records: list[AttachmentRecord],
    history_messages: list[Message],
    current_images: list[Image],
) -> RunMessages:
    resolve_attachment_media(tmp_path, [record.attachment_id for record in records])
    model = RecordingOpenAIChat(id="gpt-test", api_key="sk-test")
    team = _team(model)
    team.num_history_runs = len(history_messages)
    session = TeamSession(
        session_id="session",
        runs=[
            TeamRunOutput(
                run_id=f"history-{index}",
                session_id="session",
                messages=[message],
            )
            for index, message in enumerate(history_messages)
        ],
    )
    return await _messages._aget_run_messages(
        team,
        run_response=TeamRunOutput(run_id="run", session_id="session"),
        run_context=RunContext(run_id="run", session_id="session"),
        session=session,
        add_history_to_context=True,
        images=current_images,
    )


def _all_images(run_messages: RunMessages) -> list[Image]:
    return [image for message in run_messages.messages for image in (message.images or [])]


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


def test_agno_team_builder_reuses_user_message_instance_in_messages() -> None:
    model = RecordingOpenAIChat(id="gpt-test", api_key="sk-test")
    team = _team(model)
    input_message = Message(role="user", content="current")
    run_messages = _messages._get_run_messages(
        team,
        run_response=TeamRunOutput(run_id="run", session_id="session"),
        run_context=RunContext(run_id="run", session_id="session"),
        session=TeamSession(session_id="session"),
        input_message=input_message,
    )

    assert run_messages.user_message is input_message
    assert run_messages.messages[-1] is run_messages.user_message


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


@pytest.mark.asyncio
async def test_cross_run_image_dedupe_collapses_22_replays_to_3_inlines(tmp_path: Path) -> None:
    records = [
        _register_image_attachment(tmp_path, "att_a", b"\x89PNG\r\n\x1a\na"),
        _register_image_attachment(tmp_path, "att_b", b"\x89PNG\r\n\x1a\nb"),
        _register_image_attachment(tmp_path, "att_c", b"\x89PNG\r\n\x1a\nc"),
    ]
    history_messages = [
        Message(role="user", content=f"history {index}", images=[_image(record) for record in records])
        for index in range(22)
    ]

    run_messages = await _patched_history_run_messages(
        tmp_path,
        records,
        history_messages,
        [_image(record) for record in records],
    )

    images = _all_images(run_messages)
    history_user_messages = [
        message for message in run_messages.messages if message.from_history and message.role == "user"
    ]
    assert len(images) == 3
    assert {image.id for image in images} == {"att_a", "att_b", "att_c"}
    assert len(history_user_messages) == 22
    assert all((message.images or []) == [] for message in history_user_messages)
    assert run_messages.user_message is not None
    assert {image.id for image in (run_messages.user_message.images or [])} == {"att_a", "att_b", "att_c"}


@pytest.mark.asyncio
async def test_two_att_ids_with_identical_bytes_collapse_to_one(tmp_path: Path) -> None:
    older = _register_image_attachment(tmp_path, "att_older", b"\x89PNG\r\n\x1a\nsame")
    newer = _register_image_attachment(tmp_path, "att_newer", b"\x89PNG\r\n\x1a\nsame")
    history_message = Message(role="user", content="history", images=[_image(older)])

    run_messages = await _patched_history_run_messages(
        tmp_path,
        [older, newer],
        [history_message],
        [_image(newer)],
    )

    history_user_messages = [
        message for message in run_messages.messages if message.from_history and message.role == "user"
    ]
    assert [image.id for image in _all_images(run_messages)] == ["att_newer"]
    assert run_messages.user_message is not None
    assert [image.id for image in (run_messages.user_message.images or [])] == ["att_newer"]
    assert len(history_user_messages) == 1
    assert history_user_messages[0].images == []


@pytest.mark.asyncio
async def test_current_image_wins_over_matching_history_image(tmp_path: Path) -> None:
    record = _register_image_attachment(tmp_path, "att_same", b"\x89PNG\r\n\x1a\nsame")
    history_message = Message(role="user", content="history", images=[_image(record)])

    run_messages = await _patched_history_run_messages(
        tmp_path,
        [record],
        [history_message],
        [_image(record)],
    )

    history_user_messages = [
        message for message in run_messages.messages if message.from_history and message.role == "user"
    ]
    assert [image.id for image in _all_images(run_messages)] == ["att_same"]
    assert run_messages.user_message is not None
    assert [image.id for image in (run_messages.user_message.images or [])] == ["att_same"]
    assert len(history_user_messages) == 1
    assert history_user_messages[0].images == []


@pytest.mark.asyncio
async def test_non_att_image_id_not_used_as_lookup_key(tmp_path: Path) -> None:
    wrong_record = _register_image_attachment(tmp_path, "att_wrong", b"\x89PNG\r\n\x1a\nwrong")
    filepath_record = _register_image_attachment(tmp_path, "att_filepath", b"\x89PNG\r\n\x1a\nfilepath")
    random_id = "random-uuid-from-agno"
    resolve_attachment_media(tmp_path, [wrong_record.attachment_id, filepath_record.attachment_id])
    _INLINE_MEDIA_RECORDS_BY_ID[random_id] = wrong_record
    history_message = Message(role="user", content="history", images=[_image(filepath_record, image_id=random_id)])

    run_messages = await _patched_history_run_messages(
        tmp_path,
        [wrong_record, filepath_record],
        [history_message],
        [_image(filepath_record)],
    )

    history_user_messages = [
        message for message in run_messages.messages if message.from_history and message.role == "user"
    ]
    assert [image.id for image in _all_images(run_messages)] == ["att_filepath"]
    assert run_messages.user_message is not None
    assert [image.id for image in (run_messages.user_message.images or [])] == ["att_filepath"]
    assert len(history_user_messages) == 1
    assert history_user_messages[0].images == []


def test_apply_patch_is_idempotent() -> None:
    patched_team_sync = _messages._get_run_messages
    patched_team_async = _messages._aget_run_messages
    patched_agent_sync = agent_messages.get_run_messages
    patched_agent_async = agent_messages.aget_run_messages

    agno_team_patch.apply_patch()
    agno_team_patch.apply_patch()

    assert _messages._get_run_messages is patched_team_sync
    assert _messages._aget_run_messages is patched_team_async
    assert agent_messages.get_run_messages is patched_agent_sync
    assert agent_messages.aget_run_messages is patched_agent_async


def test_media_content_key_ignores_empty_filepath() -> None:
    assert agno_team_patch._media_content_key("image", Image(filepath="")) is None


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
