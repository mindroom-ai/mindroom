"""Tests for the durable workflow run, node and usage store.

Level: pure (the real SQLite store on a temporary directory, nothing else).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

import pytest

from mindroom.workflow_runs import store as store_module
from mindroom.workflow_runs.graph import graph_rows
from mindroom.workflow_runs.models import (
    TERMINAL_RUN_STATES,
    DispatchHandle,
    WorkflowNode,
    WorkflowNodeState,
    WorkflowRun,
    WorkflowRunState,
    new_workflow_run_id,
    validated_workflow_run_id,
)
from mindroom.workflow_runs.replay import DIRTY_DIGEST
from mindroom.workflow_runs.store import (
    SupersededAttemptError,
    WorkflowRunConflictError,
    WorkflowRunNotFoundError,
    WorkflowRunStore,
    WorkflowRunStoreError,
)
from tests.workflow_helpers import set_run

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_RUN_A = "wf-" + "a" * 32
_RUN_B = "wf-" + "b" * 32
_RUN_C = "wf-" + "c" * 32


def _run(run_id: str = _RUN_A, **changes: object) -> WorkflowRun:
    run = WorkflowRun(
        run_id=run_id,
        agent_name="planner",
        owner_user_id="@requester:example.org",
        room_id="!room:example.org",
        thread_root_event_id="$thread",
        execution_identity={"agent_name": "planner", "requester_id": "@requester:example.org"},
        persona=None,
        title="Placeholder run",
        source="def main(args):\n    return args\n",
        source_digest="source-digest",
        args={"topic": "placeholder"},
        max_concurrent=16,
        max_cost_usd=None,
        resource_profile=None,
        runtime_seconds_left=259200.0,
        origin_job_id=None,
    )
    return replace(run, **changes)


def _node(key: str = "@0", run_id: str = _RUN_A, **changes: object) -> WorkflowNode:
    lane, _, idx = key.rpartition("@")
    node = WorkflowNode(
        run_id=run_id,
        key=key,
        kind="tool",
        lane=lane,
        idx=int(idx),
        digest="digest",
        request={"kind": "tool", "args": {"name": "counting_tool"}},
        attempt=1,
    )
    return replace(node, **changes)


def _store_with_node(tmp_path: Path, generation: int = 1) -> tuple[WorkflowRunStore, WorkflowNode]:
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    store.reserve_run(_run())
    return store, store.put_node(_node(generation=generation, state=WorkflowNodeState.RUNNING), 0)


def _saved(store: WorkflowRunStore, key: str = "@0", run_id: str = _RUN_A) -> WorkflowNode:
    node = store.get_node(run_id, key)
    assert node is not None
    return node


def test_put_node_assigns_stable_sequence(tmp_path: Path) -> None:
    """A new key gets the next sequence number, and writing the key again keeps it."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    run, _ = store.reserve_run(_run(origin_job_id="job-1"))
    first, second = store.put_node(_node(key="@0"), 0), store.put_node(_node(key="@1"), 0)
    again = store.put_node(
        replace(first, generation=2, state=WorkflowNodeState.COMPLETED, result={"big": "x" * 200_000}),
        0,
    )
    assert (first.seq, second.seq, again.seq) == (1, 2, 1)
    assert _saved(store).request == first.request
    assert _saved(store).result == {"big": "x" * 200_000}
    assert store.node_by_id(run.run_id, "n2") == second
    assert store.node_by_id(run.run_id, "n3") is None
    assert store.node_by_id(run.run_id, "n02") is None
    assert store.node_by_id(run.run_id, "n0") is None
    assert store.node_by_id(run.run_id, "n" + "9" * 20) is None
    assert store.get_node(run.run_id, "@9") is None


def test_node_round_trips_every_field(tmp_path: Path) -> None:
    """A node read back equals the node that was written."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    store.reserve_run(_run())
    node = store.put_node(
        _node(
            key="/0:ab12@3",
            kind="foreach",
            generation=2,
            state=WorkflowNodeState.WAITING,
            waiting_on="approval",
            handle=DispatchHandle("job", "job-7", dispatched=True),
            label="Check item",
            name="checker",
            phase="review",
            members=("/0:ab12", "/0:cd34"),
            session_key="@1",
            cached=False,
            started_at="2026-11-19T10:00:00Z",
            error={"kind": "tool_failed", "message": "placeholder"},
            links={"job_id": "job-7", "event_id": "$card"},
        ),
        0,
    )
    assert _saved(store, "/0:ab12@3") == node
    assert node.members == ("/0:ab12", "/0:cd34")
    assert node.handle == DispatchHandle("job", "job-7", dispatched=True)


def test_reserve_is_idempotent_per_origin_job(tmp_path: Path) -> None:
    """A second reserve for the same originating job returns the first run."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    first, created = store.reserve_run(_run(_RUN_A, origin_job_id="job-1"))
    second, created_again = store.reserve_run(_run(_RUN_B, origin_job_id="job-1"))
    assert (created, created_again, second.run_id) == (True, False, first.run_id)
    assert store.get_run(_RUN_A) == first
    with pytest.raises(WorkflowRunNotFoundError):
        store.get_run(_RUN_B)


def test_prune_keeps_a_run_inside_a_run_for_as_long_as_the_run_around_it(tmp_path: Path) -> None:
    """What a run inside a run spent is part of the outer run's totals, and its report is its call's: it goes with it."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    old = {"state": WorkflowRunState.COMPLETED, "finished_at": "2000-01-01T00:00:00Z"}
    store.reserve_run(_run(_RUN_A))
    store.reserve_run(_run(_RUN_B, origin_job_id="nested:1", parent_run_id=_RUN_A, parent_node_key="@0"))
    store.add_usage(_RUN_B, "@0", 1, 1, 1, {"cost_usd": 0.3})
    store.update_run(_RUN_B, 0, **old)

    # The outer run is live, and then ended a moment ago: the run inside it stays, however long ago it ended.
    assert store.prune("2001-01-01T00:00:00Z") == 0
    store.update_run(_RUN_A, 0, state=WorkflowRunState.COMPLETED, finished_at="2001-06-01T00:00:00Z")
    assert store.prune("2001-01-01T00:00:00Z") == 0
    assert store.usage_summary(_RUN_B).totals["cost_usd"] == pytest.approx(0.3)

    assert store.prune("2002-01-01T00:00:00Z") == 2
    # A run whose outer run is gone has nobody to count it: it goes by its own age.
    store.reserve_run(_run(_RUN_C, origin_job_id="nested:2", parent_run_id=_RUN_A, parent_node_key="@1"))
    store.update_run(_RUN_C, 0, **old)
    assert store.prune("2002-01-01T00:00:00Z") == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_user_id": "@other:example.org"},
        {"agent_name": "other"},
        {"room_id": "!other:example.org"},
        {"thread_root_event_id": "$other"},
        {"source_digest": "other-digest"},
        {"args": {"topic": "other"}},
    ],
)
def test_reserve_hit_with_another_owner_or_other_args_is_refused(tmp_path: Path, changes: dict[str, object]) -> None:
    """A reserve that hits a run of another owner, source or args never returns that run."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    first, _ = store.reserve_run(_run(_RUN_A, origin_job_id="job-1"))
    with pytest.raises(WorkflowRunConflictError):
        store.reserve_run(_run(_RUN_B, origin_job_id="job-1", **changes))
    assert store.list_runs() == [first]


