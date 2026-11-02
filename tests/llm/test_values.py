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
from mindroom.llm.usage import TOKEN_FIELDS, MessageMetrics, RunMetrics, TokenCounts
from tests.fakes.token_counter import fake_token_counter

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

    assert (bare.id, bare.url, bare.filepath, bare.content) == ("provider-file", None, None, None)
    assert (both.url, both.filepath) == ("https://example.invalid/f", "/data/f")


def test_media_get_unique_ids_and_are_frozen() -> None:
    first, second = Image(content=b"a"), Image(content=b"b")

    assert first.id != second.id
    with pytest.raises(FrozenInstanceError):
        first.detail = "high"  # type: ignore[misc]
    assert replace(first, detail="high").detail == "high"


def test_file_mime_type_is_not_restricted() -> None:
    document = File(filepath="/data/model.safetensors", mime_type="application/octet-stream")

    assert document.mime_type == "application/octet-stream"


def test_stored_content_that_is_not_base64_reads_as_its_utf8_bytes() -> None:
    # Agno's reader fell back to the text's UTF-8 bytes when the stored content was not valid base64.
    assert Image.from_dict({"content": "abc"}).content == b"abc"


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


def test_message_is_mutable_and_copies_independently() -> None:
    message = Message(role="assistant", content="draft", provider_data={"signature": "s"})
    message.content = "final"
    message.from_history = True
    message.metrics.tokens = TokenCounts(input_tokens=7)
    duplicate = copy.deepcopy(message)
    duplicate.provider_data["signature"] = "other"  # type: ignore[index]

    assert (message.content, message.from_history, message.metrics.tokens.input_tokens) == ("final", True, 7)
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


def test_message_from_dict_requires_a_role() -> None:
    with pytest.raises(KeyError):
        Message.from_dict({"content": "no role"})


# --- runs ------------------------------------------------------------------------------------------------------


def test_tool_execution_is_paused_for_confirmation_or_external_execution() -> None:
    assert ToolExecution(tool_call_id="call-t", tool_name="t").is_paused is False
    assert ToolExecution(tool_call_id="call-t", tool_name="t", requires_confirmation=True).is_paused is True
    assert ToolExecution(tool_call_id="call-t", tool_name="t", external_execution_required=True).is_paused is True


def test_run_requirement_rejects_decisions_it_does_not_need() -> None:
    plain = RunRequirement(ToolExecution(tool_call_id="call-t", tool_name="t"))
    assert plain.is_resolved() is True
    with pytest.raises(ValueError, match="does not require confirmation"):
        plain.confirm()
    with pytest.raises(ValueError, match="does not require external execution"):
        plain.set_external_execution_result("x")

    confirmation = RunRequirement(ToolExecution(tool_call_id="call-t", tool_name="t", requires_confirmation=True))
    confirmation.confirm()
    with pytest.raises(ValueError, match="does not require confirmation"):
        confirmation.reject("twice")


def test_two_requirements_of_one_call_are_the_same_requirement() -> None:
    """A requirement is a view of its call, so every read of a run's requirements gives equal ones."""
    execution = ToolExecution(tool_call_id="call-t", tool_name="t", requires_confirmation=True)
    first, second = RunRequirement(execution), RunRequirement(execution)

    assert first == second
    assert first.needs_confirmation is True
    first.confirm()
    assert second.confirmation is True


def test_run_record_defaults_and_status() -> None:
    record = RunRecord(run_id="r", agent_id="a")

    assert record.status is RunStatus.running
    assert record.messages == []
    assert (record.tools, record.requirements, record.metrics) == (None, [], None)
    assert RunStatus("PAUSED") is RunStatus.paused
    assert RunStatus.cancelled.name == "cancelled"


def test_team_run_record_defaults() -> None:
    record = TeamRunRecord(run_id="t", team_id="team")

    assert record.member_responses == []
    assert record.parent_run_id is None


# --- usage -----------------------------------------------------------------------------------------------------


def test_usage_sums_keep_a_duration_only_one_report_holds() -> None:
    timed = RunMetrics(tokens=TokenCounts(input_tokens=10), duration=2.0)
    untimed = RunMetrics(tokens=TokenCounts(input_tokens=5))

    assert ((timed + untimed).tokens.input_tokens, (timed + untimed).duration) == (15, 2.0)
    assert (untimed + timed).duration == 2.0
    assert (MessageMetrics(duration=0.5) + MessageMetrics()).duration == 0.5
    assert (MessageMetrics() + MessageMetrics()).duration is None


