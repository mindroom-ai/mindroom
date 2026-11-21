"""Tests for the replay rules of workflow runs.

Level: pure (functions with no I/O).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from mindroom.workflow_runs.models import WorkflowNode, WorkflowNodeState
from mindroom.workflow_runs.replay import (
    Decision,
    DirtyLanes,
    decide,
    node_digest,
    parent_group,
    split_key,
    stale_keys,
)


def _node(key: str, digest: str, attempt: int, state: WorkflowNodeState, generation: int = 1) -> WorkflowNode:
    lane, idx = split_key(key)
    return WorkflowNode(
        run_id="wf-" + "a" * 32,
        key=key,
        kind="tool",
        lane=lane,
        idx=idx,
        digest=digest,
        request={},
        attempt=attempt,
        state=state,
        generation=generation,
    )


def _done(key: str, digest: str, attempt: int) -> WorkflowNode:
    return _node(key, digest, attempt, WorkflowNodeState.COMPLETED)


def _failed_node(key: str, digest: str, attempt: int) -> WorkflowNode:
    return _node(key, digest, attempt, WorkflowNodeState.FAILED)


def _waiting(key: str, digest: str, attempt: int) -> WorkflowNode:
    return _node(key, digest, attempt, WorkflowNodeState.WAITING)


def _running(key: str, digest: str, attempt: int) -> WorkflowNode:
    return _node(key, digest, attempt, WorkflowNodeState.RUNNING)


def _indeterminate(key: str, digest: str, attempt: int) -> WorkflowNode:
    return _node(key, digest, attempt, WorkflowNodeState.INDETERMINATE)


def test_same_attempt_returns_the_node_even_when_it_failed() -> None:
    """A redelivered request of the attempt that made the node gets that node, whatever its state."""
    node = _failed_node("@0", "d", attempt=2)
    assert decide(node, "d", attempt=2, dirty=False, rerun=False, cached=True).action == "return"
    assert decide(node, "other", attempt=2, dirty=True, rerun=True, cached=False) == Decision("return", False)
    assert (
        decide(_running("@0", "d", attempt=2), "d", attempt=2, dirty=False, rerun=False, cached=True).action == "return"
    )
    stale = replace(node, stale=True)
    assert decide(stale, "d", attempt=2, dirty=False, rerun=False, cached=True) == Decision("run", True)


def test_completed_same_digest_hits_and_live_attaches() -> None:
    """A finished call returns its saved result, and work that is still alive is joined."""
    assert decide(_done("@0", "d", attempt=1), "d", attempt=2, dirty=False, rerun=False, cached=True) == Decision(
        "hit",
        False,
    )
    assert (
        decide(_waiting("@0", "d", attempt=1), "d", attempt=2, dirty=False, rerun=False, cached=True).action == "attach"
    )
    live = decide(_running("@0", "d", attempt=1), "d", attempt=2, dirty=False, rerun=False, cached=True, live=True)
    assert live == Decision("attach", False)


def test_running_node_of_a_dead_attempt_is_reconciled_not_run() -> None:
    """Work a dead attempt left open is settled from its handle before anything else is decided."""
    assert (
        decide(_running("@0", "d", attempt=1), "d", attempt=2, dirty=False, rerun=False, cached=True).action
        == "reconcile"
    )
    queued = _node("@0", "d", 1, WorkflowNodeState.QUEUED)
    assert decide(queued, "d", attempt=2, dirty=False, rerun=False, cached=True) == Decision("reconcile", False)


def test_indeterminate_raises_until_rerun_names_it() -> None:
    """A call whose end is unknown is never issued again unless a person names it."""
    node = _indeterminate("@0", "d", attempt=1)
    assert decide(node, "d", attempt=2, dirty=False, rerun=False, cached=True).action == "raise_indeterminate"
    assert decide(node, "d", attempt=2, dirty=False, rerun=True, cached=True) == Decision("run", True)


def test_indeterminate_raises_with_a_changed_digest_and_in_a_dirty_lane() -> None:
    """Neither an edited call nor a dirty lane nor `cached=False` turns an unknown end into a retry."""
    node = _indeterminate("@0", "d", attempt=1)
    for digest, dirty, cached in (("other", False, True), ("d", True, True), ("other", True, False)):
        decision = decide(node, digest, attempt=2, dirty=dirty, rerun=False, cached=cached)
        assert decision == Decision("raise_indeterminate", False)
        assert decide(node, digest, attempt=2, dirty=dirty, rerun=True, cached=cached) == Decision("run", True)


def test_dead_running_node_is_reconciled_before_digest_dirty_or_rerun_are_looked_at() -> None:
    """Reconciling comes first, so a changed call cannot abandon work whose outcome is not known yet."""
    node = _running("@0", "d", attempt=1)
    for digest, dirty, rerun in (
        ("other", False, False),
        ("d", True, False),
        ("d", False, True),
        ("other", True, True),
    ):
        assert decide(node, digest, attempt=2, dirty=dirty, rerun=rerun, cached=False) == Decision("reconcile", False)
        replaced = decide(node, digest, attempt=2, dirty=dirty, rerun=rerun, cached=True, live=True)
        assert replaced == Decision("run", True)


def test_changed_digest_dirty_lane_and_failed_node_run_as_the_next_generation() -> None:
    """A call that must run replaces its saved node, and a first call has nothing to replace."""
    node = _done("@0", "d", attempt=1)
    assert decide(None, "d", attempt=1, dirty=False, rerun=False, cached=True) == Decision("run", False)
    assert decide(None, "d", attempt=2, dirty=True, rerun=True, cached=True) == Decision("run", False)
    assert decide(node, "other", attempt=2, dirty=False, rerun=False, cached=True) == Decision("run", True)
    assert decide(node, "d", attempt=2, dirty=True, rerun=False, cached=True) == Decision("run", True)
    assert decide(node, "d", attempt=2, dirty=False, rerun=True, cached=True) == Decision("run", True)
    for ended in (_failed_node("@0", "d", attempt=1), _node("@0", "d", 1, WorkflowNodeState.CANCELLED)):
        assert decide(ended, "d", attempt=2, dirty=False, rerun=False, cached=True) == Decision("run", True)


def test_cached_false_runs_a_completed_node() -> None:
    """`cached=False` runs a finished call again, and still joins one that is alive."""
    assert decide(_done("@0", "d", attempt=1), "d", attempt=2, dirty=False, rerun=False, cached=False) == Decision(
        "run",
        True,
    )
    assert (
        decide(_waiting("@0", "d", attempt=1), "d", attempt=2, dirty=False, rerun=False, cached=False).action
        == "attach"
    )


def test_changed_item_dirties_what_follows_the_join_and_not_its_siblings() -> None:
    """One item that ran again reruns the rest of its lane and what follows the group, nothing else."""
    dirty = DirtyLanes()
    dirty.mark("/0:a@0")  # item a of the foreach at @0 ran again
    assert dirty.is_dirty("/0:a@1")  # later in its lane
    assert dirty.is_dirty("@1")  # after the join
    assert not dirty.is_dirty("/0:b@0")  # a sibling item
    assert not dirty.is_dirty("@0")  # the group itself
    assert not dirty.is_dirty("/0:a@0")


def test_nested_join_reaches_the_root() -> None:
    """A call inside a group inside a group dirties what follows each enclosing join."""
    dirty = DirtyLanes()
    dirty.mark("/1:x/0:p0@0")
    assert dirty.is_dirty("/1:x/0:p0@1")
    assert dirty.is_dirty("/1:x@1")
    assert dirty.is_dirty("@2")
    assert not dirty.is_dirty("/1:x@0")
    assert not dirty.is_dirty("@1")
    assert not dirty.is_dirty("@0")
    assert not dirty.is_dirty("/1:x/0:p1@0")
    assert not dirty.is_dirty("/1:y@0")


def test_calls_inside_a_group_that_follows_a_dirty_call_are_dirty() -> None:
    """A group after a call that ran is itself later in that lane, so its members run again too."""
    dirty = DirtyLanes()
    dirty.mark("@0")
    assert dirty.is_dirty("@1")
    assert dirty.is_dirty("/1:a@0")
    assert dirty.is_dirty("/1:a/3:p0@2")
    assert not dirty.is_dirty("/0:a@0")
    dirty.mark("@3")  # a later call in the same lane does not make the lane clean before it
    assert dirty.is_dirty("@1")


def test_changed_membership_dirties_the_parent_after_the_group_and_no_item_lane() -> None:
    """An item added, removed or moved reruns what follows the group; the items that stayed keep their results."""
    dirty = DirtyLanes()
    dirty.mark("/1:x@0")  # the group at index 0 of item x has another member list
    assert dirty.is_dirty("/1:x@1")
    assert dirty.is_dirty("@2")  # and, through the join, what follows the enclosing group
    assert not dirty.is_dirty("/1:x@0")
    assert not dirty.is_dirty("/1:x/0:a@0")
    assert not dirty.is_dirty("/1:x/0:b@5")
    assert not dirty.is_dirty("@1")


def test_keys_split_into_lane_and_index_and_lanes_name_their_group() -> None:
    """A key is `lane@index`, and a lane is its parent lane plus `/<group index>:<item>`."""
    assert split_key("@0") == ("", 0)
    assert split_key("/0:ab12@3") == ("/0:ab12", 3)
    assert split_key("/0:ab12/2:p0@11") == ("/0:ab12/2:p0", 11)
    assert parent_group("") is None
    assert parent_group("/0:ab12") == "@0"
    assert parent_group("/0:ab12/2:p0") == "/0:ab12@2"
    assert parent_group("/12:ab12") == "@12"


def test_digest_covers_name_and_ignores_key_order() -> None:
    """The digest is over the kind and the canonical arguments, the mailbox name included."""
    digest = node_digest("agent", {"prompt": "Check it", "name": "checker", "tools": ["shell"]})
    assert digest == node_digest("agent", {"tools": ["shell"], "name": "checker", "prompt": "Check it"})
    assert digest != node_digest("agent", {"prompt": "Check it", "name": "critic", "tools": ["shell"]})
    assert digest != node_digest("agent", {"prompt": "Check it", "tools": ["shell"]})
    assert digest != node_digest("session", {"prompt": "Check it", "name": "checker", "tools": ["shell"]})
    assert digest != node_digest("agent", {"prompt": "Check it", "name": "checker", "tools": ["shell", "file"]})
    assert node_digest("value", {}) == "9a49f22227188a1075ac17b9a2f8b637ede6532e06eaa1c09bf911d192a26f0a"


@pytest.mark.parametrize("attempt", [2, 3])
def test_stale_keys_are_those_a_completed_attempt_did_not_touch(attempt: int) -> None:
    """Nodes the finished attempt never reached leave the view; those already stale are not named again."""
    nodes = [
        _done("@0", "d", attempt=attempt),
        _done("@1", "d", attempt=attempt - 1),
        _failed_node("/1:a@0", "d", attempt=1),
        replace(_done("@2", "d", attempt=1), stale=True),
    ]
    assert stale_keys(nodes, attempt) == ("@1", "/1:a@0")
    assert stale_keys([], attempt) == ()