def test_redelivered_start_hits_its_run_after_an_edited_resume(tmp_path: Path) -> None:
    """The reservation is compared as it was made, so a resume with an edited script does not change it."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    store.reserve_run(_run(_RUN_A, origin_job_id="job-1"))
    resumed = set_run(
        store,
        _RUN_A,
        source="def main(args):\n    return 1\n",
        source_digest="edited-digest",
        previous_source_digest="source-digest",
        attempt=2,
    )
    assert store.reserve_run(_run(_RUN_B, origin_job_id="job-1")) == (resumed, False)
    with pytest.raises(WorkflowRunConflictError):
        store.reserve_run(_run(_RUN_B, origin_job_id="job-1", source_digest="edited-digest"))
    with pytest.raises(WorkflowRunStoreError, match="origin_job_id"):
        set_run(store, _RUN_A, origin_job_id="job-2")
    assert store.get_run(_RUN_A) == resumed


@pytest.mark.parametrize(
    ("field", "column_value"),
    [
        ("agent_name", "other"),
        ("owner_user_id", "@other:example.org"),
        ("room_id", "!other:example.org"),
        ("thread_root_event_id", "$other"),
    ],
)
def test_a_run_keeps_its_owner_and_a_hit_checks_the_stored_one(tmp_path: Path, field: str, column_value: str) -> None:
    """The owner cannot be updated, and a row whose owner differs anyway is never returned to the first owner."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    first, _ = store.reserve_run(_run(_RUN_A, origin_job_id="job-1"))
    with pytest.raises(WorkflowRunStoreError, match=field):
        set_run(store, _RUN_A, **{field: column_value})
    assert store.reserve_run(_run(_RUN_B, origin_job_id="job-1")) == (first, False)

    with sqlite3.connect(store.database_path) as connection:
        connection.execute(f"UPDATE workflow_runs SET {field} = ?", (column_value,))  # noqa: S608
    with pytest.raises(WorkflowRunConflictError):
        store.reserve_run(_run(_RUN_B, origin_job_id="job-1"))


def test_reservation_compares_canonical_json_arguments(tmp_path: Path) -> None:
    """Key order does not matter, and a boolean is not the number it equals in Python."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    first, _ = store.reserve_run(_run(_RUN_A, origin_job_id="job-1", args={"flag": True, "items": [1, 2], "n": 1}))
    reordered = _run(_RUN_B, origin_job_id="job-1", args={"n": 1, "items": [1, 2], "flag": True})
    assert store.reserve_run(reordered) == (first, False)
    for other in ({"flag": 1, "items": [1, 2], "n": 1}, {"flag": True, "items": [1, 2], "n": True}):
        with pytest.raises(WorkflowRunConflictError):
            store.reserve_run(_run(_RUN_B, origin_job_id="job-1", args=other))
    assert store.list_runs() == [first]


def test_reserve_refuses_a_run_id_that_exists_or_is_malformed(tmp_path: Path) -> None:
    """Run IDs are generated, validated and never reused."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    store.reserve_run(_run(_RUN_A, origin_job_id="job-1"))
    with pytest.raises(WorkflowRunConflictError):
        store.reserve_run(_run(_RUN_A, origin_job_id="job-2"))
    with pytest.raises(ValueError, match="wf- followed by 32"):
        store.reserve_run(_run("wf-short"))
    assert validated_workflow_run_id(new_workflow_run_id()) != new_workflow_run_id()


def test_concurrent_writers_reserve_one_run_and_number_nodes_without_gaps(tmp_path: Path) -> None:
    """Racing starts of one job make one run, and racing nodes each get their own sequence number."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    run_ids = [f"wf-{number:032x}" for number in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        reserved = list(pool.map(lambda run_id: store.reserve_run(_run(run_id, origin_job_id="job-1")), run_ids))
        winner = reserved[0][0].run_id
        nodes = list(pool.map(lambda number: store.put_node(_node(key=f"@{number}", run_id=winner), 0), range(16)))
    assert [created for _run_record, created in reserved].count(True) == 1
    assert {run.run_id for run, _created in reserved} == {winner}
    assert [run.run_id for run in store.list_runs()] == [winner]
    assert sorted(node.seq for node in nodes) == list(range(1, 17))


def test_reserve_counts_the_owners_live_top_level_runs_in_the_write_that_stores_the_intent(tmp_path: Path) -> None:
    """Racing starts at the limit store exactly the limit; ended and nested runs and other owners take no slot."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    store.reserve_run(_run(_RUN_A, origin_job_id="job-a"))
    store.reserve_run(_run(_RUN_B, parent_run_id=_RUN_A, parent_node_key="@0"))
    store.reserve_run(_run(_RUN_C, owner_user_id="@other:example.org"))
    set_run(store, store.reserve_run(_run("wf-" + "d" * 32))[0].run_id, state=WorkflowRunState.COMPLETED)

    def reserve(number: int) -> str:
        try:
            return str(store.reserve_run(_run(f"wf-{number:032x}"), max_active=3)[1])
        except WorkflowRunStoreError as refusal:
            return str(refusal)

    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(reserve, range(8)))

    limit = "You have 3 workflow runs in progress, the limit. Wait for one to end or cancel one."
    assert sorted(answers) == ["True"] * 2 + [limit] * 6
    assert (
        len(store.list_runs(owner_user_id="@requester:example.org", top_level_only=True, include_finished=False)) == 3
    )
    # The call that reserved a run finds it again at the limit, and a refused start left no row.
    assert store.reserve_run(_run("wf-" + "e" * 32, origin_job_id="job-a"), max_active=3) == (
        store.get_run(_RUN_A),
        False,
    )
    assert len(store.list_runs()) == 6


def test_delete_intent_removes_only_a_run_that_never_started(tmp_path: Path) -> None:
    """An intent is a starting run with attempt 0, deleted as its owner's at its epoch; anything else stays."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    store.reserve_run(_run(_RUN_A, owner="job-a"))
    store.reserve_run(_run(_RUN_B, owner="job-b"))
    set_run(store, _RUN_B, attempt=1)
    assert not store.delete_intent(_RUN_A, "job-b", 0)
    assert not store.delete_intent(_RUN_A, None, 0)
    assert not store.delete_intent(_RUN_A, "job-a", 1)
    assert store.delete_intent(_RUN_A, "job-a", 0)
    assert not store.delete_intent(_RUN_A, "job-a", 0)
    assert not store.delete_intent(_RUN_B, "job-b", 0)
    assert [run.run_id for run in store.list_runs()] == [_RUN_B]


def test_update_run_changes_named_fields_only(tmp_path: Path) -> None:
    """An update writes the given fields and returns the stored run."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    run, _ = store.reserve_run(_run())
    updated = set_run(
        store,
        run.run_id,
        state=WorkflowRunState.COMPLETED,
        attempt=1,
        script_run_id="script-" + "1" * 32,
        result={"answer": [1, 2]},
        finished_at="2026-11-19T12:00:00Z",
    )
    assert updated == replace(
        run,
        state=WorkflowRunState.COMPLETED,
        epoch=1,
        attempt=1,
        script_run_id="script-" + "1" * 32,
        result={"answer": [1, 2]},
        finished_at="2026-11-19T12:00:00Z",
    )
    assert store.get_run(run.run_id) == updated
    with pytest.raises(WorkflowRunNotFoundError):
        set_run(store, _RUN_B, attempt=1)
    with pytest.raises(WorkflowRunStoreError, match="no_such_field"):
        set_run(store, run.run_id, no_such_field=1)


