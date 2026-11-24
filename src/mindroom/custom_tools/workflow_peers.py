"""Run-scoped peer messaging, shared state, and requester questions for workflow child agents."""

from __future__ import annotations

import asyncio
import json
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.delegation.lifecycle import running_child_run
from mindroom.delegation.state import child_execution_identity
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.invocation import current_tool_call
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.workflow_runs.peers import NodeRef, get_state, list_peers, peer_op_id, set_state
from mindroom.workflow_runs.store import WorkflowRunStoreError

if TYPE_CHECKING:
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity
    from mindroom.workflow_runs.engine import WorkflowEngine
    from mindroom.workflow_runs.models import WorkflowNode


def _bind(
    op: str = "",
    args: object = None,
    *,
    bound_run_id: str,
    bound_node_key: str,
) -> tuple[WorkflowEngine, str, WorkflowNode, NodeRef, str]:
    """Resolve engine, run, node, ref, and deterministic op_id strictly from the child agent's own binding."""
    obs, ctx = running_child_run(), get_tool_runtime_context()
    if not obs or not obs.child.workflow_run_id or not obs.child.workflow_node_key or not ctx or not ctx.orchestrator:
        msg = "workflow_peers tools can only be called by an agent inside a workflow run."
        raise WorkflowRunStoreError(msg)
    if obs.child.workflow_run_id != bound_run_id or obs.child.workflow_node_key != bound_node_key:
        msg = "workflow_peers tools can only be called by the bound workflow node."
        raise WorkflowRunStoreError(msg)
    engine, run_id, key = ctx.orchestrator.workflow_engine, obs.child.workflow_run_id, obs.child.workflow_node_key
    node = engine.store.get_node(run_id, key)
    if node is None:
        msg = f"Workflow node {key} is no longer available."
        raise WorkflowRunStoreError(msg)
    gen = obs.child.workflow_generation or node.generation
    ref = NodeRef(key, gen, node.name or f"n{node.seq}", node.session_key)
    call = current_tool_call()
    model_run = (call.context.run_id if call and call.context.run_id else obs.child.run_id) or ""
    call_id = call.context.call_id if call and call.context.call_id else uuid4().hex
    return engine, run_id, node, ref, peer_op_id(key, gen, model_run, call_id, op, args) if op else ""


class WorkflowPeerTools(Toolkit):
    """Peer messaging, shared state, and requester questions scoped to the child agent's workflow run."""

    @classmethod
    def build(cls, *, enabled: bool) -> WorkflowPeerTools | None:
        """Build the run-scoped peer toolkit when assembling a workflow run's child agent."""
        obs = running_child_run() if enabled else None
        if obs and obs.child.workflow_run_id and obs.child.workflow_node_key:
            return cls(run_id=obs.child.workflow_run_id, node_key=obs.child.workflow_node_key)
        return None

    def __init__(self, *, run_id: str, node_key: str) -> None:
        if not run_id or not node_key:
            msg = "WorkflowPeerTools requires a non-empty run_id and node_key."
            raise ValueError(msg)
        self._run_id = run_id
        self._node_key = node_key
        tools = [
            self.send_peer_message,
            self.read_peer_messages,
            self.list_peers,
            self.get_shared_state,
            self.set_shared_state,
            self.ask_user,
        ]
        instructions = (
            "You are inside a workflow run. Use send_peer_message(to, text) and "
            "read_peer_messages(wait_seconds=0) (returns only unread mail and advances your cursor; prefer short "
            "waits of 10 to 30 s in a bounded loop) to talk with peers by name ('*' for all live peers, "
            "'script' for the script), list_peers() to inspect peers, get_shared_state()/set_shared_state() "
            "for shared JSON values (pass the value directly; don't json.dumps it; last write wins, so pick one writer per key), "
            "and ask_user(question, options=None) to ask the requester."
        )
        super().__init__(name="workflow_peers", tools=tools, instructions=instructions)

    def matches_caller(self, owner: ToolExecutionIdentity) -> bool:
        """Return whether *owner* is the live workflow child bound to this toolkit."""
        obs = running_child_run()
        return (
            obs is not None
            and obs.child.workflow_run_id == self._run_id
            and obs.child.workflow_node_key == self._node_key
            and child_execution_identity(obs.child) == owner
        )

    def _bound(self, op: str = "", args: object = None) -> tuple[WorkflowEngine, str, WorkflowNode, NodeRef, str]:
        return _bind(op, args, bound_run_id=self._run_id, bound_node_key=self._node_key)

    async def send_peer_message(self, to: str, text: str) -> str:
        """Send a message to a peer by name, '*' (all unfinished peers), or 'script' (up to 1 MiB)."""
        engine, run_id, _, ref, op_id = self._bound("send_peer_message", {"to": to, "text": text})
        return json.dumps(await engine.peer_send(run_id, op_id=op_id, sender=ref, to=to, text=text))

    async def read_peer_messages(self, wait_seconds: float = 0) -> str:
        """Return only unread messages since your last read (advancing the cursor) as `{"messages": [{"from", "text", "sent_at"}], "note"?: ...}`.

        Waiting (`wait_seconds > 0`) releases your concurrency slot and ends when mail arrives or after `wait_seconds`
        with `"messages": []` and `"note": "No message arrived within <N> s."`. Prefer short waits (10 to 30 s) in a bounded loop over one long wait.
        """
        if isinstance(wait_seconds, bool) or wait_seconds < 0:
            msg = "wait_seconds must be a non-negative number."
            raise WorkflowRunStoreError(msg)
        engine, run_id, node, ref, op_id = self._bound("read_peer_messages", {"wait_seconds": wait_seconds})
        names = (node.name, "*") if node.name else ("*",)
        return json.dumps(await engine.wait_for_messages(run_id, ref, float(wait_seconds), op_id=op_id, names=names))

    async def list_peers(self) -> str:
        """Return the name, status and label of every current agent and session in this workflow run."""
        engine, run_id, *_ = self._bound()
        return json.dumps({"peers": await asyncio.to_thread(list_peers, engine.store, run_id)})

    async def get_shared_state(self, key: str | None = None) -> str:
        """Read one shared-state key (`{"key", "found", "value"}`) or all keys (`{key: value, ...}`) when `key` is omitted."""
        engine, run_id, _, _, op_id = self._bound("get_shared_state", {"key": key})
        val = await asyncio.to_thread(get_state, engine.store, run_id, op_id=op_id, key=key)
        return json.dumps(val if key is None else {"key": key, **val})

    async def set_shared_state(self, key: str, value: Any) -> str:  # noqa: ANN401 - arbitrary JSON value
        """Write any JSON value (dict, list, or primitive; pass the value directly, don't json.dumps it; up to 1 MiB) under `key`. Last write wins, so pick one writer per key."""
        engine, run_id, _, ref, op_id = self._bound("set_shared_state", {"key": key, "value": value})
        call = partial(set_state, engine.store, run_id, op_id=op_id, key=key, value=value, updated_by=ref.name)
        return json.dumps(await asyncio.to_thread(call))

    async def ask_user(self, question: str, options: list[str] | None = None) -> str:
        """Ask the workflow requester a question in the thread and wait for their answer."""
        engine, run_id, node, *_ = self._bound()
        return await engine.ask_child_question(run_id, node, question, options)
