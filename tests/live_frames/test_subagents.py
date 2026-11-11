"""Sub-agent runs as `io.mindroom.subagents` frames: their documents, the pinned fixture, and the per-room budget."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import AsyncMock

import nio
import pytest
import pytest_asyncio

import mindroom.live_frames.subagents as subagents_module
from mindroom.delegation.activity import SubagentItem, SubagentSnapshot, _SubagentActivity
from mindroom.live_frames.frames import LiveDocs
from mindroom.live_frames.subagents import SUBAGENT_EVENT_TYPE, SubagentFramePublisher, publisher_for, run_docs
from mindroom.tool_system.events import ToolTraceEntry, tool_markers_match_trace
from tests.conftest import make_matrix_client_mock
from tests.subagent_activity_helpers import make_snapshot

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

_ROOM_ID = "!room:example.org"
_ACCOUNT = "@mindroom_lead:example.org"
_HOMESERVER = "http://homeserver.test"
_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "subagent-frames-v1.json").read_text(encoding="utf-8"))
# The real spacing between sends; every other test sends faster.
_SEND_INTERVAL = subagents_module._SEND_INTERVAL_SECONDS


class _Room:
    """The room a fake client sends frames to: each frame and when it was sent, answered by scripted responses."""

    def __init__(self, *responses: nio.RoomSendEphemeralResponse | nio.RoomSendEphemeralError) -> None:
        self.client = make_matrix_client_mock(user_id=_ACCOUNT)
        self.client.rooms = {_ROOM_ID: SimpleNamespace(encrypted=False)}
        self.client.room_send_ephemeral = AsyncMock(side_effect=self._send)
        self.sent: list[dict[str, Any]] = []
        self.sent_at: list[float] = []
        self.responses = list(responses)

    async def _send(
        self,
        room_id: str,
        event_type: str,
        content: dict[str, Any],
        *,
        tx_id: str,
    ) -> nio.RoomSendEphemeralResponse | nio.RoomSendEphemeralError:
        assert (room_id, event_type) == (_ROOM_ID, SUBAGENT_EVENT_TYPE)
        assert tx_id == f"subagents.{content['sid']}.{content['seq']}"
        self.sent.append(content)
        self.sent_at.append(time.monotonic())
        return self.responses.pop(0) if self.responses else nio.RoomSendEphemeralResponse(room_id)

    def publisher(self) -> SubagentFramePublisher:
        return publisher_for(
            _ACCOUNT,
            _ROOM_ID,
            client=lambda: self.client,
            homeserver_url=_HOMESERVER,
        )


def _refusal(errcode: str) -> nio.RoomSendEphemeralError:
    return nio.RoomSendEphemeralError(message="refused", status_code=errcode, room_id=_ROOM_ID)


@pytest_asyncio.fixture(autouse=True)
async def relaying(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Relay frames, send them 10 ms apart, and end any publisher a test left running."""

    async def relays(_homeserver_url: str) -> bool:
        return True

    monkeypatch.setattr(subagents_module, "homeserver_relays_live_frames", relays)
    monkeypatch.setattr(subagents_module, "_SEND_INTERVAL_SECONDS", 0.01)
    yield
    tasks = [publisher._task for publisher in subagents_module._PUBLISHERS.values() if publisher._task is not None]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert subagents_module._PUBLISHERS == {}


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.001)


def _text(text: str) -> SubagentItem:
    return SubagentItem(kind="text", text=text)


def _tool(entry: ToolTraceEntry, state: Literal["running", "done", "error"], call_id: str = "call-1") -> SubagentItem:
    return SubagentItem(kind="tool", tool=entry, tool_call_id=call_id, state=state)


