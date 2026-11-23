"""The replay rules of workflow runs, as pure functions: digests, lane keys, the decision for a call, dirty lanes."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from mindroom.workflow_runs.models import LIVE_NODE_STATES, WorkflowNode, WorkflowNodeState

type _Action = Literal["hit", "attach", "raise_indeterminate", "run"]

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
    dirty: bool,
    rerun: bool,
    cached: bool,
) -> _Action:
    """Decide what a submitted call does, given the reconciled node from an earlier attempt."""
    if saved is None:
        return "run"
    if saved.state is WorkflowNodeState.INDETERMINATE and not rerun:
        return "raise_indeterminate"
    if rerun or dirty or saved.digest != digest:
        return "run"
    if saved.state in LIVE_NODE_STATES:
        return "attach"
    if saved.state is WorkflowNodeState.COMPLETED and cached:
        return "hit"
    return "run"
