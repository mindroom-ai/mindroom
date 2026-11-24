"""Run-scoped `workflow_peers` toolkit automatically given to every agent running inside a workflow run."""

from __future__ import annotations

import asyncio
import json
import math
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.delegation.lifecycle import running_child_run
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.invocation import current_tool_call
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.workflow_runs.peers import (
    NodeRef,
    get_state,
    get_state_entry,
    peer_op_id,
    set_state,
)
from mindroom.workflow_runs.peers import list_peers as list_run_peers
from mindroom.workflow_runs.store import WorkflowRunStoreError

if TYPE_CHECKING:
    from mindroom.workflow_runs.engine import WorkflowEngine
    from mindroom.workflow_runs.models import WorkflowNode


class WorkflowPeerTools(Toolkit):
    """Peer messages and shared state scoped strictly to the calling child's own workflow run."""

    def __init__(self) -> None:
        super().__init__(
            name="workflow_peers",
            tools=[
                self.send_peer_message,
                self.read_peer_messages,
                self.list_peers,
                self.get_shared_state,
                self.set_shared_state,
            ],
            instructions=(
                "You are running inside a workflow run with peer agents. "
                "Use send_peer_message(to, text) and read_peer_messages(wait_seconds=...) to coordinate by peer name "
                "(or '*' for all unfinished peers, 'script' for the workflow script), list_peers() to see peer names "
                "and statuses, and get_shared_state / set_shared_state to share JSON values across the run."
            ),
        )

    @staticmethod
    def build(*, enabled: bool) -> Toolkit | None:
        """Build `workflow_peers` only when running as an agent child of a workflow run."""
        if not enabled:
            return None
        child = running_child_run()
        if child is None or child.child.workflow_run_id is None:
            return None
        return WorkflowPeerTools()

    async def send_peer_message(self, to: str, text: str) -> str:
        """Send a message to another agent in this workflow run by name, '*' for all unfinished agents, or 'script'.

        Args:
            to: Recipient peer name, '*' for every unfinished peer, or 'script'.
            text: Message text (up to 1 MiB; for larger data write to a workspace file and send the path).

        """
        engine, run_id, node, ref, _names, model_run_id, call_id = _binding()
        op_id = peer_op_id(
            node.key,
            node.generation,
            model_run_id,
            call_id,
            "send_peer_message",
            {"to": to, "text": text},
        )
        result = await engine.peer_send(run_id, op_id=op_id, sender=ref, to=to, text=text)
        return json.dumps(result)

    async def read_peer_messages(self, wait_seconds: float = 0) -> str:
        """Read unread peer messages addressed to you, optionally waiting up to `wait_seconds` for the first one.

        Args:
            wait_seconds: Seconds to wait for a message when none are unread yet (0 returns immediately).

        """
        if not math.isfinite(wait_seconds) or wait_seconds < 0:
            msg = "wait_seconds must be a finite non-negative number."
            raise WorkflowRunStoreError(msg)
        engine, run_id, node, ref, names, model_run_id, call_id = _binding()
        op_id = peer_op_id(
            node.key,
            node.generation,
            model_run_id,
            call_id,
            "read_peer_messages",
            {"wait_seconds": wait_seconds},
        )
        messages, note = await engine.wait_for_messages(
            run_id,
            ref,
            wait_seconds,
            op_id=op_id,
            names=names,
            node_key=node.key,
            node_generation=node.generation,
            holds_slot=True,
        )
        payload: dict[str, object] = {"messages": messages}
        if note is not None:
            payload["note"] = note
        return json.dumps(payload)

    async def list_peers(self) -> str:
        """List the name, status and label of every agent and session in this workflow run."""
        engine, run_id, *_rest = _binding()
        return json.dumps({"peers": await asyncio.to_thread(list_run_peers, engine.store, run_id)})

    async def get_shared_state(self, key: str | None = None) -> str:
        """Read one key from this run's shared state, or all key-value pairs when `key` is omitted.

        Args:
            key: Optional state key to read; omit to return all keys.

        """
        engine, run_id, node, _ref, _names, model_run_id, call_id = _binding()
        op_id = peer_op_id(node.key, node.generation, model_run_id, call_id, "get_shared_state", {"key": key})
        if key is None:
            values = await asyncio.to_thread(get_state, engine.store, run_id, op_id=op_id, key=None)
            return json.dumps(values)
        entry = await asyncio.to_thread(get_state_entry, engine.store, run_id, op_id=op_id, key=key)
        return json.dumps({"key": key, **entry})

    async def set_shared_state(self, key: str, value: Any) -> str:  # noqa: ANN401 - arbitrary JSON value
        """Write a JSON value to this run's shared state under `key` (last write wins).

        Args:
            key: State key name.
            value: JSON-serializable value (up to 1 MiB).

        """
        engine, run_id, node, ref, _names, model_run_id, call_id = _binding()
        op_id = peer_op_id(
            node.key,
            node.generation,
            model_run_id,
            call_id,
            "set_shared_state",
            {"key": key, "value": value},
        )
        result = await asyncio.to_thread(
            set_state,
            engine.store,
            run_id,
            op_id=op_id,
            key=key,
            value=value,
            updated_by=ref.name,
        )
        return json.dumps(result)


def _binding() -> tuple[WorkflowEngine, str, WorkflowNode, NodeRef, tuple[str, ...], str, str]:
    """Resolve the workflow engine, run, node and operation identity strictly from the child's own binding."""
    observation = running_child_run()
    context = get_tool_runtime_context()
    if (
        observation is None
        or observation.child.workflow_run_id is None
        or observation.child.workflow_node_key is None
        or context is None
        or context.orchestrator is None
    ):
        msg = "workflow_peers tools can only be called by an agent inside a workflow run."
        raise WorkflowRunStoreError(msg)
    engine = context.orchestrator.workflow_engine
    run_id = observation.child.workflow_run_id
    node_key = observation.child.workflow_node_key
    node = engine.store.get_node(run_id, node_key)
    if node is None:
        msg = f"Workflow node {node_key} is no longer available."
        raise WorkflowRunStoreError(msg)
    reader_key, reader_gen = None, None
    if node.session_key is not None and (session := engine.store.get_node(run_id, node.session_key)) is not None:
        reader_key, reader_gen = session.key, session.generation
    name = node.name or f"n{node.seq}"
    ref = NodeRef(
        key=node.key,
        generation=observation.child.workflow_generation or node.generation,
        name=name,
        reader_key=reader_key,
        reader_generation=reader_gen,
    )
    names = (node.name, "*") if node.name else ("*",)
    call = current_tool_call()
    model_run_id = (call.context.run_id if call is not None and call.context.run_id else observation.child.run_id) or ""
    call_id = call.context.call_id if call is not None and call.context.call_id else uuid4().hex
    return engine, run_id, node, ref, names, model_run_id, call_id
