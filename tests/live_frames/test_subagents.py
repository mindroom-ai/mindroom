"""Sub-agent runs as `io.mindroom.subagents` frames: their documents, the pinned fixture, and the per-room publisher."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import nio
import pytest
import pytest_asyncio

import mindroom.live_frames.subagents as subagents_module
from mindroom.delegation.activity import SubagentSnapshot, _SubagentActivity, _SubagentItem
from mindroom.live_frames.frames import LiveDocs
from mindroom.live_frames.subagents import _run_docs
from mindroom.llm.events import ReportedCall, ToolCompleted, ToolStarted
from mindroom.tool_system.events import ToolTraceEntry, tool_markers_match_trace
from tests.subagent_activity_helpers import FRAME_ROOM_ID, FrameRoom, make_snapshot

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "subagent-frames-v1.json").read_text(encoding="utf-8"))
# The real spacing between sends; every other test sends faster.
_SEND_INTERVAL = subagents_module._SEND_INTERVAL_SECONDS
_MAX_FRAME_BYTES = 60_000


def _refusal(errcode: str) -> nio.RoomSendEphemeralError:
    return nio.RoomSendEphemeralError(message="refused", status_code=errcode, room_id=FRAME_ROOM_ID)


@pytest_asyncio.fixture(autouse=True)
async def relaying(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Relay frames, send them 10 ms apart, retry after 10 ms, and end any publisher a test left running."""

    async def relays(_homeserver_url: str) -> bool:
        return True

    monkeypatch.setattr(subagents_module, "homeserver_relays_live_frames", relays)
    monkeypatch.setattr(subagents_module, "_SEND_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(subagents_module, "RETRY_BACKOFF_SECONDS", 0.01)
    yield
    tasks = [publisher._task for publisher in subagents_module._PUBLISHERS.values() if publisher._task is not None]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    # A stopped publisher forgets itself; one still registered would hold its runs forever.
    leaked = dict(subagents_module._PUBLISHERS)
    subagents_module._PUBLISHERS.clear()
    assert leaked == {}


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.001)


def _forgotten() -> bool:
    return subagents_module._PUBLISHERS == {}


def _text(text: str) -> _SubagentItem:
    return _SubagentItem(kind="text", text=text)


def _tool(entry: ToolTraceEntry, state: Literal["running", "done", "error"], call_id: str = "call-1") -> _SubagentItem:
    return _SubagentItem(kind="tool", tool=entry, tool_call_id=call_id, state=state)


def _activity(*, task: str = "Write it all down.") -> _SubagentActivity:
    return _SubagentActivity(
        delegation_id="del-1",
        parent_delegation_id=None,
        caller_agent_name="lead",
        root_caller_agent_name="lead",
        agent_name="researcher",
        source="delegate",
        task=task,
        thread_id=None,
        started_at=1000.0,
    )


def _scripted_runs() -> list[SubagentSnapshot]:
    """Two runs as their feeds hand them over.

    The first writes, writes more, calls a tool, waits for an approval, and ends; the second, nested in it, is cancelled
    before it wrote anything.
    """
    listing = ToolTraceEntry(type="tool_call_started", tool_name="run_shell_command", args_preview="command=git tag")
    listed = replace(listing, type="tool_call_completed", result_preview="v1.0\nv2.0")
    running = make_snapshot(
        delegation_id="del-1",
        caller_agent_name="lead",
        root_caller_agent_name="lead",
        agent_name="researcher",
        task="Find when version 2 shipped.",
        thread_id="$thread:example.org",
        started_at=1762790000.1,
    )
    checking = (_text("Looking it up. Checking the tags."), _tool(listing, "running"))
    return [
        replace(running, items=(_text("Looking it up."),)),
        replace(running, items=(_text("Looking it up. Checking the tags."),)),
        replace(running, items=checking),
        replace(running, status="awaiting_approval", items=checking),
        replace(
            running,
            status="completed",
            ended_at=1762790012.5,
            items=(checking[0], _tool(listed, "done"), _text("Version 2 is tagged.")),
            result_preview="Version 2 shipped as tag v2.0.",
        ),
        make_snapshot(
            delegation_id="del-2",
            parent_delegation_id="del-1",
            caller_agent_name="researcher",
            root_caller_agent_name="lead",
            agent_name="reviewer",
            task="Check the release notes.",
            thread_id="$thread:example.org",
            status="cancelled",
            started_at=1762790003.4,
            ended_at=1762790004.0,
        ),
    ]