def test_list_runs_filters_by_owner_place_and_state(tmp_path: Path) -> None:
    """Each filter narrows the list, newest first."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    store.reserve_run(_run(_RUN_A, created_at="2026-11-19T10:00:00Z"))
    store.reserve_run(_run(_RUN_B, created_at="2026-11-19T11:00:00Z", agent_name="other", room_id="!b:example.org"))
    store.reserve_run(
        _run(
            _RUN_C,
            created_at="2026-11-19T12:00:00Z",
            parent_run_id=_RUN_A,
            parent_node_key="@0",
            owner_user_id="@other:example.org",
            thread_root_event_id=None,
        ),
    )
    set_run(store, _RUN_B, state=WorkflowRunState.FAILED)

    def ids(**filters: object) -> list[str]:
        return [run.run_id for run in store.list_runs(**filters)]

    assert ids() == [_RUN_C, _RUN_B, _RUN_A]
    assert ids(agent_name="planner") == [_RUN_C, _RUN_A]
    assert ids(owner_user_id="@requester:example.org") == [_RUN_B, _RUN_A]
    assert ids(room_id="!room:example.org", thread_root_event_id="$thread") == [_RUN_A]
    assert ids(top_level_only=True) == [_RUN_B, _RUN_A]
    assert ids(include_finished=False) == [_RUN_C, _RUN_A]
    assert [run.run_id for run in store.child_runs(_RUN_A)] == [_RUN_C]
    assert store.child_runs(_RUN_B) == []


def test_settle_refuses_an_old_generation(tmp_path: Path) -> None:
    """A late result of a replaced generation changes nothing."""
    store, node = _store_with_node(tmp_path, generation=2)
    assert not store.settle_node(node.run_id, node.key, 1, 0, state=WorkflowNodeState.COMPLETED, result="late")
    assert _saved(store).state is WorkflowNodeState.RUNNING
    assert _saved(store) == node


def test_settle_records_the_end_of_the_current_generation(tmp_path: Path) -> None:
    """The current generation's result or error is stored with its end time."""
    store, node = _store_with_node(tmp_path)
    assert store.settle_node(node.run_id, node.key, 1, 0, state=WorkflowNodeState.COMPLETED, result={"lines": 1})
    settled = _saved(store)
    assert (settled.state, settled.result, settled.error) == (WorkflowNodeState.COMPLETED, {"lines": 1}, None)
    assert settled.ended_at is not None
    assert replace(settled, state=node.state, result=None, ended_at=None) == node

    failed = store.put_node(_node(key="@1", state=WorkflowNodeState.WAITING, waiting_on="user"), 0)
    error = {"kind": "indeterminate", "message": "placeholder"}
    assert store.settle_node(failed.run_id, failed.key, 1, 0, state=WorkflowNodeState.INDETERMINATE, error=error)
    assert (_saved(store, "@1").error, _saved(store, "@1").waiting_on) == (error, None)
    assert not store.settle_node(node.run_id, "@9", 1, 0, state=WorkflowNodeState.COMPLETED)


def _two_stores(tmp_path: Path) -> tuple[WorkflowRunStore, WorkflowRunStore]:
    """Two writers on one database, as an engine and a late callback are."""
    path = tmp_path / "w.sqlite3"
    first = WorkflowRunStore(path)
    first.reserve_run(_run())
    return first, WorkflowRunStore(path)


def test_old_snapshot_cannot_replace_or_settle_a_newer_generation(tmp_path: Path) -> None:
    """A writer that read generation 1 changes nothing once generation 2 is claimed."""
    engine, late = _two_stores(tmp_path)
    claimed = engine.put_node(_node(state=WorkflowNodeState.RUNNING), 0)
    snapshot = _saved(late)
    newer = engine.put_node(replace(claimed, generation=2, digest="edited"), 0)

    with pytest.raises(WorkflowRunConflictError, match="generation 2"):
        late.put_node(replace(snapshot, attempt=2), 0)
    assert not late.update_node(snapshot.run_id, snapshot.key, 1, 0, attempt=2)
    assert not late.settle_node(snapshot.run_id, snapshot.key, 1, 0, state=WorkflowNodeState.COMPLETED, result="late")
    assert _saved(engine) == newer


def test_a_key_and_each_generation_are_claimed_once(tmp_path: Path) -> None:
    """Of two writers that claim the same new key or the same next generation, the second is refused."""
    engine, other = _two_stores(tmp_path)
    first = engine.put_node(_node(), 0)
    with pytest.raises(WorkflowRunConflictError):
        other.put_node(_node(digest="other"), 0)
    second = engine.put_node(replace(first, generation=2), 0)
    for generation in (1, 2, 4):
        with pytest.raises(WorkflowRunConflictError):
            other.put_node(replace(first, generation=generation, digest="other"), 0)
    assert _saved(engine) == second
    assert (second.seq, second.digest) == (first.seq, "digest")


def test_late_receipt_never_replaces_a_definitive_outcome(tmp_path: Path) -> None:
    """Once a generation has a proven outcome, a second settlement of it is a no-op."""
    engine, late = _two_stores(tmp_path)
    node = engine.put_node(_node(state=WorkflowNodeState.RUNNING), 0)
    assert engine.settle_node(node.run_id, node.key, 1, 0, state=WorkflowNodeState.COMPLETED, result="done")
    settled = _saved(engine)
    error = {"kind": "indeterminate", "message": "placeholder"}
    assert not late.settle_node(node.run_id, node.key, 1, 0, state=WorkflowNodeState.INDETERMINATE, error=error)
    assert not late.settle_node(node.run_id, node.key, 1, 0, state=WorkflowNodeState.COMPLETED, result="other")
    assert not late.settle_node(node.run_id, node.key, 1, 0, state=WorkflowNodeState.FAILED, error=error)
    assert _saved(late) == settled


def test_late_receipt_resolves_an_indeterminate_node(tmp_path: Path) -> None:
    """A call whose end was unknown takes the proven outcome when its receipt does arrive."""
    engine, late = _two_stores(tmp_path)
    node = engine.put_node(_node(state=WorkflowNodeState.RUNNING), 0)
    error = {"kind": "indeterminate", "message": "placeholder"}
    assert engine.settle_node(node.run_id, node.key, 1, 0, state=WorkflowNodeState.INDETERMINATE, error=error)
    assert not engine.settle_node(node.run_id, node.key, 1, 0, state=WorkflowNodeState.INDETERMINATE, error={})
    assert _saved(engine).error == error
    assert late.settle_node(node.run_id, node.key, 1, 0, state=WorkflowNodeState.COMPLETED, result="done")
    resolved = _saved(engine)
    assert (resolved.state, resolved.result, resolved.error) == (WorkflowNodeState.COMPLETED, "done", None)
    with pytest.raises(WorkflowRunStoreError, match="waiting"):
        engine.settle_node(node.run_id, node.key, 1, 0, state=WorkflowNodeState.WAITING)


