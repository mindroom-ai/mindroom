"""Sub-agent activity: the `subagent:changed` hook event, the snapshot a child run keeps, and its publication."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.ai import ai_response
from mindroom.approval import manager as approval_manager
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.config.main import Config
from mindroom.custom_tools.job import JobTools
from mindroom.delegation import activity as activity_module
from mindroom.delegation.activity import _FLUSH_INTERVAL_SECONDS, _OPEN, ActivityFeed, _SubagentActivity
from mindroom.delegation.state import DelegationChild, SubagentPersona
from mindroom.event_journal import BackgroundApprovalDecision
from mindroom.hooks import HookBindings, HookRegistry, emit, hook
from mindroom.hooks.types import _DEFAULT_EVENT_TIMEOUT_MS, BUILTIN_EVENT_NAMES, validate_event_name
from mindroom.llm.events import ReportedCall, TextDelta, ToolCallOrigin, ToolCompleted, ToolStarted
from mindroom.plugin_api import (
    EVENT_SUBAGENT_CHANGED,
    PLUGIN_API_VERSION,
    SubagentChangedContext,
    SubagentItem,
    SubagentSnapshot,
)
from mindroom.plugin_testing import load_plugin
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import register_background_runtime
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, serialize_tool_execution_identity
from tests.conftest import bind_runtime_paths, make_turn_context, test_runtime_paths
from tests.delegation_helpers import ScriptedAgents, _delegate_runtime_context, _runtime_paths, native_delegation_config
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids
from tests.subagent_activity_helpers import make_snapshot
from tests.test_background_delegation import _identity as _background_identity
from tests.test_background_delegation import _registered_reports
from tests.test_delegation_direct_audit import _identity
from tests.test_tool_hooks import _plugin
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    DelegatingLeader,
    JobApprovalCards,
    answer_of,
    delegating_config,
    tool_job_runtime,
    wait_for_status,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Sequence
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.hooks import HookContext
    from mindroom.llm.runs import RunRecord
    from mindroom.plugin_api import SubagentStatus
    from mindroom.response_turn import ResponseTurnContext
    from mindroom.tool_jobs.runtime import ToolJobRuntime
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from tests.fakes.scripted_model import ScriptStep

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


def _activity(*, task: str = "research", persona: SubagentPersona | None = None) -> _SubagentActivity:
    return _SubagentActivity.start(
        _child(task=task, persona=persona),
        parent_delegation_id="p0",
        root_caller_agent_name="lead",
        now=1.0,
    )


def _call(
    call_id: str,
    *,
    args: dict[str, object] | None = None,
    result: str | None = None,
    is_error: bool | None = None,
) -> ReportedCall:
    return ReportedCall(
        tool_call_id=call_id,
        tool_name="read_file",
        tool_args={"path": "a.txt"} if args is None else args,
        result=result,
        is_error=is_error,
    )


def test_start_reads_the_child_identity() -> None:
    """The snapshot carries the room, resolved thread, requester, model, callers, and parent link."""
    snapshot = _activity().snapshot()
    assert (snapshot.caller_agent_name, snapshot.root_caller_agent_name) == ("parent", "lead")
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


def test_a_secret_stays_redacted_while_the_text_cap_cuts_through_it() -> None:
    """The cap cuts the redacted text, so a secret whose head the cut passes never shows in any snapshot."""
    a = _activity()
    link = "see https://x.sharepoint.com/a/download.aspx?UniqueId=1&tempauth=v1.secret done\n"
    a.add_text(link, 1)
    a.add_text("x" * (16_000 - len(link) - 100), 2)
    snapshots = []
    for step in range((4_200 + len(link)) // 7 + 1):
        a.add_text("x" * 7, 3 + step)
        snapshots.append(a.snapshot())
    assert not [snapshot for snapshot in snapshots if "v1.secret" in _text(snapshot)]
    assert _text(snapshots[-1]) == "x" * 16_000


def _checked_entry(item: SubagentItem) -> ToolTraceEntry:
    """Return the item's tool entry after checking it carries only the public trace fields."""
    entry = item.tool
    assert entry is not None
    assert entry.call is None
    assert (entry.tool_call_id, entry.member_id, entry.parent_bash_call_id, entry.toolkit_name) == (None,) * 4
    assert entry.is_error is False
    return entry