@pytest.mark.parametrize("counter", TOKEN_FIELDS)
def test_usage_with_any_counter_not_zero_is_reported(counter: str) -> None:
    """A provider that reports no usage leaves every counter zero; any other counter, a cache one too, is a report."""
    assert MessageMetrics(tokens=TokenCounts(**{counter: 1})).reported
    assert RunMetrics(tokens=TokenCounts(**{counter: 1})).reported
    assert not MessageMetrics(duration=1.5, time_to_first_token=0.5).reported
    assert not RunMetrics(duration=1.5).reported


def test_token_counts_add_and_subtract_every_counter() -> None:
    first = TokenCounts(input_tokens=10, output_tokens=2, cache_read_tokens=4, audio_output_tokens=1)
    second = TokenCounts(input_tokens=3, reasoning_tokens=5, audio_total_tokens=7)

    assert first + second == TokenCounts(
        input_tokens=13,
        output_tokens=2,
        cache_read_tokens=4,
        reasoning_tokens=5,
        audio_output_tokens=1,
        audio_total_tokens=7,
    )
    assert (first + second) - second == first


def test_token_counts_with_totals_sums_input_and_output_and_the_audio_parts() -> None:
    counts = TokenCounts(
        input_tokens=10,
        output_tokens=2,
        total_tokens=99,
        cache_read_tokens=8,
        audio_input_tokens=3,
        audio_output_tokens=1,
    )

    assert counts.with_totals() == TokenCounts(
        input_tokens=10,
        output_tokens=2,
        total_tokens=12,
        cache_read_tokens=8,
        audio_input_tokens=3,
        audio_output_tokens=1,
        audio_total_tokens=4,
    )


def test_token_counts_dict_lists_every_counter_in_report_order() -> None:
    assert list(TokenCounts().to_dict()) == [
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "audio_input_tokens",
        "audio_output_tokens",
        "audio_total_tokens",
    ]
    assert set(TokenCounts().to_dict().values()) == {0}


def test_token_counts_dict_puts_the_first_names_first_and_keeps_every_other_counter() -> None:
    counts = TokenCounts(input_tokens=1, reasoning_tokens=6, audio_total_tokens=9)

    assert list(counts.to_dict(first=("audio_total_tokens", "input_tokens")).items()) == [
        ("audio_total_tokens", 9),
        ("input_tokens", 1),
        ("output_tokens", 0),
        ("total_tokens", 0),
        ("cache_read_tokens", 0),
        ("cache_write_tokens", 0),
        ("reasoning_tokens", 6),
        ("audio_input_tokens", 0),
        ("audio_output_tokens", 0),
    ]
    with pytest.raises(KeyError, match="retired_tokens"):
        counts.to_dict(first=("input_tokens", "retired_tokens"))


# --- provider --------------------------------------------------------------------------------------------------


class _ScriptedProvider:
    identity = ModelIdentity(id="scripted", provider="Synthetic")
    capabilities = Capabilities()
    native_history_route = None
    token_counter = fake_token_counter()

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
        metrics=MessageMetrics(tokens=TokenCounts(input_tokens=3)),
    )
    provider = _ScriptedProvider(
        [
            TextDelta("Hello"),
            ReasoningDelta("hmm"),
            ToolCallReady({"id": "c1"}),
            UsageReported(MessageMetrics(tokens=TokenCounts(input_tokens=3)), final=False),
            StreamProgress(),
            TextDelta(" there"),
            TurnCompleted(message=final, stop_reason="tool_use"),
        ],
    )
    request = ModelRequest(
        messages=[Message(role="user", content="hi")],
        tools=[
            ToolSpec(
                name="t",
                description="d",
                parameters={"type": "object"},
                strict=None,
            ),
        ],
    )

    turn = await complete(provider, request)

    assert turn == ModelTurn(message=final, stop_reason="tool_use")
    assert (turn.content, turn.tool_calls, turn.usage) == (
        "Hello there",
        [{"id": "c1"}],
        MessageMetrics(tokens=TokenCounts(input_tokens=3)),
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

    assert (request.tools, request.tool_choice, request.output_schema) == ((), None, None)
    assert request.overrides == RequestOverrides()
    assert RequestOverrides().omit_thinking_setting is False
    with pytest.raises(FrozenInstanceError):
        request.tool_choice = "none"  # type: ignore[misc]


def test_capabilities_default_to_the_least_capable_route() -> None:
    assert Capabilities() == Capabilities(structured_output="json_mode", omitted_media=frozenset())
    assert Capabilities(omitted_media=frozenset({"video"})).omitted_media == {"video"}


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
        RunFinished(record),
    ]

    assert [type(event).__name__ for event in events] == [
        "TextDelta",
        "ReasoningDelta",
        "ToolStarted",
        "ToolCompleted",
        "RunFinished",
    ]
    assert events[4].record is record  # type: ignore[union-attr]
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