def test_stamps_change_named_fields_and_never_an_outcome(tmp_path: Path) -> None:
    """A stamp updates the current generation's fields, and cannot reopen or rewrite a node that ended."""
    store, node = _store_with_node(tmp_path)
    handle = DispatchHandle("job", "job-7", dispatched=True)
    stamp = {"state": WorkflowNodeState.WAITING, "waiting_on": "user", "handle": handle, "links": {"job_id": "job-7"}}
    assert store.update_node(node.run_id, node.key, 1, 0, **stamp)
    assert _saved(store) == replace(node, **stamp)
    assert not store.update_node(node.run_id, "@9", 1, 0, attempt=2)

    assert store.settle_node(node.run_id, node.key, 1, 0, state=WorkflowNodeState.COMPLETED, result={"lines": 1})
    settled = _saved(store)
    assert store.update_node(node.run_id, node.key, 1, 0, attempt=2, stale=True, members=("/0:a", "/0:b"))
    assert _saved(store) == replace(settled, attempt=2, stale=True, members=("/0:a", "/0:b"))
    assert not store.update_node(node.run_id, node.key, 1, 0, state=WorkflowNodeState.RUNNING, attempt=3)
    assert _saved(store).attempt == 2
    for outcome in ({"state": WorkflowNodeState.FAILED}, {"result": "x"}, {"error": {}}, {"digest": "x"}, {}):
        with pytest.raises(WorkflowRunStoreError):
            store.update_node(node.run_id, node.key, 1, 0, **outcome)


def test_stale_nodes_leave_the_current_view(tmp_path: Path) -> None:
    """Nodes not reached by the run's current attempt leave the view; stale ones appear only on request."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    store.reserve_run(_run(attempt=2))
    first = store.put_node(_node(key="@0", attempt=2), 0)
    second = store.put_node(_node(key="@1", attempt=1), 0)
    third = store.put_node(_node(key="/1:ab@0", attempt=2), 0)
    already_stale = store.put_node(replace(_node(key="@2", attempt=1), stale=True), 0)
    store.mark_stale(first.run_id, 0)
    assert store.nodes(first.run_id) == [first, third]
    assert store.nodes(first.run_id, include_stale=True) == [first, replace(second, stale=True), third, already_stale]
    assert store.nodes(_RUN_B) == []


def test_usage_is_append_only_and_deduplicated(tmp_path: Path) -> None:
    """One entry per run, node, generation and turn; a repeat changes nothing."""
    store, node = _store_with_node(tmp_path)
    assert store.add_usage(node.run_id, node.key, 1, 1, 1, {"cost_usd": 0.5, "input_tokens": 10})
    assert not store.add_usage(node.run_id, node.key, 1, 1, 1, {"cost_usd": 0.5, "input_tokens": 10})
    store.add_usage(node.run_id, node.key, 2, 1, 2, {"cost_usd": 0.25})  # a rerun in attempt 2
    assert store.usage_summary(node.run_id).totals["cost_usd"] == 0.75
    assert store.usage_summary(node.run_id, attempt=2).attempt_cost_usd == 0.25
    assert store.usage_summary(node.run_id).totals == {
        "input_tokens": 10,
        "output_tokens": 0,
        "tool_calls": 0,
        "cost_usd": 0.75,
        "cost_unknown": False,
    }


def test_unknown_price_marks_totals_unknown(tmp_path: Path) -> None:
    """A turn without a price makes the sum a lower bound, for its node and for the run."""
    store, node = _store_with_node(tmp_path)
    store.put_node(_node(key="@1"), 0)
    store.add_usage(node.run_id, "@0", 1, 1, 1, {"cost_usd": 0.5, "output_tokens": 7, "tool_calls": 2})
    store.add_usage(node.run_id, "@1", 1, 1, 1, {"input_tokens": 3, "output_tokens": 4})
    store.add_usage(node.run_id, "@1", 1, 2, 1, {"input_tokens": 1, "cost_usd": 0.125})
    priced = {"input_tokens": 0, "output_tokens": 7, "tool_calls": 2, "cost_usd": 0.5, "cost_unknown": False}
    unpriced = {"input_tokens": 4, "output_tokens": 4, "tool_calls": 0, "cost_usd": 0.125, "cost_unknown": True}
    assert store.usage_summary(node.run_id).by_node == {"@0": priced, "@1": unpriced}
    assert store.usage_summary(node.run_id).totals == {
        "input_tokens": 4,
        "output_tokens": 11,
        "tool_calls": 2,
        "cost_usd": 0.625,
        "cost_unknown": True,
    }
    assert store.usage_summary(_RUN_B).totals == {
        "input_tokens": 0,
        "output_tokens": 0,
        "tool_calls": 0,
        "cost_usd": 0.0,
        "cost_unknown": False,
    }


def test_large_result_spills_to_a_file_and_prune_removes_it(tmp_path: Path) -> None:
    """A result over 8 MiB lives in a file; the row keeps its path and a 64 KiB preview."""
    store, node = _store_with_node(tmp_path)
    big = {"text": "x" * (9 * 1024 * 1024)}
    assert store.settle_node(node.run_id, node.key, 1, 0, state=WorkflowNodeState.COMPLETED, result=big)
    settled = _saved(store)
    assert settled.result_file == f"results/{node.run_id}/1-1.json"
    spilled = store.directory / settled.result_file
    assert json.loads(spilled.read_text()) == big
    assert isinstance(settled.result, str)
    assert len(settled.result) == 64 * 1024
    assert settled.result.startswith('{"text":"xxx')

    assert store.update_node(node.run_id, node.key, 1, 0, attempt=2)
    settled = replace(settled, attempt=2)
    assert _saved(store) == settled
    assert json.loads(spilled.read_text()) == big

    bigger = {"text": "é" * (5 * 1024 * 1024)}
    rerun = store.put_node(replace(settled, generation=2, result=bigger, result_file=None), 0)
    assert rerun.result_file == f"results/{node.run_id}/1-2.json"
    assert isinstance(rerun.result, str)
    assert len(rerun.result.encode()) <= 64 * 1024

    small = store.put_node(replace(rerun, generation=3, result="small", result_file=None), 0)
    assert (small.result, small.result_file) == ("small", None)

    set_run(store, node.run_id, state=WorkflowRunState.COMPLETED, finished_at="2026-11-01T00:00:00Z")
    assert store.prune("2026-11-02T00:00:00Z") == 1
    assert not spilled.exists()
    assert not (store.directory / "results" / node.run_id).exists()


def test_settling_again_never_touches_a_committed_spill(tmp_path: Path) -> None:
    """A row that names a spill file keeps that file and its preview, whatever a later settlement brings."""
    store, node = _store_with_node(tmp_path)
    first = {"text": "a" * (9 * 1024 * 1024)}
    assert store.settle_node(node.run_id, node.key, 1, 0, state=WorkflowNodeState.COMPLETED, result=first)
    settled = _saved(store)
    assert settled.result_file is not None
    spilled = store.directory / settled.result_file

    second = {"text": "b" * (9 * 1024 * 1024)}
    assert not store.settle_node(node.run_id, node.key, 1, 0, state=WorkflowNodeState.COMPLETED, result=second)
    assert not store.settle_node(node.run_id, node.key, 1, 0, state=WorkflowNodeState.FAILED, error={"kind": "late"})
    assert json.loads(spilled.read_text()) == first
    assert _saved(store) == settled


def test_failed_spill_write_publishes_no_file_and_no_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spill appears under its name only whole, and the row names it only after that."""
    store, first = _store_with_node(tmp_path)
    big = {"text": "a" * (9 * 1024 * 1024)}
    assert store.settle_node(first.run_id, first.key, 1, 0, state=WorkflowNodeState.COMPLETED, result=big)
    node = store.put_node(_node(key="@1", state=WorkflowNodeState.RUNNING), 0)
    results = store.directory / "results" / node.run_id

    def refuse(_fd: int) -> None:
        msg = "placeholder disk failure"
        raise OSError(msg)

    with monkeypatch.context() as patched:
        patched.setattr("os.fsync", refuse)
        with pytest.raises(OSError, match="placeholder disk failure"):
            store.settle_node(node.run_id, node.key, 1, 0, state=WorkflowNodeState.COMPLETED, result=big)
    assert [path.name for path in results.iterdir()] == ["1-1.json"]
    assert _saved(store, "@1") == node

    assert store.settle_node(node.run_id, node.key, 1, 0, state=WorkflowNodeState.COMPLETED, result=big)
    assert json.loads((results / "2-1.json").read_text()) == big
    assert sorted(path.name for path in results.iterdir()) == ["1-1.json", "2-1.json"]


