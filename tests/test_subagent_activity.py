"""Sub-agent activity: the snapshot a child run keeps, its caps, and its hand-off to a sink."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from mindroom import plugin_api
from mindroom.ai import TurnResult, ai_response
from mindroom.approval import manager as approval_manager
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.custom_tools.job import JobTools
from mindroom.delegation import activity as activity_module
from mindroom.delegation import lifecycle
from mindroom.delegation.activity import (
    _FLUSH_INTERVAL_SECONDS,
    _OPEN,
    ActivityFeed,
    SubagentItem,
    SubagentSnapshot,
    _SubagentActivity,
)
from mindroom.delegation.state import DelegationChild, SubagentPersona
from mindroom.event_journal import BackgroundApprovalDecision
from mindroom.hooks.types import BUILTIN_EVENT_NAMES, validate_event_name
from mindroom.llm.events import ReportedCall, TextDelta, ToolCallOrigin, ToolCompleted, ToolStarted
from mindroom.plugin_api import PLUGIN_API_VERSION
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import register_background_runtime
from mindroom.tool_system.events import ToolTraceEntry
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, serialize_tool_execution_identity
from tests.conftest import make_turn_context
from tests.delegation_helpers import ScriptedAgents, _delegate_runtime_context, _runtime_paths, native_delegation_config
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids
from tests.subagent_activity_helpers import make_snapshot
from tests.test_background_delegation import _identity as _background_identity
from tests.test_background_delegation import _registered_reports
from tests.test_delegation_direct_audit import _identity
from tests.test_delegation_direct_audit import _tools as _direct_tools
from tests.test_dynamic_workflow_subagents import _config as _workflow_config
from tests.test_dynamic_workflow_subagents import _spec, _Workflow
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

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.activity import SubagentStatus
    from mindroom.llm.runs import RunRecord
    from mindroom.response_turn import ResponseTurnContext
    from mindroom.tool_jobs.runtime import ToolJobRuntime
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from tests.fakes.scripted_model import ScriptStep

ROOM = "!room:example.org"
ALICE = "@alice:example.org"


def test_no_subagent_hook_event_remains() -> None:
    """Sub-agent activity is not a plugin hook: the event, its reserved namespace, and the snapshot types are gone."""
    removed_event = "subagent:" + "changed"  # Two parts, so a search for the removed name finds no trace of it.
    assert removed_event not in BUILTIN_EVENT_NAMES
    assert validate_event_name("subagent:x") == "subagent:x"
    assert PLUGIN_API_VERSION == (1, 29)
    assert not hasattr(plugin_api, "SubagentSnapshot")


def test_snapshot_items_are_frozen() -> None:
    """Snapshots and their items are immutable values."""
    snapshot = make_snapshot(items=(SubagentItem(kind="text", text="hi"),))
    with pytest.raises(AttributeError):
        snapshot.status = "failed"  # type: ignore[misc]
    assert snapshot.items[0].tool is None


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
    a.add_text("x" * (6_000 - len(link) - 100), 2)
    snapshots = []
    for step in range((4_200 + len(link)) // 7 + 1):
        a.add_text("x" * 7, 3 + step)
        snapshots.append(a.snapshot())
    assert not [snapshot for snapshot in snapshots if "v1.secret" in _text(snapshot)]
    assert set(_text(snapshots[-1])) == {"x"}
    assert 5_000 <= len(_text(snapshots[-1])) <= 6_000


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


def test_text_past_6000_characters_is_cut_to_the_newest_5000() -> None:
    """The oldest text loses its head first; the order of items stays."""
    a = _activity()
    a.add_text("a" * 4_000, 1)
    a.tool_started(ToolStarted(call=_call("c1")), 2)
    a.add_text("b" * 4_000, 3)
    items = a.snapshot().items
    texts = [i.text for i in items if i.kind == "text"]
    assert texts == ["a" * 1_000, "b" * 4_000]
    assert a.snapshot().items_dropped == 0


def test_text_after_a_cut_keeps_its_start_until_the_next_cut() -> None:
    """Between cuts the text only grows at its end, so the frames that carry it are appends."""
    a = _activity()
    a.add_text("a" * 6_001, 1)
    after_cut = _text(a.snapshot())
    a.add_text("b" * 900, 2)
    grown = _text(a.snapshot())
    a.add_text("c" * 200, 3)
    next_cut = _text(a.snapshot())
    assert (len(after_cut), len(grown), len(next_cut)) == (5_000, 5_900, 5_000)
    assert grown.startswith(after_cut)
    assert not next_cut.startswith(after_cut)
    assert next_cut == "a" * 3_900 + "b" * 900 + "c" * 200


def test_raw_text_past_the_cap_and_its_margin_is_cut_and_shows_its_newest_5000() -> None:
    """Text arriving faster than snapshots stays bounded, and the next snapshot cuts afresh."""
    a = _activity()
    a.add_text("a" * 6_000, 1)
    a.add_text("b" * 5_000, 2)
    assert _text(a.snapshot()) == "b" * 5_000


def test_an_emptied_text_item_is_removed_and_counted() -> None:
    """Text trimmed to nothing disappears and counts as dropped."""
    a = _activity()
    a.add_text("a" * 100, 1)
    a.tool_started(ToolStarted(call=_call("c1")), 2)
    a.add_text("b" * 6_000, 3)
    snapshot = a.snapshot()
    assert [i.kind for i in snapshot.items] == ["tool", "text"]
    assert snapshot.items_dropped == 1


def test_tool_items_keep_the_newest_20_and_count_the_rest() -> None:
    """The 21st tool row removes the oldest one."""
    a = _activity()
    for n in range(23):
        a.tool_started(ToolStarted(call=_call(f"c{n}")), n)
    snapshot = a.snapshot()
    assert len(snapshot.items) == 20
    assert snapshot.items[0].tool_call_id == "c3"
    assert snapshot.items_dropped == 3


def test_task_and_result_are_cut_to_1000_characters() -> None:
    """Both previews are capped."""
    a = _activity(task="t" * 3_000)
    a.set_status("completed", 5, result="r" * 3_000)
    snapshot = a.snapshot()
    assert len(snapshot.task) == 1_000
    assert snapshot.result_preview == "r" * 1_000


def test_tool_previews_are_cut_to_300_characters_and_marked_truncated() -> None:
    """A long argument or result preview is cut, with `truncated` set; a short one is left whole."""
    long_entry = ToolTraceEntry(
        type="tool_call_completed",
        tool_name="read_file",
        args_preview="a" * 500,
        result_preview="r" * 500,
    )
    short_entry = ToolTraceEntry(type="tool_call_started", tool_name="read_file", args_preview="path=a.txt")
    long_row, short_row = activity_module._public_entry(long_entry), activity_module._public_entry(short_entry)
    assert (long_row.args_preview, long_row.result_preview) == ("a" * 300, "r" * 300)
    assert long_row.truncated
    assert (short_row.args_preview, short_row.result_preview, short_row.truncated) == ("path=a.txt", None, False)


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
    assert [len(text) for text in redacted] == [5_000]
    assert a.snapshot().task == "t" * 1_000


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


class _Sink:
    """A sink that records each snapshot it receives, taking its time or failing each call on request."""

    def __init__(self, *, delay: float = 0.0, fail: bool = False) -> None:
        self.snapshots: list[SubagentSnapshot] = []
        self._delay = delay
        self._fail = fail
        self._arrived = asyncio.Event()

    def __call__(self, snapshot: SubagentSnapshot) -> None:
        self.snapshots.append(snapshot)
        self._arrived.set()
        if self._delay:
            time.sleep(self._delay)
        if self._fail:
            msg = "sink failed"
            raise RuntimeError(msg)

    async def wait_for(self, count: int) -> SubagentSnapshot:
        """Return the `count`th snapshot once it has arrived."""
        async with asyncio.timeout(2):
            while len(self.snapshots) < count:
                self._arrived.clear()
                await self._arrived.wait()
        return self.snapshots[count - 1]

    async def wait_for_status(self, status: SubagentStatus, *, agent_name: str | None = None) -> SubagentSnapshot:
        """Return the first snapshot with ``status``, of ``agent_name``'s run when given, once it has arrived."""

        def matches(snapshot: SubagentSnapshot) -> bool:
            return snapshot.status == status and agent_name in (None, snapshot.agent_name)

        async with asyncio.timeout(2):
            while not any(map(matches, self.snapshots)):
                self._arrived.clear()
                await self._arrived.wait()
        return next(filter(matches, self.snapshots))


