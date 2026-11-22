"""The replay rules of workflow runs, as pure functions: digests, lane keys, the decision for a call, dirty lanes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mindroom.workflow_runs.models import WorkflowNode, WorkflowNodeState

if TYPE_CHECKING:
    from collections.abc import Iterable

type _Action = Literal["return", "hit", "attach", "reconcile", "raise_indeterminate", "run"]
_UNSETTLED_STATES = frozenset({WorkflowNodeState.QUEUED, WorkflowNodeState.RUNNING})
_LIVE_STATES = _UNSETTLED_STATES | {WorkflowNodeState.WAITING}


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


@dataclass(frozen=True, slots=True)
class Decision:
    """What to do with a submitted call, and whether running it replaces a saved node."""

    action: _Action
    next_generation: bool


def decide(
    saved: WorkflowNode | None,
    digest: str,
    *,
    attempt: int,
    dirty: bool,
    rerun: bool,
    cached: bool,
    live: bool = False,
) -> Decision:
    """Decide what a submitted call does, given the node saved at its key.

    `reconcile` asks the caller to settle the saved node from its dispatch handle and decide
    again, with *live* set when the handle's work is still alive.
    """
    action: _Action
    if saved is None:
        action = "run"
    elif saved.attempt == attempt and not saved.stale:
        action = "return"
    elif saved.state in _UNSETTLED_STATES and not live:
        action = "reconcile"
    elif saved.state is WorkflowNodeState.INDETERMINATE and not rerun:
        action = "raise_indeterminate"
    elif rerun or dirty or saved.digest != digest:
        action = "run"
    elif saved.state in _LIVE_STATES:
        action = "attach"
    elif saved.state is WorkflowNodeState.COMPLETED and cached:
        action = "hit"
    else:
        action = "run"
    return Decision(action, next_generation=action == "run" and saved is not None)


class DirtyLanes:
    """The lanes in which a call ran in this attempt, so that the calls after it run again."""

    def __init__(self) -> None:
        self._dirty_after: dict[str, int] = {}

    def mark(self, key: str) -> None:
        """Record that the node at *key* ran: its lane is dirty after it, and so is each enclosing lane after its group."""
        group: str | None = key
        while group is not None:
            lane, index = split_key(group)
            self._dirty_after[lane] = min(index, self._dirty_after.get(lane, index))
            group = parent_group(lane)

    def is_dirty(self, key: str) -> bool:
        """Return whether the call at *key* follows, directly or through an enclosing group, a call that ran."""
        group: str | None = key
        while group is not None:
            lane, index = split_key(group)
            if lane in self._dirty_after and index > self._dirty_after[lane]:
                return True
            group = parent_group(lane)
        return False


def stale_keys(nodes: Iterable[WorkflowNode], attempt: int) -> tuple[str, ...]:
    """Return the keys of the nodes that the completed *attempt* did not reach and that are not stale yet."""
    return tuple(node.key for node in nodes if node.attempt != attempt and not node.stale)
