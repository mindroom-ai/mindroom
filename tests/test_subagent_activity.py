"""The `subagent:changed` hook event: its names, snapshot types, plugin API version, and `plugin_testing` support."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from mindroom.config.main import Config
from mindroom.delegation import activity as activity_module
from mindroom.delegation.activity import (
    _OPEN,
    FLUSH_INTERVAL_SECONDS,
    TERMINAL_STATUSES,
    ActivityFeed,
    SubagentActivity,
)
from mindroom.delegation.state import DelegationChild, SubagentPersona
from mindroom.hooks import HookBindings, HookRegistry, emit, hook
from mindroom.hooks.types import _DEFAULT_EVENT_TIMEOUT_MS, BUILTIN_EVENT_NAMES, validate_event_name
from mindroom.llm.events import ReportedCall, ToolCompleted, ToolStarted
from mindroom.plugin_api import (
    EVENT_SUBAGENT_CHANGED,
    PLUGIN_API_VERSION,
    SubagentChangedContext,
    SubagentItem,
    SubagentSnapshot,
)
from mindroom.plugin_testing import load_plugin
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, serialize_tool_execution_identity
from tests.conftest import bind_runtime_paths, test_runtime_paths
from tests.subagent_activity_helpers import make_snapshot
from tests.test_tool_hooks import _plugin

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.hooks import HookContext

ROOM = "!room:example.org"
ALICE = "@alice:example.org"
CONFIG = {"agents": {"helper": {"display_name": "Helper", "access": {"users": [ALICE]}}}}


def test_subagent_changed_is_a_builtin_observer_with_a_500_ms_timeout() -> None:
    """The event is built in, observes with a 500 ms timeout, and its namespace is reserved."""
    assert EVENT_SUBAGENT_CHANGED == "subagent:changed"
    assert EVENT_SUBAGENT_CHANGED in BUILTIN_EVENT_NAMES
    assert _DEFAULT_EVENT_TIMEOUT_MS[EVENT_SUBAGENT_CHANGED] == 500
    with pytest.raises(ValueError, match="reserved"):
        validate_event_name("subagent:custom")


def test_plugin_api_version_is_1_30() -> None:
    """The plugin API version names the release that added the event."""
    assert PLUGIN_API_VERSION == (1, 30)


def test_snapshot_items_are_frozen() -> None:
    """Snapshots and their items are immutable values."""
    snapshot = make_snapshot(items=(SubagentItem(kind="text", text="hi"),))
    with pytest.raises(AttributeError):
        snapshot.status = "failed"  # type: ignore[misc]
    assert snapshot.items[0].tool is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_plugin_testing_fires_subagent_changed(tmp_path: Path) -> None:
    """`fire` runs a plugin's `subagent:changed` hooks with the snapshot and returns the context."""
    plugin_dir = tmp_path / "watcher"
    plugin_dir.mkdir()
    (plugin_dir / "mindroom.plugin.json").write_text(
        '{"name": "watcher", "hooks_module": "hooks.py"}',
        encoding="utf-8",
    )
    (plugin_dir / "hooks.py").write_text(
        "from pathlib import Path\n"
        "from mindroom.plugin_api import hook\n"
        "@hook('subagent:changed')\n"
        "async def seen(ctx):\n"
        "    with Path(__file__).with_name('seen.txt').open('a') as f:\n"
        "        f.write(ctx.subagent.delegation_id + '\\n')\n",
        encoding="utf-8",
    )
    with load_plugin(plugin_dir, tmp_path / "runtime", config=CONFIG) as plugin:
        plugin.add_room(ROOM, members={ALICE: "Alice"})
        context = await plugin.fire(
            "subagent:changed",
            agent="helper",
            sender=ALICE,
            room=ROOM,
            subagent=make_snapshot(),
        )
        assert isinstance(context, SubagentChangedContext)
        assert context.event_name == EVENT_SUBAGENT_CHANGED
        assert context.subagent.delegation_id == "d1"
        assert (plugin_dir / "seen.txt").read_text(encoding="utf-8").split() == ["d1"]


def _child(
    *,
    delegation_id: str = "d1",
    task: str = "research",
    persona: SubagentPersona | None = None,
) -> DelegationChild:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="child",
        requester_id=ALICE,
        room_id=ROOM,
        thread_id="$raw",
        resolved_thread_id="$thread",
        session_id="child-session",
    )
    return DelegationChild(
        delegation_id=delegation_id,
        parent_tool_call_id="call",
        caller_agent_name="parent",
        child_agent_name="child",
        task=task,
        session_id="child-session",
        run_id="run",
        model_name="fast",
        requested_model_name="fast",
        depth=1,
        execution_identity=serialize_tool_execution_identity(identity),
        subagent_id="s1",
        agent_mode="standard",
        persona=persona,
    )


