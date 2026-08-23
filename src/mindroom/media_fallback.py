"""Shared inline-media fallback and model capability helpers."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from agno.exceptions import ContextWindowExceededError, ModelProviderError

from mindroom.error_handling import TRANSIENT_PROVIDER_STATUS_CODES, is_model_safeguard_refusal
from mindroom.media_inputs import MediaInputs, MediaKind
from mindroom.prompt_templates import render_prompt_template

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agno.models.base import Model
    from agno.models.message import Message

__all__ = [
    "MediaRetryDecision",
    "ModelMediaRoute",
    "append_inline_media_fallback_prompt",
    "build_model_media_route",
    "capability_teaching_blocked",
    "filter_media_inputs_for_route",
    "message_media_kinds",
    "note_wire_media_recovery",
    "record_replayed_media_isolation",
    "reset_model_media_capability_cache",
    "retry_media_inputs_after_failure",
    "strip_media_kinds_from_message",
    "suspected_replayed_media_kinds_for_route",
    "unsupported_media_kinds_for_route",
    "unsupported_replayed_media_kinds_for_route",
]

_INLINE_MEDIA_FALLBACK_MARKER = "[Inline media unavailable for this model]"
_PAYLOAD_TOO_LARGE_STATUS = 413
_SERVER_ERROR_STATUS = 500
# A per-attachment size limit is a fact about one file, not about the modality,
# and providers report it as an ordinary 400 with the reason only in the prose.
_MEDIA_SIZE_MARKERS = ("too large", "size exceeds", "maximum size", "exceeds the maximum")


@dataclass(frozen=True, slots=True)
class ModelMediaRoute:
    """Concrete model route used for process-local media capability learning."""

    provider: str
    model_id: str
    base_url: str | None = None


@dataclass(frozen=True, slots=True)
class _MediaFilterResult:
    """Media inputs after route capability filtering."""

    media_inputs: MediaInputs
    removed_kinds: frozenset[MediaKind]


@dataclass(slots=True)
class _WireMediaRecovery:
    """Whether the wire guard removed media of its own from a request that then succeeded."""

    observed: bool = False


# A mutable holder rather than a plain flag: the wire guard runs far below the
# layer that reads this, and agno may pull the provider from a task of its own,
# whose context is a copy. Mutating the object both contexts point at survives
# that, while rebinding the ContextVar inside the copy would not be visible here.
_WIRE_MEDIA_RECOVERY: ContextVar[_WireMediaRecovery | None] = ContextVar(
    "mindroom_wire_media_recovery",
    default=None,
)


def note_wire_media_recovery() -> None:
    """Report that the wire guard removed media from the request that succeeded."""
    recovery = _WIRE_MEDIA_RECOVERY.get()
    if recovery is not None:
        recovery.observed = True


@dataclass(frozen=True, slots=True)
class MediaRetryDecision:
    """Retry policy after one provider media failure.

    ``removed_kinds`` is what the retry takes off the payload; the two teachable
    sets are the subsets a success may be credited for, split by which cache the
    evidence belongs in. A retry has to strip everything present to have a chance
    of working, while it may only teach from evidence about its own class of
    input: ``teachable_kinds`` is the fresh upload of a turn that carried
    nothing else, and ``teachable_context_kinds`` is thread-context media
    MindRoom replayed into the run input that the same removal did not also
    take away as a fresh upload. A turn carrying both classes says nothing
    about the fresh kinds it removed, so those appear in neither set.
    ``teach_route_on_success`` carries the route both are credited against once
    the without-media retry actually succeeds; the attempt loop reports that via
    :meth:`record_retry_success`.
    """

    should_retry: bool
    media_inputs: MediaInputs
    removed_kinds: frozenset[MediaKind]
    teachable_kinds: frozenset[MediaKind]
    teachable_context_kinds: frozenset[MediaKind] = frozenset()
    teach_route_on_success: ModelMediaRoute | None = None

    def record_retry_success(self) -> None:
        """Teach the route caches from the successful without-media experiment.

        Only if this layer's removal is the single change behind the success.
        The wire guard strips replayed and tool-produced media from the same
        request, and it becomes the retry owner the moment this layer stops
        carrying media of its own — so on the retry it can strip a replayed
        attachment this layer cannot even see. Crediting that recovery here
        would teach the run-input cache every kind the run input happened to
        carry, on the strength of an experiment that proved nothing.

        Each half of the evidence goes where its own class of input is read.
        The fresh upload is written straight to the run-input cache, because
        this layer's experiment is exactly the experiment that cache answers.
        Context media is replayed media, so it goes through
        :func:`record_replayed_media_isolation` — the same two-strike gate the
        wire guard's own isolations pay — and never into the cache that gates
        fresh uploads. Without that split, one context-only turn on an unprimed
        route would strip the user's next upload of that kind for the life of
        the process; with it, a route whose every turn confounds a fresh upload
        with a replayed attachment pays one extra doomed provider call before it
        converges — two against crediting everything to the fresh cache (the
        measured per-turn call counts are in
        :func:`retry_media_inputs_after_failure`).

        Both sets empty over a non-empty ``removed_kinds`` is the ordinary
        outcome for a turn whose only media is a context kind the guard's cache
        already holds, and for one whose every removed kind is confounded: the
        retry still had to strip it all, and still learns nothing.
        """
        if self.teach_route_on_success is None or not (self.teachable_kinds or self.teachable_context_kinds):
            return
        recovery = _WIRE_MEDIA_RECOVERY.get()
        if recovery is not None and recovery.observed:
            return
        if self.teachable_kinds:
            _UNSUPPORTED_MEDIA_KINDS_BY_ROUTE.setdefault(self.teach_route_on_success, set()).update(
                self.teachable_kinds,
            )
        for kind in sorted(self.teachable_context_kinds):
            record_replayed_media_isolation(self.teach_route_on_success, kind)


# Intentional process-lifetime pessimism: learned negative capability state is
# cleared by restart. The two caches stay disjoint because they are taught by
# experiments over different inputs, and a conclusion may not outlive the class
# of input that produced it: dropping media a model replayed from persisted
# history proves nothing about a fresh attachment the user just uploaded, which
# may differ in format, size, or encoding. What decides which cache a lesson
# lands in is the *provenance of the media the experiment removed*, never the
# layer that ran it: the run-input experiments in `ai.py`/`teams.py` write the
# first cache from the turn's fresh upload and the second one — through the
# same two-strike gate the wire guard pays — from the thread-context media they
# stripped, and the wire guard in `model_media_guard.py` writes the second only.
# Cross-layer *reads* run only in the narrowing direction. The run-input layer
# reads the guard's cache twice: to subtract context kinds it has already
# learned, and to pre-strip context media the guard has already proven this
# route rejects. That second read decides what to strip, but only for context
# media, which is replayed media — the very class of input the guard's
# experiment covered. Nothing but a removed fresh upload may ever strip, or
# excuse, the next fresh upload: what leaves a user's attachment behind is this
# cache alone.
_UNSUPPORTED_MEDIA_KINDS_BY_ROUTE: dict[ModelMediaRoute, set[MediaKind]] = {}
_UNSUPPORTED_REPLAYED_MEDIA_KINDS_BY_ROUTE: dict[ModelMediaRoute, set[MediaKind]] = {}
# Kinds one isolation experiment has implicated, waiting for a second one.
_SUSPECTED_REPLAYED_MEDIA_KINDS_BY_ROUTE: dict[ModelMediaRoute, set[MediaKind]] = {}


def build_model_media_route(model: Model | None) -> ModelMediaRoute | None:
    """Return a process-cache key for one effective model route."""
    if model is None:
        return None

    provider = _route_text(model.provider) or model.__class__.__name__
    model_id = _route_text(model.id) or model.__class__.__name__
    return ModelMediaRoute(
        provider=provider.lower(),
        model_id=model_id,
        base_url=_route_endpoint(model),
    )


def unsupported_media_kinds_for_route(route: ModelMediaRoute | None) -> frozenset[MediaKind]:
    """Return media kinds this route rejected when sent as fresh run input."""
    if route is None:
        return frozenset()
    return frozenset(_UNSUPPORTED_MEDIA_KINDS_BY_ROUTE.get(route, set()))


def unsupported_replayed_media_kinds_for_route(route: ModelMediaRoute | None) -> frozenset[MediaKind]:
    """Return media kinds this route rejected when replayed from history or produced by a tool.

    Reading the run-input cache here too would look like a free head start and is
    not one: that cache is taught by an experiment that removes every kind the
    run input carried at once, so importing it would import a conclusion no
    single-variable experiment supports. The wire guard pays one failed call to
    learn the same thing about its own provenance class instead.
    """
    if route is None:
        return frozenset()
    return frozenset(_UNSUPPORTED_REPLAYED_MEDIA_KINDS_BY_ROUTE.get(route, set()))


def suspected_replayed_media_kinds_for_route(route: ModelMediaRoute | None) -> frozenset[MediaKind]:
    """Return kinds one experiment has implicated on this route but no second one has confirmed."""
    if route is None:
        return frozenset()
    return frozenset(_SUSPECTED_REPLAYED_MEDIA_KINDS_BY_ROUTE.get(route, set()))


def record_replayed_media_isolation(route: ModelMediaRoute | None, kind: MediaKind) -> None:
    """Record one isolation experiment, and learn the kind on the second one.

    A modality the route cannot accept is not the only thing that makes a
    request start working once one attachment leaves it: a corrupt PNG, an
    expired file URI, an unsupported codec inside a supported container and a
    mime type that does not match the bytes all arrive as an ordinary 400 from
    providers that handle that modality perfectly well. Error prose does not
    separate them reliably across vendors, so a second isolation of the same
    kind on the same route is what a lesson costs.

    The asymmetry is the argument: this cache is read for the rest of the
    process, so a wrong lesson silently blinds a route to a whole modality until
    restart, while a wrong suspicion costs one repeated experiment.

    What that buys is bounded, and the bound is worth stating. The two
    isolations are counted per ``(route, kind)`` for the life of the process and
    nothing else about them is compared: not the conversation, not the
    attachment, not how far apart they fell. Successes are not counted at all —
    a turn that goes through with that kind on the wire is never recorded here
    and does not discharge a standing suspicion — so the two may be unrelated
    one-offs weeks apart with working turns in between. The gate rules out a
    single isolated blip, and nothing beyond that.

    A single malformed attachment replayed from history is well inside what is
    left: it is present on every turn of that conversation, so it supplies both
    isolations itself and the mislearn is reliable rather than a one-off; the
    gate delays it by a turn. Rejecting the modality is the conservative end of
    that trade: the agent still answers, and the omission note tells it what it
    lost.
    """
    if route is None:
        return
    suspected = _SUSPECTED_REPLAYED_MEDIA_KINDS_BY_ROUTE.setdefault(route, set())
    if kind in suspected:
        _UNSUPPORTED_REPLAYED_MEDIA_KINDS_BY_ROUTE.setdefault(route, set()).add(kind)
        return
    suspected.add(kind)


def filter_media_inputs_for_route(
    route: ModelMediaRoute | None,
    media_inputs: MediaInputs,
) -> _MediaFilterResult:
    """Omit learned-unsupported media kinds before a model request."""
    removed_kinds = unsupported_media_kinds_for_route(route) & media_inputs.kinds()
    if not removed_kinds:
        return _MediaFilterResult(media_inputs=media_inputs, removed_kinds=frozenset())
    return _MediaFilterResult(
        media_inputs=_without_media_kinds(media_inputs, removed_kinds),
        removed_kinds=removed_kinds,
    )


def retry_media_inputs_after_failure(
    route: ModelMediaRoute | None,
    error: Exception | str,
    media_inputs: MediaInputs,
    *,
    extra_present_kinds: frozenset[MediaKind] = frozenset(),
) -> MediaRetryDecision:
    """Decide how one media-bearing request should retry after a failure.

    Every failure of a media-bearing request retries once without media —
    no error wording decides whether to retry, so unknown provider prose
    (and streamed run errors that lost their HTTP status) degrade
    gracefully instead of leaking a raw provider error to the user. The
    route capability caches learn the dropped kinds once the retry actually
    succeeds (via :meth:`MediaRetryDecision.record_retry_success`), except
    when the error names a payload-size or context-overflow cause, where
    dropping media can succeed for the wrong reason. A kind can only be
    learned when it was actually present in ``media_inputs`` or
    ``extra_present_kinds`` (media pinned to thread-history messages in the
    run input), when this layer's own retry is what removed it, and — for a
    context kind — when the wire guard's cache does not already hold it.
    Which cache it lands in follows the provenance: a fresh upload the retry
    removed on its own teaches the run-input cache directly, and a context kind
    that same removal did not confound is one isolation of replayed media and
    takes the two-strike route. A turn carrying both classes confounds every
    fresh kind it removed, and a confounded kind teaches neither cache.
    """
    if is_model_safeguard_refusal(error):
        return _no_media_retry_decision(media_inputs)
    present_kinds = media_inputs.kinds() | extra_present_kinds
    if not present_kinds:
        return _no_media_retry_decision(media_inputs)

    # Every present kind has to come off for the retry to have a chance: the
    # guard owns no message the caller put on the wire, so this layer is the
    # only one that can remove any of them, whatever either cache holds. What
    # may be *credited*, and to which cache, follows the provenance of what the
    # retry took away. The fresh upload is this layer's own variable and its
    # own conclusion. Context media is thread history MindRoom re-materialized
    # into the run input — replayed media, whatever layer removed it — so it is
    # one isolation experiment over that class of input and nothing more: it
    # goes to the two-strike gate, never to the cache that gates fresh uploads.
    # Crediting it here would let a single context-only turn on a route the
    # guard has never seen strip the user's next upload for the process
    # lifetime. A context kind the guard has already learned is subtracted
    # outright: the lesson is banked, and a third strike teaches nothing.
    #
    # A turn that carried replayed media at all is the case that decides which
    # failure the split is willing to make. Confounding is a property of the
    # *removal*, not of the kind: this layer performs one removal, and it takes
    # every fresh upload away together with every replayed attachment, so the
    # success cannot say which of them the provider objected to. A corrupt
    # replayed image and a perfectly good fresh audio produce exactly that turn,
    # and pricing the audio per kind — as if the image were not co-removed —
    # walks one bad history attachment straight into the single-strike cache
    # that gates the user's uploads. A confounded removal is therefore credited
    # to *neither* cache. Not the fresh one, which a single strike closes. Not
    # the replayed one either: that cache gates media replayed from history or
    # produced by a tool, and a kind this turn only ever uploaded was never
    # replayed, so a strike there would blind the route to a class of input the
    # experiment never touched — the same crossover leak the two caches exist to
    # prevent, pointing the other way. What is left with a provenance class to
    # speak for is the replayed kinds the turn did not also upload fresh, and
    # those take the two-strike gate. The route pays extra turns to converge
    # (see the measured costs below), and no single context attachment can blind
    # it to a fresh upload for the process lifetime.
    #
    # Measured cost of that trade, provider calls per turn on an unprimed route
    # that rejects every kind, from
    # `test_stream_agent_response_pays_two_turns_before_a_context_only_kind_is_learned`
    # and
    # `test_stream_agent_response_pays_a_third_turn_when_the_removal_confounded_two_kinds`:
    #
    #   repeated turn shape        this rule       per-kind    credit-fresh
    #   context image only         [2, 2, 1, 1]    same        [2, 1, 1, 1]
    #   fresh audio + that image   [2, 2, 2, 1, 1] [2,2,1,1,1] [2, 1, 1, 1, 1]
    #
    # So the split costs one doomed call over crediting per kind and two over
    # crediting everything to the fresh cache, and the confounded turn is the
    # one that pays: the upload cannot reach the single-strike cache until a
    # turn removes it alone, which only happens once the replayed gate's two
    # strikes let the pre-strip take the image off first.
    fresh_kinds = media_inputs.kinds()
    confounded_kinds = fresh_kinds if extra_present_kinds else frozenset()
    teachable_kinds = fresh_kinds - confounded_kinds
    teachable_context_kinds = (extra_present_kinds - confounded_kinds) - unsupported_replayed_media_kinds_for_route(
        route,
    )
    teach_route_on_success = None if capability_teaching_blocked(error) else route
    if teach_route_on_success is not None:
        # Watch the retry that is about to run: only a success this layer alone
        # caused is evidence (see :meth:`MediaRetryDecision.record_retry_success`).
        _WIRE_MEDIA_RECOVERY.set(_WireMediaRecovery())
    return MediaRetryDecision(
        should_retry=True,
        media_inputs=_without_media_kinds(media_inputs, present_kinds),
        removed_kinds=present_kinds,
        teachable_kinds=teachable_kinds,
        teachable_context_kinds=teachable_context_kinds,
        teach_route_on_success=teach_route_on_success,
    )


def reset_model_media_capability_cache() -> None:
    """Clear process-local learned model media capabilities."""
    _UNSUPPORTED_MEDIA_KINDS_BY_ROUTE.clear()
    _UNSUPPORTED_REPLAYED_MEDIA_KINDS_BY_ROUTE.clear()
    _SUSPECTED_REPLAYED_MEDIA_KINDS_BY_ROUTE.clear()
    # The recovery holder is per-context state a turn leaves behind, and a
    # stale ``observed=True`` silently suppresses the next real lesson.
    _WIRE_MEDIA_RECOVERY.set(None)


def message_media_kinds(message: Message) -> frozenset[MediaKind]:
    """Return the media kinds one provider message carries, inputs and outputs alike."""
    kinds: set[MediaKind] = set()
    if message.audio or message.audio_output:
        kinds.add("audio")
    if message.images or message.image_output:
        kinds.add("image")
    if message.files or message.file_output:
        kinds.add("file")
    if message.videos or message.video_output:
        kinds.add("video")
    return frozenset(kinds)


def strip_media_kinds_from_message(message: Message, kinds: frozenset[MediaKind]) -> None:
    """Clear every carrier of the given media kinds from one provider message."""
    if "audio" in kinds:
        message.audio = None
        message.audio_output = None
    if "image" in kinds:
        message.images = None
        message.image_output = None
    if "file" in kinds:
        message.files = None
        message.file_output = None
    if "video" in kinds:
        message.videos = None
        message.video_output = None


def append_inline_media_fallback_prompt(
    full_prompt: str,
    *,
    fallback_prompt: str,
    kinds: frozenset[MediaKind],
) -> str:
    """Append one-time guidance naming the media kinds that had to be dropped.

    The note names them because the caller decides *per kind* whether it is
    entitled to say anything at all: a turn that strips a replayed image while
    the user's fresh audio rides along has something true to tell the model and
    something false, and only a note that says which kind left can be the first
    without being the second. The wire guard one layer down already discloses
    its own removals by name, and the two layers speak about the same request.
    """
    if _INLINE_MEDIA_FALLBACK_MARKER in full_prompt:
        return full_prompt

    rendered_prompt = render_prompt_template(fallback_prompt, kinds=", ".join(sorted(kinds)))
    return f"{full_prompt.rstrip()}\n\n{_INLINE_MEDIA_FALLBACK_MARKER}\n{rendered_prompt}"


def capability_teaching_blocked(error: Exception | str) -> bool:
    """Report when a retry success would not prove the media kinds unsupported.

    Payload-size and context-overflow rejections shrink below the limit once
    media is dropped, and transient failures (429 rate limits, 5xx outages, and
    the mid-stream 200 case in ``TRANSIENT_PROVIDER_STATUS_CODES``) can pass on
    the retry because the blip passed — in both cases a successful retry says
    nothing about media capability. Status codes come from the exception object,
    never from provider error prose; streamed run errors arrive as bare text
    without a status and stay eligible to teach.

    The one thing prose is read for is a per-attachment size limit, which most
    providers report as a plain 400: learning "this route cannot take images"
    from one oversized image would blind the route to every image until restart.
    """
    lowered_error_text = str(error).lower()
    if _is_context_window_failure(error, lowered_error_text):
        return True
    if any(marker in lowered_error_text for marker in _MEDIA_SIZE_MARKERS):
        return True
    if isinstance(error, ModelProviderError) and (
        error.status_code == _PAYLOAD_TOO_LARGE_STATUS
        or error.status_code in TRANSIENT_PROVIDER_STATUS_CODES
        or error.status_code >= _SERVER_ERROR_STATUS
    ):
        return True
    return f"error code: {_PAYLOAD_TOO_LARGE_STATUS}" in lowered_error_text


def _no_media_retry_decision(media_inputs: MediaInputs) -> MediaRetryDecision:
    return MediaRetryDecision(
        should_retry=False,
        media_inputs=media_inputs,
        removed_kinds=frozenset(),
        teachable_kinds=frozenset(),
    )


def _is_context_window_failure(error: Exception | str, lowered_error_text: str) -> bool:
    """Recognize context overflow, the one non-capability failure media reduction can fix."""
    return isinstance(error, ContextWindowExceededError) or any(
        marker in lowered_error_text for marker in ModelProviderError.CONTEXT_WINDOW_PATTERNS
    )


def _without_media_kinds(media_inputs: MediaInputs, kinds: frozenset[MediaKind]) -> MediaInputs:
    return MediaInputs(
        audio=() if "audio" in kinds else media_inputs.audio,
        images=() if "image" in kinds else media_inputs.images,
        files=() if "file" in kinds else media_inputs.files,
        videos=() if "video" in kinds else media_inputs.videos,
    )


# The effective endpoint is dispatched on which endpoint attribute the model
# exposes, not on its class, so this module never imports provider model
# classes (and through them provider SDKs) just to route media errors (#1436).
# Azure models keep the endpoint in azure_endpoint, Ollama in host, most
# OpenAI-compatible providers in base_url, and Claude/Gemini only carry
# client_params.
@runtime_checkable
class _HasAzureEndpoint(Protocol):
    azure_endpoint: str | None
    base_url: object
    client_params: Mapping[str, object] | None


@runtime_checkable
class _HasHost(Protocol):
    host: str | None
    client_params: Mapping[str, object] | None


@runtime_checkable
class _HasBaseUrl(Protocol):
    base_url: object
    client_params: Mapping[str, object] | None


@runtime_checkable
class _HasClientParams(Protocol):
    client_params: Mapping[str, object] | None


def _route_endpoint(model: Model) -> str | None:
    if isinstance(model, _HasAzureEndpoint):
        return _route_endpoint_text(
            model.azure_endpoint,
            str(model.base_url) if model.base_url is not None else None,
            _client_params_endpoint(model.client_params),
        )
    if isinstance(model, _HasHost):
        return _route_endpoint_text(
            model.host,
            _client_params_endpoint(model.client_params),
        )
    if isinstance(model, _HasBaseUrl):
        return _route_endpoint_text(
            str(model.base_url) if model.base_url is not None else None,
            _client_params_endpoint(model.client_params),
        )
    if isinstance(model, _HasClientParams):
        return _client_params_endpoint(model.client_params)
    return None


def _client_params_endpoint(client_params: Mapping[str, object] | None) -> str | None:
    if client_params is None:
        return None
    for field_name in ("base_url", "host", "azure_endpoint"):
        candidate = client_params.get(field_name)
        endpoint = _route_text(candidate) if isinstance(candidate, str) else None
        if endpoint:
            return endpoint.rstrip("/")
    return None


def _route_endpoint_text(*values: str | None) -> str | None:
    for value in values:
        endpoint = _route_text(value)
        if endpoint:
            return endpoint.rstrip("/")
    return None


def _route_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None
