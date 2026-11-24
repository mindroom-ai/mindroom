"""Records, states and IDs of durable workflow runs and their nodes."""

from __future__ import annotations

import json
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


LIVE_RUN_STATES = frozenset({WorkflowRunState.STARTING, WorkflowRunState.RUNNING, WorkflowRunState.WAITING})
TERMINAL_RUN_STATES = frozenset(WorkflowRunState) - LIVE_RUN_STATES
# The states one attempt moves between, in which it may submit calls: every other change begins or ends an attempt.
SERVED_RUN_STATES = frozenset({WorkflowRunState.RUNNING, WorkflowRunState.WAITING})
_INDENTED_REPORT_BYTES = 64 * 1024
# How long a waiter sleeps before it reads the store or a job again, with or without a wake.
RECHECK_SECONDS = 1.0


class WorkflowNodeState(StrEnum):
    """Durable states of one node; `indeterminate` is a dispatched call with no recorded end."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INDETERMINATE = "indeterminate"


UNSETTLED_NODE_STATES = frozenset({WorkflowNodeState.QUEUED, WorkflowNodeState.RUNNING})
LIVE_NODE_STATES = UNSETTLED_NODE_STATES | {WorkflowNodeState.WAITING}


@dataclass(frozen=True, slots=True)
class DispatchHandle:
    """Where a node's work was handed over, so a restart reads the outcome and never repeats the work.

    `ref` is the broker call (`"<script run id>:<call id>"`), the job ID, or a child run ID.
    `dispatched` turns true once the work was handed over. A job starts before that is saved, so a job handle
    without it proves that nothing ran only together with a job runtime that holds no such job.
    `same_as` is the digest of the result that a call which runs on every resume had before: what follows the call
    is reused only when it answers that again, also when its answer is read after its attempt died.
    """

    kind: Literal["broker", "job", "run"]
    ref: str
    dispatched: bool
    same_as: str | None = None
    # The working time an agent's job has, which no approval wait and no later attempt hands out again: the moment
    # its work must have ended, or, while it waits for a person, the seconds of work it has left.
    work_until: float | None = None
    work_left: float | None = None


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
    """What the run may spend before it stops interrupted; resume with a higher limit to continue."""
    resource_profile: str | None
    runtime_seconds_left: float | None
    """Working time left; `None` is no limit."""
    origin_job_id: str | None
    """The key the run is found again by: the job ID, or the key of an inline start."""
    parent_run_id: str | None = None
    parent_node_key: str | None = None
    state: WorkflowRunState = WorkflowRunState.STARTING
    epoch: int = 0
    """Raised by each write that takes the run, begins an attempt or ends one; every other write must name it."""
    attempt: int = 0
    idle_resumes: int = 0
    previous_source_digest: str | None = None
    script_run_id: str | None = None
    job_id: str | None = None
    owner: str | None = None
    """The invocation the run is now: its job's ID, or the key of the call that waits for it itself."""
    depth: int = 0
    """How deep in a chain of subagents the agent that started the run is; the agents the run runs are one deeper."""
    claims: dict[str, str | None] = field(default_factory=dict)
    """Each invocation that took the run when it had ended, with the report of a call that waited for it itself."""
    card_event_id: str | None = None
    card_epoch: int | None = None
    """The epoch the run's final card was delivered for: the card of an ended run is final once this is its epoch."""
    large: bool = False
    """Set once, when the run scheduled more agents or projects a higher cost than the notice of a large run allows."""
    result: object | None = None
    error: str | None = None
    created_at: str = field(default_factory=utc_timestamp)
    finished_at: str | None = None

    @property
    def entry(self) -> Literal["workflow"]:
        """What the shim does with the source: always hand it to the workflow SDK."""
        return "workflow"

    def outcome_text(self) -> str:
        """Return an ended run's report, a string as it is and any other value as JSON, or its error.

        A small value is indented. A larger one is not, since indentation can make a report that was accepted many
        times larger than what a job's result may hold.
        """
        if self.state is not WorkflowRunState.COMPLETED:
            return self.error or f"Workflow run {self.run_id} ended {self.state.value}."
        if isinstance(self.result, str):
            return self.result
        compact = json.dumps(self.result)
        return json.dumps(self.result, indent=2) if len(compact) <= _INDENTED_REPORT_BYTES else compact


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
    members: tuple[str, ...] | None = None
    session_key: str | None = None
    cached: bool = False
    """Whether the attempt the node is of reused the node's saved result, and did not make it."""
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