def test_prune_removes_only_old_finished_runs(tmp_path: Path) -> None:
    """Prune takes a finished run's rows and leaves newer and unfinished runs whole."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    for run_id in (_RUN_A, _RUN_B, _RUN_C):
        store.reserve_run(_run(run_id))
        store.put_node(_node(run_id=run_id), 0)
        store.add_usage(run_id, "@0", 1, 1, 1, {"cost_usd": 1.0})
        store.append_log(run_id, 0, "placeholder line")
    set_run(store, _RUN_A, state=WorkflowRunState.CANCELLED, finished_at="2026-11-01T00:00:00Z")
    set_run(store, _RUN_B, state=WorkflowRunState.COMPLETED, finished_at="2026-11-10T00:00:00Z")
    # A resumed run keeps the end time of its earlier attempt and must not be pruned while it runs.
    set_run(store, _RUN_C, state=WorkflowRunState.RUNNING, finished_at="2026-11-01T00:00:00Z")

    assert store.prune("2026-11-05T00:00:00Z") == 1
    assert sorted(run.run_id for run in store.list_runs()) == [_RUN_B, _RUN_C]
    assert (store.nodes(_RUN_A), store.log(_RUN_A)) == ([], [])
    assert store.usage_summary(_RUN_A).totals["cost_usd"] == 0.0
    for run_id in (_RUN_B, _RUN_C):
        assert len(store.nodes(run_id)) == 1
        assert store.usage_summary(run_id).totals["cost_usd"] == 1.0
        assert store.log(run_id) == ["placeholder line"]
    assert store.prune("2026-11-05T00:00:00Z") == 0


def test_prune_compares_instants_at_an_exact_second_cutoff(tmp_path: Path) -> None:
    """A finish later in the cutoff's own second is newer than the cutoff, however its timestamp is written."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    finishes = {
        _RUN_A: "2026-11-04T23:59:59.999999Z",
        _RUN_B: "2026-11-05T00:00:00.500000Z",
        _RUN_C: "2026-11-05T00:00:00Z",
    }
    for run_id, finished_at in finishes.items():
        store.reserve_run(_run(run_id))
        set_run(store, run_id, state=WorkflowRunState.COMPLETED, finished_at=finished_at)
    assert store.prune("2026-11-05T00:00:00Z") == 1
    assert sorted(run.run_id for run in store.list_runs()) == [_RUN_B, _RUN_C]
    assert store.prune("2026-11-05T00:00:00.500001Z") == 2


def test_log_returns_the_last_lines_in_order(tmp_path: Path) -> None:
    """The log is per run and reads back oldest first, capped to the newest lines."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    store.reserve_run(_run(_RUN_A))
    store.reserve_run(_run(_RUN_B))
    for number in range(4):
        store.append_log(_RUN_A, 0, f"line {number}")
    store.append_log(_RUN_B, 0, "other run")
    assert store.log(_RUN_A) == ["line 0", "line 1", "line 2", "line 3"]
    assert store.log(_RUN_A, limit=2) == ["line 2", "line 3"]


def _schema_version(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT version FROM workflow_schema").fetchone()[0]


def test_open_waits_for_another_opener_that_holds_the_new_database(tmp_path: Path) -> None:
    """Opening a database that someone else is still creating waits for them, as every other statement does."""
    path = tmp_path / "w.sqlite3"
    holder = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("CREATE TABLE placeholder (value TEXT)")

    def release() -> None:
        time.sleep(0.3)
        holder.execute("COMMIT")
        holder.close()

    releaser = threading.Thread(target=release)
    releaser.start()
    try:
        store = WorkflowRunStore(path)
    finally:
        releaser.join()
    assert _schema_version(path) == store_module.SCHEMA_VERSION
    assert store.reserve_run(_run())[1]


def test_concurrent_first_opens_all_succeed(tmp_path: Path) -> None:
    """Openers that race to create the database each get a store on the same schema."""
    path = tmp_path / "fresh" / "w.sqlite3"
    barrier = threading.Barrier(8)

    def open_store(_number: int) -> WorkflowRunStore:
        barrier.wait()
        return WorkflowRunStore(path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        stores = list(pool.map(open_store, range(8)))
    assert len(stores) == 8
    assert _schema_version(path) == store_module.SCHEMA_VERSION
    stores[0].reserve_run(_run())
    assert [run.run_id for run in stores[7].list_runs()] == [_RUN_A]


def test_newer_schema_refuses_to_open(tmp_path: Path) -> None:
    """A database written by a newer MindRoom is not opened or changed."""
    path = tmp_path / "w.sqlite3"
    WorkflowRunStore(path)
    newer = _schema_version(path) + 1
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE workflow_schema SET version = ?", (newer,))
    with pytest.raises(WorkflowRunStoreError, match=rf"Workflow store is from a newer MindRoom \(schema {newer}\)\."):
        WorkflowRunStore(path)
    assert _schema_version(path) == newer


def test_migrations_run_in_order_from_version_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each migration runs once, in order, and the stored version follows."""
    path = tmp_path / "w.sqlite3"
    store = WorkflowRunStore(path)
    store.reserve_run(_run())
    assert _schema_version(path) == store_module.SCHEMA_VERSION == 2

    calls: list[str] = []

    def second(connection: sqlite3.Connection) -> None:
        calls.append("second")
        connection.execute("ALTER TABLE workflow_runs ADD COLUMN placeholder TEXT")

    def third(connection: sqlite3.Connection) -> None:
        calls.append("third")
        connection.execute("UPDATE workflow_runs SET placeholder = 'migrated'")

    monkeypatch.setattr(store_module, "_MIGRATIONS", (*store_module._MIGRATIONS, second, third))
    for _ in range(2):
        WorkflowRunStore(path)
    assert calls == ["second", "third"]
    assert _schema_version(path) == 4
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT placeholder FROM workflow_runs").fetchall() == [("migrated",)]

    fresh = tmp_path / "fresh.sqlite3"
    WorkflowRunStore(fresh)
    assert calls == ["second", "third", "second", "third"]
    assert _schema_version(fresh) == 4


