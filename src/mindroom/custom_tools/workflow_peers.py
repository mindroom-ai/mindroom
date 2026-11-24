"""Run-scoped peer messaging, shared state, and requester questions for workflow child agents."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.delegation.lifecycle import running_child_run
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.invocation import current_tool_call
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.workflow_runs.peers import (
    NodeRef,
    get_state,
    list_peers,
    peer_op_id,
    set_state,
)
from mindroom.workflow_runs.store import WorkflowRunStoreError

if TYPE_CHECKING:
    from mindroom.workflow_runs.engine import WorkflowEngine
    from mindroom.workflow_runs.models import WorkflowNode


@dataclass(frozen=True, slots=True)
class _PeerBinding:
    engine: WorkflowEngine
    run_id: str
    node: WorkflowNode
    ref: NodeRef
    names: tuple[str, ...]
    model_run_id: str
    call_id: str

    def op_id(self, operation: str, arguments: object) -> str:
        return peer_op_id(self.node.key, self.node.generation, self.model_run_id, self.call_id, operation, arguments)


def _bind() -> _PeerBinding:
    """Resolve engine, run, node and call identity strictly from the child agent's own binding."""
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
    run_id, node_key = observation.child.workflow_run_id, observation.child.workflow_node_key
    node = engine.store.get_node(run_id, node_key)
    if node is None:
        msg = f"Workflow node {node_key} is no longer available."
        raise WorkflowRunStoreError(msg)
    session = engine.store.get_node(run_id, node.session_key) if node.session_key else None
    ref = NodeRef(
        key=node.key,
        generation=observation.child.workflow_generation or node.generation,
        name=node.name or f"n{node.seq}",
        reader_key=session.key if session else None,
        reader_generation=session.generation if session else None,
    )
    call = current_tool_call()
    return _PeerBinding(
        engine=engine,
        run_id=run_id,
        node=node,
        ref=ref,
        names=(node.name, "*") if node.name else ("*",),
        model_run_id=(call.context.run_id if call and call.context.run_id else observation.child.run_id) or "",
        call_id=call.context.call_id if call and call.context.call_id else uuid4().hex,
    )


class WorkflowPeerTools(Toolkit):
    """Peer messaging, shared state, and requester questions scoped to the child agent's workflow run."""

    @classmethod
    def build(cls, *, enabled: bool) -> WorkflowPeerTools | None:
        """Build the run-scoped peer toolkit when assembling a workflow run's child agent."""
        obs = running_child_run() if enabled else None
        return cls() if obs and obs.child.workflow_run_id and obs.child.workflow_node_key else None

    def __init__(self) -> None:
        super().__init__(
            name="workflow_peers",
            tools=[
                self.send_peer_message,
                self.read_peer_messages,
                self.list_peers,
                self.get_shared_state,
                self.set_shared_state,
                self.ask_user,
            ],
            instructions=(
                "You are running inside a workflow run. Use send_peer_message(to, text) and "
                "read_peer_messages(wait_seconds=0) to coordinate with peer agents by name "
                "('*' for all unfinished peers, 'script' for the workflow script), list_peers() to see peer names "
                "and statuses, get_shared_state()/set_shared_state() to share JSON values across the run, and "
                "ask_user(question, options=None) to ask the workflow requester a question directly."
            ),
        )

    async def send_peer_message(self, to: str, text: str) -> str:
        """Send a message to another agent in this workflow run by name, '*' for all unfinished agents, or 'script'.

        Args:
            to: Recipient peer name, '*' for every unfinished peer, or 'script'.
            text: Message text (up to 1 MiB).

        """
        b = _bind()
        op_id = b.op_id("send_peer_message", {"to": to, "text": text})
        result = await b.engine.peer_send(b.run_id, op_id=op_id, sender=b.ref, to=to, text=text)
        return json.dumps(result)

    async def read_peer_messages(self, wait_seconds: float = 0) -> str:
        """Return unread peer messages as `{"messages": [{"from", "text", "sent_at"}], "note"?: ...}`.

        When `wait_seconds > 0`, waits up to that many seconds for the first message while releasing your
        concurrency slot. May return early with `"messages": []` and `"note"` if nobody is left to send a message.

        Args:
            wait_seconds: Seconds to wait for the first unread message (0 returns immediately; no upper ceiling).

        """
        if isinstance(wait_seconds, bool) or wait_seconds < 0:
            msg = "wait_seconds must be a non-negative number."
            raise WorkflowRunStoreError(msg)
        b = _bind()
        op_id = b.op_id("read_peer_messages", {"wait_seconds": wait_seconds})
        messages, note = await b.engine.wait_for_messages(
            b.run_id,
            b.ref,
            float(wait_seconds),
            op_id=op_id,
            names=b.names,
        )
        payload: dict[str, object] = {"messages": messages}
        if note is not None:
            payload["note"] = note
        return json.dumps(payload)

    async def list_peers(self) -> str:
        """Return the name, status and label of every current agent and session in this workflow run."""
        b = _bind()
        return json.dumps({"peers": await asyncio.to_thread(list_peers, b.engine.store, b.run_id)})

    async def get_shared_state(self, key: str | None = None) -> str:
        """Read one shared-state value by `key`, or all shared-state values when `key` is omitted.

        Args:
            key: Optional state key name; omit to return all keys.

        """
        b = _bind()
        op_id = b.op_id("get_shared_state", {"key": key})
        value = await asyncio.to_thread(get_state, b.engine.store, b.run_id, op_id=op_id, key=key)
        return json.dumps(value if key is None else {"key": key, **value})

    async def set_shared_state(self, key: str, value: Any) -> str:  # noqa: ANN401 - arbitrary JSON value
        """Write a JSON value to this run's shared state under `key` (last write wins).

        Args:
            key: State key name.
            value: JSON-serializable value (up to 1 MiB).

        """
        b = _bind()
        op_id = b.op_id("set_shared_state", {"key": key, "value": value})
        result = await asyncio.to_thread(
            set_state,
            b.engine.store,
            b.run_id,
            op_id=op_id,
            key=key,
            value=value,
            updated_by=b.ref.name,
        )
        return json.dumps(result)

    async def ask_user(self, question: str, options: list[str] | None = None) -> str:
        """Ask the workflow requester a question in the thread and wait for their answer.

        Args:
            question: Question text to show on the question card.
            options: Optional list of answer choices; omit for free-text answers.

        """
        b = _bind()
        return await b.engine.ask_child_question(b.run_id, b.node, question, options)
