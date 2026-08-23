""":class:`RunStatus` predicates over one agent or team run output.

:class:`RunStatus` is a ``str`` enum whose values are uppercase, so a status
that survived serialization comes back as ``"ERROR"`` and still satisfies enum
identity. Reading it through one normalization is therefore defense in depth
rather than a fix for a shape production emits today: it is what keeps a
lowercase string, or a status that is not a :class:`RunStatus` at all, from
reaching a site that writes a process-lifetime cache. The hole this module does
close on the live paths is the **paused** outcome, which the agent blocking
driver never checked before, and the single spelling of "this run finished with
an answer" that every media-lesson site now shares.

"Answered nothing" and "delivered nothing" are two predicates here because they
are two questions. The bar that banks a media capability lesson asks whether the
provider came back with an answer, and a generated image is one: it proves the
request went through, which is the whole of what the experiment measures. The
drivers that discard and retry an empty completed run
(:func:`is_empty_completed_run`, re-exported by ``ai_runtime``) ask a narrower
question — whether this run left behind anything this codebase can put in front
of a user — and generated media is not that, because no delivery path renders
it. Sharing one predicate between the two made a media-only run non-empty, which
skipped the empty-response notice and ended the turn in silence.

The answer definition reads one run output's own ``tools``, ``content`` and
generated media, which is the whole of the agent path's answer. A team's answer
can live entirely in ``member_responses`` instead, and the team drivers already
compute that fact
(``_collect_team_tool_executions`` and ``_has_visible_team_output``) to decide
whether to discard the run. So a caller that has already judged answeredness
passes it in (:func:`media_free_retry_succeeded`'s ``answered``) rather than
having it re-derived here from fields the answer is not in — re-deriving it is
what let the bank bar call a member-answered team run empty while the driver
delivered it.
"""

from __future__ import annotations

from agno.run.agent import RunCompletedEvent, RunOutput
from agno.run.base import RunStatus
from agno.run.team import RunCompletedEvent as TeamRunCompletedEvent
from agno.run.team import TeamRunOutput

#: Everything a driver can hold that carries the model's generated media.
#: The two run outputs are what a blocking driver settles on, and the two
#: completion events are the only place a stream sees the same channels.
MediaAnsweringOutcome = RunOutput | TeamRunOutput | RunCompletedEvent | TeamRunCompletedEvent

__all__ = [
    "MediaAnsweringOutcome",
    "carries_media_answer",
    "is_cancelled_run_output",
    "is_completed_run_output",
    "is_empty_completed_run",
    "is_errored_run_output",
    "is_paused_run_output",
    "media_free_retry_succeeded",
]


def is_errored_run_output(response: TeamRunOutput | RunOutput) -> bool:
    """Return whether a team or agent fallback run ended in an error state."""
    return _has_status(response, RunStatus.error)


def is_cancelled_run_output(response: TeamRunOutput | RunOutput) -> bool:
    """Return whether a team or agent fallback run ended in a cancelled state."""
    return _has_status(response, RunStatus.cancelled)


def is_paused_run_output(response: TeamRunOutput | RunOutput) -> bool:
    """Return whether a team or agent fallback run stopped at a pause."""
    return _has_status(response, RunStatus.paused)


def is_completed_run_output(response: TeamRunOutput | RunOutput) -> bool:
    """Return whether a team or agent fallback run reached the completed state."""
    return _has_status(response, RunStatus.completed)


def is_empty_completed_run(response: TeamRunOutput | RunOutput) -> bool:
    """Return whether one run completed with no tool calls and no visible content."""
    return is_completed_run_output(response) and _delivered_nothing(response)


