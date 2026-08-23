"""Tests for the wire-level guard over replayed and tool-produced model media."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest
from agno.exceptions import (
    ContextWindowExceededError,
    ModelAuthenticationError,
    ModelProviderError,
    ModelRateLimitError,
    RetryableModelProviderError,
)
from agno.media import Audio, File, Image, Video
from agno.models.anthropic import Claude
from agno.models.message import Message
from agno.models.openai import OpenAIChat
from agno.models.response import ModelResponse
from agno.tools.function import Function, ToolResult
from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall, ChoiceDeltaToolCallFunction
from structlog.testing import capture_logs

from mindroom import claude_stream_retry
from mindroom.claude_stream_retry import install_claude_stream_retry_hook
from mindroom.config.models import DebugConfig
from mindroom.error_handling import MODEL_SAFEGUARD_REFUSAL_MESSAGE, ModelSafeguardRefusalError
from mindroom.llm_request_logging import install_llm_request_logging
from mindroom.media_fallback import (
    build_model_media_route,
    filter_media_inputs_for_route,
    message_media_kinds,
    record_replayed_media_isolation,
    retry_media_inputs_after_failure,
    suspected_replayed_media_kinds_for_route,
    unsupported_media_kinds_for_route,
    unsupported_replayed_media_kinds_for_route,
)
from mindroom.media_inputs import MediaInputs
from mindroom.model_media_guard import install_model_media_guard

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable
    from pathlib import Path

    from agno.models.base import Model

    from mindroom.media_fallback import ModelMediaRoute
    from mindroom.media_inputs import MediaKind


def _already_learned(route: ModelMediaRoute | None, *kinds: MediaKind) -> None:
    """Seed the cache the way the guard fills it: one isolation, then the one that confirms it."""
    for kind in kinds:
        record_replayed_media_isolation(route, kind)
        record_replayed_media_isolation(route, kind)


def _omitted_note(*kinds: str) -> str:
    return (
        f"[Attachment omitted: {', '.join(kinds)} content could not be sent with this request. "
        "Say so if your answer depends on it.]"
    )


OMITTED_IMAGE_NOTE = _omitted_note("image")
_CLOSE_FAILURE_MESSAGE = "close blew up"
_CONSUMER_FAILURE_MESSAGE = "consumer gave up"


def _install_guarded_model(
    *,
    ainvoke: Callable[..., object] | None = None,
    ainvoke_stream: Callable[..., object] | None = None,
    model: OpenAIChat | None = None,
) -> OpenAIChat:
    model = model or OpenAIChat(id="history-text-model", base_url="http://localhost:9292/v1")
    if ainvoke is not None:
        vars(model)["ainvoke"] = ainvoke
    if ainvoke_stream is not None:
        vars(model)["ainvoke_stream"] = ainvoke_stream
    install_model_media_guard(cast("Model", model))
    return model


def _history_messages() -> tuple[list[Message], Image]:
    image = Image(content=b"\x89PNG\r\n\x1a\nhistory")
    return (
        [
            Message(role="user", content="earlier photo", images=[image], from_history=True),
            Message(role="assistant", content="earlier answer", from_history=True),
            Message(role="user", content="current text only"),
        ],
        image,
    )


def _all_media_message() -> Message:
    return Message(
        role="user",
        content="all media",
        audio=[Audio(content=b"audio", mime_type="audio/ogg")],
        images=[Image(content=b"image")],
        files=[File(content=b"file", filename="report.txt")],
        videos=[Video(content=b"video", mime_type="video/mp4")],
        from_history=True,
    )


def _generated_media_message(kind: MediaKind) -> Message:
    """One replayed assistant turn whose media rides the singular output field for its kind."""
    message = Message(role="assistant", content="here is the result", from_history=True)
    if kind == "audio":
        message.audio_output = Audio(content=b"generated", mime_type="audio/wav")
    elif kind == "image":
        message.image_output = Image(content=b"generated")
    elif kind == "file":
        message.file_output = File(content=b"generated", filename="report.txt")
    else:
        message.video_output = Video(content=b"generated", mime_type="video/mp4")
    return message


def _output_media_carrier(message: Message, kind: MediaKind) -> Audio | Image | File | Video | None:
    return {
        "audio": message.audio_output,
        "image": message.image_output,
        "file": message.file_output,
        "video": message.video_output,
    }[kind]


def _present_kinds(message: Message) -> list[str]:
    return sorted(message_media_kinds(message))


def _kinds_on_the_wire(messages: list[Message]) -> list[str]:
    return sorted({kind for message in messages for kind in message_media_kinds(message)})


def _snapshot(messages: list[Message]) -> list[Message]:
    return [message.model_copy(deep=True) for message in messages]


async def _collect_stream(stream: AsyncIterator[ModelResponse]) -> list[ModelResponse]:
    return [response async for response in stream]


async def _collect_stream_into(
    stream: AsyncIterator[ModelResponse],
    collected: list[ModelResponse],
) -> None:
    async for response in stream:
        collected.append(response)  # noqa: PERF401


def _media_rejection() -> Exception:
    return ModelProviderError(message="unsupported inline media", status_code=400)


def _context_overflow() -> Exception:
    return ContextWindowExceededError(message="prompt exceeds the context window")


# Every ladder is finite by construction, so a driver that keeps resending a rung
# would hang the suite rather than fail it. The longest legitimate ladder here is
# five calls.
_LADDER_CALL_CEILING = 8


@dataclass(frozen=True)
class _LadderScenario:
    """One ladder the guard must walk identically whether the turn streams or not."""

    name: str
    messages: Callable[[], list[Message]]
    learned: frozenset[MediaKind]
    rejected_kind: MediaKind | None
    expected_call_kinds: list[list[str]]
    expected_suspected: frozenset[MediaKind]
    recovers: bool
    failure: Callable[[], Exception] = _media_rejection


def _image_and_file_message() -> Message:
    return Message(
        role="user",
        content="report and screenshot",
        images=[Image(content=b"image")],
        files=[File(content=b"file", filename="report.txt")],
        from_history=True,
    )


def _captionless_image_and_file_message() -> Message:
    return Message(
        role="user",
        content="",
        images=[Image(content=b"image")],
        files=[File(content=b"file", filename="report.txt")],
        from_history=True,
    )


def _scenario_messages(history: Message) -> list[Message]:
    return [history, Message(role="user", content="current text only")]


_LADDER_SCENARIOS = [
    # The learned kind must stay off the wire on every rung, not just the first.
    _LadderScenario(
        name="an_experiment_on_top_of_a_learned_kind",
        messages=lambda: _scenario_messages(_image_and_file_message()),
        learned=frozenset({"image"}),
        rejected_kind="file",
        expected_call_kinds=[["file"], []],
        expected_suspected=frozenset({"file", "image"}),
        recovers=True,
    ),
    # One more kind per rung, so the kind that fixes the request is the kind learned.
    _LadderScenario(
        name="one_more_kind_per_attempt_until_one_is_isolated",
        messages=lambda: _scenario_messages(_all_media_message()),
        learned=frozenset(),
        rejected_kind="image",
        expected_call_kinds=[
            ["audio", "file", "image", "video"],
            ["file", "image", "video"],
            ["image", "video"],
            ["video"],
        ],
        expected_suspected=frozenset({"image"}),
        recovers=True,
    ),
    # A ladder that never recovers implicates nothing and must not poison the cache.
    _LadderScenario(
        name="an_exhausted_ladder_teaches_nothing",
        messages=lambda: _scenario_messages(_all_media_message()),
        learned=frozenset(),
        rejected_kind=None,
        expected_call_kinds=[
            ["audio", "file", "image", "video"],
            ["file", "image", "video"],
            ["image", "video"],
            ["video"],
            [],
        ],
        expected_suspected=frozenset(),
        recovers=False,
    ),
    # A rung whose note fills a blank turn rewrites the messages the ladder was
    # planned from. Re-deriving the plan mid-walk would lose the rung being
    # walked, so the drivers advance through one plan positionally instead.
    _LadderScenario(
        name="a_rung_that_fills_a_blank_turn_does_not_re_plan_the_ladder",
        messages=lambda: _scenario_messages(_captionless_image_and_file_message()),
        learned=frozenset(),
        rejected_kind="image",
        expected_call_kinds=[["file", "image"], ["image"], []],
        expected_suspected=frozenset({"image"}),
        recovers=True,
    ),
    # The same blank turn, stopping on the rung whose note fills it. Removing
    # `file` and giving that message its first text are two changes at once, so
    # the success cannot say which one the provider accepted and implicates
    # nothing.
    _LadderScenario(
        name="a_rung_that_fills_a_blank_turn_teaches_nothing",
        messages=lambda: _scenario_messages(_captionless_image_and_file_message()),
        learned=frozenset(),
        rejected_kind="file",
        expected_call_kinds=[["file", "image"], ["image"]],
        expected_suspected=frozenset(),
        recovers=True,
    ),
    # The collapse rung is the ladder's last word, and the only thing that says so
    # is the check that this rung already sent everything the guard owns. The
    # branch builds its rung from ladder state rather than from what the previous
    # rung removed, so a collapse rung that fails the same way rung 0 did would
    # otherwise be rebuilt identically and handed back forever, re-billing the
    # same oversized prompt until the turn is killed.
    _LadderScenario(
        name="a_teaching_blocked_failure_on_every_rung_stops_at_the_collapse",
        messages=lambda: _scenario_messages(_all_media_message()),
        learned=frozenset(),
        rejected_kind=None,
        expected_call_kinds=[["audio", "file", "image", "video"], []],
        expected_suspected=frozenset(),
        recovers=False,
        failure=_context_overflow,
    ),
]


def _install_scenario_model(
    scenario: _LadderScenario,
    provider_calls: list[list[Message]],
    *,
    streaming: bool,
) -> OpenAIChat:
    """Install one model whose provider answers the same way on either path."""

    def _rejected(request_messages: list[Message]) -> bool:
        if scenario.rejected_kind is None:
            return True
        return any(scenario.rejected_kind in message_media_kinds(message) for message in request_messages)

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        request_messages = cast("list[Message]", kwargs["messages"])
        provider_calls.append(_snapshot(request_messages))
        assert len(provider_calls) <= _LADDER_CALL_CEILING, "the ladder never stopped"
        if _rejected(request_messages):
            raise scenario.failure()
        return ModelResponse(content="recovered")

    async def provider_stream(*args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        yield await provider_invoke(*args, **kwargs)

    if streaming:
        return _install_guarded_model(ainvoke_stream=provider_stream)
    return _install_guarded_model(ainvoke=provider_invoke)


async def _drive(model: OpenAIChat, messages: list[Message], *, streaming: bool) -> None:
    """Run one turn through whichever provider entry point the model was installed on."""
    if streaming:
        await _collect_stream(
            model.ainvoke_stream(messages=messages, assistant_message=Message(role="assistant"), tools=[]),
        )
        return
    await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True], ids=["blocking", "streaming"])
@pytest.mark.parametrize("scenario", _LADDER_SCENARIOS, ids=[scenario.name for scenario in _LADDER_SCENARIOS])
async def test_both_paths_walk_the_same_ladder(scenario: _LadderScenario, streaming: bool) -> None:
    """Both drivers read one plan, so the strips, the rungs and the lesson cannot drift apart."""
    provider_calls: list[list[Message]] = []
    model = _install_scenario_model(scenario, provider_calls, streaming=streaming)
    route = build_model_media_route(model)
    _already_learned(route, *sorted(scenario.learned))
    messages = scenario.messages()
    kinds_before = [_present_kinds(message) for message in messages]

    if scenario.recovers:
        await _drive(model, messages, streaming=streaming)
    else:
        with pytest.raises(type(scenario.failure())):
            await _drive(model, messages, streaming=streaming)

    # A ladder is a fixed number of rungs, so this pins the call count as much as
    # it pins what each call carried.
    assert [_present_kinds(call[0]) for call in provider_calls] == scenario.expected_call_kinds
    assert suspected_replayed_media_kinds_for_route(route) == scenario.expected_suspected
    # One experiment implicates a kind; only a second one caches it.
    assert unsupported_replayed_media_kinds_for_route(route) == scenario.learned
    # The caller keeps every attachment it passed in, whichever rung answered.
    assert [_present_kinds(message) for message in messages] == kinds_before


@pytest.mark.asyncio
async def test_persisted_history_image_failure_retries_final_messages_without_image() -> None:
    """A history-only image rejection retries the wire payload without touching the session copy."""
    provider_calls: list[list[Message]] = []

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        provider_calls.append(_snapshot(cast("list[Message]", kwargs["messages"])))
        if len(provider_calls) == 1:
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        return ModelResponse(content="recovered")

    model = _install_guarded_model(ainvoke=provider_invoke)
    messages, image = _history_messages()

    response = await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])

    assert response.content == "recovered"
    assert len(provider_calls) == 2
    assert provider_calls[0][0].images
    assert provider_calls[1][0].images is None
    # The message that lost the attachment is the message that says so.
    assert OMITTED_IMAGE_NOTE in str(provider_calls[1][0].content)
    assert provider_calls[1][-1].content == "current text only"
    # The caller's own messages and their persisted media come back untouched.
    assert messages[0].images == [image]
    assert messages[0].content == "earlier photo"
    assert suspected_replayed_media_kinds_for_route(build_model_media_route(model)) == frozenset({"image"})


@pytest.mark.asyncio
async def test_persisted_history_image_failure_retries_final_stream_without_image() -> None:
    """The streaming boundary recovers from the same persisted-history rejection."""
    provider_calls: list[list[Message]] = []

    async def provider_stream(*_args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        provider_calls.append(_snapshot(cast("list[Message]", kwargs["messages"])))
        if len(provider_calls) == 1:
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        yield ModelResponse(content="recovered")

    model = _install_guarded_model(ainvoke_stream=provider_stream)
    messages, image = _history_messages()

    responses = await _collect_stream(
        model.ainvoke_stream(messages=messages, assistant_message=Message(role="assistant"), tools=[]),
    )

    assert [response.content for response in responses] == ["recovered"]
    assert len(provider_calls) == 2
    assert provider_calls[1][0].images is None
    assert OMITTED_IMAGE_NOTE in str(provider_calls[1][0].content)
    assert messages[0].images == [image]
    assert suspected_replayed_media_kinds_for_route(build_model_media_route(model)) == frozenset({"image"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        # Agno's constructor default, used for connection errors and unexpected
        # exceptions: the provider never judged this payload.
        ModelProviderError(message="provider said no"),
        ModelRateLimitError(message="Too Many Requests", status_code=429),
        ModelRateLimitError(message="overloaded", status_code=529),
        ModelProviderError(message="service unavailable", status_code=503),
        ModelProviderError(message="request timeout", status_code=408),
        ModelProviderError(message="mid-stream error event", status_code=200),
        # Server errors outside the transient set: the Cloudflare family that
        # fronts several provider APIs, and a gateway that cannot route at all.
        ModelProviderError(message="web server returned an unknown error", status_code=520),
        ModelProviderError(message="not implemented", status_code=501),
    ],
)
async def test_failures_outside_the_client_error_range_are_left_to_the_retry_ladders(error: Exception) -> None:
    """A server that failed to answer is not evidence about media, and it is not the guard's to retry.

    Only a 4xx is the provider judging this payload. Running the ladder on a 5xx
    would re-upload the same near-full-size attachments up to five times against
    a server that is not reading them.
    """
    provider_calls = 0

    async def provider_invoke(*_args: object, **_kwargs: object) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        raise error

    model = _install_guarded_model(ainvoke=provider_invoke)
    messages, image = _history_messages()

    with pytest.raises(type(error)):
        await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])

    # Answering blind on a multimodal model is the failure this prevents.
    assert provider_calls == 1
    assert messages[0].images == [image]
    assert unsupported_replayed_media_kinds_for_route(build_model_media_route(model)) == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_client_errors_about_the_caller_cost_one_call(status_code: int) -> None:
    """A rejected key answers the same for every payload, so paying the ladder for it is pure waste."""
    provider_calls = 0

    async def provider_invoke(*_args: object, **_kwargs: object) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        raise ModelProviderError(message="denied", status_code=status_code)

    model = _install_guarded_model(ainvoke=provider_invoke)
    messages, _image = _history_messages()

    with pytest.raises(ModelProviderError):
        await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])

    assert provider_calls == 1
    assert unsupported_replayed_media_kinds_for_route(build_model_media_route(model)) == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 404, 422])
async def test_client_errors_that_do_carry_modality_rejections_run_the_ladder(status_code: int) -> None:
    """A rejected payload is the case the guard exists for, whichever 4xx the provider chose.

    404 included: it reads like a missing model, and it is also what a router
    answers when it has the model but no endpoint for the modality.
    """
    provider_calls = 0

    async def provider_invoke(*_args: object, **_kwargs: object) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            raise ModelProviderError(message="unsupported inline media", status_code=status_code)
        return ModelResponse(content="recovered")

    model = _install_guarded_model(ainvoke=provider_invoke)
    messages, _image = _history_messages()

    response = await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])

    assert response.content == "recovered"
    assert provider_calls == 2
    assert suspected_replayed_media_kinds_for_route(build_model_media_route(model)) == frozenset({"image"})


@pytest.mark.asyncio
async def test_the_captured_openrouter_rejection_recovers() -> None:
    """The exact failure this guard was written for, verbatim from the issue: a 404 about the modality."""
    provider_calls: list[list[Message]] = []

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        request_messages = cast("list[Message]", kwargs["messages"])
        provider_calls.append(_snapshot(request_messages))
        if any(message.images for message in request_messages):
            raise ModelProviderError(
                message="Error code: 404 - No endpoints found that support image input",
                status_code=404,
            )
        return ModelResponse(content="recovered")

    model = _install_guarded_model(ainvoke=provider_invoke)
    messages, image = _history_messages()

    response = await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])

    assert response.content == "recovered"
    assert len(provider_calls) == 2
    assert provider_calls[1][0].images is None
    assert messages[0].images == [image]
    assert suspected_replayed_media_kinds_for_route(build_model_media_route(model)) == frozenset({"image"})


@pytest.mark.asyncio
async def test_transient_stream_failures_reach_the_hook_that_owns_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """Installed in `model_loading` order, the transient retry hook wraps the guard, not the reverse."""
    monkeypatch.setattr(claude_stream_retry, "_RETRY_BASE_DELAY_SECONDS", 0.0)
    provider_calls = 0

    async def provider_stream(*_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            raise ModelProviderError(message="overloaded_error", status_code=529)
        yield ModelResponse(content="recovered")

    model = Claude(id="claude-sonnet-5")
    vars(model)["ainvoke_stream"] = provider_stream
    install_model_media_guard(cast("Model", model))
    install_claude_stream_retry_hook(model)
    messages, image = _history_messages()

    responses = await _collect_stream(
        model.ainvoke_stream(messages=messages, assistant_message=Message(role="assistant"), tools=[]),
    )

    # The retry hook replayed the request whole; the guard never stripped anything.
    assert [response.content for response in responses] == ["recovered"]
    assert provider_calls == 2
    assert messages[0].images == [image]
    assert unsupported_replayed_media_kinds_for_route(build_model_media_route(model)) == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        RetryableModelProviderError(original_error=ValueError("reconnect"), retry_guidance_message="try again"),
        ModelAuthenticationError(message="invalid api key"),
        TypeError("unexpected keyword argument"),
        ModelSafeguardRefusalError(message=MODEL_SAFEGUARD_REFUSAL_MESSAGE),
        # The same refusal with the status a provider actually reports it under:
        # a 400 clears every other exclusion, so only the refusal check stops it.
        ModelSafeguardRefusalError(message=MODEL_SAFEGUARD_REFUSAL_MESSAGE, status_code=400),
    ],
)
async def test_failures_the_provider_did_not_report_are_not_the_guards_to_retry(error: Exception) -> None:
    """Control-flow, auth, transport, and safeguard failures keep their own owners."""
    provider_calls = 0

    async def provider_invoke(*_args: object, **_kwargs: object) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        raise error

    model = _install_guarded_model(ainvoke=provider_invoke)
    messages, _image = _history_messages()

    with pytest.raises(type(error)):
        await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])

    assert provider_calls == 1
    assert unsupported_replayed_media_kinds_for_route(build_model_media_route(model)) == frozenset()


@pytest.mark.asyncio
async def test_a_learned_kind_shrinks_the_next_turn_to_a_single_experiment() -> None:
    """Learning one kind per turn is what makes a multi-kind history converge."""
    provider_calls: list[list[Message]] = []

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        request_messages = cast("list[Message]", kwargs["messages"])
        provider_calls.append(_snapshot(request_messages))
        if any(message.images or message.audio for message in request_messages):
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        return ModelResponse(content="recovered")

    model = _install_guarded_model(ainvoke=provider_invoke)
    messages = [_all_media_message(), Message(role="user", content="current text only")]

    turn_calls: list[int] = []
    for _turn in range(5):
        await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])
        turn_calls.append(len(provider_calls) - sum(turn_calls))

    # Two turns to learn image, two more to learn audio, then nothing left to
    # pay: a steady state of one doomed call per turn is what convergence rules
    # out, and the repeated experiment is what corroboration costs.
    assert turn_calls == [4, 4, 2, 2, 1]
    assert unsupported_replayed_media_kinds_for_route(build_model_media_route(model)) == frozenset(
        {"audio", "image"},
    )
    assert _present_kinds(provider_calls[-1][0]) == ["file", "video"]


@pytest.mark.asyncio
async def test_the_strip_a_learned_route_already_applies_is_not_a_recovery() -> None:
    """Rung 0 takes off what the failed call was already missing, so it changed nothing.

    Once the guard has learned anything — the ordinary steady state — every
    request arrives with that kind already stripped, including the run-input
    layer's own failed call. Reporting rung 0 as a recovery the guard caused
    would make `record_retry_success` stand down on a turn the guard did not
    touch, and the run-input cache would never learn that the fresh upload it
    dropped is the one the provider rejects.
    """
    provider_calls: list[list[Message]] = []

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        provider_calls.append(_snapshot(cast("list[Message]", kwargs["messages"])))
        return ModelResponse(content="answered")

    model = _install_guarded_model(ainvoke=provider_invoke)
    route = build_model_media_route(model)
    _already_learned(route, "image")

    # The run-input layer's failed call, and the without-media retry it decides on.
    decision = retry_media_inputs_after_failure(
        route,
        ModelProviderError(message="unsupported inline media", status_code=400),
        MediaInputs(audio=(Audio(content=b"voice note", mime_type="audio/ogg"),)),
    )
    assert decision.should_retry
    assert decision.removed_kinds == frozenset({"audio"})

    # That retry reaches the wire. The replayed image is off it before the first
    # call because the route already learned it, so rung 0 is the only rung.
    messages, _image = _history_messages()
    await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])
    decision.record_retry_success()

    assert [_present_kinds(call[0]) for call in provider_calls] == [[]]
    assert unsupported_media_kinds_for_route(route) == frozenset({"audio"})


@pytest.mark.asyncio
async def test_a_removal_that_took_a_learned_replay_and_a_fresh_upload_credits_neither_cache() -> None:
    """Both layers over one route, on the turn where their two caches could cross.

    The guard never owns a message the caller put on the wire, and the thread
    context MindRoom pins into the run input is exactly that: a context image
    reaches the provider however much the guard's cache holds about replays of
    that kind, so the layer above is the only one that can take it off. It has
    to — declining that retry is this branch's founding failure mode arriving
    unhandled. What it may not do is bank the removal: the image is a kind the
    guard already learned, and a third strike teaches nothing.

    The audio the user actually uploaded left on the same removal, so it is not
    a separate question either. The success cannot say whether the provider
    objected to the upload or to the replayed image beside it, and the audio was
    never replayed, so the two-strike gate would be the wrong cache for it even
    if the evidence were clean. The turn therefore teaches nothing at all, and
    the user's next audio and next image both still go out.
    """
    provider_calls: list[list[Message]] = []

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        request_messages = cast("list[Message]", kwargs["messages"])
        provider_calls.append(_snapshot(request_messages))
        if any(message.images or message.audio for message in request_messages):
            raise ModelProviderError(message="No endpoints found that support image input", status_code=404)
        return ModelResponse(content="recovered")

    model = _install_guarded_model(ainvoke=provider_invoke)
    route = build_model_media_route(model)
    _already_learned(route, "image")
    upload = Audio(content=b"voice note", mime_type="audio/ogg")

    with pytest.raises(ModelProviderError) as failure:
        await model.aresponse(
            messages=[
                Message(role="user", content="old photo", images=[Image(content=b"history")], from_history=True),
                Message(role="user", content="thread context photo", images=[Image(content=b"context")]),
                Message(role="user", content="what is in it?", audio=[upload]),
            ],
        )

    assert [_present_kinds(message) for message in provider_calls[0]] == [[], ["image"], ["audio"]]

    decision = retry_media_inputs_after_failure(
        route,
        failure.value,
        MediaInputs(audio=(upload,)),
        extra_present_kinds=frozenset({"image"}),
    )
    assert decision.should_retry is True
    assert decision.removed_kinds == frozenset({"audio", "image"})
    assert decision.teachable_kinds == frozenset()
    assert decision.teachable_context_kinds == frozenset()

    # The retry reaches the wire with both gone. Only the replayed image is left
    # for the guard, and taking off what the route already learned is not a
    # recovery the guard caused, so the layer above is free to record — and
    # records nothing, because its one removal proved nothing about either kind.
    await model.aresponse(
        messages=[
            Message(role="user", content="old photo", images=[Image(content=b"history")], from_history=True),
            Message(role="user", content="thread context photo"),
            Message(role="user", content="what is in it?"),
        ],
    )
    decision.record_retry_success()

    assert [_kinds_on_the_wire(call) for call in provider_calls] == [["audio", "image"], []]
    assert unsupported_media_kinds_for_route(route) == frozenset()
    # The audio the user uploaded is absent from the replayed gate's suspicions:
    # a kind this turn only ever uploaded was never replayed at all.
    assert suspected_replayed_media_kinds_for_route(route) == frozenset({"image"})
    assert unsupported_replayed_media_kinds_for_route(route) == frozenset({"image"})
    # Neither the user's next image nor their next voice note pays for it.
    fresh_upload = MediaInputs(images=(Image(content=b"just uploaded"),), audio=(Audio(content=b"just recorded"),))
    assert filter_media_inputs_for_route(route, fresh_upload).media_inputs == fresh_upload


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["audio", "image", "file", "video"])
async def test_output_media_carriers_are_stripped_with_their_kind(kind: MediaKind) -> None:
    """Model-generated media rides a singular output field, one per kind, and all four must go.

    A carrier the guard cannot see is a carrier it cannot take off: the kind
    goes unnoticed, the ladder finds nothing to experiment with, and the request
    fails with the same media on it.
    """
    provider_calls: list[list[Message]] = []

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        provider_calls.append(_snapshot(cast("list[Message]", kwargs["messages"])))
        if len(provider_calls) == 1:
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        return ModelResponse(content="recovered")

    model = _install_guarded_model(ainvoke=provider_invoke)
    history = _generated_media_message(kind)
    messages = [history, Message(role="user", content="what did you produce?")]

    await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])

    assert len(provider_calls) == 2
    assert _output_media_carrier(provider_calls[0][0], kind) is not None
    assert _output_media_carrier(provider_calls[1][0], kind) is None
    assert _present_kinds(provider_calls[1][0]) == []
    assert _output_media_carrier(history, kind) is not None
    assert suspected_replayed_media_kinds_for_route(build_model_media_route(model)) == frozenset({kind})


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, "", "   "], ids=["none", "empty", "whitespace"])
async def test_stripping_a_message_with_no_text_of_its_own_leaves_the_note_as_its_content(
    content: str | None,
) -> None:
    """A message whose only text was its caption must not reach the provider empty."""
    provider_calls: list[list[Message]] = []

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        provider_calls.append(_snapshot(cast("list[Message]", kwargs["messages"])))
        if len(provider_calls) == 1:
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        return ModelResponse(content="recovered")

    model = _install_guarded_model(ainvoke=provider_invoke)
    messages = [
        Message(role="user", content=content, images=[Image(content=b"image")], from_history=True),
        Message(role="user", content="what was in that?"),
    ]

    await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])

    assert provider_calls[1][0].content == OMITTED_IMAGE_NOTE
    # The note is a second changed variable on that rung: the turn went from
    # empty to non-empty, which can decide acceptance on its own. Nothing is
    # implicated, so not even the suspicion a clean isolation would have earned.
    assert suspected_replayed_media_kinds_for_route(build_model_media_route(model)) == frozenset()
    assert unsupported_replayed_media_kinds_for_route(build_model_media_route(model)) == frozenset()


@pytest.mark.asyncio
async def test_a_captioned_message_still_teaches_when_the_note_only_extends_it() -> None:
    """Only a turn that had nothing to say is confounded; a caption keeps the experiment clean."""
    provider_calls: list[list[Message]] = []

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        provider_calls.append(_snapshot(cast("list[Message]", kwargs["messages"])))
        if len(provider_calls) == 1:
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        return ModelResponse(content="recovered")

    model = _install_guarded_model(ainvoke=provider_invoke)
    messages = [
        Message(role="user", content="look at this", images=[Image(content=b"image")], from_history=True),
        Message(role="user", content="what was in that?"),
    ]

    await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])

    assert provider_calls[1][0].content == f"look at this\n\n{OMITTED_IMAGE_NOTE}"
    assert suspected_replayed_media_kinds_for_route(build_model_media_route(model)) == frozenset({"image"})


@pytest.mark.asyncio
async def test_guard_hands_back_the_caller_s_own_list_with_agno_s_edits_intact() -> None:
    """Agno mutates the list it passes down, so the guard swaps elements, never the list."""
    provider_calls: list[list[Message]] = []
    seen_lists: list[list[Message]] = []

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        request_messages = cast("list[Message]", kwargs["messages"])
        seen_lists.append(request_messages)
        provider_calls.append(_snapshot(request_messages))
        if len(provider_calls) == 1:
            # Exactly what `Model._ainvoke_with_retry` and `_remove_temporary_messages` do.
            request_messages.append(Message(role="user", content="retry guidance", temporary=True))
            request_messages[:] = list(reversed(request_messages))
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        return ModelResponse(content="recovered")

    model = _install_guarded_model(ainvoke=provider_invoke)
    messages, image = _history_messages()
    history_message = messages[0]

    await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])

    assert seen_lists[0] is messages
    assert seen_lists[1] is messages
    assert any(message.content == "retry guidance" for message in provider_calls[1])
    # Restoration is by identity, so reordering cannot put a copy back in place of an original.
    assert history_message in messages
    assert next(message for message in messages if message is history_message).images == [image]


@pytest.mark.asyncio
async def test_a_reordered_list_gets_every_original_back_where_its_copy_ended_up() -> None:
    """Restoration matches copies by identity, because the index one went in at is not the one it comes out of.

    A route that already learned a kind substitutes on the very first rung, so
    the copies are in the list while agno appends retry guidance and rewrites
    the order. Putting originals back by position would drop agno's edit and
    leave a stripped copy — carrying the omission note — in the caller's list.
    """
    provider_calls: list[list[Message]] = []

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        request_messages = cast("list[Message]", kwargs["messages"])
        provider_calls.append(_snapshot(request_messages))
        if len(provider_calls) == 1:
            # Exactly what `Model._ainvoke_with_retry` and `_remove_temporary_messages` do.
            request_messages.append(Message(role="user", content="retry guidance", temporary=True))
            request_messages[:] = list(reversed(request_messages))
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        return ModelResponse(content="recovered")

    model = _install_guarded_model(ainvoke=provider_invoke)
    _already_learned(build_model_media_route(model), "image")
    history_message = _image_and_file_message()
    messages = _scenario_messages(history_message)

    await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])

    # Rung 0 already substitutes: the learned image is off the first call.
    assert [_kinds_on_the_wire(call) for call in provider_calls] == [["file"], []]
    assert [message.content for message in messages] == [
        "retry guidance",
        "current text only",
        "report and screenshot",
    ]
    assert messages[2] is history_message
    assert _present_kinds(history_message) == ["file", "image"]


@pytest.mark.asyncio
async def test_unstripped_messages_reach_the_provider_without_being_copied() -> None:
    """Only the messages that lose media are copied; the rest stay on the hot path untouched."""
    seen_lists: list[list[Message]] = []

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        seen_lists.append(list(cast("list[Message]", kwargs["messages"])))
        if len(seen_lists) == 1:
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        return ModelResponse(content="recovered")

    model = _install_guarded_model(ainvoke=provider_invoke)
    messages, image = _history_messages()

    await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])

    retried = seen_lists[1]
    assert retried[0] is not messages[0]
    assert retried[1] is messages[1]
    assert retried[2] is messages[2]
    # The copy is shallow: the caller keeps the very media objects it passed in.
    assert messages[0].images is not None
    assert messages[0].images[0] is image


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["audio", "image", "file", "video"])
async def test_learned_kind_is_stripped_before_the_doomed_call(kind: MediaKind) -> None:
    """Once learned, the guard pays no failed call and clears only the learned kind."""
    provider_calls: list[list[Message]] = []

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        provider_calls.append(_snapshot(cast("list[Message]", kwargs["messages"])))
        return ModelResponse(content="ok")

    model = _install_guarded_model(ainvoke=provider_invoke)
    _already_learned(build_model_media_route(model), kind)
    history = _all_media_message()
    messages = [history, Message(role="user", content="current text only")]

    await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])

    assert len(provider_calls) == 1
    filtered = provider_calls[0][0]
    filtered_by_kind = {
        "audio": filtered.audio,
        "image": filtered.images,
        "file": filtered.files,
        "video": filtered.videos,
    }
    assert filtered_by_kind.pop(kind) is None
    assert all(remaining for remaining in filtered_by_kind.values())
    # The original replayed message keeps every attachment for the next turn.
    assert all([history.audio, history.images, history.files, history.videos])


@pytest.mark.asyncio
async def test_learned_replayed_kind_never_drops_a_fresh_user_upload() -> None:
    """The guard's own lesson must not reach the cache that gates run-input attachments."""

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        if any(message.images for message in cast("list[Message]", kwargs["messages"])):
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        return ModelResponse(content="recovered")

    model = _install_guarded_model(ainvoke=provider_invoke)

    for _turn in range(2):
        messages, _image = _history_messages()
        await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])

    route = build_model_media_route(model)
    assert unsupported_replayed_media_kinds_for_route(route) == frozenset({"image"})
    fresh_upload = MediaInputs(images=(Image(content=b"just uploaded"),))
    assert filter_media_inputs_for_route(route, fresh_upload).media_inputs == fresh_upload