def _activity(*, task: str = "research", persona: SubagentPersona | None = None) -> SubagentActivity:
    return SubagentActivity.start(_child(task=task, persona=persona), parent_delegation_id="p0", now=1.0)


def _call(call_id: str, *, result: str | None = None, is_error: bool | None = None) -> ReportedCall:
    return ReportedCall(
        tool_call_id=call_id,
        tool_name="read_file",
        tool_args={"path": "a.txt"},
        result=result,
        is_error=is_error,
    )


def test_start_reads_the_child_identity() -> None:
    """The snapshot carries the room, resolved thread, requester, model, and parent link."""
    snapshot = _activity().snapshot()
    assert (snapshot.room_id, snapshot.thread_id, snapshot.requester_id) == (ROOM, "$thread", ALICE)
    assert (snapshot.model_name, snapshot.parent_delegation_id, snapshot.source) == ("fast", "p0", "delegate")
    assert (snapshot.status, snapshot.started_at, snapshot.ended_at) == ("running", 1.0, None)


def test_text_deltas_merge_into_one_item() -> None:
    """Consecutive text becomes one item."""
    a = _activity()
    a.add_text("Hel", 2)
    a.add_text("lo", 3)
    items = a.snapshot().items
    assert [(i.kind, i.text) for i in items] == [("text", "Hello")]
    assert a.snapshot().updated_at == 3


def test_a_secret_split_across_deltas_is_redacted() -> None:
    """Redaction sees the whole item, so a secret split across deltas is caught."""
    a = _activity()
    a.add_text("see https://x.sharepoint.com/a/download.aspx?UniqueId=1&temp", 1)
    a.add_text("auth=v1.secret done", 2)
    text = a.snapshot().items[0].text
    assert "v1.secret" not in text
    assert "***redacted***" in text


def test_tool_rows_pair_by_call_id_and_carry_no_full_call() -> None:
    """A completion fills the row its call id started."""
    a = _activity()
    a.tool_started(ToolStarted(call=_call("c1")), 2)
    assert a.snapshot().items[0].state == "running"
    a.tool_completed(ToolCompleted(call=_call("c1", result="hello")), 3)
    (item,) = a.snapshot().items
    assert item.state == "done"
    assert item.tool_call_id == "c1"
    assert item.tool is not None
    assert item.tool.result_preview
    assert item.tool.call is None


def test_a_completion_without_a_start_appends_a_done_row() -> None:
    """An unmatched completion still shows up."""
    a = _activity()
    a.tool_completed(ToolCompleted(call=_call("c9", result="x")), 2)
    assert [(i.tool_call_id, i.state) for i in a.snapshot().items] == [("c9", "done")]


def test_failed_tool_call_is_an_error_row() -> None:
    """A failed call ends in state error."""
    a = _activity()
    a.tool_started(ToolStarted(call=_call("c1")), 2)
    a.tool_completed(ToolCompleted(call=_call("c1", result="boom", is_error=True)), 3)
    assert a.snapshot().items[0].state == "error"


def test_text_keeps_the_newest_16000_characters() -> None:
    """The oldest text loses its head first; the order of items stays."""
    a = _activity()
    a.add_text("a" * 10_000, 1)
    a.tool_started(ToolStarted(call=_call("c1")), 2)
    a.add_text("b" * 10_000, 3)
    items = a.snapshot().items
    texts = [i.text for i in items if i.kind == "text"]
    assert sum(map(len, texts)) == 16_000
    assert texts == ["a" * 6_000, "b" * 10_000]
    assert a.snapshot().items_dropped == 0


def test_an_emptied_text_item_is_removed_and_counted() -> None:
    """Text trimmed to nothing disappears and counts as dropped."""
    a = _activity()
    a.add_text("a" * 100, 1)
    a.tool_started(ToolStarted(call=_call("c1")), 2)
    a.add_text("b" * 16_000, 3)
    snapshot = a.snapshot()
    assert [i.kind for i in snapshot.items] == ["tool", "text"]
    assert snapshot.items_dropped == 1