def _docs(data: dict[str, Any]) -> LiveDocs:
    return LiveDocs(
        md=data["md"],
        reasoning=data["reasoning"],
        tools=tuple(data["tools"]),
        status=data["status"],
        activity=data["activity"],
    )


def _frame_tools(frame: dict[str, Any]) -> list[dict[str, Any]]:
    if "snapshot" in frame:
        return frame["snapshot"]["tools"]
    return [entry for _, entry in frame["delta"].get("tools", {}).get("set", [])]


def _with_sids(frames: list[dict[str, Any]], sids: list[str]) -> list[dict[str, Any]]:
    """Return the frames with each stream id replaced, in order of first appearance, by the next of ``sids``."""
    names = dict(zip(dict.fromkeys(frame["sid"] for frame in frames), sids, strict=True))
    return [{**frame, "sid": names[frame["sid"]]} for frame in frames]


@pytest.mark.asyncio
async def test_frames_match_the_pinned_fixture() -> None:
    """Two runs' snapshots go out as exactly the frames MindRoom Chat's parser is tested against."""
    room = FrameRoom()
    cases = _FIXTURE["cases"]
    snapshots = _scripted_runs()
    for count, snapshot in enumerate(snapshots, start=1):
        room.publish(snapshot)
        await _until(lambda count=count: len(room.sent) == count)
    await _until(_forgotten)

    fixture_sids = list(dict.fromkeys(case["frame"]["sid"] for case in cases))
    assert _with_sids(room.sent, fixture_sids) == [case["frame"] for case in cases]
    assert [_run_docs(snapshot, previews=True) for snapshot in snapshots] == [_docs(case["after"]) for case in cases]
    first_of_run = [
        index == 0 or case["frame"]["run"] != cases[index - 1]["frame"]["run"] for index, case in enumerate(cases)
    ]
    expected_before = [None if first else cases[index - 1]["after"] for index, first in enumerate(first_of_run)]
    assert [case["before"] for case in cases] == expected_before


def test_markers_and_tools_line_up() -> None:
    """Each tool call is a reply's marker line in the transcript, numbered by its entry in the tools list."""
    search = ToolTraceEntry(
        type="tool_call_completed",
        tool_name="web_search",
        args_preview="query=a",
        result_preview="1",
    )
    read = ToolTraceEntry(type="tool_call_started", tool_name="read_file", args_preview="path=b")
    snapshot = make_snapshot(
        items=(_text("Intro"), _tool(search, "done"), _text("Middle"), _tool(read, "running", "call-2")),
    )

    docs = _run_docs(snapshot, previews=True)

    assert docs.md == "Intro\n\n🔧 `web_search` [1]\n\nMiddle\n\n🔧 `read_file` [2] ⏳\n\n"
    assert tool_markers_match_trace(docs.md, [search, read])
    assert docs.tools == (
        {"type": "tool_call_completed", "tool_name": "web_search", "args_preview": "query=a", "result_preview": "1"},
        {"type": "tool_call_started", "tool_name": "read_file", "args_preview": "path=b"},
    )
    assert docs.status == "streaming"


def test_text_left_adjacent_by_a_dropped_tool_row_stays_two_paragraphs() -> None:
    """When the tool row cap drops the call between two stretches of text, they do not run together."""
    assert _run_docs(make_snapshot(items=(_text("Before."), _text("After."))), previews=True).md == "Before.\n\nAfter."


def test_a_text_cut_drops_the_oldest_text_and_keeps_every_tool_call() -> None:
    """Cutting text to fit a frame keeps each marker and its tools entry, so they still line up."""
    entry = ToolTraceEntry(type="tool_call_completed", tool_name="read_file")
    snapshot = make_snapshot(items=(_text("abcdef"), _tool(entry, "done"), _text("ghi")))

    docs = _run_docs(snapshot, previews=True, text_cut=8)

    assert docs.md == "\n\n🔧 `read_file` [1]\n\ni"
    assert tool_markers_match_trace(docs.md, [entry])


