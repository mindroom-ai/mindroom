"""Behavior of the ``mindroom.llm`` values that the codecs do not cover."""
# ruff: noqa: D103

from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError, replace
from typing import TYPE_CHECKING

import pytest

from mindroom.llm.errors import (
    ContextWindowExceeded,
    InvalidRequest,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from mindroom.llm.events import (
    ReasoningDelta,
    ReportedCall,
    RequestCompleted,
    RunFinished,
    ToolCompleted,
    ToolStarted,
)
from mindroom.llm.media import Audio, File, Image, Video, canonical_mime_type
from mindroom.llm.messages import Message
from mindroom.llm.provider import (
    Capabilities,
    ModelIdentity,
    ModelRequest,
    ModelTurn,
    RequestOverrides,
    StreamProgress,
    TextDelta,
    ToolCallReady,
    ToolSpec,
    TurnCompleted,
    UsageReported,
    complete,
)
from mindroom.llm.runs import RunRecord, RunRequirement, RunStatus, TeamRunRecord, ToolExecution
from mindroom.llm.usage import MessageMetrics, RunMetrics

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from mindroom.llm.provider import StreamEvent


# --- media -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [Image, Audio, Video])
def test_media_requires_exactly_one_source(kind: type[Image | Audio | Video]) -> None:
    with pytest.raises(ValueError, match="One of 'url', 'filepath', or 'content' must be provided"):
        kind()
    with pytest.raises(ValueError, match="Only one of"):
        kind(url="https://example.invalid/x", content=b"x")

    assert kind(content=b"x").content == b"x"


def test_file_needs_no_source_and_allows_several() -> None:
    # Agno accepted a file known only by its id or a provider-side object, and stored runs hold such files.
    bare = File(id="provider-file", mime_type="application/pdf")
    both = File(url="https://example.invalid/f", filepath="/data/f")

    assert bare.to_dict() == {"id": "provider-file", "mime_type": "application/pdf"}
    assert (both.url, both.filepath) == ("https://example.invalid/f", "/data/f")


def test_media_get_unique_ids_and_are_frozen() -> None:
    first, second = Image(content=b"a"), Image(content=b"b")

    assert first.id != second.id
    with pytest.raises(FrozenInstanceError):
        first.detail = "high"  # type: ignore[misc]
    assert replace(first, detail="high").detail == "high"


def test_file_mime_type_is_not_restricted() -> None:
    document = File(filepath="/data/model.safetensors", mime_type="application/octet-stream")

    assert document.to_dict()["mime_type"] == "application/octet-stream"


def test_file_text_content_round_trips_by_mime_type() -> None:
    text = File(content="héllo".encode(), mime_type="text/plain")
    binary = File(content=b"\x00\x01", mime_type="application/octet-stream")
    looks_like_base64 = File(content=b"TestData", mime_type="text/plain")

    assert text.to_dict()["content"] == "héllo"
    assert binary.to_dict()["content"] == "AAE="
    assert File.from_dict(looks_like_base64.to_dict()).content == b"TestData"
    assert File.from_dict(binary.to_dict()).content == b"\x00\x01"


def test_file_to_dict_keeps_text_content_and_base64_encodes_text_bytes_that_are_not_utf8() -> None:
    # A tool's Agno ``File`` can carry its content as text, which the stored run keeps as it is.
    assert File(content="plain text").to_dict()["content"] == "plain text"
    # Latin-1 text bytes cannot be stored as UTF-8 text, so they are stored as base64 like binary content; reading
    # a text/* file decodes its stored content as UTF-8 text, as Agno did, so these bytes do not come back.
    assert File(content="café".encode("latin-1"), mime_type="text/plain").to_dict()["content"] == "Y2Fm6Q=="


def test_stored_content_that_is_not_base64_reads_as_its_utf8_bytes() -> None:
    # Agno's reader fell back to the text's UTF-8 bytes when the stored content was not valid base64.
    assert Image.from_dict({"content": "abc"}).content == b"abc"


def test_media_to_dict_without_content_keeps_scalars() -> None:
    audio = Audio(id="a", content=b"x", mime_type="audio/wav", transcript="hi")

    assert audio.to_dict(include_content=False) == {
        "id": "a",
        "mime_type": "audio/wav",
        "sample_rate": 24000,
        "channels": 1,
        "transcript": "hi",
    }


# --- messages --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "canonical"),
    [
        ("Image/PNG; charset=binary", "image/png"),
        ("application/pdf", "application/pdf"),
        (" ; q=1", None),
        (None, None),
    ],
)
def test_a_declared_mime_type_reads_as_its_lowercase_type_without_parameters(
    declared: str | None,
    canonical: str | None,
) -> None:
    """A Content-Type header or an attachment's MIME type is compared as a bare lowercase ``type/subtype``."""
    assert canonical_mime_type(declared) == canonical


