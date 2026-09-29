"""Tests that fitted desktop replies stay within one encrypted to-device message."""

from __future__ import annotations

from dataclasses import replace

import pytest

from mindroom.desktop.protocol import MAX_INLINE_RESPONSE_BYTES, DesktopResponse
from mindroom.desktop.reply_fitting import (
    _WIDEST_METRICS,
    fits_inline,
    leftmost_fitting,
    response_metrics,
    success_response,
)
from tests.desktop_helpers import _LONGEST_SESSION_ID, MEDIA, _command, _longest_request_id


def test_success_response_answers_the_command_with_its_result_and_screenshot() -> None:
    """A success reply carries the command's identifiers, the result, and an optional screenshot."""
    command = _command("list_apps", request_id="request-7", session_id="session-7")
    assert success_response(command, result={"apps": []}) == DesktopResponse(
        request_id="request-7",
        session_id="session-7",
        ok=True,
        result={"apps": []},
    )
    assert success_response(command, result={}, screenshot=MEDIA).screenshot == MEDIA


def test_fitting_reserves_the_widest_value_of_every_metric() -> None:
    """Fitting reserves room for each metric the bridge adds, at the largest integer JSON holds exactly."""
    assert response_metrics(elapsed_ms=1, structured_bytes=2, screenshot_bytes=3) == {
        "elapsed_ms": 1,
        "structured_bytes": 2,
        "screenshot_bytes": 3,
    }
    assert dict.fromkeys(("elapsed_ms", "structured_bytes", "screenshot_bytes"), 2**53 - 1) == _WIDEST_METRICS


@pytest.mark.parametrize("character", ["x", "\x01", "€", "😀"], ids=["ascii", "control", "bmp", "astral"])
def test_fits_inline_admits_exactly_the_results_whose_widest_envelope_fits(character: str) -> None:
    """The largest result that fits with the widest metrics is admitted, and one more character is not."""
    command = replace(_command("read_file", request_id=_longest_request_id("fit-")), session_id=_LONGEST_SESSION_ID)

    def envelope_bytes(count: int) -> int:
        result = {"text": character * count, "metrics": _WIDEST_METRICS}
        return success_response(command, result=result).content_bytes()

    low, high = 0, MAX_INLINE_RESPONSE_BYTES
    while low < high:
        middle = (low + high + 1) // 2
        low, high = (middle, high) if envelope_bytes(middle) <= MAX_INLINE_RESPONSE_BYTES else (low, middle - 1)
    assert fits_inline(command, {"text": character * low})
    assert not fits_inline(command, {"text": character * (low + 1)})


def test_leftmost_fitting_finds_the_fewest_characters_to_drop() -> None:
    """The search returns the smallest drop whose reply fits, so one fewer would overflow."""
    command = replace(_command("read_file", request_id=_longest_request_id("drop-")), session_id=_LONGEST_SESSION_ID)
    text = "\x01" * MAX_INLINE_RESPONSE_BYTES

    def reply(dropped: int) -> dict[str, object]:
        return {"text": text[: len(text) - dropped]}

    dropped = leftmost_fitting(command, 0, len(text), reply)
    assert fits_inline(command, reply(dropped))
    assert not fits_inline(command, reply(dropped - 1))


def test_leftmost_fitting_keeps_everything_when_the_whole_reply_fits() -> None:
    """Nothing is dropped from a reply that already fits."""
    command = _command("list_folders")
    entries = [{"name": str(number)} for number in range(10)]
    assert (
        leftmost_fitting(command, 0, len(entries), lambda dropped: {"folders": entries[: len(entries) - dropped]}) == 0
    )