def _scripted_run() -> list[SubagentSnapshot]:
    """One run as its feed hands it over: text, more text, a tool call, an approval wait, and its end."""
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
        updated_at=1762790000.1,
    )
    return [
        replace(running, items=(_text("Looking it up."),)),
        replace(running, items=(_text("Looking it up. Checking the tags."),)),
        replace(running, items=(_text("Looking it up. Checking the tags."), _tool(listing, "running"))),
        replace(
            running,
            status="awaiting_approval",
            items=(_text("Looking it up. Checking the tags."), _tool(listing, "running")),
        ),
        replace(
            running,
            status="completed",
            ended_at=1762790012.5,
            items=(
                _text("Looking it up. Checking the tags."),
                _tool(listed, "done"),
                _text("Version 2 is tagged."),
            ),
            result_preview="Version 2 shipped as tag v2.0.",
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


@pytest.mark.asyncio
async def test_frames_match_the_pinned_fixture() -> None:
    """A run's snapshots go out as exactly the frames MindRoom Chat's parser is tested against."""
    room = _Room()
    cases = _FIXTURE["cases"]
    snapshots = _scripted_run()
    for count, snapshot in enumerate(snapshots, start=1):
        room.publisher().update(snapshot, previews=True)
        await _until(lambda count=count: len(room.sent) == count)
    await _until(lambda: subagents_module._PUBLISHERS == {})

    assert len({frame["sid"] for frame in room.sent}) == 1
    sid = cases[0]["frame"]["sid"]
    assert [{**frame, "sid": sid} for frame in room.sent] == [case["frame"] for case in cases]
    assert [run_docs(snapshot, previews=True) for snapshot in snapshots] == [_docs(case["after"]) for case in cases]
    assert [case["before"] for case in cases] == [None, *(case["after"] for case in cases[:-1])]


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

    docs = run_docs(snapshot, previews=True)

    assert docs.md == "Intro\n\n🔧 `web_search` [1]\n\nMiddle\n\n🔧 `read_file` [2] ⏳\n\n"
    assert tool_markers_match_trace(docs.md, [search, read])
    assert docs.tools == (
        {"type": "tool_call_completed", "tool_name": "web_search", "args_preview": "query=a", "result_preview": "1"},
        {"type": "tool_call_started", "tool_name": "read_file", "args_preview": "path=b"},
    )
    assert docs.status == "streaming"


def test_text_left_adjacent_by_a_dropped_tool_row_stays_two_paragraphs() -> None:
    """When the tool row cap drops the call between two stretches of text, they do not run together."""
    assert run_docs(make_snapshot(items=(_text("Before."), _text("After."))), previews=True).md == "Before.\n\nAfter."


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
    assert run_docs(make_snapshot(status=status), previews=True).status == docs_status


@pytest.mark.asyncio
async def test_no_previews_when_hidden() -> None:
    """When the room's turn hides tool calls, every frame's tool rows carry only their type and tool name."""
    room = _Room()
    first = ToolTraceEntry(type="tool_call_started", tool_name="run_shell_command", args_preview="command=ls")
    done = replace(first, type="tool_call_completed", result_preview="a.txt", truncated=True)
    second = ToolTraceEntry(type="tool_call_started", tool_name="read_file", args_preview="path=a.txt")
    room.publisher().update(make_snapshot(items=(_tool(first, "running"),)), previews=False)
    await _until(lambda: len(room.sent) == 1)
    room.publisher().update(
        make_snapshot(items=(_tool(done, "done"), _tool(second, "running", "call-2"))),
        previews=False,
    )
    await _until(lambda: len(room.sent) == 2)

    assert "snapshot" in room.sent[0]
    assert "delta" in room.sent[1]
    tools = [entry for frame in room.sent for entry in _frame_tools(frame)]
    assert len(tools) == 3
    assert all(set(entry) == {"type", "tool_name"} for entry in tools)


@pytest.mark.asyncio
async def test_long_text_stays_under_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run writing 50,000 characters never sends a frame over 60,000 bytes, and each cut of its oldest text is a snapshot."""
    monkeypatch.setattr(subagents_module, "_SEND_INTERVAL_SECONDS", 0)
    room = _Room()
    activity = _SubagentActivity(
        delegation_id="del-1",
        subagent_id="sub-1",
        parent_delegation_id=None,
        caller_agent_name="lead",
        root_caller_agent_name="lead",
        agent_name="researcher",
        source="delegate",
        task="Write it all down.",
        room_id=_ROOM_ID,
        thread_id=None,
        requester_id="@user:example.org",
        model_name="default",
        started_at=1000.0,
        updated_at=1000.0,
    )
    chunks = [f"{index:03d} Größe 日本語 {'text ' * 20}"[:100] for index in range(500)]
    for chunk in chunks:
        activity.add_text(chunk, 1000.0)
        room.publisher().update(activity.snapshot(), previews=True)
        await asyncio.sleep(0)
    activity.set_status("completed", 1001.0, result="Done.")
    room.publisher().update(activity.snapshot(), previews=True)
    await _until(lambda: subagents_module._PUBLISHERS == {})

    sizes = [len(json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode()) for frame in room.sent]
    assert max(sizes) < 60_000
    # Only appends go out as deltas; once the cap cuts the oldest text, the frame is a snapshot.
    assert all(frame["delta"]["md"]["del"] == 0 for frame in room.sent if "delta" in frame)
    capped = [frame for frame in room.sent if "snapshot" in frame and len(frame["snapshot"]["md"]) == 6_000]
    assert len(capped) > 1
    assert room.sent[-1]["snapshot"]["md"] == "".join(chunks)[-6_000:]


@pytest.mark.asyncio
async def test_budget_round_robin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs that always have something new share one account's budget in turn, at least 125 ms apart."""
    monkeypatch.setattr(subagents_module, "_SEND_INTERVAL_SECONDS", _SEND_INTERVAL)
    room = _Room()
    for count in range(6):
        for run in ("a", "b", "c"):
            room.publisher().update(make_snapshot(delegation_id=run, items=(_text("x" * (count + 1)),)), previews=True)
        await _until(lambda count=count: len(room.sent) > count)

    assert _SEND_INTERVAL == 0.125
    assert [frame["run"] for frame in room.sent] == ["a", "b", "c", "a", "b", "c"]
    assert all(later - earlier >= 0.125 for earlier, later in pairwise(room.sent_at))


@pytest.mark.asyncio
async def test_snapshot_every_10_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run's frames are deltas between snapshots 10 s apart, which also tell clients a quiet run is alive."""
    clock = [1000.0]
    monkeypatch.setattr(subagents_module, "monotonic", lambda: clock[0])
    room = _Room()
    for text, now in (("a", 1000.0), ("ab", 1009.9), (None, 1010.0), ("abc", 1015.0)):
        clock[0] = now
        sent = len(room.sent)
        if text is not None:
            room.publisher().update(make_snapshot(items=(_text(text),)), previews=True)
        await _until(lambda sent=sent: len(room.sent) > sent)
    room.publisher().update(make_snapshot(status="completed", ended_at=1016.0), previews=True)
    await _until(lambda: subagents_module._PUBLISHERS == {})

    assert ["snapshot" in frame for frame in room.sent] == [True, False, True, False, True]
    assert room.sent[2]["snapshot"]["md"] == "ab"


@pytest.mark.asyncio
async def test_terminal_frame_is_a_snapshot_and_ends_the_stream() -> None:
    """A run's last frame is a snapshot with its result; then its stream ends and the idle publisher is forgotten."""
    room = _Room()
    room.publisher().update(make_snapshot(items=(_text("Working"),)), previews=True)
    await _until(lambda: len(room.sent) == 1)
    room.publisher().update(
        make_snapshot(status="failed", ended_at=1002.0, items=(_text("Working"),), result_preview="It broke."),
        previews=True,
    )
    await _until(lambda: subagents_module._PUBLISHERS == {})

    assert len(room.sent) == 2
    last = room.sent[1]
    assert last["snapshot"]["status"] == "pending"
    assert last["meta"]["status"] == "failed"
    assert last["meta"]["ended_at"] == 1002.0
    assert last["meta"]["result"] == "It broke."
    assert last["meta"]["task"] == "Find the answer"
    assert "result" not in room.sent[0]["meta"]


@pytest.mark.asyncio
async def test_retry_resends_a_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A frame the homeserver failed to take is followed by a snapshot, so clients never apply a delta to a gap."""
    monkeypatch.setattr(subagents_module, "RETRY_BACKOFF_SECONDS", 0.01)
    room = _Room(nio.RoomSendEphemeralResponse(_ROOM_ID), _refusal("M_UNKNOWN_TOKEN"))
    room.publisher().update(make_snapshot(items=(_text("a"),)), previews=True)
    await _until(lambda: len(room.sent) == 1)
    latest = make_snapshot(items=(_text("ab"),))
    room.publisher().update(latest, previews=True)
    await _until(lambda: len(room.sent) == 3)
    room.publisher().update(replace(latest, status="completed", ended_at=1001.0), previews=True)
    await _until(lambda: subagents_module._PUBLISHERS == {})

    assert [frame["seq"] for frame in room.sent] == [0, 1, 2, 3]
    assert "delta" in room.sent[1]
    assert room.sent[2]["snapshot"] == run_docs(latest, previews=True).to_json()
    assert room.sent[2]["meta"]["task"] == latest.task


@pytest.mark.asyncio
async def test_stop_drops_the_stream() -> None:
    """A refused frame ends that run's stream while the other runs go on; its next update starts a new stream."""
    ok = nio.RoomSendEphemeralResponse(_ROOM_ID)
    room = _Room(ok, ok, _refusal("M_FORBIDDEN"))
    publisher = room.publisher()
    publisher.update(make_snapshot(delegation_id="a", items=(_text("a"),)), previews=True)
    publisher.update(make_snapshot(delegation_id="b", items=(_text("b"),)), previews=True)
    await _until(lambda: len(room.sent) == 2)
    publisher.update(make_snapshot(delegation_id="a", items=(_text("a2"),)), previews=True)
    await _until(lambda: len(room.sent) == 3)
    publisher.update(make_snapshot(delegation_id="b", items=(_text("b2"),)), previews=True)
    await _until(lambda: len(room.sent) == 4)
    publisher.update(make_snapshot(delegation_id="a", items=(_text("a3"),)), previews=True)
    await _until(lambda: len(room.sent) == 5)

    first_a, _, refused, b_delta, new_a = room.sent
    assert (refused["run"], "delta" in refused) == ("a", True)
    assert (b_delta["run"], "delta" in b_delta) == ("b", True)
    assert (new_a["run"], new_a["seq"], new_a["snapshot"]["md"]) == ("a", 0, "a3")
    assert new_a["sid"] != first_a["sid"]


@pytest.mark.asyncio
async def test_unrelayed_homeserver_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a homeserver that relays frames, the publisher drops its runs and is forgotten without sending."""

    async def relays(_homeserver_url: str) -> bool:
        return False

    monkeypatch.setattr(subagents_module, "homeserver_relays_live_frames", relays)
    room = _Room()
    room.publisher().update(make_snapshot(items=(_text("a"),)), previews=True)
    await _until(lambda: subagents_module._PUBLISHERS == {})

    room.client.room_send_ephemeral.assert_not_awaited()
