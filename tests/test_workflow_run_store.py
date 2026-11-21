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
from typing import TYPE_CHECKING

import pytest

from mindroom.workflow_runs import store as store_module
from mindroom.workflow_runs.models import (
    DispatchHandle,
    WorkflowNode,
    WorkflowNodeState,
    WorkflowRun,
    WorkflowRunState,
    new_workflow_run_id,
    validated_workflow_run_id,
)
from mindroom.workflow_runs.store import (
    WorkflowRunConflictError,
    WorkflowRunNotFoundError,
    WorkflowRunStore,
    WorkflowRunStoreError,
)

if TYPE_CHECKING:
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
    return store, store.put_node(_node(generation=generation, state=WorkflowNodeState.RUNNING))


def _saved(store: WorkflowRunStore, key: str = "@0", run_id: str = _RUN_A) -> WorkflowNode:
    node = store.get_node(run_id, key)
    assert node is not None
    return node


def test_put_node_assigns_stable_sequence(tmp_path: Path) -> None:
    """A new key gets the next sequence number, and writing the key again keeps it."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    run, _ = store.reserve_run(_run(origin_job_id="job-1"))
    first, second = store.put_node(_node(key="@0")), store.put_node(_node(key="@1"))
    again = store.put_node(
        replace(first, generation=2, state=WorkflowNodeState.COMPLETED, result={"big": "x" * 200_000}),
    )
    assert (first.seq, second.seq, again.seq) == (1, 2, 1)
    assert _saved(store).request == first.request
    assert _saved(store).result == {"big": "x" * 200_000}
    assert store.node_by_seq(run.run_id, 2) == second
    assert store.node_by_seq(run.run_id, 3) is None
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
            group_key="@0",
            members=("/0:ab12", "/0:cd34"),
            session_key="@1",
            cached=False,
            started_at="2026-11-19T10:00:00Z",
            error={"kind": "tool_failed", "message": "placeholder"},
            links={"job_id": "job-7", "event_id": "$card"},
        ),
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
    resumed = store.update_run(
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
        store.update_run(_RUN_A, origin_job_id="job-2")
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
        store.update_run(_RUN_A, **{field: column_value})
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
        nodes = list(pool.map(lambda number: store.put_node(_node(key=f"@{number}", run_id=winner)), range(16)))
    assert [created for _run_record, created in reserved].count(True) == 1
    assert {run.run_id for run, _created in reserved} == {winner}
    assert [run.run_id for run in store.list_runs()] == [winner]
    assert sorted(node.seq for node in nodes) == list(range(1, 17))


def test_delete_intent_removes_only_a_run_that_never_started(tmp_path: Path) -> None:
    """An intent is a starting run with attempt 0; anything further along stays."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    store.reserve_run(_run(_RUN_A))
    store.reserve_run(_run(_RUN_B))
    store.update_run(_RUN_B, attempt=1)
    assert store.delete_intent(_RUN_A)
    assert not store.delete_intent(_RUN_A)
    assert not store.delete_intent(_RUN_B)
    assert [run.run_id for run in store.list_runs()] == [_RUN_B]