def test_message_defaults() -> None:
    first, second = Message(role="user", content="a"), Message(role="user", content="b")

    assert first.id != second.id
    assert isinstance(first.created_at, int)
    assert first.metrics == MessageMetrics()
    assert (first.from_history, first.add_to_agent_memory) == (False, True)
    assert Message.from_dict({"role": "user"}).to_dict().keys() == {
        "id",
        "from_history",
        "stop_after_tool_call",
        "role",
        "created_at",
    }


def test_message_is_mutable_and_copies_independently() -> None:
    message = Message(role="assistant", content="draft", provider_data={"signature": "s"})
    message.content = "final"
    message.from_history = True
    message.metrics.input_tokens = 7
    duplicate = copy.deepcopy(message)
    duplicate.provider_data["signature"] = "other"  # type: ignore[index]

    assert (message.content, message.from_history, message.metrics.input_tokens) == ("final", True, 7)
    assert message.provider_data == {"signature": "s"}
    assert replace(message, from_history=False).from_history is False


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("plain", "plain"),
        (None, ""),
        ([], ""),
        ([{"type": "text", "text": "first"}, {"type": "text", "text": "second"}], "first"),
        (
            [{"type": "image_url", "image_url": {"url": "data:,"}}],
            '[{"type": "image_url", "image_url": {"url": "data:,"}}]',
        ),
        (["ünicode"], '["ünicode"]'),
    ],
)
def test_message_get_content_string(content: str | list[object] | None, expected: str) -> None:
    assert Message(role="user", content=content).get_content_string() == expected


def test_message_to_dict_drops_empty_values_but_keeps_false_and_zero() -> None:
    message = Message(
        role="tool",
        content="",
        tool_args={},
        tool_calls=[],
        provider_data={},
        tool_call_error=False,
        created_at=0,
        id="m",
    )

    assert message.to_dict() == {
        "id": "m",
        "content": "",
        "from_history": False,
        "stop_after_tool_call": False,
        "role": "tool",
        "tool_call_error": False,
        "created_at": 0,
    }


def test_message_from_dict_requires_a_role() -> None:
    with pytest.raises(KeyError):
        Message.from_dict({"content": "no role"})


# --- runs ------------------------------------------------------------------------------------------------------


def test_tool_execution_is_paused_for_confirmation_or_external_execution() -> None:
    assert ToolExecution(tool_name="t").is_paused is False
    assert ToolExecution(tool_name="t", requires_confirmation=True).is_paused is True
    assert ToolExecution(tool_name="t", external_execution_required=True).is_paused is True


def test_run_requirement_rejects_decisions_it_does_not_need() -> None:
    plain = RunRequirement(ToolExecution(tool_name="t"))
    assert plain.is_resolved() is True
    with pytest.raises(ValueError, match="does not require confirmation"):
        plain.confirm()
    with pytest.raises(ValueError, match="does not require external execution"):
        plain.set_external_execution_result("x")

    confirmation = RunRequirement(ToolExecution(tool_name="t", requires_confirmation=True))
    confirmation.confirm()
    with pytest.raises(ValueError, match="does not require confirmation"):
        confirmation.reject("twice")


def test_run_requirement_positional_tool_execution_and_defaults() -> None:
    execution = ToolExecution(tool_name="t", requires_confirmation=True)
    first, second = RunRequirement(execution), RunRequirement(execution)

    assert first.id != second.id
    assert first.created_at.tzinfo is not None
    assert first.needs_confirmation is True
    assert first.to_dict()["created_at"] == first.created_at.isoformat()


def test_run_record_defaults_and_status() -> None:
    record = RunRecord(run_id="r", agent_id="a")

    assert record.status is RunStatus.running
    assert record.messages == []
    assert (record.tools, record.requirements, record.metrics) == (None, None, None)
    assert record.to_dict()["status"] == "RUNNING"
    assert RunStatus("PAUSED") is RunStatus.paused
    assert RunStatus.cancelled.name == "cancelled"
    assert RunRecord.from_dict({"run_id": "r", "status": "REGENERATED"}).status is RunStatus.regenerated


def test_team_run_record_defaults() -> None:
    record = TeamRunRecord(run_id="t", team_id="team")

    assert record.member_responses == []
    assert record.parent_run_id is None
    assert "agent_id" not in record.to_dict()


# --- usage -----------------------------------------------------------------------------------------------------


def test_run_metrics_sum_keeps_the_cost_only_one_run_reported() -> None:
    # A team turn sums its member runs, and only some providers, such as OpenRouter, report a request's cost.
    priced = RunMetrics(input_tokens=10, cost=0.25, duration=2.0)
    unpriced = RunMetrics(input_tokens=5, duration=1.0)

    total = priced + unpriced
    assert (total.input_tokens, total.cost, total.duration) == (15, 0.25, 3.0)
    assert (unpriced + priced).cost == 0.25


# --- provider --------------------------------------------------------------------------------------------------