@pytest.mark.asyncio
async def test_a_learned_kind_comes_off_history_and_stays_on_the_current_turn() -> None:
    """Provenance is the whole difference here: both messages carry an image of the learned kind.

    What a model refused to replay says nothing about the attachment the user
    just uploaded, which differs in format, size, and encoding. Stripping that
    one would make the agent answer the user's own image blind, and the fallback
    for it belongs to the run-input layer above, not to this guard.
    """
    provider_calls: list[list[Message]] = []

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        provider_calls.append(_snapshot(cast("list[Message]", kwargs["messages"])))
        raise ModelProviderError(message="images unsupported", status_code=400)

    model = _install_guarded_model(ainvoke=provider_invoke)
    _already_learned(build_model_media_route(model), "image")
    history_image = Image(content=b"history")
    fresh_image = Image(content=b"just uploaded")
    messages = [
        Message(role="user", content="old photo", images=[history_image], from_history=True),
        Message(role="user", content="and here is a new one", images=[fresh_image]),
    ]

    with pytest.raises(ModelProviderError):
        await model.aresponse(messages=messages)

    assert len(provider_calls) == 1
    assert provider_calls[0][0].images is None
    assert provider_calls[0][0].content == f"old photo\n\n{OMITTED_IMAGE_NOTE}"
    assert [image.content for image in provider_calls[0][1].images or []] == [b"just uploaded"]
    assert provider_calls[0][1].content == "and here is a new one"
    assert messages[1].images == [fresh_image]