def test_tool_rows_pair_by_call_id_and_carry_only_redacted_public_fields() -> None:
    """A completion fills the row its call id started; neither row carries the call or internal fields."""
    a = _activity()
    origin = ToolCallOrigin(parent_bash_call_id="bash-1", toolkit_name="files")
    keyed = "api_key=example-secret"
    a.tool_started(ToolStarted(call=_call("c1", args={"query": keyed}), origin=origin), 2)
    (running,) = a.snapshot().items
    assert running.state == "running"
    assert "example-secret" not in (_checked_entry(running).args_preview or "")
    a.tool_completed(ToolCompleted(call=_call("c1", args={"query": keyed}, result=keyed), origin=origin), 3)
    (item,) = a.snapshot().items
    assert item.state == "done"
    assert item.tool_call_id == "c1"
    entry = _checked_entry(item)
    assert entry.result_preview
    assert "example-secret" not in entry.result_preview
    assert "example-secret" not in (entry.args_preview or "")


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


def test_the_task_is_redacted_once_from_a_capped_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """Starting redacts the task's head, capped with a margin past the preview, and snapshots reuse that preview."""
    redacted: list[str] = []

    def recording(text: str) -> str:
        redacted.append(text)
        return redact_sensitive_text(text)

    monkeypatch.setattr(activity_module, "redact_sensitive_text", recording)
    a = _activity(task="t" * 50_000)
    a.snapshot()
    a.snapshot()
    assert [len(text) for text in redacted] == [6_000]
    assert a.snapshot().task == "t" * 2_000


def test_terminal_status_sets_ended_at_and_running_clears_it() -> None:
    """Terminal statuses end the run; resuming clears the end."""
    a = _activity()
    a.set_status("failed", 4, result="nope")
    assert (a.snapshot().ended_at, a.snapshot().status) == (4, "failed")
    a.set_status("running", 6)
    assert (a.snapshot().ended_at, a.snapshot().status) == (None, "running")
    a.set_status("awaiting_approval", 7)
    assert a.snapshot().ended_at is None
    for terminal in ("completed", "cancelled", "denied"):
        a.set_status(terminal, 8)
        assert a.snapshot().ended_at == 8


def test_workflow_children_report_source_workflow() -> None:
    """A workflow persona marks the snapshot's source."""
    persona = SubagentPersona(source_kind="workflow", source_name="w", system_prompt="p")
    assert _activity(persona=persona).snapshot().source == "workflow"


@pytest.fixture(autouse=True)
def _forget_open_runs() -> Iterator[None]:
    yield
    _OPEN.clear()


class _Hook:
    """A `subagent:changed` hook that records each snapshot it receives, holding or failing each call on request.

    ``agents`` and ``rooms`` scope the hook as a plugin's decorator would.
    """

    def __init__(
        self,
        *,
        hold: asyncio.Event | None = None,
        fail: bool = False,
        timeout_ms: int = 5_000,
        agents: list[str] | None = None,
        rooms: list[str] | None = None,
    ) -> None:
        self.snapshots: list[SubagentSnapshot] = []
        self._arrived = asyncio.Condition()

        @hook(EVENT_SUBAGENT_CHANGED, timeout_ms=timeout_ms, agents=agents, rooms=rooms)
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

    async def wait_for_status(self, status: SubagentStatus, *, agent_name: str | None = None) -> SubagentSnapshot:
        """Return the first snapshot with ``status``, of ``agent_name``'s run when given, once it has arrived."""

        def matches(snapshot: SubagentSnapshot) -> bool:
            return snapshot.status == status and agent_name in (None, snapshot.agent_name)

        async with asyncio.timeout(2), self._arrived:
            await self._arrived.wait_for(lambda: any(map(matches, self.snapshots)))
        return next(filter(matches, self.snapshots))


