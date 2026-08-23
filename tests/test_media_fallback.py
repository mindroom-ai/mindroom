"""Tests for learned model media capability fallback policy."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from agno.exceptions import ContextWindowExceededError, ModelProviderError
from agno.media import Audio, Image
from agno.models.message import Message
from agno.models.openai import OpenAIChat

from mindroom import ai_runtime
from mindroom.error_handling import MODEL_SAFEGUARD_REFUSAL_MESSAGE, ModelSafeguardRefusalError
from mindroom.media_fallback import (
    MediaRetryDecision,
    ModelMediaRoute,
    build_model_media_route,
    capability_teaching_blocked,
    filter_media_inputs_for_route,
    note_wire_media_recovery,
    record_replayed_media_isolation,
    reset_model_media_capability_cache,
    retry_media_inputs_after_failure,
    suspected_replayed_media_kinds_for_route,
    unsupported_media_kinds_for_route,
    unsupported_replayed_media_kinds_for_route,
)
from mindroom.media_inputs import MediaInputs
from mindroom.prompts import INLINE_MEDIA_FALLBACK_PROMPT_TEMPLATE


def test_unknown_model_route_sends_all_media() -> None:
    """Unknown route should optimistically keep every supplied media kind."""
    media = _media_inputs()

    filtered = filter_media_inputs_for_route(_route(), media)

    assert filtered.media_inputs == media
    assert filtered.removed_kinds == frozenset()


def test_model_route_includes_provider_model_and_base_url() -> None:
    """Route construction should key learned support by concrete model endpoint."""
    model = OpenAIChat(id="qwen-local", base_url="http://localhost:9292/v1/")

    assert build_model_media_route(model) == ModelMediaRoute(
        provider="openai",
        model_id="qwen-local",
        base_url="http://localhost:9292/v1",
    )


@pytest.mark.parametrize(
    "error",
    [
        # Z.ai code 1214 as it reaches the streamed run-error path: bare message,
        # no exception object, no status code, no "Error code: 400" marker.
        "messages[30].content[0].type type error",
        "Error code: 400 - messages.content.type is invalid, allowed values: ['text']",
        "audio input is not supported - hint: you may need to provide the mmproj",
        "Rate limit exceeded",
        "Error code: 400 - invalid api key provided",
        ModelProviderError(message="Some brand new provider wording about content", status_code=400),
    ],
)
def test_any_failure_retries_without_media_and_teaches_on_success(error: Exception | str) -> None:
    """No error wording decides the retry: every failure drops all media once.

    The route capability cache learns the dropped kinds only when the retry
    actually succeeds, which never happens for failures unrelated to media
    (auth, rate limits, outages) because their retry fails identically.
    """
    media = _media_inputs()
    route = _route()

    decision = retry_media_inputs_after_failure(route, error, media)

    assert decision.should_retry is True
    assert decision.removed_kinds == frozenset({"audio", "image", "file", "video"})
    assert decision.media_inputs == MediaInputs()
    assert decision.teach_route_on_success == route
    # Nothing is taught until the without-media retry actually succeeds.
    assert filter_media_inputs_for_route(route, media).media_inputs == media

    decision.record_retry_success()

    assert unsupported_media_kinds_for_route(route) == frozenset({"audio", "image", "file", "video"})
    filtered = filter_media_inputs_for_route(route, media)
    assert filtered.removed_kinds == frozenset({"audio", "image", "file", "video"})
    assert filtered.media_inputs == MediaInputs()


def test_no_media_present_never_retries() -> None:
    """Media-shaped errors without any media sent are not retried."""
    decision = retry_media_inputs_after_failure(_route(), "audio input is not supported", MediaInputs())

    assert decision.should_retry is False
    assert decision.removed_kinds == frozenset()


@pytest.mark.parametrize(
    "error",
    [
        ModelSafeguardRefusalError(message=MODEL_SAFEGUARD_REFUSAL_MESSAGE),
        MODEL_SAFEGUARD_REFUSAL_MESSAGE,
    ],
)
def test_safeguard_refusal_never_retries_without_media(error: Exception | str) -> None:
    """A deterministic refusal must not enter the generic media fallback loop."""
    media = _media_inputs()

    decision = retry_media_inputs_after_failure(_route(), error, media)

    assert decision.should_retry is False
    assert decision.media_inputs == media
    assert decision.removed_kinds == frozenset()


@pytest.mark.parametrize(
    "error",
    [
        ContextWindowExceededError(message="prompt is too long: 250000 tokens > 200000 maximum"),
        "Error code: 400 - maximum context length is 128000 tokens",
        ModelProviderError(message="Request Entity Too Large", status_code=413),
        # A per-attachment size limit is reported as a plain 400 and says
        # nothing about the modality itself.
        ModelProviderError(message="image size exceeds 5 MB", status_code=400),
        ModelProviderError(message="Attachment is too large", status_code=400),
        # Transient failures can pass on the retry because the blip passed, so
        # a lucky retry success must not disable media for the route.
        ModelProviderError(message="upstream connect error", status_code=502),
        ModelProviderError(message="model overloaded", status_code=503),
        ModelProviderError(message="Too Many Requests", status_code=429),
        ModelProviderError(message="Request Timeout", status_code=408),
        ModelProviderError(message="mid-stream provider error", status_code=200),
    ],
)
def test_size_context_and_transient_failures_retry_but_never_teach(error: Exception | str) -> None:
    """Oversized requests and transient failures must not teach capability on retry success."""
    media = _media_inputs()
    route = _route()

    decision = retry_media_inputs_after_failure(route, error, media)

    assert decision.should_retry is True
    assert decision.teach_route_on_success is None

    decision.record_retry_success()

    assert unsupported_media_kinds_for_route(route) == frozenset()


def test_different_base_url_does_not_inherit_negative_cache() -> None:
    """Effective route should include endpoint, not just provider/model."""
    media = _media_inputs()
    first_route = _route(base_url="http://localhost:9292/v1")
    second_route = _route(base_url="http://localhost:9293/v1")

    retry_media_inputs_after_failure(first_route, "audio input is not supported", media).record_retry_success()

    filtered = filter_media_inputs_for_route(second_route, media)
    assert filtered.removed_kinds == frozenset()
    assert filtered.media_inputs == media


def test_context_media_kinds_retry_and_credit_the_replayed_cache_on_an_unprimed_route() -> None:
    """Media pinned to history triggers the retry, and its credit goes where replays are read.

    This is the crossover the caches exist to prevent, arriving from the one
    direction the subtraction in ``retry_media_inputs_after_failure`` cannot
    close: subtracting the guard's cache only helps once the guard has *already*
    learned the kind, and every route's first failure is unprimed. A turn whose
    only media is thread context is media this layer can strip but never own —
    the guard structurally cannot see it, so it will never learn it either — and
    banking that removal in the run-input cache would strip the user's next
    fresh upload of that kind for the life of the process.

    So the evidence keeps its provenance instead: one isolation of replayed
    media, sent through the same two-strike gate the guard pays, which is also
    the cache the pre-strip reads. The route still converges; it takes one more
    turn (``[2, 2, 1, 1]`` rather than ``[2, 1, 1, 1]``), which is that gate's
    stated price.
    """
    route = _route()

    decision = retry_media_inputs_after_failure(
        route,
        "image input is not supported",
        MediaInputs(),
        extra_present_kinds=frozenset({"image"}),
    )

    assert decision.should_retry is True
    assert decision.removed_kinds == frozenset({"image"})
    assert decision.teachable_kinds == frozenset()
    assert decision.teachable_context_kinds == frozenset({"image"})

    decision.record_retry_success()

    assert suspected_replayed_media_kinds_for_route(route) == frozenset({"image"})
    assert unsupported_replayed_media_kinds_for_route(route) == frozenset()
    # The cache that gates fresh uploads never hears about it, so the next
    # image the user actually attaches still goes out.
    assert unsupported_media_kinds_for_route(route) == frozenset()
    fresh_image = MediaInputs(images=(MagicMock(name="image"),))
    assert filter_media_inputs_for_route(route, fresh_image).media_inputs == fresh_image

    retry_media_inputs_after_failure(
        route,
        "image input is not supported",
        MediaInputs(),
        extra_present_kinds=frozenset({"image"}),
    ).record_retry_success()

    # Second isolation: the guard's cache converges, which is what the
    # per-turn pre-strip reads, and the fresh-upload cache still stays out of it.
    assert unsupported_replayed_media_kinds_for_route(route) == frozenset({"image"})
    assert unsupported_media_kinds_for_route(route) == frozenset()
    assert filter_media_inputs_for_route(route, fresh_image).media_inputs == fresh_image


def test_a_fresh_upload_co_removed_with_a_context_kind_is_confounded_across_kinds_too() -> None:
    """Confounding belongs to the removal, so a different kind co-removed confounds just as hard.

    One retry took the user's audio and a replayed image away together. Reading
    that as "the audio was the only fresh kind, so the audio is proven" is the
    per-kind accounting this test exists to rule out: a corrupt image in thread
    history produces exactly this turn, and crediting the single-strike cache
    would drop every audio file the user uploads for the rest of the process on
    the strength of it. The audio is credited to nothing at all — nothing ever
    replayed it, so the two-strike gate is not its cache either — and only the
    image, which the turn did carry as replayed media, walks that ladder.

    Two identical turns run the ladder to its end rather than stopping at the
    first strike, where an empty replayed cache would pass for the wrong reason:
    the second strike turns the image into a lesson, and the audio is still
    absent from both caches and from the suspicion set behind one of them.
    """
    route = _route()

    for _turn in range(2):
        decision = retry_media_inputs_after_failure(
            route,
            "Error code: 400 - unsupported inline media",
            MediaInputs(audio=(MagicMock(name="audio"),)),
            extra_present_kinds=frozenset({"image"}),
        )

        assert decision.removed_kinds == frozenset({"audio", "image"})
        assert decision.teachable_kinds == frozenset()
        assert decision.teachable_context_kinds == frozenset({"image"})

        decision.record_retry_success()

    assert unsupported_media_kinds_for_route(route) == frozenset()
    assert suspected_replayed_media_kinds_for_route(route) == frozenset({"image"})
    assert unsupported_replayed_media_kinds_for_route(route) == frozenset({"image"})
    fresh_audio = MediaInputs(audio=(MagicMock(name="audio"),))
    assert filter_media_inputs_for_route(route, fresh_audio).media_inputs == fresh_audio


def test_a_kind_arriving_as_both_a_fresh_upload_and_context_is_confounded_evidence() -> None:
    """The same kind on both sides isolates neither, so neither cache hears about it.

    One removal took the user's attachment and the replayed copy away together,
    and the success cannot say which of the two the provider objected to — a
    corrupt image in thread history produces exactly this turn, and so does a
    corrupt image the user just uploaded. Crediting the single-strike run-input
    cache would let the first blind the route to fresh uploads for the process
    lifetime; crediting the two-strike gate would let the second blind it to
    thread history. The evidence names no provenance, so it is banked nowhere.

    The price is stated plainly: while a conversation keeps re-uploading a kind
    its own history already holds, that turn shape never converges and keeps
    paying two calls a turn. It answers correctly every time, and the first turn
    that ships the upload without the replayed copy beside it settles the cache.
    """
    route = _route()

    decision = retry_media_inputs_after_failure(
        route,
        "image input is not supported",
        MediaInputs(images=(MagicMock(name="image"),)),
        extra_present_kinds=frozenset({"image"}),
    )

    # Both copies still have to come off for the retry to have a chance.
    assert decision.removed_kinds == frozenset({"image"})
    assert decision.teachable_kinds == frozenset()
    assert decision.teachable_context_kinds == frozenset()

    decision.record_retry_success()

    assert suspected_replayed_media_kinds_for_route(route) == frozenset()
    assert unsupported_replayed_media_kinds_for_route(route) == frozenset()
    assert unsupported_media_kinds_for_route(route) == frozenset()
    fresh_image = MediaInputs(images=(MagicMock(name="image"),))
    assert filter_media_inputs_for_route(route, fresh_image).media_inputs == fresh_image


def test_a_wire_guard_recovery_blocks_the_context_credit_too() -> None:
    """A success the guard caused is not evidence for either cache.

    The guard becomes the retry owner the moment this layer stops carrying
    media of its own, so on a context-only retry it is exactly the layer most
    likely to have made the difference — sending that success to the two-strike
    gate would let the guard corroborate its own removal through the layer above.
    """
    route = _route()

    decision = retry_media_inputs_after_failure(
        route,
        "image input is not supported",
        MediaInputs(),
        extra_present_kinds=frozenset({"image"}),
    )
    note_wire_media_recovery()
    decision.record_retry_success()

    assert suspected_replayed_media_kinds_for_route(route) == frozenset()
    assert unsupported_replayed_media_kinds_for_route(route) == frozenset()
    assert unsupported_media_kinds_for_route(route) == frozenset()


def test_a_context_kind_the_guard_has_learned_is_removed_but_not_this_layers_to_credit() -> None:
    """Removal and credit are different questions, and only the second one is the guard's.

    The guard never owns a message the caller put on the wire, so context media
    reaches the provider whatever the guard learned, and this layer is the only
    one that can take it off: the retry has to strip it or it cannot recover.
    What the retry may not do is bank it. Context media is thread history
    MindRoom re-materialized into the run input, so crediting a kind the guard
    already learned from replays would walk a replayed-media lesson into the
    cache that gates fresh uploads — the exact crossover the two separate
    caches exist to prevent.

    The audio here is the second half of the same rule. It is a fresh upload
    *and* a context kind, so the one removal isolated neither copy: it is
    confounded evidence, and confounded evidence belongs to no cache — not the
    one that gates the user's next upload, and not the two-strike gate either.
    """
    route = _route()
    record_replayed_media_isolation(route, "image")
    record_replayed_media_isolation(route, "image")

    decision = retry_media_inputs_after_failure(
        route,
        "audio input is not supported",
        MediaInputs(audio=(MagicMock(name="audio"),)),
        extra_present_kinds=frozenset({"image", "audio"}),
    )

    assert decision.should_retry is True
    assert decision.removed_kinds == frozenset({"audio", "image"})
    assert decision.teachable_kinds == frozenset()
    # The learned image is subtracted outright — a third strike teaches nothing
    # — and the confounded audio is subtracted too, so the turn teaches nothing.
    assert decision.teachable_context_kinds == frozenset()

    decision.record_retry_success()

    assert unsupported_media_kinds_for_route(route) == frozenset()
    assert suspected_replayed_media_kinds_for_route(route) == frozenset({"image"})
    assert unsupported_replayed_media_kinds_for_route(route) == frozenset({"image"})
    fresh_image = MediaInputs(images=(MagicMock(name="image"),))
    assert filter_media_inputs_for_route(route, fresh_image).media_inputs == fresh_image
    fresh_audio = MediaInputs(audio=(MagicMock(name="audio"),))
    assert filter_media_inputs_for_route(route, fresh_audio).media_inputs == fresh_audio


def test_a_fresh_upload_stays_this_layers_variable_whatever_the_guard_learned() -> None:
    """The guard never touches a current-turn upload, so this layer still owns that kind.

    Only the context kinds are the guard's; the same kind arriving as a fresh
    upload is removed by this layer's retry and is therefore its to credit.
    """
    route = _route()
    record_replayed_media_isolation(route, "image")
    record_replayed_media_isolation(route, "image")

    decision = retry_media_inputs_after_failure(
        route,
        "image input is not supported",
        MediaInputs(images=(MagicMock(name="image"),)),
    )

    assert decision.removed_kinds == frozenset({"image"})

    decision.record_retry_success()

    assert unsupported_media_kinds_for_route(route) == frozenset({"image"})


def test_a_context_only_retry_the_guard_has_learned_still_strips_and_teaches_nothing() -> None:
    """Nothing left to learn is not nothing left to try: the removal is still this layer's alone.

    A turn whose only media is a context kind the guard already learned is the
    branch's founding failure mode — the guard cannot reach that message, so
    declining the retry here hands the user a raw provider error instead of a
    degraded answer. The retry runs, and it simply banks nothing.
    """
    route = _route()
    record_replayed_media_isolation(route, "image")
    record_replayed_media_isolation(route, "image")

    decision = retry_media_inputs_after_failure(
        route,
        "image input is not supported",
        MediaInputs(),
        extra_present_kinds=frozenset({"image"}),
    )

    assert decision.should_retry is True
    assert decision.removed_kinds == frozenset({"image"})
    assert decision.teachable_kinds == frozenset()

    decision.record_retry_success()

    assert unsupported_media_kinds_for_route(route) == frozenset()


def test_the_first_attempt_note_names_the_kind_it_took_while_another_kind_survives() -> None:
    """A pre-strip that leaves a fresh upload behind must say which kind actually left.

    The gate deciding whether to speak has always reasoned per kind; the note it
    gated did not, so it only stayed silent when the removed kind was the same
    kind as the surviving upload. Strip a replayed image, keep a fresh audio,
    and unqualified prose tells the model its attachments were rejected while it
    is holding the audio — the hunt-for-invisible-files failure the gate exists
    to prevent, one kind sideways. Both drivers build this attempt, so naming
    the kinds fixes all four paths at once.
    """
    model = OpenAIChat(id="qwen-local", base_url="http://localhost:9292/v1")
    route = build_model_media_route(model)
    record_replayed_media_isolation(route, "image")
    record_replayed_media_isolation(route, "image")
    run_input = [
        Message(role="user", content="thread context photo", images=[Image(content=b"context")]),
        Message(role="user", content="and what about this recording?"),
    ]

    attempt = ai_runtime.MediaAttempt.initial(
        run_input,
        MediaInputs(audio=(Audio(content=b"audio-bytes", mime_type="audio/ogg"),)),
        model,
        fallback_prompt=INLINE_MEDIA_FALLBACK_PROMPT_TEMPLATE,
        run_id=None,
    )

    assert attempt.removed_media_kinds == frozenset({"image"})
    assert attempt.attempt_media_inputs.kinds() == frozenset({"audio"})
    note = str(attempt.attempt_prompt[-1].content)
    assert "[Inline media unavailable for this model]" in note
    assert "The model rejected inline image attachments for this turn." in note
    assert "audio" not in note


def test_unsupported_media_kinds_for_route_defaults_empty() -> None:
    """Unknown and None routes report no learned-unsupported kinds."""
    assert unsupported_media_kinds_for_route(None) == frozenset()
    assert unsupported_media_kinds_for_route(_route()) == frozenset()


def test_cache_can_be_reset() -> None:
    """Tests need explicit access to clear process-local learned state."""
    media = _media_inputs()
    route = _route()

    retry_media_inputs_after_failure(route, "image input is not supported", media).record_retry_success()
    record_replayed_media_isolation(route, "audio")
    record_replayed_media_isolation(route, "audio")
    assert filter_media_inputs_for_route(route, media).media_inputs == MediaInputs()

    reset_model_media_capability_cache()

    assert filter_media_inputs_for_route(route, media).media_inputs == media
    assert unsupported_replayed_media_kinds_for_route(route) == frozenset()
    assert suspected_replayed_media_kinds_for_route(route) == frozenset()


def test_replayed_media_lesson_never_suppresses_a_fresh_upload() -> None:
    """What a model refuses to replay says nothing about what the user may upload next.

    A replayed attachment differs from a fresh one in format, size, and encoding,
    so the wire guard's experiments write their own cache and only the run-input
    experiments in `ai.py`/`teams.py` gate run-input media.
    """
    route = _route()
    media = _media_inputs()

    record_replayed_media_isolation(route, "image")
    record_replayed_media_isolation(route, "image")

    assert unsupported_media_kinds_for_route(route) == frozenset()
    assert filter_media_inputs_for_route(route, media).removed_kinds == frozenset()
    assert unsupported_replayed_media_kinds_for_route(route) == frozenset({"image"})


def test_one_isolation_does_not_blind_a_route_to_a_modality() -> None:
    """A corrupt PNG isolates exactly like an unsupported modality, so one experiment is a suspicion.

    The blast radius decides the threshold: this cache is read for the rest of
    the process, and a replayed attachment is present on every turn of its
    conversation, so a wrong lesson is permanent and a wrong suspicion costs one
    repeated experiment.
    """
    route = _route()

    record_replayed_media_isolation(route, "image")

    assert suspected_replayed_media_kinds_for_route(route) == frozenset({"image"})
    assert unsupported_replayed_media_kinds_for_route(route) == frozenset()

    record_replayed_media_isolation(route, "video")
    record_replayed_media_isolation(route, "image")

    assert unsupported_replayed_media_kinds_for_route(route) == frozenset({"image"})


def test_a_recovery_the_wire_guard_caused_teaches_the_run_input_cache_nothing() -> None:
    """Two retry layers share one request, and only the one that changed something may conclude."""
    route = _route()
    media = _media_inputs()

    decision = retry_media_inputs_after_failure(route, "Error code: 400 - unsupported inline media", media)
    # The retry succeeds because the guard dropped a replayed attachment this
    # layer never sent and cannot see.
    note_wire_media_recovery()
    decision.record_retry_success()

    assert unsupported_media_kinds_for_route(route) == frozenset()


@pytest.mark.asyncio
async def test_a_recovery_reported_from_a_task_of_agnos_own_still_reaches_this_layer() -> None:
    """Agno may pull the provider from a task whose context is a copy of this one.

    A ``ContextVar`` rebound inside that copy is invisible here, so the wire
    guard's report would be lost and this layer would credit a recovery it did
    not cause. Mutating the holder both contexts point at is what survives the
    copy — this test is the only place that distinguishes the two.
    """
    route = _route()
    media = _media_inputs()

    decision = retry_media_inputs_after_failure(route, "Error code: 400 - unsupported inline media", media)

    async def guard_strips_replayed_media_in_its_own_task() -> None:
        note_wire_media_recovery()

    await asyncio.create_task(guard_strips_replayed_media_in_its_own_task())
    decision.record_retry_success()

    assert unsupported_media_kinds_for_route(route) == frozenset()


def test_reset_clears_the_recovery_a_previous_turn_observed() -> None:
    """A stale ``observed=True`` suppresses the next real lesson without a sound.

    The holder is per-context state one turn leaves behind, so the reset the
    autouse fixture calls has to clear it along with the route caches.
    """
    route = _route()
    retry_media_inputs_after_failure(route, "image input is not supported", _media_inputs())
    note_wire_media_recovery()

    reset_model_media_capability_cache()

    MediaRetryDecision(
        should_retry=True,
        media_inputs=MediaInputs(),
        removed_kinds=frozenset({"image"}),
        teachable_kinds=frozenset({"image"}),
        teach_route_on_success=route,
    ).record_retry_success()
    assert unsupported_media_kinds_for_route(route) == frozenset({"image"})


@pytest.mark.parametrize(
    "message",
    [
        # Anthropic, verbatim: no marker is a substring of this.
        "image exceeds 5 MB maximum: 5924309 bytes > 5242880 bytes",
        # OpenAI, verbatim: "maximum size" never appears.
        "Maximum content size limit (10485760) exceeded",
    ],
)
def test_real_provider_size_prose_slips_past_the_markers(message: str) -> None:
    """The markers are a cheap first filter over unstable prose, not a safety mechanism.

    Only Gemini's wording happens to match them. The replayed-media cache
    survives that because a lesson needs a second, independent isolation; the
    run-input cache accepts the risk, since it gates one turn's fresh uploads
    rather than every replay of one conversation's history.
    """
    assert capability_teaching_blocked(ModelProviderError(message=message, status_code=400)) is False


def test_run_input_lesson_does_not_reach_the_replayed_cache() -> None:
    """The run-input experiment removes every kind at once, so it attributes nothing.

    A multi-kind conclusion cannot gate a single kind, which is why the wire
    guard pays its own failed call instead of inheriting this one.
    """
    route = _route()

    retry_media_inputs_after_failure(route, "image input is not supported", _media_inputs()).record_retry_success()

    assert unsupported_media_kinds_for_route(route) == frozenset({"audio", "image", "file", "video"})
    assert unsupported_replayed_media_kinds_for_route(route) == frozenset()


def _route(base_url: str = "http://localhost:9292/v1") -> ModelMediaRoute:
    return ModelMediaRoute(provider="openai", model_id="qwen-local", base_url=base_url)


def _media_inputs() -> MediaInputs:
    return MediaInputs(
        audio=(MagicMock(name="audio"),),
        images=(MagicMock(name="image"),),
        files=(MagicMock(name="file"),),
        videos=(MagicMock(name="video"),),
    )


def test_run_input_media_helpers_cover_pinned_history_media() -> None:
    """Run-input helpers report, collect, and strip media pinned to history messages."""
    image = Image(content=b"\x89PNG\r\n\x1a\npayload")
    audio = Audio(content=b"audio-bytes", mime_type="audio/ogg")
    history = Message(role="user", content="earlier", images=[image], audio=[audio])
    current = Message(role="user", content="now")
    run_input = [history, current]

    collected = ai_runtime.media_inputs_from_run_input(run_input)
    assert collected.kinds() == frozenset({"image", "audio"})
    assert ai_runtime.media_inputs_from_run_input("plain prompt").kinds() == frozenset()
    assert list(collected.images) == [image]
    assert list(collected.audio) == [audio]

    stripped = ai_runtime.append_inline_media_fallback_to_run_input(
        run_input,
        fallback_prompt="Use attachment tools instead of the {kinds} you cannot see.",
        removed_kinds=frozenset({"image"}),
        note_kinds=frozenset({"image"}),
    )
    assert stripped[0].images is None
    assert [item.content for item in (stripped[0].audio or [])] == [audio.content]
    assert "[Inline media unavailable for this model]" in str(stripped[-1].content)
    assert "Use attachment tools instead of the image you cannot see." in str(stripped[-1].content)
    # The original run input stays untouched for later retries.
    assert history.images == [image]