def test_failed_migration_leaves_the_version_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A migration that raises rolls back, so the next open tries it again."""
    path = tmp_path / "w.sqlite3"
    WorkflowRunStore(path)

    def broken(connection: sqlite3.Connection) -> None:
        connection.execute("ALTER TABLE workflow_runs ADD COLUMN placeholder TEXT")
        msg = "placeholder failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(store_module, "_MIGRATIONS", (*store_module._MIGRATIONS, broken))
    with pytest.raises(RuntimeError, match="placeholder failure"):
        WorkflowRunStore(path)
    assert _schema_version(path) == store_module.SCHEMA_VERSION
    with sqlite3.connect(path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(workflow_runs)")]
    assert "placeholder" not in columns


def test_claim_takes_an_ended_run_only_at_the_epoch_it_names(tmp_path: Path) -> None:
    """A resume names the epoch the run had when it was accepted; a run that moved on since is left whole."""
    store, node = _store_with_node(tmp_path)
    ended = set_run(store, _RUN_A, state=WorkflowRunState.FAILED, job_id="job-1", finished_at="2026-01-01T00:00:00Z")
    changes: dict[str, object] = {"state": WorkflowRunState.STARTING, "job_id": "job-2", "finished_at": None}
    claim = partial(store.update_run, _RUN_A, when=TERMINAL_RUN_STATES, rerun=[node.key], **changes)

    for stale in (ended.epoch - 1, ended.epoch + 1):
        with pytest.raises(SupersededAttemptError):
            claim(epoch=stale)
    assert (store.get_run(_RUN_A), _saved(store).digest) == (ended, "digest")

    taken = claim(epoch=ended.epoch)

    assert taken == store.get_run(_RUN_A) == replace(ended, epoch=ended.epoch + 1, **changes)
    assert _saved(store).digest == ""
    # Taken once: the same claim finds a run that is no longer ended, and one that ended again has another epoch.
    with pytest.raises(SupersededAttemptError):
        claim(epoch=ended.epoch)
    set_run(store, _RUN_A, state=WorkflowRunState.FAILED)
    with pytest.raises(SupersededAttemptError):
        claim(epoch=ended.epoch)


def test_claim_counts_the_owners_live_runs_in_the_write_that_takes_the_run(tmp_path: Path) -> None:
    """An ended run that is taken becomes live again, so it needs a free slot; a refused claim changes nothing."""
    store, node = _store_with_node(tmp_path)
    ended = set_run(store, _RUN_A, state=WorkflowRunState.COMPLETED)
    store.reserve_run(_run(_RUN_B))
    claim = partial(store.update_run, _RUN_A, ended.epoch, when=TERMINAL_RUN_STATES, rerun=[node.key])

    with pytest.raises(WorkflowRunStoreError, match="You have 1 workflow runs in progress, the limit"):
        claim(max_active=1, state=WorkflowRunState.STARTING)
    assert (store.get_run(_RUN_A), _saved(store).digest) == (ended, "digest")

    assert claim(max_active=2, state=WorkflowRunState.STARTING).state is WorkflowRunState.STARTING


_FENCED_WRITES: dict[str, Callable[[WorkflowRunStore, WorkflowNode, int], object]] = {
    "update_run": lambda store, node, epoch: store.update_run(node.run_id, epoch, result="late"),
    "end": lambda store, node, epoch: store.update_run(node.run_id, epoch, state=WorkflowRunState.COMPLETED),
    "put_node": lambda store, node, epoch: store.put_node(replace(node, generation=2, digest="late"), epoch),
    "update_node": lambda store, node, epoch: store.update_node(node.run_id, node.key, 1, epoch, attempt=9),
    "settle_node": lambda store, node, epoch: store.settle_node(
        node.run_id,
        node.key,
        1,
        epoch,
        state=WorkflowNodeState.COMPLETED,
        result="late",
        dirty_after=True,
    ),
    "mark_stale": lambda store, node, epoch: store.mark_stale(node.run_id, epoch),
    "append_log": lambda store, node, epoch: store.append_log(node.run_id, epoch, "late"),
}


@pytest.mark.parametrize("write", _FENCED_WRITES)
@pytest.mark.parametrize("moved_on", ["next_attempt", "ended"])
def test_every_write_of_an_attempt_is_refused_once_its_run_moved_on(tmp_path: Path, write: str, moved_on: str) -> None:
    """A write names the epoch it was decided on: after the run began another attempt or ended, it changes nothing."""
    store, node = _store_with_node(tmp_path)
    store.put_node(_node("@1", attempt=0, state=WorkflowNodeState.COMPLETED), 0)
    held = set_run(store, _RUN_A, state=WorkflowRunState.RUNNING).epoch
    if moved_on == "next_attempt":
        set_run(store, _RUN_A, state=WorkflowRunState.STARTING)
        set_run(store, _RUN_A, state=WorkflowRunState.RUNNING)
    else:
        # The epoch alone does not open an ended run: also a write that names it is refused.
        with sqlite3.connect(store.database_path) as connection:
            connection.execute("UPDATE workflow_runs SET state = 'failed'")
    before = store.get_run(_RUN_A), store.nodes(_RUN_A, include_stale=True), store.log(_RUN_A)

    with pytest.raises(SupersededAttemptError):
        _FENCED_WRITES[write](store, node, held)

    assert (store.get_run(_RUN_A), store.nodes(_RUN_A, include_stale=True), store.log(_RUN_A)) == before


_EXPECTED_DIRTY_AFTER: dict[str, set[str]] = {
    "@0": {"@1", "@2", "@3", "/1:a@0", "/1:a@1", "/1:b@0", "/1:b/0:x@0", "/1:b/0:x@1", "/1:b@1", "/3:a@0"},
    "@1": {"@2", "@3", "/3:a@0"},
    "@2": {"@3", "/3:a@0"},
    "@3": set(),
    "/1:a@0": {"/1:a@1", "@2", "@3", "/3:a@0"},
    "/1:a@1": {"@2", "@3", "/3:a@0"},
    "/1:b@0": {"/1:b@1", "@2", "@3", "/3:a@0"},
    "/1:b/0:x@0": {"/1:b/0:x@1", "/1:b@1", "@2", "@3", "/3:a@0"},
    "/1:b/0:x@1": {"/1:b@1", "@2", "@3", "/3:a@0"},
    "/1:b@1": {"@2", "@3", "/3:a@0"},
    "/3:a@0": set(),
}


@pytest.mark.parametrize(("ran", "expected"), list(_EXPECTED_DIRTY_AFTER.items()))
def test_call_that_runs_makes_exactly_what_follows_it_in_earlier_attempts_run_again(
    tmp_path: Path,
    ran: str,
    expected: set[str],
) -> None:
    """The store's one write marks earlier-attempt rows after *ran* dirty, live rows included.

    Not a row of the call's own attempt, not an explicit rerun (''), and not an indeterminate call.
    """
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    store.reserve_run(_run())
    for key in _EXPECTED_DIRTY_AFTER:
        store.put_node(_node(key, state=WorkflowNodeState.COMPLETED, attempt=1), 0)
    store.put_node(_node("@7", state=WorkflowNodeState.WAITING, attempt=1), 0)
    store.put_node(replace(_node("@8", state=WorkflowNodeState.COMPLETED, attempt=1), digest=""), 0)
    store.put_node(_node("@9", state=WorkflowNodeState.INDETERMINATE, attempt=1), 0)
    store.put_node(_node("@10", state=WorkflowNodeState.COMPLETED, attempt=2), 0)

    store.put_node(replace(_saved(store, ran), generation=2, attempt=2), 0, dirty_after=True)

    marked = {node.key for node in store.nodes(_RUN_A) if node.digest == DIRTY_DIGEST}
    # Every key in _EXPECTED_DIRTY_AFTER precedes @7 in the root lane except @3 and /3:a@0? No, @3 and /3:a@0 also precede @7!
    assert marked == expected | {"@7"}
    assert _saved(store, "@8").digest == ""
    assert _saved(store, "@9").digest == "digest"
    assert _saved(store, "@10").digest == "digest"


def test_first_attempt_and_a_clean_stamp_blank_nothing(tmp_path: Path) -> None:
    """Rows of the attempt that writes are never earlier ones, and a write without `dirty_after` leaves digests alone."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    store.reserve_run(_run())
    for key in ("@0", "@1"):
        store.put_node(_node(key, state=WorkflowNodeState.COMPLETED, attempt=1), 0, dirty_after=True)
    store.put_node(_node("@2", state=WorkflowNodeState.RUNNING, attempt=2), 0)
    assert store.update_node(_RUN_A, "@0", 1, 0, attempt=2)
    assert store.settle_node(_RUN_A, "@2", 1, 0, state=WorkflowNodeState.COMPLETED)

    assert [node.digest for node in store.nodes(_RUN_A)] == ["digest"] * 3
    # The stamp that says the group changed marks what follows it dirty, in its own write.
    assert store.update_node(_RUN_A, "@0", 1, 0, dirty_after=True, attempt=3)
    assert [node.digest for node in store.nodes(_RUN_A)] == ["digest", DIRTY_DIGEST, DIRTY_DIGEST]


