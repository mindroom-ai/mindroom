"""The replay rules of workflow runs, as pure functions: digests, lane keys, the decision for a call, dirty lanes."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Literal

from mindroom.workflow_runs.models import LIVE_NODE_STATES, UNSETTLED_NODE_STATES, WorkflowNode, WorkflowNodeState

if TYPE_CHECKING:
    from collections.abc import Iterable

type _Action = Literal["return", "hit", "attach", "reconcile", "raise_indeterminate", "run"]

# Written onto earlier-attempt rows after a node that ran, so they rerun without colliding with explicit reruns ('').
DIRTY_DIGEST = "dirty"


def node_digest(kind: str, args: dict[str, object]) -> str:
    """Return the SHA-256 of a call's kind and arguments in canonical JSON."""
    canonical = json.dumps({"kind": kind, "args": args}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def result_digest(result: object) -> str:
    """Return the SHA-256 of a result in canonical JSON, which tells 1 from 1.0 and from true as the script may."""
    return hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def split_key(key: str) -> tuple[str, int]:
    """Split a node key `lane@index` into its lane and index."""
    lane, _, index = key.rpartition("@")
    return lane, int(index)


def parent_group(lane: str) -> str | None:
    """Return the key of the group node that opened *lane*, or `None` for the root lane."""
    if not lane:
        return None
    parent_lane, _, segment = lane.rpartition("/")
    return f"{parent_lane}@{segment.partition(':')[0]}"


def may_have_written(started: bool | None) -> bool:
    """Return whether work that was handed over and ended without a result may have had an effect.

    The one rule of what may run again, asked by every path that ends a node: a stop, a timeout, a failure, a
    restart. *started* says whether the node's tool was invoked, or whether its agent started a tool call, and is
    None when no record can say. Only a record that proves the work did not start makes it safe to run again;
    anything else ends `indeterminate` and runs again only when a resume names it in `rerun`.
    """
    return started is not False


def decide(
    saved: WorkflowNode | None,
    digest: str,
    *,
    attempt: int,
    dirty: bool,
    rerun: bool,
    cached: bool,
    live: bool = False,
) -> _Action:
    """Decide what a submitted call does, given the node saved at its key.

    `reconcile` asks the caller to settle the saved node from its dispatch handle and decide
    again, with *live* set when the handle's work is still alive.
    """
    action: _Action
    if saved is None:
        action = "run"
    elif saved.attempt == attempt and not saved.stale:
        action = "return"
    elif saved.state in UNSETTLED_NODE_STATES and not live:
        action = "reconcile"
    elif saved.state is WorkflowNodeState.INDETERMINATE and not rerun:
        action = "raise_indeterminate"
    elif rerun or dirty or saved.digest != digest:
        action = "run"
    elif saved.state in LIVE_NODE_STATES:
        action = "attach"
    elif saved.state is WorkflowNodeState.COMPLETED and cached:
        action = "hit"
    else:
        action = "run"
    return action


def stale_keys(nodes: Iterable[WorkflowNode], attempt: int) -> tuple[str, ...]:
    """Return the keys of the nodes that the completed *attempt* did not reach and that are not stale yet."""
    return tuple(node.key for node in nodes if node.attempt != attempt and not node.stale)