def test_tool_items_keep_the_newest_50_and_count_the_rest() -> None:
    """The 51st tool row removes the oldest one."""
    a = _activity()
    for n in range(53):
        a.tool_started(ToolStarted(call=_call(f"c{n}")), n)
    snapshot = a.snapshot()
    assert len(snapshot.items) == 50
    assert snapshot.items[0].tool_call_id == "c3"
    assert snapshot.items_dropped == 3


def test_task_and_result_are_cut_to_2000_characters() -> None:
    """Both previews are capped."""
    a = _activity(task="t" * 3_000)
    a.set_status("completed", 5, result="r" * 3_000)
    snapshot = a.snapshot()
    assert len(snapshot.task) == 2_000
    assert snapshot.result_preview == "r" * 2_000


def test_terminal_status_sets_ended_at_and_running_clears_it() -> None:
    """Terminal statuses end the run; resuming clears the end."""
    a = _activity()
    a.set_status("failed", 4, result="nope")
    assert (a.snapshot().ended_at, a.snapshot().status) == (4, "failed")
    a.set_status("running", 6)
    assert (a.snapshot().ended_at, a.snapshot().status) == (None, "running")
    a.set_status("awaiting_approval", 7)
    assert a.snapshot().ended_at is None
    assert {"completed", "failed", "cancelled", "denied"} == TERMINAL_STATUSES


def test_workflow_children_report_source_workflow() -> None:
    """A workflow persona marks the snapshot's source."""
    persona = SubagentPersona(source_kind="workflow", source_name="w", system_prompt="p")
    assert _activity(persona=persona).snapshot().source == "workflow"


@pytest.fixture(autouse=True)
def _forget_open_runs() -> Iterator[None]:
    yield
    _OPEN.clear()


class _Hook:
    """A `subagent:changed` hook that records each snapshot it receives, holding or failing each call on request."""

    def __init__(self, *, hold: asyncio.Event | None = None, fail: bool = False) -> None:
        self.snapshots: list[SubagentSnapshot] = []
        self._arrived = asyncio.Condition()

        @hook(EVENT_SUBAGENT_CHANGED, timeout_ms=5_000)
        async def seen(ctx: SubagentChangedContext) -> None:
            async with self._arrived:
                self.snapshots.append(ctx.subagent)
                self._arrived.notify_all()
            if hold is not None:
                await hold.wait()
            if fail:
                msg = "hook failed"
                raise RuntimeError(msg)

        self.callback = seen

    async def wait_for(self, count: int) -> SubagentSnapshot:
        """Return the `count`th snapshot once it has arrived."""
        async with asyncio.timeout(2), self._arrived:
            await self._arrived.wait_for(lambda: len(self.snapshots) >= count)
        return self.snapshots[count - 1]


def _open(tmp_path: Path, seen: _Hook | None, *, delegation_id: str = "d1") -> ActivityFeed | None:
    runtime_paths = test_runtime_paths(tmp_path)
    registry = HookRegistry.empty() if seen is None else HookRegistry.from_plugins([_plugin("t", [seen.callback])])
    return ActivityFeed.open(
        _child(delegation_id=delegation_id),
        parent_delegation_id="p0",
        bindings=HookBindings(registry=registry),
        config=bind_runtime_paths(Config(), runtime_paths),
        runtime_paths=runtime_paths,
        correlation_id="corr",
    )


def _text(snapshot: SubagentSnapshot) -> str:
    return "".join(item.text for item in snapshot.items if item.kind == "text")


@pytest.mark.asyncio
async def test_no_feed_without_a_subagent_changed_hook(tmp_path: Path) -> None:
    """Without a listening hook there is nothing to publish, so no feed and no entry."""
    assert _open(tmp_path, None) is None
    assert not _OPEN


@pytest.mark.asyncio
async def test_opening_publishes_a_running_snapshot_at_once(tmp_path: Path) -> None:
    """A new child shows up as soon as it starts."""
    seen = _Hook()
    assert _open(tmp_path, seen) is not None
    snapshot = await seen.wait_for(1)
    assert (snapshot.delegation_id, snapshot.status, snapshot.parent_delegation_id) == ("d1", "running", "p0")
    assert snapshot.items == ()


@pytest.mark.asyncio
async def test_bursts_coalesce_to_one_emission_per_window(tmp_path: Path) -> None:
    """Twenty text deltas in one burst reach the hook as one snapshot, a window after the first."""
    seen = _Hook()
    feed = _open(tmp_path, seen)
    assert feed is not None
    await seen.wait_for(1)
    loop = asyncio.get_running_loop()
    started = loop.time()
    feed.text("0 ")
    await asyncio.sleep(0)
    for n in range(1, 20):
        feed.text(f"{n} ")
    snapshot = await seen.wait_for(2)
    assert loop.time() - started >= FLUSH_INTERVAL_SECONDS * 0.8
    assert _text(snapshot) == "".join(f"{n} " for n in range(20))
    await asyncio.sleep(FLUSH_INTERVAL_SECONDS * 1.2)
    assert len(seen.snapshots) == 2


