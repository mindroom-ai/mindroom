"""The replay rules of workflow runs, as pure functions: digests, lane keys, the decision for a call, dirty lanes."""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from typing import Literal, cast

from mindroom.script_runs.models import ScriptCallRecord, ScriptCallState, ScriptToolGrant
from mindroom.workflow_runs.models import LIVE_NODE_STATES, NodeReceipt, WorkflowNode, WorkflowNodeState

type _Action = Literal["hit", "attach", "raise_indeterminate", "run"]
type Outcome = tuple[WorkflowNodeState, object, dict[str, object] | None]

# Written onto earlier-attempt rows after a node that ran, so they rerun without colliding with explicit reruns ('').
DIRTY_DIGEST = "dirty"
INTERRUPTED: dict[str, object] = {"kind": "interrupted", "message": "The call was interrupted before its tool ran."}
# How the engine's wait for an agent with a timeout is named, before its job's ID.
TIMED_PREFIX = "timed:"
OWNER_NOT_READY = "The run's agent is not ready in the run's room yet. Try again in a moment."
NO_AGENTS = "A workflow runs agents as background tool jobs. Set background_tool_jobs.enabled: true."
RUN_ENDED = "The run ended before the agent did."
OUT_OF_TIME = 'The run used its {hours:g} hours of working time. workflow(action="resume", run_id=...) continues it.'
_SHELL_COMMAND = ScriptToolGrant("shell", "run_shell_command")
# The node of the script's tool call that the current task runs, for a call of the `workflow` toolkit made in it.
CALLING_NODE: ContextVar[tuple[str, str, int] | None] = ContextVar("workflow_calling_node", default=None)


def node_digest(kind: str, args: dict[str, object]) -> str:
    """Return the SHA-256 of a call's kind and arguments in canonical JSON."""
    return result_digest({"kind": kind, "args": args})


def result_digest(result: object) -> str:
    """Return the SHA-256 of a result in canonical JSON, which tells 1 from 1.0 and from true as the script may."""
    return hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def split_key(key: str) -> tuple[str, int]:
    """Split a node key `lane@index` (or child key `lane@index?<hash>`) into its lane and index."""
    lane, _, index = key.rpartition("@")
    return lane, int(index.partition("?")[0])


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


def decide(saved: WorkflowNode | None, digest: str, *, cached: bool) -> _Action:
    """Decide what a submitted call does, given the reconciled node from an earlier attempt."""
    if saved is None:
        return "run"
    if saved.state is WorkflowNodeState.INDETERMINATE:
        return "run" if not saved.digest else "raise_indeterminate"
    if saved.digest != digest:
        return "run"
    if saved.state in LIVE_NODE_STATES:
        return "attach"
    if saved.state is WorkflowNodeState.COMPLETED and cached:
        return "hit"
    return "run"


def calling_node() -> tuple[str, str, int] | None:
    """Return the run, the key and the generation of the script's tool call this context runs in, if it runs in one."""
    return CALLING_NODE.get()


def nested_key(outer: tuple[str, str, int], source: bytes, args: object) -> str:
    """Return the key of a run inside a run: the call it is made in, its script and its arguments, and no call ID.

    A call that is made again under another ID, in the same turn or in a continued one, finds the run it started
    by this key: it waits for a run that still runs, and gets the report of one that ended.
    """
    identity = [*outer, hashlib.sha256(source).hexdigest(), args]
    return "nested:" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def changed(node: WorkflowNode, outcome: Outcome) -> bool:
    """Return whether a call that runs on every resume answered something else than before, which dirties what follows.

    Its handle holds the digest of the earlier result, compared whole and as JSON. The live call and the look at the
    receipt of a call whose attempt died both ask, and save the answer with the node's end.
    """
    same_as = node.handle.same_as if node.handle is not None else None
    state, result, _ = outcome
    return same_as is not None and not (state is WorkflowNodeState.COMPLETED and result_digest(result) == same_as)


def indeterminate(node: WorkflowNode, said: object = None) -> Outcome:
    """Return the end of a call that may have run, with what its tool *said* when it failed after it was invoked."""
    message = (
        f"This call may already have run (attempt {node.attempt}). Check its effect; "
        f'to run it again call workflow(action="resume", run_id=..., rerun=["n{node.seq}"]).'
    )
    message = f"{said}\n\n{message}" if said else message
    return WorkflowNodeState.INDETERMINATE, None, {"kind": "indeterminate", "message": message}


def unless_proven(node: WorkflowNode, started: bool | None, safe: Outcome, said: object = None) -> Outcome:
    """Return how a node that ended without a result ends: as *safe* to run again only if its record proves that."""
    return indeterminate(node, said) if may_have_written(started) else safe


def _completed_outcome(node: WorkflowNode, call: ScriptCallRecord) -> Outcome:
    """Return how the node of a call that answered ends; a shell command ended only if its exit status says so."""
    facts = call.metadata or {}
    if call.grant != _SHELL_COMMAND:
        return WorkflowNodeState.COMPLETED, call.result, None
    if facts.get("not_started"):
        # The shell tool refused before any command ran, and its text says why.
        return WorkflowNodeState.FAILED, None, {"kind": "failed", "message": str(call.result)}
    if facts.get("exit_code") is None:
        # No status says the command ended: its answer was lost on the way, so it may have run.
        return indeterminate(node)
    return WorkflowNodeState.COMPLETED, {"output": call.result, "exit_code": facts["exit_code"]}, None


def call_outcome(node: WorkflowNode, call: ScriptCallRecord) -> Outcome:
    """Map a broker receipt to how its node ends; the effect stamp says what is safe to run again."""
    if call.state is ScriptCallState.COMPLETED:
        return _completed_outcome(node, call)
    details = cast("dict[str, object]", call.error if isinstance(call.error, dict) else {})
    refusal = "denied" if details.get("kind") == "approval_denied" else "failed"
    # A failed receipt without the stamp is a refusal. Without either, the call was claimed and still waited for
    # hooks or approval when its attempt ended.
    failed = call.state is ScriptCallState.FAILED
    safe = {"kind": refusal, "message": details.get("message")} if failed else INTERRUPTED
    # With the stamp the tool was invoked: whatever failed then, the tool, a check of its result or the way back,
    # the call may have had its effect, unless the call's own word says it never left for its tool.
    started = call.effect_started_at is not None and not details.get("not_started")
    said = details.get("message") if failed else None
    return unless_proven(node, started, (WorkflowNodeState.FAILED, None, safe), said)


def receipt(node: WorkflowNode) -> NodeReceipt:
    """Return what the script is told about *node*; a result too large for the row is named, not inlined."""
    node_id = f"n{node.seq}"
    spilled = node.result_file is not None
    return NodeReceipt(
        id=node_id,
        state=node.state,
        cached=False,
        result=None if spilled else node.result,
        error=node.error,
        result_ref=f"results/{node_id}" if spilled else None,
    )