@pytest.mark.asyncio
async def test_guard_stands_down_while_the_outer_fallback_owns_media_on_the_wire() -> None:
    """Two retry owners over one request would re-upload the attachment just rejected."""
    provider_calls = 0
    history_image = Image(content=b"history")
    current_audio = Audio(content=b"current", mime_type="audio/ogg")
    messages = [
        Message(role="user", content="old image", images=[history_image], from_history=True),
        Message(role="user", content="current audio", audio=[current_audio]),
    ]

    async def provider_invoke(*_args: object, **_kwargs: object) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        raise ModelProviderError(message="images unsupported", status_code=400)

    model = _install_guarded_model(ainvoke=provider_invoke)

    with pytest.raises(ModelProviderError):
        await model.aresponse(messages=messages)

    assert provider_calls == 1
    assert messages[0].images == [history_image]
    assert messages[1].audio == [current_audio]
    assert unsupported_replayed_media_kinds_for_route(build_model_media_route(model)) == frozenset()


@pytest.mark.asyncio
async def test_a_model_delegating_to_itself_opens_only_one_retry_owner() -> None:
    """`CodexResponses.ainvoke` consumes its own `ainvoke_stream`; both are guarded."""
    provider_calls = 0

    class _SelfDelegatingModel(OpenAIChat):
        async def ainvoke(self, *args: object, **kwargs: object) -> ModelResponse:
            merged = ModelResponse(content="")
            async for delta in self.ainvoke_stream(*args, **kwargs):
                merged.content = f"{merged.content}{delta.content or ''}"
            return merged

    async def provider_stream(*_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        nonlocal provider_calls
        provider_calls += 1
        raise ModelProviderError(message="unsupported inline media", status_code=400)
        yield  # pragma: no cover - unreachable, keeps this an async generator

    model = _install_guarded_model(
        ainvoke_stream=provider_stream,
        model=_SelfDelegatingModel(id="self-delegating-model", base_url="http://localhost:9292/v1"),
    )
    messages, _image = _history_messages()

    with pytest.raises(ModelProviderError):
        await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])

    # One owner: one doomed call plus one experiment. Nested owners would make four.
    assert provider_calls == 2