def test_update_run_changes_named_fields_only(tmp_path: Path) -> None:
    """An update writes the given fields and returns the stored run."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    run, _ = store.reserve_run(_run())
    updated = store.update_run(
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
        attempt=1,
        script_run_id="script-" + "1" * 32,
        result={"answer": [1, 2]},
        finished_at="2026-11-19T12:00:00Z",
    )
    assert store.get_run(run.run_id) == updated
    assert store.run_for_script("script-" + "1" * 32) == updated
    assert store.run_for_script("script-" + "2" * 32) is None
    with pytest.raises(WorkflowRunNotFoundError):
        store.update_run(_RUN_B, attempt=1)
    with pytest.raises(WorkflowRunStoreError, match="no_such_field"):
        store.update_run(run.run_id, no_such_field=1)


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
    store.update_run(_RUN_B, state=WorkflowRunState.FAILED)

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
    assert not store.settle_node(node.run_id, node.key, 1, state=WorkflowNodeState.COMPLETED, result="late")
    assert _saved(store).state is WorkflowNodeState.RUNNING
    assert _saved(store) == node


def test_settle_records_the_end_of_the_current_generation(tmp_path: Path) -> None:
    """The current generation's result or error is stored with its end time."""
    store, node = _store_with_node(tmp_path)
    assert store.settle_node(node.run_id, node.key, 1, state=WorkflowNodeState.COMPLETED, result={"lines": 1})
    settled = _saved(store)
    assert (settled.state, settled.result, settled.error) == (WorkflowNodeState.COMPLETED, {"lines": 1}, None)
    assert settled.ended_at is not None
    assert replace(settled, state=node.state, result=None, ended_at=None) == node

    failed = store.put_node(_node(key="@1", state=WorkflowNodeState.WAITING, waiting_on="user"))
    error = {"kind": "indeterminate", "message": "placeholder"}
    assert store.settle_node(failed.run_id, failed.key, 1, state=WorkflowNodeState.INDETERMINATE, error=error)
    assert (_saved(store, "@1").error, _saved(store, "@1").waiting_on) == (error, None)
    assert not store.settle_node(node.run_id, "@9", 1, state=WorkflowNodeState.COMPLETED)


def _two_stores(tmp_path: Path) -> tuple[WorkflowRunStore, WorkflowRunStore]:
    """Two writers on one database, as an engine and a late callback are."""
    path = tmp_path / "w.sqlite3"
    first = WorkflowRunStore(path)
    first.reserve_run(_run())
    return first, WorkflowRunStore(path)


def test_old_snapshot_cannot_replace_or_settle_a_newer_generation(tmp_path: Path) -> None:
    """A writer that read generation 1 changes nothing once generation 2 is claimed."""
    engine, late = _two_stores(tmp_path)
    claimed = engine.put_node(_node(state=WorkflowNodeState.RUNNING))
    snapshot = _saved(late)
    newer = engine.put_node(replace(claimed, generation=2, digest="edited"))

    with pytest.raises(WorkflowRunConflictError, match="generation 2"):
        late.put_node(replace(snapshot, attempt=2))
    assert not late.update_node(snapshot.run_id, snapshot.key, 1, attempt=2)
    assert not late.settle_node(snapshot.run_id, snapshot.key, 1, state=WorkflowNodeState.COMPLETED, result="late")
    assert _saved(engine) == newer


def test_a_key_and_each_generation_are_claimed_once(tmp_path: Path) -> None:
    """Of two writers that claim the same new key or the same next generation, the second is refused."""
    engine, other = _two_stores(tmp_path)
    first = engine.put_node(_node())
    with pytest.raises(WorkflowRunConflictError):
        other.put_node(_node(digest="other"))
    second = engine.put_node(replace(first, generation=2))
    for generation in (1, 2, 4):
        with pytest.raises(WorkflowRunConflictError):
            other.put_node(replace(first, generation=generation, digest="other"))
    assert _saved(engine) == second
    assert (second.seq, second.digest) == (first.seq, "digest")


def test_late_receipt_never_replaces_a_definitive_outcome(tmp_path: Path) -> None:
    """Once a generation has a proven outcome, a second settlement of it is a no-op."""
    engine, late = _two_stores(tmp_path)
    node = engine.put_node(_node(state=WorkflowNodeState.RUNNING))
    assert engine.settle_node(node.run_id, node.key, 1, state=WorkflowNodeState.COMPLETED, result="done")
    settled = _saved(engine)
    error = {"kind": "indeterminate", "message": "placeholder"}
    assert not late.settle_node(node.run_id, node.key, 1, state=WorkflowNodeState.INDETERMINATE, error=error)
    assert not late.settle_node(node.run_id, node.key, 1, state=WorkflowNodeState.COMPLETED, result="other")
    assert not late.settle_node(node.run_id, node.key, 1, state=WorkflowNodeState.FAILED, error=error)
    assert _saved(late) == settled


