"""The graph of a workflow run as frames show it: rows, fitting and deltas (pure level)."""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from mindroom.live_frames.transport import frame_fits
from mindroom.workflow_runs.graph import (
    REPLACE,
    fit_snapshot,
    graph_delta,
    graph_meta,
    graph_rows,
    node_label,
    projected_cost_usd,
)
from mindroom.workflow_runs.models import WorkflowNode, WorkflowNodeState, WorkflowRun, WorkflowRunState

if TYPE_CHECKING:
    from collections.abc import Iterable

_ROOM = "!room:example.test"
_CLIENT: Any = SimpleNamespace(rooms={_ROOM: SimpleNamespace(encrypted=True)})
_RUN = WorkflowRun(
    run_id="wf-" + "a" * 32,
    agent_name="watcher",
    owner_user_id="@alice:example.test",
    room_id=_ROOM,
    thread_root_event_id="$thread:example.test",
    execution_identity={},
    persona=None,
    title="Audit the invented module",
    source="",
    source_digest="",
    args=None,
    max_concurrent=16,
    max_cost_usd=None,
    resource_profile=None,
    runtime_seconds_left=None,
    origin_job_id=None,
    state=WorkflowRunState.RUNNING,
    attempt=1,
    created_at="2026-11-22T10:00:00+00:00",
)
_USAGE = {
    "input_tokens": 10,
    "output_tokens": 5,
    "tool_calls": 2,
    "cost_usd": 1.5,
    "attempt_cost_usd": 0.5,
    "projected_cost_usd": 3.0,
    "cost_unknown": False,
}
_STARTED = "2026-11-22T10:00:{:02d}+00:00"


def _node(seq: int, key: str | None = None, **fields: Any) -> WorkflowNode:  # noqa: ANN401
    key = f"@{seq}" if key is None else key
    lane, _, index = key.rpartition("@")
    values: dict[str, Any] = {
        "run_id": _RUN.run_id,
        "key": key,
        "seq": seq,
        "kind": "agent",
        "lane": lane,
        "idx": int(index) if index.isdecimal() else 0,
        "digest": "d",
        "request": {},
        "attempt": 1,
        "label": f"call {seq}",
        "cached": False,
        "started_at": _STARTED.format(seq % 60),
    }
    return WorkflowNode(**(values | fields))


def _nodes(*, label: str = "call", **states: int) -> list[WorkflowNode]:
    """Return agent calls in the root lane, as many in each state as *states* says."""
    made: list[WorkflowNode] = []
    for state, count in states.items():
        for _ in range(count):
            made.append(_node(len(made) + 1, state=WorkflowNodeState(state), label=label))
    return made


def _group(seq: int, key: str, op: str = "foreach", **fields: Any) -> WorkflowNode:  # noqa: ANN401
    return _node(seq, key, kind="group", request={"op": op}, state=WorkflowNodeState.COMPLETED, **fields)


def _fits(meta: dict[str, object]) -> Any:  # noqa: ANN401
    """Return the transport's own check of a frame with *meta*, as it goes out to an encrypted room."""

    def fits(snapshot: dict[str, object]) -> bool:
        frame = {"v": 1, "run": _RUN.run_id, "sid": "s" * 32, "seq": 0, "meta": meta, "snapshot": snapshot}
        return frame_fits(_CLIENT, _ROOM, frame, event_type="io.mindroom.workflow")

    return fits


def _fitted(nodes: Iterable[WorkflowNode], edges: Iterable[tuple[str, str, int]] = (), log: Iterable[str] = ()) -> Any:  # noqa: ANN401
    nodes = list(nodes)
    meta = graph_meta(_RUN, nodes, _USAGE, large=False)
    snapshot = fit_snapshot(graph_rows(_RUN, nodes, {}, edges, log), fits=_fits(meta))
    assert _fits(meta)(snapshot)
    return meta, snapshot


