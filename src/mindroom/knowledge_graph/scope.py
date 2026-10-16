"""Which graph an agent or private instance reads and writes.

Graphiti's ``group_id`` partitions a graph but does not authorize access, so MindRoom derives the graph from the
caller's resolved identity and never lets a model name one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.embeddings.signatures import effective_mem0_embedder_signature
from mindroom.requester_identity import agent_instances
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.tool_system.worker_routing import build_requester_execution_identity

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass(frozen=True)
class GraphScope:
    """One agent, or one requester's private instance of it, and the graph it owns."""

    agent_name: str
    execution_identity: ToolExecutionIdentity | None
    graph_name: str


def _graph_name(scope_key: str, signature: Sequence[str]) -> str:
    """Return the graph name for a scope and embedder, which reveals neither."""
    digest = hashlib.sha256("|".join((scope_key, *signature)).encode()).hexdigest()
    return f"kg_{digest[:24]}"


def _graph_signature(config: Config) -> tuple[str, ...]:
    """Return the embedder settings its vectors depend on, so another embedder means another graph."""
    embedder = config.memory.embedder
    return effective_mem0_embedder_signature(
        embedder.provider,
        embedder.config.model,
        host=embedder.config.host,
        dimensions=embedder.config.dimensions,
    )


def graph_scope(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> GraphScope:
    """Return the graph of one agent, or of the private instance the identity resolves to."""
    state_root = resolve_agent_storage(agent_name, config, runtime_paths, execution_identity).state_root
    # The state root is unique per agent and per private instance, and stable across restarts.
    scope_key = state_root.relative_to(runtime_paths.storage_root).as_posix()
    is_private = config.get_agent(agent_name).private is not None
    return GraphScope(
        agent_name=agent_name,
        execution_identity=execution_identity if is_private else None,
        graph_name=_graph_name(scope_key, _graph_signature(config)),
    )


def graph_scopes(config: Config, runtime_paths: RuntimePaths) -> list[GraphScope]:
    """Return every graph to maintain: each graph-enabled shared agent, and each private instance of a private one."""
    return [
        graph_scope(
            agent_name,
            config,
            runtime_paths,
            None
            if requester_id is None
            else build_requester_execution_identity(
                agent_name=agent_name,
                runtime_paths=runtime_paths,
                requester_id=requester_id,
            ),
        )
        for agent_name, requester_id in agent_instances(config, runtime_paths, config.knowledge_graph_agents())
    ]