@pytest.mark.parametrize(
    ("status", "docs_status"),
    [
        ("running", "streaming"),
        ("awaiting_approval", "streaming"),
        ("paused", "pending"),
        ("completed", "pending"),
        ("failed", "pending"),
        ("cancelled", "pending"),
        ("denied", "pending"),
    ],
)
def test_docs_stream_only_while_the_run_works(status: str, docs_status: str) -> None:
    """The reply renderer's stream status follows the run: streaming while it runs or waits for approval."""
    assert _run_docs(make_snapshot(status=status), previews=True).status == docs_status


@pytest.mark.asyncio
async def test_no_previews_when_hidden() -> None:
    """When the room's turn hides tool calls, every frame's tool rows carry only their type and tool name."""
    room = FrameRoom()
    first = ToolTraceEntry(type="tool_call_started", tool_name="run_shell_command", args_preview="command=ls")
    done = replace(first, type="tool_call_completed", result_preview="a.txt", truncated=True)
    second = ToolTraceEntry(type="tool_call_started", tool_name="read_file", args_preview="path=a.txt")
    room.publish(make_snapshot(items=(_tool(first, "running"),)), previews=False)
    await _until(lambda: len(room.sent) == 1)
    room.publish(make_snapshot(items=(_tool(done, "done"), _tool(second, "running", "call-2"))), previews=False)
    await _until(lambda: len(room.sent) == 2)

    assert ["snapshot" in frame for frame in room.sent] == [True, False]
    tools = [entry for frame in room.sent for entry in _frame_tools(frame)]
    assert len(tools) == 3
    assert all(set(entry) == {"type", "tool_name"} for entry in tools)


@pytest.mark.asyncio
async def test_an_unchanged_snapshot_sends_nothing_and_a_status_change_sends_a_snapshot() -> None:
    """No frame carries an empty delta: nothing new sends nothing, and a change of meta alone is a snapshot."""
    room = FrameRoom()
    snapshot = make_snapshot(items=(_text("Working"),))
    room.publish(snapshot)
    await _until(lambda: len(room.sent) == 1)
    room.publish(snapshot)
    await asyncio.sleep(0.05)
    assert len(room.sent) == 1
    room.publish(replace(snapshot, status="awaiting_approval"))
    await _until(lambda: len(room.sent) == 2)

    assert (room.sent[1]["seq"], room.sent[1]["meta"]["status"]) == (1, "awaiting_approval")
    assert room.sent[1]["snapshot"] == _run_docs(snapshot, previews=True).to_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("leading_tool", [False, True], ids=["text", "tool_first"])
@pytest.mark.parametrize("encrypted", [False, True], ids=["plaintext", "encrypted"])
@pytest.mark.parametrize("previews", [True, False], ids=["previews", "hidden"])
async def test_long_text_stays_under_the_cap(
    monkeypatch: pytest.MonkeyPatch,
    leading_tool: bool,
    encrypted: bool,
    previews: bool,
) -> None:
    """A run writing 50,000 characters sends only appends between cuts, a snapshot after each cut, all under 60,000 bytes.

    Hidden previews stay hidden in every frame, deltas and the snapshots after cuts alike.
    """
    monkeypatch.setattr(subagents_module, "_SEND_INTERVAL_SECONDS", 0)
    room = FrameRoom(encrypted=encrypted)
    activity = _activity()
    if leading_tool:
        activity.tool_started(ToolStarted(call=ReportedCall("call-1", "read_file", {"path": "a.txt"})))
    texts = []
    for index in range(500):
        activity.add_text(f"{index:03d} Größe 日本語 {'text ' * 20}"[:100])
        snapshot = activity.snapshot()
        texts.append("".join(item.text for item in snapshot.items if item.kind == "text"))
        room.publish(snapshot, previews=previews)
        # The publisher sends without a pause here, so it answers within a few turns of the event loop.
        while len(room.sent) <= index:  # noqa: ASYNC110
            await asyncio.sleep(0)
    room.publish(replace(snapshot, status="completed", ended_at=1001.0), previews=previews)
    await _until(_forgotten)

    cut = [False, *(not later.startswith(earlier) for earlier, later in pairwise(texts))]
    assert sum(cut) > 40
    assert ["snapshot" in frame for frame in room.sent] == [True, *cut[1:], True]
    assert all(frame["delta"]["md"]["del"] == 0 for frame in room.sent if "delta" in frame)
    assert max(room.sent_bytes) < _MAX_FRAME_BYTES
    assert room.encrypted == []
    assert ("_preview" in json.dumps(room.sent)) is (previews and leading_tool)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("character", "encrypted"),
    [("ж", True), ("日", True), ("😀", True), ("ж", False), ("😀", False)],
    ids=["encrypted-cyrillic", "encrypted-cjk", "encrypted-emoji", "plaintext-cyrillic", "plaintext-emoji"],
)
async def test_frames_at_the_caps_fit_the_room(character: str, encrypted: bool) -> None:
    """With the text, tool rows, task, and result at their caps, every frame goes out under 60,000 bytes."""
    room = FrameRoom(encrypted=encrypted)
    activity = _activity(task=character * 1_000)
    for index in range(20):
        call = ReportedCall(f"call-{index}", "run_shell_command", {"command": character * 400}, result=character * 400)
        activity.tool_started(ToolStarted(call=call))
        activity.tool_completed(ToolCompleted(call=call))
    for index in range(12):
        activity.add_text(character * 500)
        room.publish(activity.snapshot())
        await _until(lambda index=index: len(room.sent) > index)
    activity.set_status("completed", 1001.0, result=character * 1_000)
    room.publish(activity.snapshot())
    await _until(_forgotten)

    assert max(room.sent_bytes) < _MAX_FRAME_BYTES
    # Every frame encrypted went out: none was refused for its size after encrypting.
    assert room.encrypted == []
    assert (room.sent[-1]["meta"]["status"], room.sent[-1]["meta"]["result"]) == ("completed", character * 1_000)
    snapshots = [frame["snapshot"] for frame in room.sent if "snapshot" in frame]
    assert all("args_preview" not in entry for snapshot in snapshots for entry in snapshot["tools"])


