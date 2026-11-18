"""Frame building pinned by the fixture MindRoom Chat's applier shares."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from mindroom.live_frames.frames import LiveDocs, fitted_tools, live_delta, live_frame, new_publisher_id

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


def test_live_delta_carries_segments_only_when_they_change() -> None:
    """Reasoning segments ride in a delta whole, and only in the delta that changes them."""
    before = LiveDocs(
        md="",
        reasoning="a",
        tools=(),
        status="streaming",
        activity="",
        segments=({"start": 0, "after_tool": 0},),
    )
    assert "segments" not in live_delta(before, replace(before, reasoning="ab"))
    after = replace(before, reasoning="a\n\nb", segments=(*before.segments, {"start": 3, "after_tool": 1}))
    assert live_delta(before, after)["segments"] == [dict(segment) for segment in after.segments]
    assert after.to_json()["segments"] == [{"start": 0, "after_tool": 0}, {"start": 3, "after_tool": 1}]


def test_publisher_ids_order_by_start_time() -> None:
    """A later publisher's id sorts after an earlier one's, which is how clients order publisher generations."""
    first = new_publisher_id()
    second = new_publisher_id()
    assert len(first.split("-")[0]) == 13
    assert first.split("-")[0] <= second.split("-")[0]


def test_live_frames_carry_compactions_in_snapshot_and_delta() -> None:
    """A snapshot carries the compaction entries; a delta carries the whole list, and only when it changed."""
    running = {"id": "c1", "phase": "mid_turn", "after_tool": 2, "status": "running"}
    before = LiveDocs(md="a", reasoning="", tools=(), status="streaming", activity="", compactions=(running,))
    assert before.to_json()["compactions"] == [running]

    assert "compactions" not in live_delta(before, replace(before, md="ab"))
    finished = {**running, "status": "success", "after_tokens": 4_000}
    after = replace(before, compactions=(finished,))
    assert live_delta(before, after) == {"compactions": [finished]}


def test_fitted_tools_cut_long_previews_and_leave_the_oldest_bare() -> None:
    """Frames cut each preview to the cap, drop the oldest entries' previews, and mark only the entries they changed."""
    short = {"type": "tool_call_completed", "tool_name": "a", "args_preview": "x", "result_preview": "ok"}
    long = {"type": "tool_call_completed", "tool_name": "b", "args_preview": "y" * 1200, "truncated": True}
    running = {"type": "tool_call_started", "tool_name": "c", "args_preview": "z" * 300, "progress": "step"}
    tools = (short, long, running)

    assert fitted_tools(tools, bare=0) == (
        short,
        {
            "type": "tool_call_completed",
            "tool_name": "b",
            "args_preview": "y" * 199 + "…",
            "truncated": True,
            "clipped": True,
        },
        {
            "type": "tool_call_started",
            "tool_name": "c",
            "args_preview": "z" * 199 + "…",
            "progress": "step",
            "clipped": True,
        },
    )
    assert fitted_tools(tools, bare=2)[:2] == (
        {"type": "tool_call_completed", "tool_name": "a", "clipped": True},
        {"type": "tool_call_completed", "tool_name": "b", "truncated": True, "clipped": True},
    )
    # An entry without previews has nothing to lose.
    bare = {"type": "tool_call_started", "tool_name": "d"}
    assert fitted_tools((bare,), bare=1) == (bare,)


def test_a_job_handle_result_keeps_its_handle_in_frames() -> None:
    """Chat reads a background job's status from the result, so a cut or bare entry keeps the handle."""
    handle = {"job_id": "j" * 64, "tool": "mindroom_dev", "status": "waiting", "summary": "s" * 300}
    entry = {"type": "tool_call_started", "tool_name": "run_subagent", "result_preview": json.dumps(handle)}
    minimal = {"job_id": "j" * 64, "tool": "mindroom_dev", "status": "waiting"}

    cut, bare = fitted_tools((entry, entry), bare=1)

    assert json.loads(cut["result_preview"]) == minimal
    assert json.loads(bare["result_preview"]) == minimal
    assert cut["clipped"] is True
    short = {**entry, "result_preview": json.dumps({**minimal, "summary": None})}
    assert fitted_tools((short,), bare=0) == (short,)
    (short_bare,) = fitted_tools((short,), bare=1)
    assert json.loads(short_bare["result_preview"]) == minimal


def test_a_bare_entry_is_never_bigger_than_its_cut_form() -> None:
    """A minimal job handle in another key order stays as it is when its entry goes bare, so frames only shrink."""
    result = json.dumps({"job_id": "j1", "status": "waiting", "tool": "mindroom_dev"})
    entry = {"type": "tool_call_completed", "tool_name": "run_subagent", "result_preview": result}
    sizes = [
        len(json.dumps(fitted_tools((entry, entry, entry), bare=bare), separators=(",", ":"))) for bare in range(4)
    ]
    assert sizes == sorted(sizes, reverse=True)
    assert fitted_tools((entry,), bare=1) == (entry,)


def test_a_bare_entry_compares_by_compact_size() -> None:
    """Frames go out as compact JSON, so a bare form that saves bytes there is taken even when spaced JSON ties."""
    result = json.dumps({"job_id": "j1", "tool": "sync", "status": "running", "label": None})
    entry = {"type": "tool_call_completed", "tool_name": "read_status", "result_preview": result}
    (bare,) = fitted_tools((entry,), bare=1)
    assert json.loads(bare["result_preview"]) == {"job_id": "j1", "tool": "sync", "status": "running"}