def _open(tmp_path: Path, seen: _Hook | None, *, delegation_id: str = "d1") -> ActivityFeed | None:
    runtime_paths = test_runtime_paths(tmp_path)
    registry = HookRegistry.empty() if seen is None else HookRegistry.from_plugins([_plugin("t", [seen.callback])])
    return ActivityFeed.open(
        _child(delegation_id=delegation_id),
        parent_delegation_id="p0",
        root_caller_agent_name="lead",
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
    assert loop.time() - started >= _FLUSH_INTERVAL_SECONDS * 0.8
    assert _text(snapshot) == "".join(f"{n} " for n in range(20))
    await asyncio.sleep(_FLUSH_INTERVAL_SECONDS * 1.2)
    assert len(seen.snapshots) == 2


@pytest.mark.asyncio
async def test_a_status_change_emits_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A status change wakes the sender instead of waiting out the window, and carries the text before it."""
    monkeypatch.setattr(activity_module, "_FLUSH_INTERVAL_SECONDS", 60)
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
    await asyncio.sleep(_FLUSH_INTERVAL_SECONDS * 1.2)
    assert len(seen.snapshots) == 1
    hold.set()
    snapshot = await seen.wait_for(2)
    assert _text(snapshot) == "abcde"
    await asyncio.sleep(_FLUSH_INTERVAL_SECONDS * 1.2)
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


def _changed(tmp_path: Path, snapshot: SubagentSnapshot) -> SubagentChangedContext:
    runtime_paths = test_runtime_paths(tmp_path)
    return SubagentChangedContext(
        **HookBindings().context_kwargs(
            EVENT_SUBAGENT_CHANGED,
            config=bind_runtime_paths(Config(), runtime_paths),
            runtime_paths=runtime_paths,
            correlation_id="corr",
        ),
        subagent=snapshot,
    )


@pytest.mark.asyncio
async def test_a_room_scoped_hook_sees_only_the_runs_in_its_rooms(tmp_path: Path) -> None:
    """`rooms=` scopes a hook by the room the sub-agent runs in."""
    seen = _Hook(rooms=[ROOM])
    registry = HookRegistry.from_plugins([_plugin("t", [seen.callback])])
    for room_id in (ROOM, "!elsewhere:example.org"):
        await emit(registry, EVENT_SUBAGENT_CHANGED, _changed(tmp_path, make_snapshot(room_id=room_id)))
    assert [snapshot.room_id for snapshot in seen.snapshots] == [ROOM]


@pytest.mark.asyncio
async def test_an_agent_scoped_hook_sees_the_runs_of_chains_its_agents_started(tmp_path: Path) -> None:
    """`agents=` scopes a hook by the agent in the room whose turn started the chain, also for a nested sub-agent."""
    seen = _Hook(agents=["helper"])
    registry = HookRegistry.from_plugins([_plugin("t", [seen.callback])])
    for root, caller in (("helper", "helper"), ("helper", "researcher"), ("researcher", "researcher")):
        snapshot = make_snapshot(caller_agent_name=caller, root_caller_agent_name=root, agent_name="researcher")
        await emit(registry, EVENT_SUBAGENT_CHANGED, _changed(tmp_path, snapshot))
    assert [(snapshot.root_caller_agent_name, snapshot.caller_agent_name) for snapshot in seen.snapshots] == [
        ("helper", "helper"),
        ("helper", "researcher"),
    ]


@pytest.mark.asyncio
async def test_no_feed_when_every_hook_is_scoped_to_other_runs(tmp_path: Path) -> None:
    """Hooks whose rooms or agents never include this child keep no entry for it; a matching scope does.

    The agent a hook matches is the chain's root caller, not the child's own caller.
    """
    assert _open(tmp_path, _Hook(rooms=["!elsewhere:example.org"])) is None
    assert _open(tmp_path, _Hook(agents=["someone_else"])) is None
    assert not _OPEN
    assert _open(tmp_path, _Hook(agents=["parent"])) is None
    seen = _Hook(rooms=[ROOM], agents=["lead"])
    assert _open(tmp_path, seen) is not None
    assert (await seen.wait_for(1)).status == "running"


def _delegating_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: _Hook,
    child_turns: list[Sequence[ScriptStep]],
    *,
    grandchild_turns: list[Sequence[ScriptStep]] | None = None,
) -> tuple[ResponseTurnContext, ToolRuntimeContext, RuntimePaths, Config]:
    """Return a `leader` turn that delegates one task to `child`, whose model plays ``child_turns``.

    With ``grandchild_turns``, `child` may delegate to `grandchild`, whose model plays them.
    """
    agents = {
        "leader": AgentConfig(display_name="Leader", delegate_to=["child"]),
        "child": AgentConfig(display_name="Child", tools=["calculator"]),
    }
    models = {"child": ScriptedModel(child_turns)}
    if grandchild_turns is not None:
        agents["child"].delegate_to = ["grandchild"]
        agents["grandchild"] = AgentConfig(display_name="Grandchild")
        models["grandchild"] = ScriptedModel(grandchild_turns)
    config = native_delegation_config(agents=agents)
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    identity = _identity()
    parent = ScriptedModel(
        [calls(tool_call("run_subagent", "delegate", agent_name="child", task="Add one and two")), answer("Done")],
    )
    ScriptedAgents(models, by_session={identity.session_id: parent}).install(monkeypatch)
    registry = HookRegistry.from_plugins([_plugin("watch", [seen.callback])])
    runtime = _delegate_runtime_context(config, paths, execution_identity=identity, hook_registry=registry)
    turn = replace(
        make_turn_context(
            "leader",
            session_id=identity.session_id,
            run_id="leader-run",
            correlation_id="corr-leader",
            room_id=identity.room_id,
            thread_id=identity.thread_id,
            requester_id=identity.requester_id,
        ),
        prompt="Delegate",
        execution_identity=runtime.execution_identity,
        supports_native_tool_approval=True,
    )
    return turn, runtime, paths, config


@pytest.mark.asyncio
async def test_a_delegated_child_reports_running_text_tools_then_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child's run reaches the hook as it starts, with its tool and words, and finally with its result."""
    seen = _Hook()
    child_turns = [calls(tool_call("add", "add", a=1, b=2)), answer("Three")]
    turn, runtime, paths, config = _delegating_turn(tmp_path, monkeypatch, seen, child_turns)

    with tool_runtime_context(runtime):
        result = await ai_response(turn, paths, config)

    assert result.text.endswith("Done")
    last = await seen.wait_for_status("completed")
    assert seen.snapshots[0].status == "running"
    assert last is seen.snapshots[-1]
    assert last.result_preview == "Three"
    assert [item.kind for item in last.items] == ["tool", "text"]
    assert last.items[0].tool is not None
    assert (last.items[0].tool.tool_name, last.items[0].state) == ("add", "done")
    assert _text(last) == "Three"
    assert (last.caller_agent_name, last.agent_name) == ("leader", "child")
    assert last.room_id == _identity().room_id
    assert last.parent_delegation_id is None
    assert not _OPEN


@pytest.mark.asyncio
async def test_a_nested_child_names_its_parent_and_reaches_hooks_scoped_to_the_leader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child's own sub-agent links the child's run and names it caller, and the leader in the room as root caller."""
    seen = _Hook(agents=["leader"])
    child_turns = [
        calls(tool_call("run_subagent", "nested", agent_name="grandchild", task="Go deeper")),
        answer("Child done"),
    ]
    turn, runtime, paths, config = _delegating_turn(
        tmp_path,
        monkeypatch,
        seen,
        child_turns,
        grandchild_turns=[answer("Grandchild done")],
    )

    with tool_runtime_context(runtime):
        result = await ai_response(turn, paths, config)

    assert result.text.endswith("Done")
    child = await seen.wait_for_status("completed", agent_name="child")
    grandchild = await seen.wait_for_status("completed", agent_name="grandchild")
    assert (child.parent_delegation_id, child.caller_agent_name, child.root_caller_agent_name) == (
        None,
        "leader",
        "leader",
    )
    assert grandchild.parent_delegation_id == child.delegation_id
    assert (grandchild.caller_agent_name, grandchild.root_caller_agent_name) == ("child", "leader")
    assert grandchild.result_preview == "Grandchild done"
    assert not _OPEN


@pytest.mark.asyncio
async def test_a_child_stays_on_time_while_a_hook_is_slow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hook that blocks every call never holds the child or its caller up; the hook still gets the end."""
    hold = asyncio.Event()
    seen = _Hook(hold=hold, timeout_ms=10_000)
    child_turns = [calls(tool_call("add", "add", a=1, b=2)), answer("Three")]
    turn, runtime, paths, config = _delegating_turn(tmp_path, monkeypatch, seen, child_turns)

    with tool_runtime_context(runtime):
        result = await asyncio.wait_for(ai_response(turn, paths, config), 5)

    assert result.text.endswith("Done")
    assert [snapshot.status for snapshot in seen.snapshots] == ["running"]
    hold.set()
    assert (await seen.wait_for_status("completed")).result_preview == "Three"


@pytest.mark.asyncio
async def test_a_cancelled_child_ends_cancelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A child whose stream is cancelled is published as cancelled, with what it wrote before."""

    async def cancel() -> None:
        raise asyncio.CancelledError

    seen = _Hook()
    turn, runtime, paths, config = _delegating_turn(
        tmp_path,
        monkeypatch,
        seen,
        [[TextDelta("Working"), Await(cancel)]],
    )

    with tool_runtime_context(runtime), pytest.raises(asyncio.CancelledError):
        await ai_response(turn, paths, config)

    last = await seen.wait_for_status("cancelled")
    assert last is seen.snapshots[-1]
    assert _text(last) == "Working"
    assert last.ended_at is not None


async def _background_delegation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: _Hook,
) -> tuple[DelegatingLeader, ToolJobRuntime, JobApprovalCards, ToolRuntimeContext]:
    """Return a leader whose subagent calls run as jobs, delegating to `code`, whose `write_report` needs approval."""
    paths = _runtime_paths(tmp_path)
    config = delegating_config(
        child_tools=["reports"],
        rules=[ApprovalRuleConfig(match="write_report", action="require_approval")],
    )
    config.background_tool_jobs.approval_wait_timeout = 30
    identity = _background_identity("p")
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    cards = JobApprovalCards(asyncio.get_running_loop().create_future(), runtime.journal)
    monkeypatch.setattr(approval_manager, "get_approval_store", lambda: cards)
    agents = ScriptedAgents({"code": ScriptedModel([calls(tool_call("write_report", "w")), answer("Child done")])})
    agents.install(monkeypatch)
    registry = HookRegistry.from_plugins([_plugin("watch", [seen.callback])])
    context = _delegate_runtime_context(config, paths, execution_identity=identity, hook_registry=registry)
    return DelegatingLeader(config, paths, identity, agents), runtime, cards, context


async def _delegate_in_background(
    leader: DelegatingLeader,
    runtime: ToolJobRuntime,
    context: ToolRuntimeContext,
    while_waiting: Callable[[], Awaitable[None]],
) -> RunRecord:
    """Run the leader's one subagent call as a job, with ``while_waiting`` running beside it, then stop the runtime."""
    with _registered_reports():
        try:
            with tool_runtime_context(context):
                beside = asyncio.create_task(while_waiting())
                result = await asyncio.wait_for(
                    leader.reply(
                        calls(tool_call("run_subagent", "first", task="Report", agent_name="code")),
                        answer("Parent done"),
                    ),
                    JOB_TEST_TIMEOUT,
                )
                await beside
        finally:
            await runtime.shutdown()
    return result


@pytest.mark.asyncio
async def test_a_background_childs_approval_wait_reports_awaiting_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child its job asks approvals for shows as awaiting them, runs again once decided, then completes."""
    seen = _Hook()
    leader, runtime, cards, context = await _background_delegation(tmp_path, monkeypatch, seen)

    async def approve() -> None:
        await asyncio.wait_for(cards.posted.wait(), JOB_TEST_TIMEOUT)
        waiting = await seen.wait_for_status("awaiting_approval")
        assert waiting.items[0].tool_call_id == "w"
        cards.decision.set_result(BackgroundApprovalDecision("approved", None))

    result = await _delegate_in_background(leader, runtime, context, approve)

    assert "Child done" in str(answer_of(result, "first").content)
    last = await seen.wait_for_status("completed")
    statuses = [snapshot.status for snapshot in seen.snapshots]
    assert statuses[0] == "running"
    assert "running" in statuses[statuses.index("awaiting_approval") :]
    assert last is seen.snapshots[-1]
    assert last.result_preview == "Child done"


@pytest.mark.asyncio
async def test_a_background_child_cancelled_while_awaiting_approval_ends_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job cancelled while its child waits for an approval publishes the child as cancelled, not still waiting."""
    seen = _Hook()
    leader, runtime, cards, context = await _background_delegation(tmp_path, monkeypatch, seen)

    async def cancel() -> None:
        await asyncio.wait_for(cards.posted.wait(), JOB_TEST_TIMEOUT)
        await seen.wait_for_status("awaiting_approval")
        [job] = runtime.retained_jobs()
        await JobTools(leader.paths).job("cancel", job.job_id)
        await wait_for_status(runtime, job.job_id, "cancelled")

    await _delegate_in_background(leader, runtime, context, cancel)

    last = await seen.wait_for_status("cancelled")
    assert last is seen.snapshots[-1]
    assert last.ended_at is not None
    assert not _OPEN