@pytest.mark.asyncio
async def test_budget_round_robin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs that always have something new share one account's budget in turn, at least 125 ms apart."""
    monkeypatch.setattr(subagents_module, "_SEND_INTERVAL_SECONDS", _SEND_INTERVAL)
    room = FrameRoom()
    for count in range(6):
        for run in ("a", "b", "c"):
            room.publish(make_snapshot(delegation_id=run, items=(_text("x" * (count + 1)),)))
        await _until(lambda count=count: len(room.sent) > count)

    assert _SEND_INTERVAL == 0.125
    assert [frame["run"] for frame in room.sent] == ["a", "b", "c", "a", "b", "c"]
    assert all(later - earlier >= 0.125 for earlier, later in pairwise(room.sent_at))


@pytest.mark.asyncio
async def test_snapshot_every_10_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A working run's frames are deltas between snapshots 10 s apart."""
    clock = [1000.0]
    monkeypatch.setattr(subagents_module, "monotonic", lambda: clock[0])
    room = FrameRoom()
    for text, now in (("a", 1000.0), ("ab", 1009.9), ("ab", 1010.0), ("abc", 1015.0)):
        clock[0] = now
        sent = len(room.sent)
        room.publish(make_snapshot(items=(_text(text),)))
        await _until(lambda sent=sent: len(room.sent) > sent)
    room.publish(make_snapshot(status="completed", ended_at=1016.0))
    await _until(_forgotten)

    assert ["snapshot" in frame for frame in room.sent] == [True, False, True, False, True]
    assert room.sent[2]["snapshot"]["md"] == "ab"


@pytest.mark.asyncio
async def test_a_working_run_keeps_alive_and_a_paused_one_goes_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """A quiet running run still sends snapshots, so clients keep it; a paused run sends its snapshot, then nothing."""
    monkeypatch.setattr(subagents_module, "_SNAPSHOT_INTERVAL_SECONDS", 0.05)
    room = FrameRoom()
    snapshot = make_snapshot(items=(_text("Working"),))
    room.publish(snapshot)
    await _until(lambda: len(room.sent) == 3)
    room.publish(replace(snapshot, status="paused"))
    await _until(lambda: len(room.sent) == 4)
    await asyncio.sleep(0.2)
    assert len(room.sent) == 4
    room.publish(snapshot)
    await _until(lambda: len(room.sent) == 5)

    assert all("snapshot" in frame for frame in room.sent[:5])
    assert [frame["meta"]["status"] for frame in room.sent[:5]] == ["running"] * 3 + ["paused", "running"]


