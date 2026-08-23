"""Tests for the run-status predicates that gate every media capability lesson.

The whole table is written out on purpose. `media_free_retry_succeeded` was once
a denylist of the unhappy statuses, which quietly said "yes" to `running` — the
dataclass default of both run outputs, so the status of a run nothing ever
finished. It was then an allowlist of `completed` alone, which said "yes" to a
run that finished with no content and no tool call — the very shape the drivers
that ask it throw away and retry. A table over every status, in every spelling,
crossed with what the run actually answered, on both output types, makes both
classes of hole impossible to miss.

Each shape carries two expectations, not one, because banking and delivering are
two questions. A run that answered only in generated media banks a lesson — the
provider accepted the request, which is the whole of what the experiment
measures — and is still an empty run to deliver, because nothing downstream
renders an image the model produced. Collapsing the two columns is what once
made a media-only turn skip the empty-response notice and end in silence.
"""

from __future__ import annotations

import pytest
from agno.media import Audio, File, Image, Video
from agno.models.response import ToolExecution
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput

from mindroom.run_output_status import (
    is_cancelled_run_output,
    is_empty_completed_run,
    is_errored_run_output,
    is_paused_run_output,
    media_free_retry_succeeded,
)

_STATUS_SPELLINGS = [
    pytest.param(lambda status: status, id="enum"),
    pytest.param(lambda status: status.value.upper(), id="upper"),
    pytest.param(lambda status: status.value.lower(), id="lower"),
]
_OUTPUT_TYPES = [
    pytest.param(RunOutput, id="agent"),
    pytest.param(TeamRunOutput, id="team"),
]
_TOOL_CALL = ToolExecution(tool_name="run_shell_command", tool_args={"cmd": "pwd"}, result="/app")
# What the run left behind, whether that counts as having answered, and whether
# any of it can be put in front of the user. The whole list crosses `completed`
# in the test below; the status table crosses the four shapes that are distinct
# to it — text, a tool call alone, media alone, and nothing.
#
# One row per media channel, because a model answers in media as readily as in
# text: Gemini appends the image it generated and leaves `content` empty, and an
# OpenAI audio answer arrives on `response_audio` the same way. Agno propagates
# every one of these onto the run output, so a bank bar reading `tools` and
# `content` alone would refuse the lesson a media-answering route just earned,
# and that route would re-walk the whole ladder every turn instead of
# converging. Callers that know better still say so through `answered`, which
# the table below crosses over every one of these shapes.
#
# The deliverable column is `False` for every one of those media rows: no
# delivery path in this codebase renders a generated image, video, file or
# spoken answer, so the run is still the empty one the drivers discard, retry
# once, and finish with the empty-response notice.
_TEXT_SHAPE = pytest.param({"content": "Here is the answer"}, True, True, id="text")
_TOOL_ONLY_SHAPE = pytest.param({"content": None, "tools": [_TOOL_CALL]}, True, True, id="tool-call-only")
_MEDIA_ONLY_SHAPE = pytest.param(
    {"content": None, "images": [Image(content=b"generated")]},
    True,
    False,
    id="image-only",
)
_EMPTY_SHAPE = pytest.param({"content": ""}, False, False, id="empty-content")
_RUN_SHAPES = [
    _TEXT_SHAPE,
    _TOOL_ONLY_SHAPE,
    pytest.param({"content": "   ", "tools": [_TOOL_CALL]}, True, True, id="blank-text-with-tool-call"),
    _MEDIA_ONLY_SHAPE,
    pytest.param({"content": None, "videos": [Video(content=b"generated")]}, True, False, id="video-only"),
    pytest.param({"content": "  ", "audio": [Audio(content=b"generated")]}, True, False, id="audio-list-only"),
    pytest.param({"content": None, "files": [File(content=b"generated")]}, True, False, id="file-only"),
    pytest.param({"content": None, "response_audio": Audio(content=b"spoken")}, True, False, id="response-audio-only"),
    pytest.param({"content": None}, False, False, id="no-content"),
    _EMPTY_SHAPE,
    pytest.param({"content": "  \n "}, False, False, id="blank-content"),
]
_STATUS_TABLE_SHAPES = [_TEXT_SHAPE, _TOOL_ONLY_SHAPE, _MEDIA_ONLY_SHAPE, _EMPTY_SHAPE]
_PREDICATES = [
    pytest.param(
        is_errored_run_output,
        lambda status, _answered, _deliverable: status is RunStatus.error,
        id="errored",
    ),
    pytest.param(
        is_cancelled_run_output,
        lambda status, _answered, _deliverable: status is RunStatus.cancelled,
        id="cancelled",
    ),
    pytest.param(is_paused_run_output, lambda status, _answered, _deliverable: status is RunStatus.paused, id="paused"),
    pytest.param(
        media_free_retry_succeeded,
        lambda status, answered, _deliverable: status is RunStatus.completed and answered,
        id="succeeded",
    ),
    pytest.param(
        is_empty_completed_run,
        lambda status, _answered, deliverable: status is RunStatus.completed and not deliverable,
        id="empty-completed",
    ),
]