@pytest.mark.asyncio
async def test_a_status_change_emits_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A status change wakes the sender instead of waiting out the window, and carries the text before it."""
    monkeypatch.setattr(activity_module, "FLUSH_INTERVAL_SECONDS", 60)
    seen = _Hook()
    feed = _open(tmp_path, seen)
    assert feed is not None
    await seen.wait_for(1)
    feed.text("asking")
    await asyncio.sleep(0)
    feed.status("awaiting_approval")
    snapshot = await seen.wait_for(2)
    assert (snapshot.status, _text(snapshot)) == ("awaiting_approval", "asking")


@pytest.mark.asyncio
async def test_one_emission_in_flight_and_latest_wins(tmp_path: Path) -> None:
    """Marks during a slow emission wait for it, then go out together as one snapshot."""
    hold = asyncio.Event()
    seen = _Hook(hold=hold)
    feed = _open(tmp_path, seen)
    assert feed is not None
    await seen.wait_for(1)
    for letter in "abcde":
        feed.text(letter)
    await asyncio.sleep(FLUSH_INTERVAL_SECONDS * 1.2)
    assert len(seen.snapshots) == 1
    hold.set()
    snapshot = await seen.wait_for(2)
    assert _text(snapshot) == "abcde"
    await asyncio.sleep(FLUSH_INTERVAL_SECONDS * 1.2)
    assert len(seen.snapshots) == 2


@pytest.mark.asyncio
async def test_a_failing_hook_does_not_raise_into_the_feed(tmp_path: Path) -> None:
    """A hook that raises on every call never reaches the child, and later changes still reach the hook."""
    seen = _Hook(fail=True)
    feed = _open(tmp_path, seen)
    assert feed is not None
    await seen.wait_for(1)
    feed.text("still here")
    feed.status("completed", result="done")
    snapshot = await seen.wait_for(2)
    assert (snapshot.status, _text(snapshot)) == ("completed", "still here")


@pytest.mark.asyncio
async def test_an_emission_error_keeps_the_sender_sending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An error outside the hooks is logged, and marks made while that emission ran still go out."""
    entered, release = asyncio.Event(), asyncio.Event()
    seen = _Hook()

    async def flaky_emit(registry: HookRegistry, event_name: str, context: HookContext) -> None:
        if not entered.is_set():
            entered.set()
            await release.wait()
            msg = "emit failed"
            raise RuntimeError(msg)
        await emit(registry, event_name, context)

    monkeypatch.setattr(activity_module, "emit", flaky_emit)
    feed = _open(tmp_path, seen)
    assert feed is not None
    await asyncio.wait_for(entered.wait(), 2)
    feed.text("after")
    release.set()
    snapshot = await seen.wait_for(1)
    assert _text(snapshot) == "after"


@pytest.mark.asyncio
async def test_a_paused_child_reopens_with_its_transcript(tmp_path: Path) -> None:
    """Resuming the same delegation keeps its transcript and start time, and runs again."""
    seen = _Hook()
    feed = _open(tmp_path, seen)
    assert feed is not None
    feed.text("a")
    feed.status("paused")
    paused = await seen.wait_for(1)
    assert paused.status == "paused"
    assert _open(tmp_path, seen) is not None
    resumed = await seen.wait_for(2)
    assert (resumed.status, _text(resumed), resumed.ended_at) == ("running", "a", None)
    assert resumed.started_at == paused.started_at


@pytest.mark.asyncio
async def test_a_terminal_status_forgets_the_entry(tmp_path: Path) -> None:
    """A finished run is published once with its result, and the same id later starts empty."""
    seen = _Hook()
    feed = _open(tmp_path, seen)
    assert feed is not None
    feed.text("a")
    feed.status("completed", result="done")
    finished = await seen.wait_for(1)
    assert (finished.status, finished.result_preview, _text(finished)) == ("completed", "done", "a")
    assert finished.ended_at is not None
    assert not _OPEN
    assert _open(tmp_path, seen) is not None
    fresh = await seen.wait_for(2)
    assert (fresh.status, fresh.items) == ("running", ())