@pytest.mark.asyncio
async def test_terminal_frame_is_a_snapshot_and_ends_the_stream() -> None:
    """A run's last frame is a snapshot with its result; the idle publisher is then forgotten, and a new run starts one."""
    room = FrameRoom()
    room.publish(make_snapshot(items=(_text("Working"),)))
    await _until(lambda: len(room.sent) == 1)
    room.publish(make_snapshot(status="failed", ended_at=1002.0, items=(_text("Working"),), result_preview="It broke."))
    await _until(_forgotten)
    room.publish(make_snapshot(delegation_id="d2", items=(_text("Next"),)))
    await _until(lambda: len(room.sent) == 3)

    first, last, next_run = room.sent
    assert "result" not in first["meta"]
    assert (last["snapshot"]["status"], last["meta"]["status"], last["meta"]["ended_at"]) == (
        "pending",
        "failed",
        1002.0,
    )
    assert (last["meta"]["task"], last["meta"]["result"]) == ("Find the answer", "It broke.")
    assert (next_run["run"], next_run["seq"], "snapshot" in next_run) == ("d2", 0, True)


@pytest.mark.asyncio
async def test_retry_resends_a_snapshot() -> None:
    """A frame the homeserver failed to take is followed by a snapshot, so clients never apply a delta to a gap."""
    room = FrameRoom(nio.RoomSendEphemeralResponse(FRAME_ROOM_ID), _refusal("M_UNKNOWN_TOKEN"))
    room.publish(make_snapshot(items=(_text("a"),)))
    await _until(lambda: len(room.sent) == 1)
    latest = make_snapshot(items=(_text("ab"),))
    room.publish(latest)
    await _until(lambda: len(room.sent) == 3)

    assert [frame["seq"] for frame in room.sent] == [0, 1, 2]
    assert "delta" in room.sent[1]
    assert room.sent[2]["snapshot"] == _run_docs(latest, previews=True).to_json()
    assert room.sent[2]["meta"]["task"] == latest.task


@pytest.mark.asyncio
async def test_a_refused_run_stays_quiet_until_it_ends() -> None:
    """A refused frame ends that run's stream while the others go on; the run sends nothing more but its end."""
    ok = nio.RoomSendEphemeralResponse(FRAME_ROOM_ID)
    room = FrameRoom(ok, ok, _refusal("M_FORBIDDEN"))
    room.publish(make_snapshot(delegation_id="a", items=(_text("a"),)))
    room.publish(make_snapshot(delegation_id="b", items=(_text("b"),)))
    await _until(lambda: len(room.sent) == 2)
    room.publish(make_snapshot(delegation_id="a", items=(_text("a2"),)))
    await _until(lambda: len(room.sent) == 3)
    room.publish(make_snapshot(delegation_id="a", items=(_text("a3"),)))
    room.publish(make_snapshot(delegation_id="b", items=(_text("b2"),)))
    await _until(lambda: len(room.sent) == 4)
    await asyncio.sleep(0.05)
    assert len(room.sent) == 4
    room.publish(make_snapshot(delegation_id="a", status="completed", ended_at=1001.0, items=(_text("a4"),)))
    await _until(lambda: len(room.sent) == 5)

    first_a, _, refused, b_delta, end_a = room.sent
    assert (refused["run"], "delta" in refused) == ("a", True)
    assert (b_delta["run"], "delta" in b_delta) == ("b", True)
    assert (end_a["run"], end_a["seq"], end_a["meta"]["status"], end_a["snapshot"]["md"]) == ("a", 0, "completed", "a4")
    assert end_a["sid"] != first_a["sid"]


@pytest.mark.asyncio
async def test_a_run_that_ends_while_its_refused_frame_is_in_flight_still_sends_its_end() -> None:
    """The end that arrives during a frame the room then refuses gets its one attempt, and the publisher is forgotten."""
    room = FrameRoom(nio.RoomSendEphemeralResponse(FRAME_ROOM_ID), _refusal("M_FORBIDDEN"))
    record = room.client.room_send_ephemeral.side_effect
    release = asyncio.Event()

    async def held(*args: object, **kwargs: object) -> nio.RoomSendEphemeralResponse | nio.RoomSendEphemeralError:
        response = await record(*args, **kwargs)
        if len(room.sent) == 2:
            await release.wait()
        return response

    room.client.room_send_ephemeral.side_effect = held
    room.publish(make_snapshot(items=(_text("a"),)))
    await _until(lambda: len(room.sent) == 1)
    room.publish(make_snapshot(items=(_text("ab"),)))
    await _until(lambda: len(room.sent) == 2)
    room.publish(make_snapshot(status="completed", ended_at=1001.0, items=(_text("abc"),), result_preview="Done."))
    release.set()
    await _until(_forgotten)

    assert [frame["meta"]["status"] for frame in room.sent] == ["running", "running", "completed"]
    assert (room.sent[2]["snapshot"]["md"], room.sent[2]["meta"]["result"]) == ("abc", "Done.")