@pytest.mark.asyncio
async def test_current_turn_provenance_does_not_leak_between_model_instances() -> None:
    """Provenance is keyed by model, so one model's loop cannot reclassify another's input."""
    inner_calls = 0

    async def inner_invoke(*_args: object, **_kwargs: object) -> ModelResponse:
        nonlocal inner_calls
        inner_calls += 1
        raise ModelProviderError(message="unsupported inline media", status_code=400)

    inner_model = _install_guarded_model(
        ainvoke=inner_invoke,
        model=OpenAIChat(id="inner-model", base_url="http://localhost:9292/v1"),
    )

    async def outer_invoke(*_args: object, **_kwargs: object) -> ModelResponse:
        with pytest.raises(ModelProviderError):
            await inner_model.ainvoke(
                messages=[Message(role="user", content="fresh upload", images=[Image(content=b"fresh")])],
                assistant_message=Message(role="assistant"),
                tools=[],
            )
        return ModelResponse(content="done")

    outer_model = _install_guarded_model(
        ainvoke=outer_invoke,
        model=OpenAIChat(id="outer-model", base_url="http://localhost:9292/v1"),
    )

    await outer_model.aresponse(messages=[Message(role="user", content="start the loop")])

    # The inner model has no provenance of its own, so a non-history message is
    # the outer fallback's to retry, not the guard's.
    assert inner_calls == 1
    assert unsupported_replayed_media_kinds_for_route(build_model_media_route(inner_model)) == frozenset()