def test_label_is_name_or_first_prompt_line_redacted_and_cut() -> None:
    """A call is labelled by its name, else by the first line of its prompt, with no credential and at most 80 characters."""
    assert node_label("agent", {"prompt": "Research tides.\nCite sources."}, None) == "Research tides."
    assert node_label("agent", {"prompt": "Research tides."}, "tides") == "tides"
    assert node_label("ask", {"question": "Deploy?\nPlease answer."}, None) == "Deploy?"
    assert node_label("tool", {"toolkit": "shell", "function": "run_shell_command"}, None) == "shell.run_shell_command"
    assert node_label("group", {"op": "foreach"}, None) == "foreach"
    assert len(node_label("agent", {"prompt": "x" * 500}, None)) == 80
    secret = node_label("agent", {"prompt": "Use api_key=sk-inventedinventedinvented1234 now"}, None)
    assert "sk-invented" not in secret


def test_meta_has_exactly_the_pinned_fields_and_true_counts() -> None:
    """The meta of a frame names the run, where it stands, and counts every call, a cancelled one as failed."""
    nodes = [
        *_nodes(queued=3, running=2, waiting=1, completed=4, failed=1, cancelled=1, indeterminate=2),
        _node(90, state=WorkflowNodeState.COMPLETED, cached=True),
        _group(91, "@91"),
        _node(92, state=WorkflowNodeState.FAILED, stale=True),
    ]
    meta = graph_meta(_RUN, nodes, _USAGE | {"extra": 1}, large=True)

    assert meta == {
        "agent": "watcher",
        "thread_id": "$thread:example.test",
        "title": "Audit the invented module",
        "status": "running",
        "attempt": 1,
        "started_at": 1795341600,
        "ended_at": None,
        "max_concurrent": 16,
        "max_cost_usd": None,
        "large": True,
        "counts": {
            "queued": 3,
            "running": 2,
            "waiting": 1,
            "completed": 5,
            "failed": 2,
            "indeterminate": 2,
            "cached": 1,
        },
        "usage": _USAGE,
    }


def test_rows_have_exactly_the_pinned_fields() -> None:
    """A call's row and a group's row carry the fields the frame contract names, and nothing else."""
    group = _group(1, "@0", phase="Audit")
    call = _node(
        2,
        "/0:a@0",
        name="agent-2",
        phase="Audit",
        state=WorkflowNodeState.FAILED,
        error={"kind": "failed", "message": "boom " * 100},
        ended_at=_STARTED.format(9),
        links={"agent": "watcher", "model": "default", "delegation_id": "del", "subagent_id": "sub", "job_id": "job"},
    )
    used = {"input_tokens": 7, "output_tokens": 3, "tool_calls": 1, "cost_usd": 0.12, "cost_unknown": False}
    graph = graph_rows(_RUN, [group, call], {call.key: used}, [("n2", "n2", 3)], ["a line"])

    assert graph.phases == ["Audit"]
    assert graph.groups == [
        {"id": "n1", "kind": "foreach", "phase": 0, "parent": None, "total": 1, "done": 0, "failed": 1},
    ]
    row = graph.nodes[0]
    assert len(row["error"]) == 200
    assert row["error"].startswith("boom boom")
    assert graph.nodes == [
        {
            "id": "n2",
            "kind": "agent",
            "label": "call 2",
            "name": "agent-2",
            "phase": 0,
            "group": "n1",
            "status": "failed",
            "waiting_on": None,
            "agent": "watcher",
            "model": "default",
            "run": "del",
            "subagent": "sub",
            "job": "job",
            "session": None,
            "turns": 1,
            "started_at": 1795341602,
            "ended_at": 1795341609,
            "cached": False,
            "error": row["error"],
            "usage": {"input_tokens": 7, "output_tokens": 3, "tool_calls": 1, "cost_usd": 0.12},
        },
    ]
    assert graph.edges == [{"from": "n2", "to": "n2", "kind": "message", "count": 3}]
    assert graph.log == ["a line"]


def test_1000_unfinished_nodes_with_long_unicode_labels_fit_and_counts_stay_true() -> None:
    """A frame of a large run shows what a person can act on first and counts the rest, by status."""
    nodes = _nodes(queued=900, running=60, failed=40, label="検査 " * 16)
    meta, snapshot = _fitted(nodes)
    shown = Counter(node["status"] for node in snapshot["nodes"])

    assert shown["failed"] == 40
    assert shown["queued"] + snapshot["omitted"]["queued"] == 900
    assert shown["running"] + snapshot["omitted"]["running"] == 60
    assert 0 < len(snapshot["nodes"]) < 1000
    assert meta["counts"]["queued"] == 900
    assert meta["counts"]["failed"] == 40


