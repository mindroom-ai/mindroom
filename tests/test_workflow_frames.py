"""The live graph frames of a workflow run: snapshots, deltas, their size and their pacing (pure level)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from mindroom.live_frames import subagents as subagents_module
from mindroom.live_frames import workflow as frames_module
from mindroom.live_frames.workflow import publish_run
from mindroom.workflow_runs.graph import graph_meta, graph_rows
from mindroom.workflow_runs.models import WorkflowNodeState, WorkflowRunState
from tests.subagent_activity_helpers import FRAME_ACCOUNT, FRAME_HOMESERVER, FRAME_ROOM_ID, FrameRoom, make_snapshot
from tests.test_workflow_graph import _RUN, _USAGE, _node, _nodes

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from mindroom.workflow_runs.models import WorkflowNode

_MAX_FRAME_BYTES = 60_000


@pytest_asyncio.fixture(autouse=True)
async def relaying(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Relay frames, send them 10 ms apart, and end any publisher a test left running."""

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
    subagents_module._PUBLISHERS.clear()


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.001)


def _publish(room: FrameRoom, nodes: list[WorkflowNode], *, log: tuple[str, ...] = (), **run: Any) -> None:  # noqa: ANN401
    """Queue the run's view as its next frame, as the run's view does after each change."""
    changed = (
        _RUN if not run else type(_RUN)(**{**{name: getattr(_RUN, name) for name in _RUN.__dataclass_fields__}, **run})
    )
    meta = graph_meta(changed, nodes, _USAGE, large=False)
    graph = graph_rows(changed, nodes, {}, [], log)
    publish_run(
        FRAME_ACCOUNT,
        FRAME_ROOM_ID,
        _RUN.run_id,
        meta,
        graph,
        client=lambda: room.client,
        homeserver_url=FRAME_HOMESERVER,
    )


@pytest.mark.asyncio
async def test_frame_has_exactly_the_pinned_shape() -> None:
    """A frame names its run and stream, counts up, and carries the run's meta and a snapshot or a delta."""
    room = FrameRoom()
    nodes = [_node(1, state=WorkflowNodeState.RUNNING)]
    _publish(room, nodes)
    await _until(lambda: len(room.sent) == 1)

    frame = room.sent[0]
    assert room.sent_types == ["io.mindroom.workflow"]
    assert list(frame) == ["v", "run", "sid", "seq", "meta", "snapshot"]
    assert (frame["v"], frame["run"], frame["seq"]) == (1, _RUN.run_id, 0)
    assert frame["meta"] == graph_meta(_RUN, nodes, _USAGE, large=False)
    assert list(frame["snapshot"]) == ["phases", "groups", "nodes", "edges", "omitted", "log"]
    assert [node["id"] for node in frame["snapshot"]["nodes"]] == ["n1"]


@pytest.mark.asyncio
async def test_first_frame_is_a_snapshot_then_deltas_then_a_snapshot_every_ten_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream starts with a snapshot, sends what changed after it, and a whole snapshot again when one is due."""
    monkeypatch.setattr(frames_module, "_SNAPSHOT_INTERVAL_SECONDS", 0.4)
    room = FrameRoom()
    one, two = _node(1, state=WorkflowNodeState.RUNNING), _node(2, state=WorkflowNodeState.RUNNING)
    _publish(room, [one])
    await _until(lambda: len(room.sent) == 1)
    _publish(room, [one, two], log=("started two",))
    await _until(lambda: len(room.sent) == 2)

    first, second = room.sent
    assert "snapshot" in first
    assert second["delta"] == {"nodes": [second["delta"]["nodes"][0]], "log_append": ["started two"]}
    assert second["delta"]["nodes"][0]["id"] == "n2"
    assert (second["sid"], second["seq"]) == (first["sid"], 1)

    # Nothing changed: no frame, until a snapshot is due again, which nothing has to ask for.
    _publish(room, [one, two], log=("started two",))
    await asyncio.sleep(0.05)
    assert len(room.sent) == 2
    await _until(lambda: len(room.sent) == 3)
    assert "snapshot" in room.sent[2]
    assert [node["id"] for node in room.sent[2]["snapshot"]["nodes"]] == ["n1", "n2"]


@pytest.mark.asyncio
async def test_change_of_the_meta_alone_is_a_delta_with_no_rows() -> None:
    """A run whose numbers changed and whose rows did not sends its meta with an empty delta."""
    room = FrameRoom()
    nodes = [_node(1, state=WorkflowNodeState.RUNNING)]
    _publish(room, nodes)
    await _until(lambda: len(room.sent) == 1)
    _publish(room, nodes, max_concurrent=4)
    await _until(lambda: len(room.sent) == 2)

    assert room.sent[1]["delta"] == {}
    assert room.sent[1]["meta"]["max_concurrent"] == 4


@pytest.mark.asyncio
async def test_removed_node_sends_a_snapshot_not_a_delta() -> None:
    """A row that left the run's view cannot be told by a delta, so the next frame is a snapshot."""
    room = FrameRoom()
    one, two = _node(1, state=WorkflowNodeState.RUNNING), _node(2, state=WorkflowNodeState.RUNNING)
    _publish(room, [one, two])
    await _until(lambda: len(room.sent) == 1)
    _publish(room, [one])
    await _until(lambda: len(room.sent) == 2)

    assert [node["id"] for node in room.sent[1]["snapshot"]["nodes"]] == ["n1"]