def _open(seen: _Sink, *, delegation_id: str = "d1") -> ActivityFeed:
    return ActivityFeed.open(
        _child(delegation_id=delegation_id),
        parent_delegation_id="p0",
        root_caller_agent_name="lead",
        sink=seen,
    )


def _text(snapshot: SubagentSnapshot) -> str:
    return "".join(item.text for item in snapshot.items if item.kind == "text")


@pytest.mark.asyncio
async def test_opening_publishes_a_running_snapshot_at_once() -> None:
    """A new child shows up as soon as it starts."""
    seen = _Sink()
    _open(seen)
    snapshot = await seen.wait_for(1)
    assert (snapshot.delegation_id, snapshot.status, snapshot.parent_delegation_id) == ("d1", "running", "p0")
    assert snapshot.items == ()


@pytest.mark.asyncio
async def test_bursts_coalesce_to_one_hand_off_per_window() -> None:
    """Twenty text deltas in one burst reach the sink as one snapshot, a window after the first."""
    seen = _Sink()
    feed = _open(seen)
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
async def test_a_status_change_is_handed_off_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A status change wakes the sender instead of waiting out the window, and carries the text before it."""
    monkeypatch.setattr(activity_module, "_FLUSH_INTERVAL_SECONDS", 60)
    seen = _Sink()
    feed = _open(seen)
    await seen.wait_for(1)
    feed.text("asking")
    await asyncio.sleep(0)
    feed.status("awaiting_approval")
    snapshot = await seen.wait_for(2)
    assert (snapshot.status, _text(snapshot)) == ("awaiting_approval", "asking")


@pytest.mark.asyncio
async def test_a_failing_sink_does_not_raise_into_the_feed() -> None:
    """A sink that raises on every call never reaches the child, and later changes still reach the sink."""
    seen = _Sink(fail=True)
    feed = _open(seen)
    await seen.wait_for(1)
    feed.text("still here")
    feed.status("completed", result="done")
    snapshot = await seen.wait_for(2)
    assert (snapshot.status, _text(snapshot)) == ("completed", "still here")


@pytest.mark.asyncio
async def test_a_paused_child_reopens_with_its_transcript() -> None:
    """Resuming the same delegation keeps its transcript and start time, and runs again."""
    seen = _Sink()
    feed = _open(seen)
    feed.text("a")
    feed.status("paused")
    paused = await seen.wait_for(1)
    assert paused.status == "paused"
    _open(seen)
    resumed = await seen.wait_for(2)
    assert (resumed.status, _text(resumed), resumed.ended_at) == ("running", "a", None)
    assert resumed.started_at == paused.started_at


@pytest.mark.asyncio
async def test_a_resumed_attempt_publishes_to_its_own_sink() -> None:
    """The sink of the attempt that resumes a paused child takes the later snapshots."""
    first, second = _Sink(), _Sink()
    feed = _open(first)
    feed.status("paused")
    await first.wait_for(1)
    _open(second)
    assert (await second.wait_for(1)).status == "running"
    assert len(first.snapshots) == 1


@pytest.mark.asyncio
async def test_a_terminal_status_forgets_the_entry() -> None:
    """A finished run is published once with its result, and the same id later starts empty."""
    seen = _Sink()
    feed = _open(seen)
    feed.text("a")
    feed.status("completed", result="done")
    finished = await seen.wait_for(1)
    assert (finished.status, finished.result_preview, _text(finished)) == ("completed", "done", "a")
    assert finished.ended_at is not None
    assert not _OPEN
    _open(seen)
    fresh = await seen.wait_for(2)
    assert (fresh.status, fresh.items) == ("running", ())


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> _Sink:
    """Return a sink that takes the snapshots of every child run in the test."""
    sink = _Sink()
    monkeypatch.setattr(lifecycle, "_subagent_sink", lambda *_: sink)
    return sink


def _delegating_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    runtime = _delegate_runtime_context(config, paths, execution_identity=identity)
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
    seen: _Sink,
) -> None:
    """A child's run reaches the sink as it starts, with its tool and words, and finally with its result."""
    child_turns = [calls(tool_call("add", "add", a=1, b=2)), answer("Three")]
    turn, runtime, paths, config = _delegating_turn(tmp_path, monkeypatch, child_turns)

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
async def test_a_nested_child_names_its_parent_and_the_leader_as_root_caller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: _Sink,
) -> None:
    """A child's own sub-agent links the child's run and names it caller, and the leader in the room as root caller."""
    child_turns = [
        calls(tool_call("run_subagent", "nested", agent_name="grandchild", task="Go deeper")),
        answer("Child done"),
    ]
    turn, runtime, paths, config = _delegating_turn(
        tmp_path,
        monkeypatch,
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
async def test_a_child_stays_on_time_while_the_sink_is_slow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sink that takes a long time per snapshot never fails the child or its caller; the sink still gets the end."""
    seen = _Sink(delay=0.3)
    monkeypatch.setattr(lifecycle, "_subagent_sink", lambda *_: seen)
    child_turns = [calls(tool_call("add", "add", a=1, b=2)), answer("Three")]
    turn, runtime, paths, config = _delegating_turn(tmp_path, monkeypatch, child_turns)

    with tool_runtime_context(runtime):
        result = await asyncio.wait_for(ai_response(turn, paths, config), 5)

    assert result.text.endswith("Done")
    assert (await seen.wait_for_status("completed")).result_preview == "Three"


@pytest.mark.asyncio
async def test_a_child_without_a_sink_opens_no_feed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing publishing sub-agent activity, a delegation runs and leaves no feed behind."""
    child_turns = [answer("Three")]
    turn, runtime, paths, config = _delegating_turn(tmp_path, monkeypatch, child_turns)

    with tool_runtime_context(runtime):
        result = await ai_response(turn, paths, config)

    assert result.text.endswith("Done")
    assert not _OPEN


@pytest.mark.asyncio
async def test_a_cancelled_child_ends_cancelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seen: _Sink) -> None:
    """A child whose stream is cancelled is published as cancelled, with what it wrote before."""

    async def cancel() -> None:
        raise asyncio.CancelledError

    turn, runtime, paths, config = _delegating_turn(
        tmp_path,
        monkeypatch,
        [[TextDelta("Working"), Await(cancel)]],
    )

    with tool_runtime_context(runtime), pytest.raises(asyncio.CancelledError):
        await ai_response(turn, paths, config)

    last = await seen.wait_for_status("cancelled")
    assert last is seen.snapshots[-1]
    assert _text(last) == "Working"
    assert last.ended_at is not None
    assert last.result_preview == "Delegation cancelled."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("child_response", "reason"),
    [
        (
            AsyncMock(side_effect=RuntimeError("child exploded: api_key=example-secret")),
            redact_sensitive_text("child exploded: api_key=example-secret"),
        ),
        (
            AsyncMock(return_value=TurnResult("child answer")),
            "Delegated run ended without a retained terminal outcome.",
        ),
    ],
    ids=["raises", "retains_no_outcome"],
)
async def test_a_direct_child_that_settles_no_outcome_ends_failed_with_why(
    tmp_path: Path,
    child_response: AsyncMock,
    reason: str,
    seen: _Sink,
) -> None:
    """A direct child whose run raises, or ends without its outcome, is published failed with what its caller is told."""
    tools, config, paths = _direct_tools(tmp_path)

    with (
        tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=_identity())),
        patch("mindroom.ai.ai_response", child_response),
    ):
        result = await tools.run_subagent(agent_name="child", task="Do the work")

    assert "example-secret" not in result
    last = await seen.wait_for_status("failed")
    assert last is seen.snapshots[-1]
    assert (last.source, last.agent_name, last.result_preview) == ("delegate", "child", reason)
    assert last.ended_at is not None
    assert not _OPEN