def test_rows_come_in_the_order_a_person_can_act_on() -> None:
    """Waiting for a person first, then failed, running, queued, and completed newest first."""
    nodes = [
        _node(1, state=WorkflowNodeState.COMPLETED, ended_at=_STARTED.format(10)),
        _node(2, state=WorkflowNodeState.COMPLETED, ended_at=_STARTED.format(20)),
        _node(3, state=WorkflowNodeState.QUEUED),
        _node(4, state=WorkflowNodeState.RUNNING),
        _node(5, state=WorkflowNodeState.INDETERMINATE),
        _node(6, state=WorkflowNodeState.WAITING, waiting_on="user"),
    ]
    _, snapshot = _fitted(nodes)

    assert [node["id"] for node in snapshot["nodes"]] == ["n6", "n5", "n4", "n3", "n2", "n1"]
    assert snapshot["omitted"] == {
        "queued": 0,
        "running": 0,
        "waiting": 0,
        "completed": 0,
        "failed": 0,
        "edges": 0,
        "groups": 0,
        "phases": 0,
        "log": 0,
    }


def test_dense_edges_are_counted_when_left_out() -> None:
    """Edges come after every call, the busiest first, and the ones left out are counted."""
    nodes = _nodes(completed=100)
    edges = [(f"n{1 + index % 100}", f"n{1 + index // 100}", index) for index in range(5000)]
    _, snapshot = _fitted(nodes, edges)

    assert len(snapshot["nodes"]) == 100
    assert 0 < len(snapshot["edges"]) < 5000
    assert snapshot["omitted"]["edges"] + len(snapshot["edges"]) == 5000
    assert snapshot["edges"][0]["count"] == 4999


def test_groups_that_do_not_fit_fold_into_their_parent() -> None:
    """Inner groups leave a frame that has no room for them, and the group around them keeps its total."""
    nodes: list[WorkflowNode] = []
    lane = ""
    for depth in range(2000):
        nodes.append(_group(depth + 1, f"{lane}@0"))
        lane = f"{lane}/0:p0"
    nodes.append(_node(2001, f"{lane}@0", state=WorkflowNodeState.COMPLETED))
    whole = graph_rows(_RUN, nodes, {}, [], [])
    _, snapshot = _fitted(nodes)

    assert len(whole.groups) == 2000
    assert 0 < len(snapshot["groups"]) < 2000
    assert snapshot["groups"][0] == whole.groups[0]
    assert snapshot["groups"][0]["total"] == 1
    assert snapshot["omitted"]["groups"] + len(snapshot["groups"]) == 2000
    kept = {group["id"] for group in snapshot["groups"]}
    assert {node["group"] for node in snapshot["nodes"]} <= kept


def test_delta_has_only_changed_rows_and_new_log_lines() -> None:
    """Between two snapshots a delta names the rows that changed or came, and the log lines that were added."""
    one, two = _node(1, state=WorkflowNodeState.RUNNING), _node(2, state=WorkflowNodeState.RUNNING)
    _, before = _fitted([one, two], log=["one"])
    renamed, came = (
        _node(2, state=WorkflowNodeState.RUNNING, label="renamed"),
        _node(3, state=WorkflowNodeState.RUNNING),
    )
    _, after = _fitted([one, renamed, came], log=["one", "two"])

    delta = graph_delta(before, after)

    assert delta == {"nodes": [after["nodes"][1], after["nodes"][2]], "log_append": ["two"]}
    assert graph_delta(after, after) is None


def test_log_window_that_moved_appends_only_the_new_lines() -> None:
    """A log longer than a frame shows keeps its last 20 lines, and a delta adds the ones that came."""
    lines = [f"line {index}" for index in range(30)]
    _, before = _fitted([], log=lines[:25])
    _, after = _fitted([], log=lines)

    assert before["log"] == lines[5:25]
    assert graph_delta(before, after) == {"log_append": lines[25:]}


