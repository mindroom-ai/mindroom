"""Frame building pinned by the fixture MindRoom Chat's applier shares."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from mindroom.live_frames.frames import LiveDocs, live_delta, live_frame, new_publisher_id

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "live-frames-v1.json").read_text(encoding="utf-8"))


def _docs(data: dict[str, object]) -> LiveDocs:
    return LiveDocs(
        md=data["md"],
        reasoning=data["reasoning"],
        tools=tuple(data["tools"]),
        status=data["status"],
        activity=data["activity"],
    )


@pytest.mark.parametrize("case", _FIXTURE["cases"], ids=[case["name"] for case in _FIXTURE["cases"]])
def test_frames_match_the_shared_fixture(case: dict[str, object]) -> None:
    """The builder produces exactly the frames the client's applier is tested against."""
    expected = case["frame"]
    after = _docs(case["after"])
    if "snapshot" in expected:
        frame = live_frame(
            reply_event_id=expected["reply"],
            publisher_id=expected["sid"],
            seq=expected["seq"],
            snapshot=after,
        )
    else:
        frame = live_frame(
            reply_event_id=expected["reply"],
            publisher_id=expected["sid"],
            seq=expected["seq"],
            delta=live_delta(_docs(case["before"]), after),
        )
    assert frame == expected


def test_splice_counts_utf16_units_and_never_splits_a_surrogate_pair() -> None:
    """Replacing one astral character with another deletes and inserts both of its code units."""
    before = LiveDocs(md="a😀b", reasoning="", tools=(), status="streaming", activity="")
    assert live_delta(before, replace(before, md="a😁b")) == {"md": {"at": 1, "del": 2, "ins": "😁", "len": 4}}
    assert live_delta(before, before) == {}


def test_publisher_ids_order_by_start_time() -> None:
    """A later publisher's id sorts after an earlier one's, which is how clients order publisher generations."""
    first = new_publisher_id()
    second = new_publisher_id()
    assert len(first.split("-")[0]) == 13
    assert first.split("-")[0] <= second.split("-")[0]