@pytest.mark.asyncio
async def test_every_frame_passes_the_transport_check_in_an_encrypted_room_with_1000_queued_nodes() -> None:
    """Snapshots and deltas of a large run stay within what the homeserver takes, as they go out encrypted."""
    room = FrameRoom(encrypted=True)
    nodes = _nodes(queued=1000, label="検査 " * 16)
    _publish(room, nodes)
    await _until(lambda: len(room.sent) == 1)
    # Forty calls no frame showed start: they push rows out, so the next frame is a snapshot.
    started = [_node(node.seq, state=WorkflowNodeState.RUNNING, label=node.label) for node in nodes[-40:]]
    _publish(room, [*nodes[:-40], *started])
    await _until(lambda: len(room.sent) == 2)
    _publish(room, [*nodes[:-40], *started], state=WorkflowRunState.COMPLETED)
    await _until(lambda: len(room.sent) == 3)

    assert all(size <= _MAX_FRAME_BYTES for size in room.sent_bytes)
    first, second, last = room.sent
    assert first["meta"]["counts"]["queued"] == 1000
    assert len(first["snapshot"]["nodes"]) + first["snapshot"]["omitted"]["queued"] == 1000
    assert [node["status"] for node in second["snapshot"]["nodes"][:40]] == ["running"] * 40
    assert second["snapshot"]["omitted"]["queued"] + len(second["snapshot"]["nodes"]) == 1000
    assert last["meta"]["status"] == "completed"
    assert "snapshot" in last


@pytest.mark.asyncio
async def test_run_that_ended_sends_one_last_snapshot_and_then_nothing() -> None:
    """The end of a run is a snapshot, after which its stream is gone: no keepalive follows."""
    room = FrameRoom()
    nodes = [_node(1, state=WorkflowNodeState.COMPLETED)]
    _publish(room, [_node(1, state=WorkflowNodeState.RUNNING)])
    await _until(lambda: len(room.sent) == 1)
    _publish(room, nodes, state=WorkflowRunState.COMPLETED)
    await _until(lambda: len(room.sent) == 2)
    await _until(lambda: subagents_module._PUBLISHERS == {})

    assert room.sent[1]["meta"]["status"] == "completed"
    assert "snapshot" in room.sent[1]


@pytest.mark.asyncio
async def test_frames_of_a_run_and_of_a_subagent_share_the_pace_of_their_account_and_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One account's frames in one room go out one after another, whatever they show."""
    monkeypatch.setattr(subagents_module, "_SEND_INTERVAL_SECONDS", 0.05)
    room = FrameRoom()
    room.publish(make_snapshot())
    _publish(room, [_node(1, state=WorkflowNodeState.RUNNING)])
    await _until(lambda: len(room.sent) == 2)

    assert sorted(room.sent_types) == ["io.mindroom.subagents", "io.mindroom.workflow"]
    assert len(subagents_module._PUBLISHERS) == 1
    assert room.sent_at[1] - room.sent_at[0] >= 0.045