def test_removed_row_or_changed_phases_asks_for_a_snapshot() -> None:
    """A row that went, another list of phases, or another count of what was left out needs a whole snapshot."""
    one, two = _node(1, state=WorkflowNodeState.RUNNING), _node(2, state=WorkflowNodeState.RUNNING)
    _, both = _fitted([one, two])
    _, single = _fitted([one])
    _, phased = _fitted([_node(1, state=WorkflowNodeState.RUNNING, phase="Audit"), two])

    assert graph_delta(both, single) is REPLACE
    assert graph_delta(both, phased) is REPLACE
    assert graph_delta(both, {**both, "omitted": {**both["omitted"], "queued": 1}}) is REPLACE


def test_huge_log_and_500_long_phase_names_with_no_nodes_fall_back_to_counts_and_fit() -> None:
    """Text that is too long for a frame is cut, and what still does not fit is replaced by its count."""
    nodes = [
        _node(index + 1, state=WorkflowNodeState.COMPLETED, phase=f"{index} " + "相" * 300) for index in range(500)
    ]
    meta, snapshot = _fitted(nodes, log=["行" * 5000] * 400)

    assert meta["counts"]["completed"] == 500
    assert snapshot["phases"] == []
    assert snapshot["nodes"] == []
    assert snapshot["omitted"]["phases"] == 500
    assert snapshot["omitted"]["completed"] == 500
    assert snapshot["omitted"]["log"] == 20


def test_long_texts_are_cut_in_the_frame_and_not_in_the_run() -> None:
    """A title and a phase name are cut to 80 characters, and the log to its last 20 lines of 200 characters."""
    nodes = [_node(1, phase="p" * 300)]
    graph = graph_rows(_RUN, nodes, {}, [], ["l" * 900] * 50)
    meta = graph_meta(WorkflowRun(**{**_fields(_RUN), "title": "t" * 300}), nodes, _USAGE, large=False)

    assert graph.phases == ["p" * 80]
    assert graph.log == ["l" * 200] * 20
    assert len(meta["title"]) == 80
    assert meta["title"].startswith("tttt")


def test_counts_only_form_fits_for_any_run() -> None:
    """A run with more groups than any frame holds still sends a frame: its counts."""
    nodes = [_group(index + 1, f"@{index}") for index in range(10_000)]
    _, snapshot = _fitted(nodes)

    assert snapshot == {
        "phases": [],
        "groups": [],
        "nodes": [],
        "edges": [],
        "omitted": {
            "queued": 0,
            "running": 0,
            "waiting": 0,
            "completed": 0,
            "failed": 0,
            "edges": 0,
            "groups": 10_000,
            "phases": 0,
            "log": 0,
        },
        "log": [],
    }


def test_projection_includes_failed_turns_in_the_average() -> None:
    """The projection is what was spent plus the average of every finished turn for each agent still to end."""
    assert projected_cost_usd([0.6, 0.0, 0.3], open_agents=4, spent=0.9) == 0.9 + 0.3 * 4
    assert projected_cost_usd([], open_agents=4, spent=0.0) == 0.0


def test_stale_nodes_are_not_in_the_graph() -> None:
    """A call the script no longer reaches is in no row and in no count."""
    nodes = [_node(1, state=WorkflowNodeState.COMPLETED), _node(2, state=WorkflowNodeState.FAILED, stale=True)]
    meta, snapshot = _fitted(nodes)

    assert [node["id"] for node in snapshot["nodes"]] == ["n1"]
    assert meta["counts"]["failed"] == 0


def test_run_inside_a_run_is_a_group_under_the_call_that_started_it() -> None:
    """The group of a nested run names the call it is under, and counts the calls it is told the run has."""
    call = _node(1, state=WorkflowNodeState.RUNNING)
    nested = _group(2, "@1/wf-" + "b" * 32, op="workflow", group_key="@1", lane="@1", idx=0)
    graph = graph_rows(_RUN, [call, nested], {}, [], [], inside={nested.key: Counter(completed=2, failed=1, queued=4)})

    assert graph.groups == [
        {"id": "n2", "kind": "workflow", "phase": None, "parent": "n1", "total": 7, "done": 2, "failed": 1},
    ]


def _fields(run: WorkflowRun) -> dict[str, Any]:
    return {name: getattr(run, name) for name in run.__dataclass_fields__}