@pytest.mark.asyncio
async def test_a_workflow_participant_reports_source_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: _Sink,
) -> None:
    """A Dynamic Workflow participant, the caller's own copy on the direct path, ends completed as a workflow run."""
    workflow = _Workflow(tmp_path, monkeypatch, _workflow_config())
    created = await workflow.create(_spec([{"id": "critic", "system_prompt": "Critic prompt", "tools": []}]))
    assert created["status"] == "ok", created

    with tool_runtime_context(
        _delegate_runtime_context(workflow.config, workflow.paths, execution_identity=_identity()),
    ):
        run = json.loads(await workflow.tools.run_workflow(workflow_id="review", input={}))

    assert run["status"] == "completed", run
    last = await seen.wait_for_status("completed")
    assert last is seen.snapshots[-1]
    assert (last.source, last.caller_agent_name, last.root_caller_agent_name, last.agent_name) == (
        "workflow",
        "leader",
        "leader",
        "leader",
    )
    assert last.result_preview == "Answer 0."
    assert not _OPEN


async def _background_delegation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    context = _delegate_runtime_context(config, paths, execution_identity=identity)
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
    seen: _Sink,
) -> None:
    """A child its job asks approvals for shows as awaiting them, runs again once decided, then completes."""
    leader, runtime, cards, context = await _background_delegation(tmp_path, monkeypatch)

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
    seen: _Sink,
) -> None:
    """A job cancelled while its child waits for an approval publishes the child as cancelled, not still waiting."""
    leader, runtime, cards, context = await _background_delegation(tmp_path, monkeypatch)

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