def test_prune_keeps_the_run_of_a_job_that_has_not_saved_its_report(tmp_path: Path) -> None:
    """An old run stays while its job still has to deliver it, and every run of a job while nobody can say which do."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    for run_id, job in ((_RUN_A, "job-a"), (_RUN_B, "job-b"), (_RUN_C, None)):
        store.reserve_run(_run(run_id, job_id=job))
        set_run(store, run_id, state=WorkflowRunState.COMPLETED, finished_at="2026-11-01T00:00:00Z")

    assert store.prune("2026-11-05T00:00:00Z", None) == 1
    assert store.prune("2026-11-05T00:00:00Z", {"job-a"}) == 1
    assert [run.run_id for run in store.list_runs()] == [_RUN_A]
    assert store.prune("2026-11-05T00:00:00Z", set()) == 1


def test_report_that_was_accepted_is_delivered_in_a_form_that_is_no_larger_than_it_came(tmp_path: Path) -> None:
    """A small report reads indented; one whose indentation would outgrow what a job's result may hold stays compact."""
    del tmp_path
    small = {"items": [1, 2]}
    deep: object = list(range(40_000))
    for _ in range(12):
        deep = [deep]
    done = WorkflowRunState.COMPLETED

    assert _run(state=done, result=small).outcome_text() == json.dumps(small, indent=2)
    text = _run(state=done, result=deep).outcome_text()
    assert json.loads(text) == deep
    assert len(text) <= len(json.dumps(deep))


def test_nodes_for_a_view_come_without_their_requests_and_results(tmp_path: Path) -> None:
    """What a run's view reads of a call is all but its request and its result, which can be megabytes each."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    store.reserve_run(_run())
    store.put_node(_node("@0", kind="group", request={"op": "foreach"}, members=("/0:a",), label="foreach"), 0)
    call = _node("/0:a@0", request={"prompt": "p" * 10_000}, result="r" * 10_000, links={"job_id": "job"})
    store.put_node(replace(call, state=WorkflowNodeState.FAILED, error={"kind": "failed", "message": "boom"}), 0)

    group, light = store.nodes(_RUN_A, light=True)

    assert (group.request, group.members, group.label) == ({"op": "foreach"}, None, "foreach")
    assert (light.request, light.result) == ({"op": None}, None)
    assert (light.state, light.error, light.links) == (
        call.state.FAILED,
        {"kind": "failed", "message": "boom"},
        call.links,
    )
    assert store.nodes(_RUN_A)[1].request == call.request


def test_every_write_of_a_run_tells_who_listens_which_run_changed(tmp_path: Path) -> None:
    """Each write that changes what a run shows names the run, after it is saved; a refused write names none."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    store.reserve_run(_run())
    changed: list[str] = []
    store.on_change = changed.append

    store.put_node(_node("@0"), 0)
    store.update_node(_RUN_A, "@0", 1, 0, state=WorkflowNodeState.RUNNING)
    store.settle_node(_RUN_A, "@0", 1, 0, state=WorkflowNodeState.COMPLETED, result=1)
    store.add_usage(_RUN_A, "@0", 1, 1, 1, {"cost_usd": 0.5})
    store.append_log(_RUN_A, 0, "a line")
    store.mark_stale(_RUN_A, 0)
    store.update_run(_RUN_A, 0, large=True)
    with pytest.raises(SupersededAttemptError):
        store.append_log(_RUN_A, 7, "of an attempt the run left")

    assert changed == [_RUN_A] * 7
    assert store.get_run(_RUN_A).large is True