@pytest.mark.asyncio
async def test_a_refused_run_starts_a_new_stream_when_it_resumes() -> None:
    """A resumed attempt of a refused run is a new stream, starting with a snapshot."""
    room = FrameRoom(nio.RoomSendEphemeralResponse(FRAME_ROOM_ID), _refusal("M_FORBIDDEN"))
    room.publish(make_snapshot(items=(_text("a"),)))
    await _until(lambda: len(room.sent) == 1)
    room.publish(make_snapshot(items=(_text("ab"),)))
    await _until(lambda: len(room.sent) == 2)
    room.publish(make_snapshot(items=(_text("ab2"),)))
    room.publish(make_snapshot(status="paused", items=(_text("ab2"),)))
    await asyncio.sleep(0.05)
    assert len(room.sent) == 2
    room.publish(make_snapshot(items=(_text("abc"),)))
    await _until(lambda: len(room.sent) == 3)

    assert (room.sent[2]["seq"], room.sent[2]["snapshot"]["md"]) == (0, "abc")
    assert room.sent[2]["sid"] != room.sent[0]["sid"]


@pytest.mark.asyncio
async def test_unrelayed_homeserver_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a homeserver that relays frames, the publisher drops its runs and is forgotten without sending."""

    async def relays(_homeserver_url: str) -> bool:
        return False

    monkeypatch.setattr(subagents_module, "homeserver_relays_live_frames", relays)
    room = FrameRoom()
    room.publish(make_snapshot(items=(_text("a"),)))
    await _until(_forgotten)

    room.client.room_send_ephemeral.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_homeserver_that_does_not_relay_is_asked_again_only_after_a_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a check that failed or said no, updates for the next minute send nothing and ask the homeserver nothing."""
    checks: list[str] = []
    clock = [1000.0]

    async def relays(homeserver_url: str) -> bool:
        checks.append(homeserver_url)
        return False

    monkeypatch.setattr(subagents_module, "homeserver_relays_live_frames", relays)
    monkeypatch.setattr(subagents_module, "monotonic", lambda: clock[0])
    room = FrameRoom()
    for count in range(1, 21):
        room.publish(make_snapshot(items=(_text("x" * count),)))
        await _until(_forgotten)
    assert len(checks) == 1
    clock[0] += 60.0
    room.publish(make_snapshot(items=(_text("later"),)))
    await _until(_forgotten)

    assert len(checks) == 2
    room.client.room_send_ephemeral.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_client_missing_for_one_retry_is_waited_for() -> None:
    """An account between clients, as while it signs in again, sends its frames once its client is back."""
    room = FrameRoom()
    clients = iter([None])
    room.publish(make_snapshot(items=(_text("a"),)), client=lambda: next(clients, room.client))
    await _until(lambda: len(room.sent) == 1)

    assert "snapshot" in room.sent[0]


@pytest.mark.asyncio
async def test_a_client_missing_for_longer_ends_the_publisher() -> None:
    """An account without a client past one retry, as when its bot stopped, drops its runs and is forgotten."""
    room = FrameRoom()
    room.publish(make_snapshot(items=(_text("a"),)), client=lambda: None)
    await _until(_forgotten)

    room.client.room_send_ephemeral.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_error_in_the_loop_ends_the_publisher_and_a_later_run_starts_anew() -> None:
    """A bug building a frame ends the room's frames without touching the run; the next publish starts afresh."""
    room = FrameRoom()
    room.publish(make_snapshot(items=(_SubagentItem(kind="tool"),)))
    await _until(_forgotten)
    room.publish(make_snapshot(delegation_id="d2", items=(_text("a"),)))
    await _until(lambda: len(room.sent) == 1)

    assert (room.sent[0]["run"], room.sent[0]["seq"]) == ("d2", 0)