@pytest.mark.asyncio
async def test_guard_does_not_retry_after_stream_progress() -> None:
    """A provider error after visible stream output must propagate without replaying the request."""
    provider_calls = 0

    async def provider_stream(*_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        nonlocal provider_calls
        provider_calls += 1
        yield ModelResponse(content="visible partial")
        raise ModelProviderError(message="unsupported inline media", status_code=400)

    model = _install_guarded_model(ainvoke_stream=provider_stream)
    messages, _image = _history_messages()
    collected: list[ModelResponse] = []

    with pytest.raises(ModelProviderError):
        await _collect_stream_into(
            model.ainvoke_stream(messages=messages, assistant_message=Message(role="assistant"), tools=[]),
            collected,
        )

    assert [response.content for response in collected] == ["visible partial"]
    assert provider_calls == 1
    assert messages[0].images


@pytest.mark.asyncio
async def test_streamed_retry_teaches_only_once_the_provider_finished_the_stream() -> None:
    """A retry that dies halfway is the same failure wearing a prefix, and proves nothing."""
    provider_calls = 0

    async def provider_stream(*_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        yield ModelResponse(content="first retry chunk")
        raise ModelProviderError(message="died mid-stream", status_code=400)

    model = _install_guarded_model(ainvoke_stream=provider_stream)
    messages, image = _history_messages()
    collected: list[ModelResponse] = []

    with pytest.raises(ModelProviderError, match="died mid-stream"):
        await _collect_stream_into(
            model.ainvoke_stream(messages=messages, assistant_message=Message(role="assistant"), tools=[]),
            collected,
        )

    assert [response.content for response in collected] == ["first retry chunk"]
    assert unsupported_replayed_media_kinds_for_route(build_model_media_route(model)) == frozenset()
    assert messages[0].images == [image]


@pytest.mark.asyncio
async def test_streamed_retry_abandoned_by_its_consumer_repeats_the_experiment_next_turn() -> None:
    """An early close is not a provider verdict, so the lesson waits rather than being guessed."""
    provider_calls = 0

    async def provider_stream(*_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        yield ModelResponse(content="first retry chunk")
        yield ModelResponse(content="second retry chunk")

    model = _install_guarded_model(ainvoke_stream=provider_stream)
    messages, image = _history_messages()
    stream = cast(
        "AsyncGenerator[ModelResponse, None]",
        model.ainvoke_stream(messages=messages, assistant_message=Message(role="assistant"), tools=[]),
    )

    assert (await anext(stream)).content == "first retry chunk"
    await stream.aclose()

    assert unsupported_replayed_media_kinds_for_route(build_model_media_route(model)) == frozenset()
    assert messages[0].images == [image]


@pytest.mark.asyncio
async def test_streamed_retry_teaches_when_the_provider_reaches_the_end() -> None:
    """A stream the provider carried to its end is the proof the cache is allowed to keep."""
    provider_calls = 0

    async def provider_stream(*_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        yield ModelResponse(content="recovered")

    model = _install_guarded_model(ainvoke_stream=provider_stream)
    messages, image = _history_messages()

    responses = await _collect_stream(
        model.ainvoke_stream(messages=messages, assistant_message=Message(role="assistant"), tools=[]),
    )

    assert [response.content for response in responses] == ["recovered"]
    assert suspected_replayed_media_kinds_for_route(build_model_media_route(model)) == frozenset({"image"})
    assert messages[0].images == [image]


@pytest.mark.asyncio
async def test_a_streamed_retry_that_handed_the_consumer_nothing_teaches_nothing() -> None:
    """Ending without raising is not an answer: a stream that emitted no chunk proved nothing.

    This is the wire-level twin of the bar the drivers apply to a terminal run
    output, where a ``completed`` run with no content and no tool call is thrown
    away and retried rather than banked. Two such turns would otherwise walk the
    two-strike gate all the way to a lesson and blind the route to replayed
    images for the life of the process, on the strength of a consumer that
    received literally nothing.
    """
    provider_calls = 0

    async def provider_stream(*_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls % 2 == 1:
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        # The retry reaches its end without raising and without a single chunk.
        for response in ():
            yield response

    model = _install_guarded_model(ainvoke_stream=provider_stream)
    route = build_model_media_route(model)

    for _turn in range(2):
        messages, image = _history_messages()

        responses = await _collect_stream(
            model.ainvoke_stream(messages=messages, assistant_message=Message(role="assistant"), tools=[]),
        )

        assert responses == []
        assert messages[0].images == [image]
        assert suspected_replayed_media_kinds_for_route(route) == frozenset()
        assert unsupported_replayed_media_kinds_for_route(route) == frozenset()
        assert unsupported_media_kinds_for_route(route) == frozenset()

    assert provider_calls == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "empty_chunks",
    [
        [{"role": "assistant"}, {"output_tokens": 0}],
        [{"content": None}],
        [{"content": ""}],
        [{"content": "   "}],
        [{"content": None, "tool_calls": []}],
    ],
    ids=["role-and-usage-deltas", "none-content", "empty-content", "blank-content", "no-tool-calls"],
)
async def test_a_streamed_retry_whose_chunks_carried_no_answer_teaches_nothing(
    empty_chunks: list[dict[str, object]],
) -> None:
    """Chunks reaching the consumer is not the same fact as the completion answering.

    A ``ModelResponse`` is not an answer. Agno's OpenAI adapter emits a role-only
    first delta and a usage-only final chunk, so an empty completion still hands
    its consumer two chunks — and the guard used to call any chunk at all a
    success. Two such turns clear the two-strike gate and blind the route to
    replayed images for the life of the process on the strength of a completion
    that said nothing. The bar is the wire twin of the one every driver applies
    to a terminal run output: visible content, or a tool call.
    """
    provider_calls = 0

    async def provider_stream(*_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls % 2 == 1:
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        for chunk in empty_chunks:
            yield ModelResponse(**chunk)  # type: ignore[arg-type]

    model = _install_guarded_model(ainvoke_stream=provider_stream)
    route = build_model_media_route(model)

    # Two turns, because the gate is two strikes: one turn alone cannot tell a
    # cache that never fills from one that fills on the second pass.
    for _turn in range(2):
        messages, image = _history_messages()

        responses = await _collect_stream(
            model.ainvoke_stream(messages=messages, assistant_message=Message(role="assistant"), tools=[]),
        )

        # The chunks still reach the consumer: only what may be learned changes.
        assert len(responses) == len(empty_chunks)
        assert messages[0].images == [image]
        assert suspected_replayed_media_kinds_for_route(route) == frozenset()
        assert unsupported_replayed_media_kinds_for_route(route) == frozenset()
        assert unsupported_media_kinds_for_route(route) == frozenset()

    assert provider_calls == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answering_chunks",
    [
        [{"role": "assistant"}, {"content": "recovered"}, {"output_tokens": 3}],
        [{"role": "assistant"}, {"tool_calls": [{"id": "call-1", "function": {"name": "get_weather"}}]}],
        [{"role": "assistant"}, {"content": None, "audio": Audio(content=b"spoken", format="wav")}],
        [{"role": "assistant"}, {"content": None, "images": [Image(content=b"generated")]}],
    ],
    ids=["content-among-empty-deltas", "tool-call-only", "audio-only", "image-only"],
)
async def test_a_streamed_retry_that_did_answer_still_teaches(
    answering_chunks: list[dict[str, object]],
) -> None:
    """The answer bar is not a blanket disable: one answering chunk among empty ones still banks.

    A model answers in media as readily as in text. An OpenAI audio delta sets
    ``audio`` and leaves ``content`` ``None``, and a Gemini inline image lands in
    ``images`` the same way, so a bar that reads text and tool calls alone would
    call a delivered answer nothing and make this route re-walk the whole ladder
    on every later turn.
    """
    provider_calls = 0

    async def provider_stream(*_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        for chunk in answering_chunks:
            yield ModelResponse(**chunk)  # type: ignore[arg-type]

    model = _install_guarded_model(ainvoke_stream=provider_stream)
    route = build_model_media_route(model)
    messages, image = _history_messages()

    await _collect_stream(
        model.ainvoke_stream(messages=messages, assistant_message=Message(role="assistant"), tools=[]),
    )

    assert suspected_replayed_media_kinds_for_route(route) == frozenset({"image"})
    assert messages[0].images == [image]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "empty_response",
    [{"content": None}, {"content": ""}, {"content": "   "}, {"content": None, "tool_calls": []}],
    ids=["none-content", "empty-content", "blank-content", "no-tool-calls"],
)
async def test_a_blocking_retry_that_came_back_empty_teaches_nothing(
    empty_response: dict[str, object],
) -> None:
    """The blocking path never inspected what it got back, so it banked from an empty completion.

    ``ainvoke`` returning without raising says the provider accepted the request,
    not that it answered it. The same bar the streaming path applies applies
    here, read off the returned ``ModelResponse``.
    """
    provider_calls = 0

    async def provider_invoke(*_args: object, **_kwargs: object) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls % 2 == 1:
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        return ModelResponse(**empty_response)  # type: ignore[arg-type]

    model = _install_guarded_model(ainvoke=provider_invoke)
    route = build_model_media_route(model)

    for _turn in range(2):
        messages, image = _history_messages()

        await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])

        assert messages[0].images == [image]
        assert suspected_replayed_media_kinds_for_route(route) == frozenset()
        assert unsupported_replayed_media_kinds_for_route(route) == frozenset()
        assert unsupported_media_kinds_for_route(route) == frozenset()

    assert provider_calls == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answering_response",
    [
        {"content": "recovered"},
        {"content": None, "tool_calls": [{"id": "call-1", "function": {"name": "get_weather"}}]},
        {"content": None, "images": [Image(content=b"generated")]},
        {"content": None, "videos": [Video(content=b"generated")]},
        {"content": None, "audios": [Audio(content=b"spoken", format="wav")]},
        {"content": None, "files": [File(content=b"generated", mime_type="application/pdf")]},
        {"content": None, "audio": Audio(content=b"spoken", format="wav")},
        {"content": None, "parsed": {"answer": 42}},
    ],
    ids=["content", "tool-call-only", "image-only", "video-only", "audio-list-only", "file-only", "audio", "parsed"],
)
async def test_a_blocking_retry_that_did_answer_still_teaches(answering_response: dict[str, object]) -> None:
    """The blocking answer bar is not a blanket disable either: a real answer still banks.

    Every channel a ``ModelResponse`` can answer through is here, because the
    text-and-tool-call reading looked exhaustive and was not: Gemini appends an
    inline image to ``images`` without touching ``content``, and an OpenAI audio
    answer sets ``audio`` the same way. Classifying either as "answered nothing"
    costs the route its lesson and it re-walks the ladder every turn forever.
    """
    provider_calls = 0

    async def provider_invoke(*_args: object, **_kwargs: object) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        return ModelResponse(**answering_response)  # type: ignore[arg-type]

    model = _install_guarded_model(ainvoke=provider_invoke)
    route = build_model_media_route(model)
    messages, image = _history_messages()

    await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])

    assert suspected_replayed_media_kinds_for_route(route) == frozenset({"image"})
    assert messages[0].images == [image]