def test_turn_costs_are_those_of_the_runs_own_priced_turns(tmp_path: Path) -> None:
    """The cost of each model turn of a run's agents is listed by itself; a subagent's and an unpriced one are not."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    store.reserve_run(_run())
    for turn, cost in ((1, 0.6), (2, 0.0), ("cut:run", 0.3), ("sub:del", 9.0), (3, None)):
        store.add_usage(_RUN_A, "@0", 1, turn, 1, {"cost_usd": cost})

    assert sorted(store.usage_summary(_RUN_A).turn_costs) == [0.0, 0.3, 0.6]


def test_v1_to_v2_migration_adds_attempt_column_and_scopes_log_queries(tmp_path: Path) -> None:
    """Opening a v1 database adds workflow_log.attempt, backfills it from workflow_runs, and scopes logs by attempt."""
    path = tmp_path / "v1.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE workflow_schema (version INTEGER NOT NULL)")
        store_module._create_schema(conn)
        conn.execute("INSERT INTO workflow_schema (version) VALUES (1)")
        conn.commit()
    # Populate v1 runs at attempt 1 (unambiguous) and attempt 2 (ambiguous) with pre-upgrade log rows.
    with sqlite3.connect(path) as conn:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(workflow_log)")]
        assert "attempt" not in cols
        for run, line in ((_run(_RUN_A, attempt=1), "legacy attempt 1"), (_run(_RUN_B, attempt=2), "legacy multi")):
            values = {
                **store_module._row_values(store_module.asdict(run), store_module._RUN_JSON_FIELDS),
                "reservation_digest": "d",
            }
            store_module._insert(conn, "INSERT INTO workflow_runs", values)
            conn.execute(
                "INSERT INTO workflow_log (run_id, created_at, text) VALUES (?, ?, ?)",
                (run.run_id, "2026-01-01T00:00:00Z", line),
            )
        conn.commit()

    store = WorkflowRunStore(path)
    assert _schema_version(path) == 2
    with sqlite3.connect(path) as conn:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(workflow_log)")]
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "attempt" in cols
    assert "workflow_log_run_attempt_idx" in indexes
    assert store.log(_RUN_A, attempt=1) == ["legacy attempt 1"]
    assert store.log(_RUN_A, attempt=0) == []
    # Ambiguous multi-attempt v1 runs keep pre-upgrade rows at attempt 0 rather than misattributing to attempt 2.
    assert store.log(_RUN_B, attempt=0) == ["legacy multi"]
    assert store.log(_RUN_B, attempt=2) == []

    store.append_log(_RUN_A, store.get_run(_RUN_A).epoch, "attempt 1 after upgrade")
    set_run(store, _RUN_A, attempt=2)
    store.append_log(_RUN_A, store.get_run(_RUN_A).epoch, "attempt 2")

    assert store.log(_RUN_A) == ["legacy attempt 1", "attempt 1 after upgrade", "attempt 2"]
    assert store.log(_RUN_A, attempt=1) == ["legacy attempt 1", "attempt 1 after upgrade"]
    assert store.log(_RUN_A, attempt=2) == ["attempt 2"]


def test_usage_summary_scopes_by_node_to_current_generations_while_keeping_totals(tmp_path: Path) -> None:
    """Per-node usage filters to current generations while run totals keep full multi-attempt spend."""
    store, _ = _store_with_node(tmp_path)
    store.add_usage(_RUN_A, "@0", 1, 1, 1, {"input_tokens": 100, "cost_usd": 0.50})
    store.add_usage(_RUN_A, "@0", 2, 1, 2, {"input_tokens": 50, "cost_usd": 0.25})
    store.add_usage(_RUN_A, "@1", 1, 1, 1, {"input_tokens": 20, "cost_usd": 0.10})
    store.add_usage(_RUN_A, "@2", 1, 1, 1, {"input_tokens": 40, "cost_usd": 0.40})

    scoped = store.usage_summary(_RUN_A, attempt=2, current_generations={"@0": 2, "@1": 1})
    assert scoped.totals["cost_usd"] == pytest.approx(1.25)
    assert scoped.totals["input_tokens"] == 210
    assert scoped.attempt_cost_usd == pytest.approx(0.25)
    assert set(scoped.by_node) == {"@0", "@1"}
    assert scoped.by_node["@0"]["cost_usd"] == pytest.approx(0.25)
    assert scoped.by_node["@0"]["input_tokens"] == 50
    assert scoped.by_node["@1"]["cost_usd"] == pytest.approx(0.10)
    # Default (current_generations=None) preserves full per-key sum.
    assert store.usage_summary(_RUN_A).by_node["@0"]["cost_usd"] == pytest.approx(0.75)


def test_node_previews_extracts_bounded_text_shell_and_json_and_skips_state_and_large_blobs(tmp_path: Path) -> None:
    """node_previews redacts structured secrets, survives bad UTF-8 surrogates, and skips state/null/stale/>16 KiB blobs."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    store.reserve_run(_run())
    cases: list[tuple[str, str, WorkflowNodeState, object, bool]] = [
        ("@0", "agent", WorkflowNodeState.COMPLETED, "  hello " + "a" * 80, False),
        ("@1", "tool", WorkflowNodeState.COMPLETED, {"output": "shell stdout\n", "exit_code": 0}, False),
        ("@2", "turn", WorkflowNodeState.COMPLETED, {"id": 9007199254740993, "ok": True}, False),
        ("@3", "agent", WorkflowNodeState.COMPLETED, {"name": "PASSWORD", "value": "example-private-value"}, False),
        ("@4", "state", WorkflowNodeState.COMPLETED, "secret shared state", False),
        ("@5", "tool", WorkflowNodeState.COMPLETED, {"blob": "x" * 20_000}, False),
        ("@6", "agent", WorkflowNodeState.COMPLETED, None, False),
        ("@7", "agent", WorkflowNodeState.COMPLETED, {"text": "y" * (9 * 1024 * 1024)}, False),
        ("@8", "agent", WorkflowNodeState.COMPLETED, "stale text", True),
        ("@9", "agent", WorkflowNodeState.RUNNING, None, False),
        ("@10", "agent", WorkflowNodeState.COMPLETED, "surrogate \ud800 ok", False),
        ("@11", "agent", WorkflowNodeState.COMPLETED, "raw bad utf8 placeholder", False),
        ("@12", "agent", WorkflowNodeState.COMPLETED, "malformed json placeholder", False),
        (
            "@13",
            "tool",
            WorkflowNodeState.COMPLETED,
            {"output": '{"name": "PASSWORD", "value": "example-private-value"}', "exit_code": 0},
            False,
        ),
    ]
    for key, kind, state, result, stale in cases:
        store.put_node(_node(key=key, kind=kind, state=WorkflowNodeState.RUNNING, stale=stale), 0)
        if state is WorkflowNodeState.COMPLETED:
            store.settle_node(_RUN_A, key, 1, 0, state=state, result=result)
    # Inject raw invalid UTF-8 surrogate bytes and malformed JSON directly into SQLite to verify resilience.
    with sqlite3.connect(store.database_path) as conn:
        conn.execute(
            "UPDATE workflow_nodes SET result_json = CAST(? AS TEXT) WHERE run_id = ? AND key = '@11'",
            (b'"\xed\xa0\x80raw"', _RUN_A),
        )
        conn.execute("UPDATE workflow_nodes SET result_json = '{bad' WHERE run_id = ? AND key = '@12'", (_RUN_A,))
        conn.commit()

    previews = store.node_previews(_RUN_A, max_chars=80)
    assert set(previews) == {"@0", "@1", "@2", "@3", "@10", "@11", "@13"}
    assert "example-private-value" not in previews["@13"]
    assert previews["@13"] == '{"name":"PASSWORD","value":"***redacted***"}'
    assert previews["@0"].startswith("hello ")
    assert len(previews["@0"]) <= 80
    assert previews["@1"] == "shell stdout"
    assert previews["@2"] == '{"id":9007199254740993,"ok":true}'
    assert "example-private-value" not in previews["@3"]
    assert previews["@3"] == '{"name":"PASSWORD","value":"***redacted***"}'
    assert previews["@10"] == "surrogate ? ok"
    assert previews["@10"].encode("utf-8")
    assert "\ufffd" in previews["@11"]
    assert previews["@11"].encode("utf-8")
    assert previews["@11"].endswith("raw")
    # Passing node_previews through graph_rows keeps structured secret masking intact.
    rows = {row["id"]: row for row in graph_rows(store.nodes(_RUN_A, light=True), {}, [], [], previews=previews).nodes}
    assert "example-private-value" not in rows["n4"]["preview"]
    assert rows["n4"]["preview"] == '{"name":"PASSWORD","value":"***redacted***"}'
    assert store.node_previews(_RUN_A, max_chars=0) == {}


def test_octet_length_fallback_registers_on_older_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On SQLite < 3.43, connections register a Python octet_length fallback that handles None, bytes, and UTF-8."""
    assert store_module._octet_length(None) is None
    assert store_module._octet_length(b"abc") == 3
    assert store_module._octet_length("検査") == 6
    assert store_module._octet_length("\ud800") == 3

    monkeypatch.setattr(store_module, "_OCTET_LENGTH_VERSION", (99, 0, 0))
    store, _ = _store_with_node(tmp_path)
    store.settle_node(_RUN_A, "@0", 1, 0, state=WorkflowNodeState.COMPLETED, result="fallback ok")
    assert store.node_previews(_RUN_A) == {"@0": "fallback ok"}