def test_late_receipt_resolves_an_indeterminate_node(tmp_path: Path) -> None:
    """A call whose end was unknown takes the proven outcome when its receipt does arrive."""
    engine, late = _two_stores(tmp_path)
    node = engine.put_node(_node(state=WorkflowNodeState.RUNNING))
    error = {"kind": "indeterminate", "message": "placeholder"}
    assert engine.settle_node(node.run_id, node.key, 1, state=WorkflowNodeState.INDETERMINATE, error=error)
    assert not engine.settle_node(node.run_id, node.key, 1, state=WorkflowNodeState.INDETERMINATE, error={})
    assert _saved(engine).error == error
    assert late.settle_node(node.run_id, node.key, 1, state=WorkflowNodeState.COMPLETED, result="done")
    resolved = _saved(engine)
    assert (resolved.state, resolved.result, resolved.error) == (WorkflowNodeState.COMPLETED, "done", None)
    with pytest.raises(WorkflowRunStoreError, match="waiting"):
        engine.settle_node(node.run_id, node.key, 1, state=WorkflowNodeState.WAITING)


def test_stamps_change_named_fields_and_never_an_outcome(tmp_path: Path) -> None:
    """A stamp updates the current generation's fields, and cannot reopen or rewrite a node that ended."""
    store, node = _store_with_node(tmp_path)
    handle = DispatchHandle("job", "job-7", dispatched=True)
    stamp = {"state": WorkflowNodeState.WAITING, "waiting_on": "user", "handle": handle, "links": {"job_id": "job-7"}}
    assert store.update_node(node.run_id, node.key, 1, **stamp)
    assert _saved(store) == replace(node, **stamp)
    assert not store.update_node(node.run_id, "@9", 1, attempt=2)

    assert store.settle_node(node.run_id, node.key, 1, state=WorkflowNodeState.COMPLETED, result={"lines": 1})
    settled = _saved(store)
    assert store.update_node(node.run_id, node.key, 1, attempt=2, stale=True, members=("/0:a", "/0:b"))
    assert _saved(store) == replace(settled, attempt=2, stale=True, members=("/0:a", "/0:b"))
    assert not store.update_node(node.run_id, node.key, 1, state=WorkflowNodeState.RUNNING, attempt=3)
    assert _saved(store).attempt == 2
    for outcome in ({"state": WorkflowNodeState.FAILED}, {"result": "x"}, {"error": {}}, {"digest": "x"}, {}):
        with pytest.raises(WorkflowRunStoreError):
            store.update_node(node.run_id, node.key, 1, **outcome)


def test_stale_nodes_leave_the_current_view(tmp_path: Path) -> None:
    """Nodes are listed in sequence order, and stale ones only on request."""
    store, first = _store_with_node(tmp_path)
    second, third = store.put_node(_node(key="@1")), store.put_node(_node(key="/1:ab@0"))
    store.mark_stale(first.run_id, ["@1", "@7"])
    assert store.nodes(first.run_id) == [first, third]
    assert store.nodes(first.run_id, include_stale=True) == [first, replace(second, stale=True), third]
    assert store.nodes(_RUN_B) == []


def test_usage_is_append_only_and_deduplicated(tmp_path: Path) -> None:
    """One entry per run, node, generation and turn; a repeat changes nothing."""
    store, node = _store_with_node(tmp_path)
    assert store.add_usage(node.run_id, node.key, 1, 1, 1, {"cost_usd": 0.5, "input_tokens": 10})
    assert not store.add_usage(node.run_id, node.key, 1, 1, 1, {"cost_usd": 0.5, "input_tokens": 10})
    store.add_usage(node.run_id, node.key, 2, 1, 2, {"cost_usd": 0.25})  # a rerun in attempt 2
    assert store.usage_totals(node.run_id)["cost_usd"] == 0.75
    assert store.usage_totals(node.run_id, attempt=2)["cost_usd"] == 0.25
    assert store.usage_totals(node.run_id) == {
        "input_tokens": 10,
        "output_tokens": 0,
        "tool_calls": 0,
        "cost_usd": 0.75,
        "cost_unknown": False,
    }