@pytest.mark.asyncio
async def test_a_blocking_retry_that_came_back_empty_still_stands_the_outer_layer_down() -> None:
    """The twin of the streamed case: crediting the guard's own removal is unconditional.

    The guard is what took the replayed image off the request whether or not the
    completion answered, so the run-input layer may not credit its own removal
    for this turn either.
    """
    provider_calls = 0

    async def provider_invoke(*_args: object, **_kwargs: object) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        return ModelResponse(content=None)

    model = _install_guarded_model(ainvoke=provider_invoke)
    route = build_model_media_route(model)

    decision = retry_media_inputs_after_failure(
        route,
        _media_rejection(),
        MediaInputs(audio=(Audio(content=b"voice note", mime_type="audio/ogg"),)),
    )
    assert decision.teachable_kinds == frozenset({"audio"})

    messages, _image = _history_messages()
    await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])
    decision.record_retry_success()

    assert unsupported_media_kinds_for_route(route) == frozenset()
    assert suspected_replayed_media_kinds_for_route(route) == frozenset()


@pytest.mark.asyncio
async def test_a_streamed_retry_that_handed_the_consumer_nothing_still_stands_the_outer_layer_down() -> None:
    """Suppressing the layer above is the conservative direction, so it does not wait for a chunk.

    The guard, not the run-input retry, is what took the replayed image off the
    request — that stays true whatever the stream went on to emit, so the outer
    layer may not credit its own removal for this turn either.
    """
    provider_calls = 0

    async def provider_stream(*_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        for response in ():
            yield response

    model = _install_guarded_model(ainvoke_stream=provider_stream)
    route = build_model_media_route(model)

    # The run-input layer's own without-media retry, armed before the wire call.
    decision = retry_media_inputs_after_failure(
        route,
        _media_rejection(),
        MediaInputs(audio=(Audio(content=b"voice note", mime_type="audio/ogg"),)),
    )
    assert decision.teachable_kinds == frozenset({"audio"})

    messages, _image = _history_messages()
    assert (
        await _collect_stream(
            model.ainvoke_stream(messages=messages, assistant_message=Message(role="assistant"), tools=[]),
        )
        == []
    )
    decision.record_retry_success()

    assert unsupported_media_kinds_for_route(route) == frozenset()
    assert suspected_replayed_media_kinds_for_route(route) == frozenset()


@pytest.mark.asyncio
async def test_the_callers_messages_carry_their_media_between_streamed_chunks() -> None:
    """Agno reads the live list to persist and to cancel, so a stripped copy must not sit in it.

    The substitution is scoped to each provider pull, not to the whole stream.
    """

    async def provider_stream(*_args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        request_messages = cast("list[Message]", kwargs["messages"])
        if request_messages[0].images:
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        yield ModelResponse(content="first retry chunk")
        yield ModelResponse(content="second retry chunk")

    model = _install_guarded_model(ainvoke_stream=provider_stream)
    messages, image = _history_messages()

    seen_between_chunks: list[list[Image] | None] = [
        messages[0].images
        async for _response in model.ainvoke_stream(
            messages=messages,
            assistant_message=Message(role="assistant"),
            tools=[],
        )
    ]

    assert seen_between_chunks == [[image], [image]]
    assert messages[0].content == "earlier photo"


@pytest.mark.asyncio
async def test_a_consumer_that_raises_mid_stream_still_gets_its_messages_back() -> None:
    """The substitution unwinds through the consumer's own failure, not only through ours."""

    async def provider_stream(*_args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        request_messages = cast("list[Message]", kwargs["messages"])
        if request_messages[0].images:
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        yield ModelResponse(content="first retry chunk")
        yield ModelResponse(content="second retry chunk")

    model = _install_guarded_model(ainvoke_stream=provider_stream)
    messages, image = _history_messages()

    async def fail_on_the_first_chunk() -> None:
        async for _response in model.ainvoke_stream(
            messages=messages,
            assistant_message=Message(role="assistant"),
            tools=[],
        ):
            raise RuntimeError(_CONSUMER_FAILURE_MESSAGE)

    with pytest.raises(RuntimeError, match=_CONSUMER_FAILURE_MESSAGE):
        await fail_on_the_first_chunk()

    assert messages[0].images == [image]
    assert messages[0].content == "earlier photo"


@pytest.mark.asyncio
async def test_closing_guarded_initial_stream_closes_provider_stream() -> None:
    """Closing the public guard after one chunk must finalize the active initial provider stream."""
    provider_closed = False

    async def provider_stream(*_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        nonlocal provider_closed
        try:
            yield ModelResponse(content="first")
            yield ModelResponse(content="second")
        finally:
            provider_closed = True

    model = _install_guarded_model(ainvoke_stream=provider_stream)
    stream = cast(
        "AsyncGenerator[ModelResponse, None]",
        model.ainvoke_stream(
            messages=[Message(role="user", content="text only")],
            assistant_message=Message(role="assistant"),
            tools=[],
        ),
    )

    assert (await anext(stream)).content == "first"
    await stream.aclose()

    assert provider_closed is True


@pytest.mark.asyncio
async def test_closing_guarded_retry_stream_closes_provider_stream() -> None:
    """Closing after fallback progress must finalize the active retry provider stream."""
    provider_calls = 0
    retry_stream_closed = False

    async def provider_stream(*_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        nonlocal provider_calls, retry_stream_closed
        provider_calls += 1
        if provider_calls == 1:
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        try:
            yield ModelResponse(content="first retry chunk")
            yield ModelResponse(content="second retry chunk")
        finally:
            retry_stream_closed = True

    model = _install_guarded_model(ainvoke_stream=provider_stream)
    messages, _image = _history_messages()
    stream = cast(
        "AsyncGenerator[ModelResponse, None]",
        model.ainvoke_stream(messages=messages, assistant_message=Message(role="assistant"), tools=[]),
    )

    assert (await anext(stream)).content == "first retry chunk"
    await stream.aclose()

    assert provider_calls == 2
    assert retry_stream_closed is True


@pytest.mark.asyncio
async def test_close_failure_does_not_mask_the_error_that_drives_the_retry() -> None:
    """The provider failure decides the retry, so a noisy `aclose()` must stay out of the way."""
    provider_calls = 0

    class _ExplodingCloseStream:
        """A provider stream whose finalizer is as broken as its request."""

        def __aiter__(self) -> _ExplodingCloseStream:
            return self

        async def __anext__(self) -> ModelResponse:
            raise ModelProviderError(message="unsupported inline media", status_code=400)

        async def aclose(self) -> None:
            raise RuntimeError(_CLOSE_FAILURE_MESSAGE)

    async def recovered_stream() -> AsyncIterator[ModelResponse]:
        yield ModelResponse(content="recovered")

    def provider_stream(*_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            return cast("AsyncIterator[ModelResponse]", _ExplodingCloseStream())
        return recovered_stream()

    model = _install_guarded_model(ainvoke_stream=provider_stream)
    messages, _image = _history_messages()

    responses = await _collect_stream(
        model.ainvoke_stream(messages=messages, assistant_message=Message(role="assistant"), tools=[]),
    )

    assert [response.content for response in responses] == ["recovered"]
    assert provider_calls == 2


@pytest.mark.asyncio
async def test_context_window_error_reduces_history_media_without_teaching() -> None:
    """Context overflow is a valid reduction, but shrinking a payload proves no incapability."""
    provider_calls = 0

    async def provider_invoke(*_args: object, **_kwargs: object) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            raise ContextWindowExceededError(message="prompt exceeds the context window")
        return ModelResponse(content="recovered")

    model = _install_guarded_model(ainvoke=provider_invoke)
    messages, _image = _history_messages()

    response = await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])

    assert response.content == "recovered"
    assert provider_calls == 2
    assert suspected_replayed_media_kinds_for_route(build_model_media_route(model)) == frozenset()
    assert unsupported_replayed_media_kinds_for_route(build_model_media_route(model)) == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True], ids=["blocking", "streaming"])
async def test_a_failure_that_can_teach_nothing_costs_two_calls_whatever_the_history_holds(
    streaming: bool,
) -> None:
    """Isolation buys a cacheable conclusion; with none for sale it is k uploads for nothing.

    Context overflow is an ordinary 400 (`agno/exceptions.py`), so without this
    the ladder walks all four rungs, recovers, learns nothing, and pays the same
    four again on the next turn of the same conversation, forever.
    """
    provider_calls: list[list[Message]] = []

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        request_messages = cast("list[Message]", kwargs["messages"])
        provider_calls.append(_snapshot(request_messages))
        if any(message_media_kinds(message) for message in request_messages):
            raise ContextWindowExceededError(message="prompt exceeds the context window")
        return ModelResponse(content="recovered")

    async def provider_stream(*args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        yield await provider_invoke(*args, **kwargs)

    model = (
        _install_guarded_model(ainvoke_stream=provider_stream)
        if streaming
        else _install_guarded_model(ainvoke=provider_invoke)
    )
    messages = _scenario_messages(_all_media_message())

    await _drive(model, messages, streaming=streaming)

    # The request as sent, then everything the guard owns off at once.
    assert [_present_kinds(call[0]) for call in provider_calls] == [["audio", "file", "image", "video"], []]
    assert suspected_replayed_media_kinds_for_route(build_model_media_route(model)) == frozenset()


@pytest.mark.asyncio
async def test_the_same_replayed_attachment_supplies_both_isolations_and_is_cached() -> None:
    """The two-isolation gate rules out a one-off, not a persistently bad attachment.

    A corrupt attachment isolates exactly like an unsupported modality, so one
    run is not proof — but suspicion is keyed by route and kind with nothing to
    tell one attachment from another, and the same replayed image is on the wire
    every turn of its conversation. It therefore corroborates itself on turn two
    and blinds the route to images until restart. `docs/images.md` states that
    limit; this pins it, so the weaker guarantee is the enforced one.
    """
    provider_calls: list[list[Message]] = []

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        request_messages = cast("list[Message]", kwargs["messages"])
        provider_calls.append(_snapshot(request_messages))
        if any(message.images for message in request_messages):
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        return ModelResponse(content="recovered")

    model = _install_guarded_model(ainvoke=provider_invoke)
    route = build_model_media_route(model)
    # One attachment, replayed from history on both turns.
    corrupt_image = Image(content=b"\x89PNG\r\n\x1a\ncorrupt")

    def replay_it() -> list[Message]:
        return [
            Message(role="user", content="earlier photo", images=[corrupt_image], from_history=True),
            Message(role="user", content="current text only"),
        ]

    await model.ainvoke(messages=replay_it(), assistant_message=Message(role="assistant"), tools=[])

    assert suspected_replayed_media_kinds_for_route(route) == frozenset({"image"})
    assert unsupported_replayed_media_kinds_for_route(route) == frozenset()

    await model.ainvoke(messages=replay_it(), assistant_message=Message(role="assistant"), tools=[])

    assert unsupported_replayed_media_kinds_for_route(route) == frozenset({"image"})
    # Both turns paid the doomed call; repeating the experiment is what the lesson costs.
    assert [_present_kinds(call[0]) for call in provider_calls] == [["image"], [], ["image"], []]


@pytest.mark.asyncio
async def test_a_working_turn_between_two_unrelated_isolations_does_not_discharge_the_suspicion() -> None:
    """The gate counts isolations per route and kind, and counts nothing else.

    Not the conversation, not the attachment, not the working turns in between:
    a success is never recorded, so a standing suspicion is never discharged.
    Two unrelated one-off isolations therefore still promote to a permanent
    lesson, even with a turn that carried that very kind to the provider
    successfully between them. That is the property `docs/images.md` claims —
    a single isolated blip is ruled out, and nothing beyond it — so it is the
    property pinned here.
    """
    provider_calls: list[list[Message]] = []
    # A route that handles images perfectly well, and two unrelated attachments
    # it cannot read — a truncated file in one conversation, a mislabelled one
    # in another.
    unreadable = {b"\x89PNG\r\n\x1a\ntruncated", b"\x89PNG\r\n\x1a\nwrong-mime"}

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        request_messages = cast("list[Message]", kwargs["messages"])
        provider_calls.append(_snapshot(request_messages))
        if any(image.content in unreadable for message in request_messages for image in message.images or []):
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        return ModelResponse(content="answered")

    model = _install_guarded_model(ainvoke=provider_invoke)
    route = build_model_media_route(model)

    async def turn_replaying(content: bytes) -> None:
        """Run one turn of a conversation of its own, replaying one image from its history."""
        await model.ainvoke(
            messages=[
                Message(role="user", content="earlier photo", images=[Image(content=content)], from_history=True),
                Message(role="user", content="current text only"),
            ],
            assistant_message=Message(role="assistant"),
            tools=[],
        )

    # One conversation, one bad attachment: a suspicion, not yet a lesson.
    await turn_replaying(b"\x89PNG\r\n\x1a\ntruncated")

    assert suspected_replayed_media_kinds_for_route(route) == frozenset({"image"})
    assert unsupported_replayed_media_kinds_for_route(route) == frozenset()

    # An unrelated conversation whose replayed image the provider accepts.
    await turn_replaying(b"\x89PNG\r\n\x1a\nfine")

    # It reached the provider with the image on the wire and it worked, and the
    # suspicion neither notices nor cares.
    assert _present_kinds(provider_calls[-1][0]) == ["image"]
    assert suspected_replayed_media_kinds_for_route(route) == frozenset({"image"})

    # A third conversation, a different bad attachment: the second isolation.
    await turn_replaying(b"\x89PNG\r\n\x1a\nwrong-mime")

    assert unsupported_replayed_media_kinds_for_route(route) == frozenset({"image"})
    assert [_present_kinds(call[0]) for call in provider_calls] == [["image"], [], ["image"], ["image"], []]


@pytest.mark.asyncio
async def test_tool_media_rejection_retries_followup_without_reexecuting_tool() -> None:
    """Tool-produced media belongs to the wire guard, so retrying it must not rerun the tool."""
    provider_calls: list[list[Message]] = []
    tool_calls = 0

    def capture_screenshot() -> ToolResult:
        nonlocal tool_calls
        tool_calls += 1
        return ToolResult(content="captured", images=[Image(content=b"tool screenshot")])

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        provider_calls.append(_snapshot(cast("list[Message]", kwargs["messages"])))
        if len(provider_calls) == 1:
            return ModelResponse(
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "capture_screenshot", "arguments": "{}"},
                    },
                ],
            )
        if len(provider_calls) == 2:
            raise ModelProviderError(message="images unsupported", status_code=400)
        return ModelResponse(content="recovered")

    model = _install_guarded_model(ainvoke=provider_invoke)
    messages = [Message(role="user", content="inspect the page")]

    response = await model.aresponse(
        messages=messages,
        tools=[Function(name="capture_screenshot", entrypoint=capture_screenshot)],
    )

    assert response.content == "recovered"
    assert tool_calls == 1
    assert len(provider_calls) == 3
    assert any(message.images for message in provider_calls[1])
    assert all(message.images is None for message in provider_calls[2])