@pytest.mark.parametrize(("predicate", "expected"), _PREDICATES)
@pytest.mark.parametrize(("shape", "answered", "deliverable"), _STATUS_TABLE_SHAPES)
@pytest.mark.parametrize("status", list(RunStatus))
@pytest.mark.parametrize("spell", _STATUS_SPELLINGS)
@pytest.mark.parametrize("output_type", _OUTPUT_TYPES)
def test_each_predicate_answers_for_exactly_its_own_status_and_answer(
    predicate: object,
    expected: object,
    shape: dict[str, object],
    answered: bool,
    deliverable: bool,
    status: RunStatus,
    spell: object,
    output_type: object,
) -> None:
    """Every predicate reads the status it owns, and the two success bars read their own answer."""
    response = output_type(run_id="run-1", status=spell(status), **shape)  # type: ignore[operator]

    assert predicate(response) is expected(status, answered, deliverable)  # type: ignore[operator]


@pytest.mark.parametrize(("shape", "answered", "deliverable"), _RUN_SHAPES)
@pytest.mark.parametrize("output_type", _OUTPUT_TYPES)
def test_a_completed_run_banks_a_lesson_only_when_it_answered(
    shape: dict[str, object],
    answered: bool,
    deliverable: bool,
    output_type: object,
) -> None:
    """`completed` and empty is the shape the drivers discard and retry, so it proves nothing.

    Banking and delivering part company on generated media. A completed run that
    came back with an image answered — the provider took the request, which is
    the only thing the media experiment asks — and it is still the empty run
    this codebase has nothing to show for, so the turn earns the notice rather
    than silence.
    """
    response = output_type(run_id="run-1", status=RunStatus.completed, **shape)  # type: ignore[operator]

    assert media_free_retry_succeeded(response) is answered
    assert is_empty_completed_run(response) is not deliverable


@pytest.mark.parametrize("output_type", _OUTPUT_TYPES)
def test_the_default_status_of_an_unfinished_run_is_not_a_success(output_type: object) -> None:
    """A run output nobody settled carries `running`, which must never bank a lesson."""
    response = output_type(run_id="run-1", content="Here is the answer")  # type: ignore[operator]

    assert response.status == RunStatus.running
    assert media_free_retry_succeeded(response) is False


@pytest.mark.parametrize(
    "status",
    ["", "  ", "COMPLETE", "finished", "succeeded", "ok", None],
)
@pytest.mark.parametrize("output_type", _OUTPUT_TYPES)
def test_a_status_that_is_not_completed_is_not_a_success(status: object, output_type: object) -> None:
    """An empty, absent, or unrecognised status says nothing, so it cannot say success."""
    response = output_type(run_id="run-1", status=status, content="Here is the answer")  # type: ignore[operator]

    assert media_free_retry_succeeded(response) is False


@pytest.mark.parametrize(
    "response",
    [None, "COMPLETED", RunStatus.completed, object(), {"status": "COMPLETED"}],
)
def test_an_outcome_that_is_not_a_run_output_is_not_a_success(response: object) -> None:
    """Only a real run output carries a status this may judge; anything else banks nothing."""
    assert media_free_retry_succeeded(response) is False


@pytest.mark.parametrize("answered", [True, False])
@pytest.mark.parametrize(("shape", "_locally_answered", "deliverable"), _RUN_SHAPES)
@pytest.mark.parametrize("output_type", _OUTPUT_TYPES)
def test_a_callers_own_answer_replaces_the_local_reading(
    answered: bool,
    shape: dict[str, object],
    _locally_answered: bool,
    deliverable: bool,
    output_type: object,
) -> None:
    """A team's answer lives in its members, so the driver that read it decides, not this leaf.

    Both directions matter. A run whose own fields are empty above a member that
    replied must bank, and a run whose own fields carry text the driver
    nonetheless judged empty must not.

    No caller's answer moves the delivery verdict: whether this turn has
    anything to show the user is read off the run itself, so a driver vouching
    for a member's answer cannot turn a media-only run into a deliverable one.
    """
    response = output_type(run_id="run-1", status=RunStatus.completed, **shape)  # type: ignore[operator]
    assert is_empty_completed_run(response) is not deliverable

    assert media_free_retry_succeeded(response, answered=answered) is answered


@pytest.mark.parametrize("answered", [True, False])
@pytest.mark.parametrize("status", [status for status in RunStatus if status is not RunStatus.completed])
@pytest.mark.parametrize("output_type", _OUTPUT_TYPES)
def test_a_callers_own_answer_never_overrides_the_status_half(
    answered: bool,
    status: RunStatus,
    output_type: object,
) -> None:
    """The experiment has to have finished; no caller may vouch for that on the run's behalf."""
    response = output_type(run_id="run-1", status=status, content="Here is the answer")  # type: ignore[operator]

    assert media_free_retry_succeeded(response, answered=answered) is False