def test_unknown_price_marks_totals_unknown(tmp_path: Path) -> None:
    """A turn without a price makes the sum a lower bound, for its node and for the run."""
    store, node = _store_with_node(tmp_path)
    store.put_node(_node(key="@1"))
    store.add_usage(node.run_id, "@0", 1, 1, 1, {"cost_usd": 0.5, "output_tokens": 7, "tool_calls": 2})
    store.add_usage(node.run_id, "@1", 1, 1, 1, {"input_tokens": 3, "output_tokens": 4})
    store.add_usage(node.run_id, "@1", 1, 2, 1, {"input_tokens": 1, "cost_usd": 0.125})
    priced = {"input_tokens": 0, "output_tokens": 7, "tool_calls": 2, "cost_usd": 0.5, "cost_unknown": False}
    unpriced = {"input_tokens": 4, "output_tokens": 4, "tool_calls": 0, "cost_usd": 0.125, "cost_unknown": True}
    assert store.node_usage(node.run_id) == {"@0": priced, "@1": unpriced}
    assert store.usage_totals(node.run_id) == {
        "input_tokens": 4,
        "output_tokens": 11,
        "tool_calls": 2,
        "cost_usd": 0.625,
        "cost_unknown": True,
    }
    assert store.usage_totals(_RUN_B) == {
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
    assert store.settle_node(node.run_id, node.key, 1, state=WorkflowNodeState.COMPLETED, result=big)
    settled = _saved(store)
    assert settled.result_file == f"results/{node.run_id}/1-1.json"
    spilled = store.directory / settled.result_file
    assert json.loads(spilled.read_text()) == big
    assert isinstance(settled.result, str)
    assert len(settled.result) == 64 * 1024
    assert settled.result.startswith('{"text":"xxx')

    assert store.update_node(node.run_id, node.key, 1, attempt=2)
    settled = replace(settled, attempt=2)
    assert _saved(store) == settled
    assert json.loads(spilled.read_text()) == big

    bigger = {"text": "é" * (5 * 1024 * 1024)}
    rerun = store.put_node(replace(settled, generation=2, result=bigger, result_file=None))
    assert rerun.result_file == f"results/{node.run_id}/1-2.json"
    assert isinstance(rerun.result, str)
    assert len(rerun.result.encode()) <= 64 * 1024

    small = store.put_node(replace(rerun, generation=3, result="small", result_file=None))
    assert (small.result, small.result_file) == ("small", None)

    store.update_run(node.run_id, state=WorkflowRunState.COMPLETED, finished_at="2026-11-01T00:00:00Z")
    assert store.prune("2026-11-02T00:00:00Z") == 1
    assert not spilled.exists()
    assert not (store.directory / "results" / node.run_id).exists()


def test_settling_again_never_touches_a_committed_spill(tmp_path: Path) -> None:
    """A row that names a spill file keeps that file and its preview, whatever a later settlement brings."""
    store, node = _store_with_node(tmp_path)
    first = {"text": "a" * (9 * 1024 * 1024)}
    assert store.settle_node(node.run_id, node.key, 1, state=WorkflowNodeState.COMPLETED, result=first)
    settled = _saved(store)
    assert settled.result_file is not None
    spilled = store.directory / settled.result_file

    second = {"text": "b" * (9 * 1024 * 1024)}
    assert not store.settle_node(node.run_id, node.key, 1, state=WorkflowNodeState.COMPLETED, result=second)
    assert not store.settle_node(node.run_id, node.key, 1, state=WorkflowNodeState.FAILED, error={"kind": "late"})
    assert json.loads(spilled.read_text()) == first
    assert _saved(store) == settled


def test_failed_spill_write_publishes_no_file_and_no_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spill appears under its name only whole, and the row names it only after that."""
    store, first = _store_with_node(tmp_path)
    big = {"text": "a" * (9 * 1024 * 1024)}
    assert store.settle_node(first.run_id, first.key, 1, state=WorkflowNodeState.COMPLETED, result=big)
    node = store.put_node(_node(key="@1", state=WorkflowNodeState.RUNNING))
    results = store.directory / "results" / node.run_id

    def refuse(_fd: int) -> None:
        msg = "placeholder disk failure"
        raise OSError(msg)

    with monkeypatch.context() as patched:
        patched.setattr("os.fsync", refuse)
        with pytest.raises(OSError, match="placeholder disk failure"):
            store.settle_node(node.run_id, node.key, 1, state=WorkflowNodeState.COMPLETED, result=big)
    assert [path.name for path in results.iterdir()] == ["1-1.json"]
    assert _saved(store, "@1") == node

    assert store.settle_node(node.run_id, node.key, 1, state=WorkflowNodeState.COMPLETED, result=big)
    assert json.loads((results / "2-1.json").read_text()) == big
    assert sorted(path.name for path in results.iterdir()) == ["1-1.json", "2-1.json"]


def test_prune_removes_only_old_finished_runs(tmp_path: Path) -> None:
    """Prune takes a finished run's rows and leaves newer and unfinished runs whole."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    for run_id in (_RUN_A, _RUN_B, _RUN_C):
        store.reserve_run(_run(run_id))
        store.put_node(_node(run_id=run_id))
        store.add_usage(run_id, "@0", 1, 1, 1, {"cost_usd": 1.0})
        store.append_log(run_id, "placeholder line")
    store.update_run(_RUN_A, state=WorkflowRunState.CANCELLED, finished_at="2026-11-01T00:00:00Z")
    store.update_run(_RUN_B, state=WorkflowRunState.COMPLETED, finished_at="2026-11-10T00:00:00Z")
    # A resumed run keeps the end time of its earlier attempt and must not be pruned while it runs.
    store.update_run(_RUN_C, state=WorkflowRunState.RUNNING, finished_at="2026-11-01T00:00:00Z")

    assert store.prune("2026-11-05T00:00:00Z") == 1
    assert sorted(run.run_id for run in store.list_runs()) == [_RUN_B, _RUN_C]
    assert (store.nodes(_RUN_A), store.log(_RUN_A)) == ([], [])
    assert store.usage_totals(_RUN_A)["cost_usd"] == 0.0
    for run_id in (_RUN_B, _RUN_C):
        assert len(store.nodes(run_id)) == 1
        assert store.usage_totals(run_id)["cost_usd"] == 1.0
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
        store.update_run(run_id, state=WorkflowRunState.COMPLETED, finished_at=finished_at)
    assert store.prune("2026-11-05T00:00:00Z") == 1
    assert sorted(run.run_id for run in store.list_runs()) == [_RUN_B, _RUN_C]
    assert store.prune("2026-11-05T00:00:00.500001Z") == 2


def test_log_returns_the_last_lines_in_order(tmp_path: Path) -> None:
    """The log is per run and reads back oldest first, capped to the newest lines."""
    store = WorkflowRunStore(tmp_path / "w.sqlite3")
    store.reserve_run(_run(_RUN_A))
    store.reserve_run(_run(_RUN_B))
    for number in range(4):
        store.append_log(_RUN_A, f"line {number}")
    store.append_log(_RUN_B, "other run")
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
    assert _schema_version(path) == store_module.SCHEMA_VERSION == 1

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
    assert _schema_version(path) == 3
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT placeholder FROM workflow_runs").fetchall() == [("migrated",)]

    fresh = tmp_path / "fresh.sqlite3"
    WorkflowRunStore(fresh)
    assert calls == ["second", "third", "second", "third"]
    assert _schema_version(fresh) == 3


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
    assert _schema_version(path) == 1
    with sqlite3.connect(path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(workflow_runs)")]
    assert "placeholder" not in columns