@pytest.mark.asyncio
async def test_streamed_tool_media_rejection_retries_followup_without_reexecuting_tool() -> None:
    """Streaming keeps current-turn provenance bound across Agno's complete tool loop."""
    provider_calls: list[list[Message]] = []
    tool_calls = 0

    def capture_screenshot() -> ToolResult:
        nonlocal tool_calls
        tool_calls += 1
        return ToolResult(content="captured", images=[Image(content=b"tool screenshot")])

    async def provider_stream(*_args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        provider_calls.append(_snapshot(cast("list[Message]", kwargs["messages"])))
        if len(provider_calls) == 1:
            yield ModelResponse(
                tool_calls=[
                    ChoiceDeltaToolCall(
                        index=0,
                        id="call-1",
                        type="function",
                        function=ChoiceDeltaToolCallFunction(name="capture_screenshot", arguments="{}"),
                    ),
                ],
            )
            return
        if len(provider_calls) == 2:
            raise ModelProviderError(message="images unsupported", status_code=400)
        yield ModelResponse(content="recovered")

    model = _install_guarded_model(ainvoke_stream=provider_stream)
    messages = [Message(role="user", content="inspect the page")]

    responses = [
        response
        async for response in model.aresponse_stream(
            messages=messages,
            tools=[Function(name="capture_screenshot", entrypoint=capture_screenshot)],
        )
    ]

    assert any(response.content == "recovered" for response in responses)
    assert tool_calls == 1
    assert len(provider_calls) == 3
    assert any(message.images for message in provider_calls[1])
    assert all(message.images is None for message in provider_calls[2])


@pytest.mark.asyncio
async def test_a_deep_copied_model_guards_itself_and_not_the_model_it_came_from(tmp_path: Path) -> None:
    """Agno deep-copies models for memory, learning, and culture work; the copy must guard its own route."""
    provider_calls = 0

    async def provider_invoke(*_args: object, **_kwargs: object) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        return ModelResponse(content="recovered")

    # `model_loading.get_model_instance` order: the guard wraps whatever the
    # logging hook left behind, so copy-safety is a property of the chain.
    model = OpenAIChat(id="history-text-model", base_url="http://localhost:9292/v1")
    vars(model)["ainvoke"] = provider_invoke
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(),
        default_log_dir=tmp_path,
    )
    install_model_media_guard(cast("Model", model))
    copied = deepcopy(model)
    copied.id = "copied-model"
    messages, _image = _history_messages()

    response = await copied.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])

    assert response.content == "recovered"
    assert suspected_replayed_media_kinds_for_route(build_model_media_route(copied)) == frozenset({"image"})
    # A closure bound to the source instance would have implicated the source route.
    assert suspected_replayed_media_kinds_for_route(build_model_media_route(model)) == frozenset()


