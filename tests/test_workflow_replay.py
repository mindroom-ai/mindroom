"""Tests for the replay rules of workflow runs.

Level: pure (functions with no I/O).
"""

from __future__ import annotations

from mindroom.workflow_runs.models import WorkflowNode, WorkflowNodeState
from mindroom.workflow_runs.replay import (
    DIRTY_DIGEST,
    decide,
    node_digest,
    parent_group,
    split_key,
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


def test_completed_same_digest_hits_and_live_attaches() -> None:
    """A finished call returns its saved result, and work that is still alive is joined."""
    assert decide(_done("@0", "d", attempt=1), "d", cached=True) == "hit"
    assert decide(_waiting("@0", "d", attempt=1), "d", cached=True) == "attach"
    assert decide(_running("@0", "d", attempt=1), "d", cached=True) == "attach"


def test_indeterminate_raises_until_rerun_names_it() -> None:
    """A call whose end is unknown is never issued again unless a person names it."""
    assert decide(_indeterminate("@0", "d", attempt=1), "d", cached=True) == "raise_indeterminate"
    assert decide(_indeterminate("@0", "", attempt=1), "d", cached=True) == "run"


def test_indeterminate_raises_with_a_changed_digest_and_in_a_dirty_lane() -> None:
    """Neither an edited call nor a dirty lane nor `cached=False` turns an unknown end into a retry."""
    for saved_digest, digest, cached in (
        ("d", "other", True),
        (DIRTY_DIGEST, "d", True),
        (DIRTY_DIGEST, "other", False),
    ):
        assert decide(_indeterminate("@0", saved_digest, attempt=1), digest, cached=cached) == "raise_indeterminate"
        assert decide(_indeterminate("@0", "", attempt=1), digest, cached=cached) == "run"


def test_live_node_with_changed_digest_dirty_or_rerun_runs() -> None:
    """A reconciled live node whose call changed, is dirty, or was named for rerun is replaced."""
    for saved_digest, digest in (("d", "other"), (DIRTY_DIGEST, "d"), ("", "d"), ("", "other")):
        assert decide(_running("@0", saved_digest, attempt=1), digest, cached=True) == "run"


def test_changed_digest_dirty_lane_and_failed_node_run_as_the_next_generation() -> None:
    """A call that must run replaces its saved node, and a first call has nothing to replace."""
    assert decide(None, "d", cached=True) == "run"
    assert decide(_done("@0", "d", attempt=1), "other", cached=True) == "run"
    assert decide(_done("@0", DIRTY_DIGEST, attempt=1), "d", cached=True) == "run"
    assert decide(_done("@0", "", attempt=1), "d", cached=True) == "run"
    for ended in (_failed_node("@0", "d", attempt=1), _node("@0", "d", 1, WorkflowNodeState.CANCELLED)):
        assert decide(ended, "d", cached=True) == "run"


def test_cached_false_runs_a_completed_node() -> None:
    """`cached=False` runs a finished call again, and still joins one that is alive."""
    assert decide(_done("@0", "d", attempt=1), "d", cached=False) == "run"
    assert decide(_waiting("@0", "d", attempt=1), "d", cached=False) == "attach"


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