def media_free_retry_succeeded(response: object, *, answered: bool | None = None) -> bool:
    """Report whether a without-media retry proved that media caused the failure.

    Every driver that holds a terminal run output asks this before banking a
    media capability lesson that lasts the life of the process, so the bar is
    an allowlist of one status — ``completed`` — plus an answer. A denylist of
    the unhappy statuses would bank on ``running`` — the dataclass default of
    both run outputs, so the status of an output nothing ever finished — as
    well as on ``pending``, on an unrecognised string, and on an empty one. An
    error, a cancellation and a pause each leave the experiment unfinished, and
    an outcome that is not a run output at all carries no status to judge.

    ``completed`` alone is not the experiment coming back either: a run that
    finished with no tool call, no generated media and no content is the shape a
    driver throws away and retries, and it proves nothing about the media. Only
    the media-free attempt coming back with a real answer says the media is what
    the provider objected to.

    What counts as an answer is the caller's to say when the caller already
    knows. ``answered`` replaces the local reading for exactly the drivers that
    computed one before asking: a team run answers through its members, so
    :func:`_answered_nothing` — which reads this output's own ``tools``,
    ``content`` and generated media — would call a member-answered run empty and
    refuse a lesson the driver's own delivered answer had already earned. Left
    unset, the local reading stands, which is the agent path's own definition.
    Either way the answer counts generated media, which is where
    :func:`is_empty_completed_run` deliberately parts company with this bar: a
    provider that answered in a channel the delivery path cannot render still
    proved the media was the problem. The status half is never the caller's to
    override.
    """
    if not isinstance(response, (TeamRunOutput, RunOutput)):
        return False
    if not is_completed_run_output(response):
        return False
    return not _answered_nothing(response) if answered is None else answered


def carries_media_answer(outcome: MediaAnsweringOutcome) -> bool:
    """Return whether one finished run answered through a channel that is not text.

    A model answers in media as readily as in words. Gemini appends the image it
    generated to ``images`` and never touches ``content``, and an OpenAI audio
    answer arrives on ``response_audio`` the same way. Agno propagates every one
    of these channels onto the run output a blocking driver settles on and onto
    the completion event a stream carries, so a bar that reads ``tools`` and
    ``content`` alone calls a delivered media answer "answered nothing" — and
    the route that just proved the media was what the provider objected to
    re-walks the whole ladder on every later turn instead of converging.
    """
    return bool(
        outcome.images or outcome.videos or outcome.audio or outcome.files or outcome.response_audio,
    )


def _answered_nothing(response: TeamRunOutput | RunOutput) -> bool:
    """Return whether a run left behind no tool call, no generated media, and no visible content."""
    if response.tools or carries_media_answer(response):
        return False
    return _has_no_visible_content(response)


def _delivered_nothing(response: TeamRunOutput | RunOutput) -> bool:
    """Return whether a run left behind nothing this codebase can put in front of a user.

    Deliberately blind to generated media, which is the one way this differs
    from :func:`_answered_nothing`. Nothing in the delivery path renders
    ``RunOutput.images``, ``videos``, ``audio``, ``files`` or ``response_audio``
    — ``_extract_response_content`` reads ``content`` and ``tools``, and the
    streaming and gateway layers never look at the media fields at all — so a
    run whose whole answer is an image hands the user an empty message. Calling
    that run non-empty skips the empty-response notice and ends the turn in
    silence, which reads to the user like a crash. The bar that banks a media
    capability lesson still counts the image: the provider did answer, and that
    is what the experiment measures.

    When a delivery path for generated media arrives, this predicate is the
    thing to teach about it.
    """
    if response.tools:
        return False
    return _has_no_visible_content(response)


def _has_no_visible_content(response: TeamRunOutput | RunOutput) -> bool:
    """Return whether one run output's own ``content`` is absent or whitespace."""
    content = response.content
    return content is None or (isinstance(content, str) and not content.strip())


def _has_status(response: TeamRunOutput | RunOutput, expected: RunStatus) -> bool:
    status = response.status.value if isinstance(response.status, RunStatus) else response.status
    return str(status).lower() == expected.value.lower()
