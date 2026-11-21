"""Records, states and IDs of durable workflow runs and their nodes."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from mindroom.script_runs.models import utc_timestamp


class WorkflowRunState(StrEnum):
    """Durable lifecycle states of a workflow run."""

    STARTING = "starting"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_RUN_STATES = frozenset(WorkflowRunState) - {
    WorkflowRunState.STARTING,
    WorkflowRunState.RUNNING,
    WorkflowRunState.WAITING,
}


class WorkflowNodeState(StrEnum):
    """Durable states of one node; `indeterminate` is a dispatched call with no recorded end."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class DispatchHandle:
    """Where a node's work was handed over, so a restart reads the outcome and never repeats the work.

    `ref` is the broker call (`"<script run id>:<call id>"`), the job ID, or a child run ID.
    `dispatched` turns true in the same transaction that hands the work over.
    `same_as` is the digest of the result that a call which runs on every resume had before: what follows the call
    is reused only when it answers that again, also when its answer is read after its attempt died.
    """

    kind: Literal["broker", "job", "run"]
    ref: str
    dispatched: bool
    same_as: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    """One durable workflow run: its owner, script, options and current attempt."""

    run_id: str
    agent_name: str
    owner_user_id: str
    room_id: str
    thread_root_event_id: str | None
    execution_identity: dict[str, object]
    persona: dict[str, object] | None
    title: str | None
    source: str
    source_digest: str
    args: object
    max_concurrent: int
    max_cost_usd: float | None
    resource_profile: str | None
    runtime_seconds_left: float | None
    """Working time left; `None` is no limit."""
    origin_job_id: str | None
    """The key the run is found again by: the job ID, or the key of an inline start."""
    parent_run_id: str | None = None
    parent_node_key: str | None = None
    state: WorkflowRunState = WorkflowRunState.STARTING
    attempt: int = 0
    idle_resumes: int = 0
    previous_source_digest: str | None = None
    script_run_id: str | None = None
    job_id: str | None = None
    card_event_id: str | None = None
    result: object | None = None
    error: str | None = None
    created_at: str = field(default_factory=utc_timestamp)
    finished_at: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkflowNode:
    """One call of a run's script, with its full request so a restart can restore it."""

    run_id: str
    key: str
    seq: int = 0
    """Order of first submission within the run, assigned by the store; the public ID is `n<seq>`."""
    kind: str
    lane: str
    idx: int
    digest: str
    request: dict[str, object]
    generation: int = 1
    state: WorkflowNodeState = WorkflowNodeState.QUEUED
    waiting_on: str | None = None
    handle: DispatchHandle | None = None
    attempt: int
    stale: bool = False
    label: str = ""
    name: str | None = None
    phase: str | None = None
    group_key: str | None = None
    members: tuple[str, ...] | None = None
    session_key: str | None = None
    cached: bool = True
    started_at: str | None = None
    ended_at: str | None = None
    result: object | None = None
    result_file: str | None = None
    """Set when the result was too large for the row: the file's path relative to the store's directory."""
    error: dict[str, object] | None = None
    links: dict[str, str] = field(default_factory=dict)
    """IDs of what the node made: `delegation_id`, `subagent_id`, `job_id`, `event_id`, `child_run_id`."""


@dataclass(frozen=True, slots=True)
class NodeRequest:
    """One node submitted by a script through the gateway."""

    key: str
    kind: str
    args: dict[str, object]
    meta: dict[str, object]


@dataclass(frozen=True, slots=True)
class NodeReceipt:
    """What the gateway answers for a node: its public ID, its state, and its outcome once it has one."""

    id: str
    state: str
    cached: bool
    result: object | None
    error: dict[str, object] | None
    result_ref: str | None = None


_WORKFLOW_RUN_ID_RE = re.compile(r"wf-[0-9a-f]{32}")


def new_workflow_run_id() -> str:
    """Return a fresh workflow run ID."""
    return f"wf-{uuid4().hex}"


def validated_workflow_run_id(run_id: str) -> str:
    """Return *run_id* when it has the generated form, else raise `ValueError`."""
    if _WORKFLOW_RUN_ID_RE.fullmatch(run_id) is None:
        msg = "Workflow run ID must be wf- followed by 32 lowercase hexadecimal characters."
        raise ValueError(msg)
    return run_id