def test_installing_the_guard_twice_leaves_every_wrapper_alone() -> None:
    """Re-installation must not stack a second retry owner on any of the four entry points."""
    model = _install_guarded_model()
    installed = {name: vars(model)[name] for name in ("ainvoke", "ainvoke_stream", "aresponse", "aresponse_stream")}

    install_model_media_guard(cast("Model", model))

    assert {name: vars(model)[name] for name in installed} == installed


@pytest.mark.asyncio
async def test_positional_messages_disable_the_guard_loudly() -> None:
    """Agno passes `messages=` everywhere; if that ever changes, silence would be the worst answer."""
    provider_calls: list[tuple[object, ...]] = []

    async def provider_invoke(*args: object, **_kwargs: object) -> ModelResponse:
        provider_calls.append(args)
        raise ModelProviderError(message="unsupported inline media", status_code=400)

    model = _install_guarded_model(ainvoke=provider_invoke)
    messages, image = _history_messages()

    with capture_logs() as logs, pytest.raises(ModelProviderError):
        await model.ainvoke(messages, assistant_message=Message(role="assistant"), tools=[])

    assert provider_calls == [(messages,)]
    assert messages[0].images == [image]
    assert any("passed messages positionally" in str(entry.get("event")) for entry in logs)


@pytest.mark.asyncio
async def test_learned_kinds_are_stripped_even_while_the_outer_layer_owns_media() -> None:
    """Standing down as retry owner is not standing down as the layer that already knows better."""
    provider_calls: list[list[Message]] = []

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        provider_calls.append(_snapshot(cast("list[Message]", kwargs["messages"])))
        raise ModelProviderError(message="images unsupported", status_code=400)

    model = _install_guarded_model(ainvoke=provider_invoke)
    _already_learned(build_model_media_route(model), "image")
    history_image = Image(content=b"history")
    current_audio = Audio(content=b"current", mime_type="audio/ogg")
    messages = [
        Message(role="user", content="old image", images=[history_image], from_history=True),
        Message(role="user", content="current audio", audio=[current_audio]),
    ]

    with pytest.raises(ModelProviderError):
        await model.aresponse(messages=messages)

    # One call: the known-bad image never went, and the outer layer keeps the retry.
    assert len(provider_calls) == 1
    assert provider_calls[0][0].images is None
    assert provider_calls[0][1].audio
    assert messages[0].images == [history_image]


@pytest.mark.asyncio
async def test_an_attempt_that_removes_two_kinds_leaves_one_note_naming_both() -> None:
    """The strip is one set per attempt, so the message that lost media says so once."""
    provider_calls: list[list[Message]] = []

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        request_messages = cast("list[Message]", kwargs["messages"])
        provider_calls.append(_snapshot(request_messages))
        if any(message.files for message in request_messages):
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        return ModelResponse(content="recovered")

    model = _install_guarded_model(ainvoke=provider_invoke)
    _already_learned(build_model_media_route(model), "image")
    messages = _scenario_messages(_image_and_file_message())

    await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])

    assert provider_calls[1][0].content == f"report and screenshot\n\n{_omitted_note('file', 'image')}"


@pytest.mark.asyncio
async def test_structured_content_gets_the_note_in_the_shape_it_already_uses() -> None:
    """Content parts reach the provider verbatim, so the note has to look like the parts around it."""
    provider_calls: list[list[Message]] = []

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        provider_calls.append(_snapshot(cast("list[Message]", kwargs["messages"])))
        if len(provider_calls) == 1:
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        return ModelResponse(content="recovered")

    model = _install_guarded_model(ainvoke=provider_invoke)
    parts = [{"type": "text", "text": "look at this"}]
    history = Message(role="user", content=parts, images=[Image(content=b"image")], from_history=True)
    messages = [history, Message(role="user", content="what was it?")]

    await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])

    assert provider_calls[1][0].content == [
        {"type": "text", "text": "look at this"},
        {"type": "text", "text": OMITTED_IMAGE_NOTE},
    ]
    # The caller's own list is never appended to.
    assert history.content == [{"type": "text", "text": "look at this"}]


@pytest.mark.asyncio
async def test_structured_content_with_no_text_part_to_copy_is_left_alone() -> None:
    """Inventing a part shape the provider does not accept would cost more than staying quiet."""
    provider_calls: list[list[Message]] = []

    async def provider_invoke(*_args: object, **kwargs: object) -> ModelResponse:
        provider_calls.append(_snapshot(cast("list[Message]", kwargs["messages"])))
        if len(provider_calls) == 1:
            raise ModelProviderError(message="unsupported inline media", status_code=400)
        return ModelResponse(content="recovered")

    model = _install_guarded_model(ainvoke=provider_invoke)
    parts = [{"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}}]
    history = Message(role="user", content=parts, images=[Image(content=b"image")], from_history=True)
    messages = [history, Message(role="user", content="what was it?")]

    await model.ainvoke(messages=messages, assistant_message=Message(role="assistant"), tools=[])

    assert provider_calls[1][0].content == parts
    assert provider_calls[1][0].images is None