class _ScriptedProvider:
    identity = ModelIdentity(id="scripted", provider="Synthetic", provider_name="Synthetic")
    capabilities = Capabilities()
    native_history_route = None
    replay_estimator = None

    def __init__(self, events: Sequence[StreamEvent]) -> None:
        self._events = events
        self.requests: list[ModelRequest] = []
        self.model_params: dict[str, object] = {}

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        for event in self._events:
            yield event


@pytest.mark.asyncio
async def test_complete_drains_the_stream_and_returns_the_turn() -> None:
    final = Message(
        role="assistant",
        content="Hello there",
        tool_calls=[{"id": "c1"}],
        metrics=MessageMetrics(input_tokens=3),
    )
    provider = _ScriptedProvider(
        [
            TextDelta("Hello"),
            ReasoningDelta("hmm"),
            ToolCallReady({"id": "c1"}),
            UsageReported(MessageMetrics(input_tokens=3), final=False),
            StreamProgress(),
            TextDelta(" there"),
            TurnCompleted(message=final, stop_reason="tool_use"),
        ],
    )
    request = ModelRequest(
        messages=[Message(role="user", content="hi")],
        tools=[ToolSpec(name="t", description="d", parameters={"type": "object"})],
    )

    turn = await complete(provider, request)

    assert turn == ModelTurn(message=final, stop_reason="tool_use")
    assert (turn.content, turn.tool_calls, turn.usage) == (
        "Hello there",
        [{"id": "c1"}],
        MessageMetrics(input_tokens=3),
    )
    assert provider.requests == [request]


@pytest.mark.asyncio
async def test_complete_raises_when_the_stream_never_completes_the_turn() -> None:
    provider = _ScriptedProvider([TextDelta("partial")])

    with pytest.raises(ProviderUnavailable, match="without completing the turn") as info:
        await complete(provider, ModelRequest(messages=[]))

    assert (info.value.provider, info.value.retryable) == ("Synthetic", True)


def test_model_request_defaults_are_immutable_and_empty() -> None:
    request = ModelRequest(messages=())

    assert (request.tools, request.tool_choice, request.response_format) == ((), None, None)
    assert request.overrides == RequestOverrides()
    assert RequestOverrides().disable_thinking is False
    with pytest.raises(FrozenInstanceError):
        request.tool_choice = "none"  # type: ignore[misc]


def test_capabilities_default_to_the_least_capable_route() -> None:
    assert Capabilities() == Capabilities(structured_output="json_mode", omitted_media=frozenset())
    assert Capabilities(claude_family=True, omitted_media=frozenset({"video"})).omitted_media == {"video"}


# --- errors ----------------------------------------------------------------------------------------------------


def test_provider_error_fields_and_retryable_classes() -> None:
    error = ProviderError("boom", provider="OpenAI Chat", status_code=500, code="server_error")

    assert str(error) == "boom"
    assert (error.provider, error.status_code, error.code, error.retryable) == (
        "OpenAI Chat",
        500,
        "server_error",
        False,
    )
    assert RateLimited("slow down", provider="p", status_code=429).retryable is True
    assert ProviderUnavailable("down", provider="p").retryable is True
    assert ContextWindowExceeded("too long", provider="p").retryable is False
    assert InvalidRequest("bad", provider="p", status_code=400).retryable is False
    assert all(
        issubclass(kind, ProviderError)
        for kind in (RateLimited, ProviderUnavailable, ContextWindowExceeded, InvalidRequest)
    )


# --- events ----------------------------------------------------------------------------------------------------


def test_run_events_are_frozen_values() -> None:
    tool = ToolExecution(tool_call_id="c", tool_name="t", tool_args={})
    record = RunRecord(run_id="r", agent_id="a", status=RunStatus.completed)
    events = [
        TextDelta("hi"),
        ReasoningDelta("why"),
        ToolStarted(tool.call),
        ToolCompleted(tool.call),
        RequestCompleted(model_id="m", provider="P", usage=MessageMetrics(output_tokens=1)),
        RunFinished(record),
    ]

    assert [type(event).__name__ for event in events] == [
        "TextDelta",
        "ReasoningDelta",
        "ToolStarted",
        "ToolCompleted",
        "RequestCompleted",
        "RunFinished",
    ]
    assert events[5].record is record  # type: ignore[union-attr]
    with pytest.raises(FrozenInstanceError):
        events[0].text = "changed"  # type: ignore[misc]


def test_a_tool_event_reports_the_call_as_it_was_when_the_event_was_made() -> None:
    tool = ToolExecution(tool_call_id="c", tool_name="t", tool_args={"q": 1})
    started = ToolStarted(tool.call)
    tool.result = "done"
    tool.is_error = False

    assert started.call == ReportedCall("c", "t", {"q": 1})
    assert ToolCompleted(tool.call).call == ReportedCall("c", "t", {"q": 1}, result="done", is_error=False)
    with pytest.raises(FrozenInstanceError):
        started.call.result = "changed"  # type: ignore[misc]
