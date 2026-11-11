"""Run one configured agent's turn from another Python program, without a Matrix homeserver or ``mindroom run``."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agents import tool_workdir
from mindroom.ai import ai_response
from mindroom.config.main import Config
from mindroom.config.resolution import agent_knowledge_base_ids, memory_backend, memory_search
from mindroom.constants import AI_RUN_METADATA_KEY, resolve_runtime_paths
from mindroom.entity_resolution import persist_offline_entity_accounts
from mindroom.matrix.state import matrix_state_for_runtime
from mindroom.providers.model_http_client import closing_model_connections
from mindroom.response_turn import detached_turn_context
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import (
    DetachedRequesterContext,
    detached_requester_context,
    detached_tool_scope,
)
from mindroom.tool_system.sandbox_proxy import tool_placement
from mindroom.tool_system.worker_routing import build_tool_execution_identity
from mindroom.turn_model import ModelScope, TurnBudget, resolve_turn_model

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from mindroom.constants import RuntimePaths


__all__ = ["HeadlessConfigError", "HeadlessResult", "run_turn", "validate_config"]


class HeadlessConfigError(ValueError):
    """A config, storage, or requester that a headless run cannot serve; ``problems`` names each reason."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass(frozen=True)
class HeadlessResult:
    """How one headless turn ended: its reply text, and whether the turn completed or failed with that text."""

    text: str
    completed: bool
    failed: bool
    # The tool calls the agent itself made in the attempt that ended the turn; a subagent's calls are not counted.
    tool_calls: int
    run_metadata: Mapping[str, Any]


def _runtime_paths(storage: Path) -> RuntimePaths:
    storage = storage.expanduser().resolve()
    return resolve_runtime_paths(config_path=storage / "config.yaml", storage_path=storage)


def _agent_problems(config: Config, runtime_paths: RuntimePaths, agent: str) -> list[str]:
    """Return why ``agent`` or a subagent it may delegate to needs a running MindRoom or a Matrix room."""
    tools = config_plugins(config, runtime_paths).tools
    placement = tool_placement(runtime_paths, tools)
    problems = []
    for name in sorted(config.get_agent_delegation_closure(agent)):
        if agent_knowledge_base_ids(config, name):
            problems.append(f"agent {name!r}: knowledge bases need a running MindRoom to index them")
        if memory_backend(config, name) == "file" and memory_search(config, name).mode == "semantic":
            problems.append(f"agent {name!r}: semantic memory_search needs a running MindRoom to index memory")
        agent_tools = resolve_agent_tools(config, name, tools)
        mcp = [tool for tool in agent_tools.tool_names if (metadata := tools.get(tool)) and metadata.mcp_server_id]
        if mcp:
            problems.append(f"agent {name!r}: MCP tools need a running MindRoom: {', '.join(mcp)}")
        routed = [
            tool for tool in agent_tools.tool_names if tool not in mcp and agent_tools.runs_in_worker(tool, placement)
        ]
        if routed:
            problems.append(f"agent {name!r}: {', '.join(routed)} run in a worker; leave them out of its worker_tools")
        if (
            "shell" in agent_tools.tool_names
            and "shell" not in routed
            and not config.defaults.allow_shell_without_worker
        ):
            problems.append(
                f"agent {name!r}: shell runs in this process; set defaults.allow_shell_without_worker: true",
            )
    return problems


def _problems(config: Config, runtime_paths: RuntimePaths, agent: str, requester_id: str | None) -> list[str]:
    if agent in config.teams:
        return [f"{agent!r} is a team; headless runs serve agents only"]
    if agent not in config.agents:
        return [f"no agent named {agent!r} is configured"]
    problems = []
    if requester_id is None:
        problems.append("administrators is empty; add the requester to it or pass requester_id")
    elif requester_id not in config.administrators:
        problems.append(f"requester {requester_id!r} is not in administrators")
    if config.plugins:
        problems.append("plugins need a running MindRoom to load them")
    if any(
        account.password or account.access_token
        for account in matrix_state_for_runtime(runtime_paths).accounts.values()
    ):
        problems.append(
            f"storage {runtime_paths.storage_root} holds a MindRoom instance's Matrix accounts; "
            "give headless runs a storage of their own",
        )
    return [*problems, *_agent_problems(config, runtime_paths, agent)]


def validate_config(
    config: Mapping[str, Any],
    *,
    agent: str,
    storage: Path,
    requester_id: str | None = None,
) -> Config:
    """Validate ``config`` as ``run_turn`` would run it, raising ``HeadlessConfigError`` for what it cannot serve."""
    runtime_paths = _runtime_paths(storage)
    validated = Config.validate_with_runtime(dict(config), runtime_paths)
    requester_id = requester_id or next(iter(validated.administrators), None)
    if problems := _problems(validated, runtime_paths, agent, requester_id):
        raise HeadlessConfigError(problems)
    return validated


async def run_turn(
    config: Mapping[str, Any],
    *,
    agent: str,
    prompt: str,
    storage: Path,
    workdir: Path | None = None,
    requester_id: str | None = None,
) -> HeadlessResult:
    """Run ``agent``'s reply to ``prompt`` on this event loop, as ``requester_id`` or else the first administrator.

    MindRoom keeps its state under ``storage``. ``workdir``, when set, is where the file and shell tools of the agent
    and its subagents work. Cancelling the call cancels the turn and its running tool calls.
    """
    validated = validate_config(config, agent=agent, storage=storage, requester_id=requester_id)
    workdir = workdir.expanduser().resolve() if workdir is not None else None
    runtime_paths = _runtime_paths(storage)
    requester = requester_id or validated.administrators[0]
    persist_offline_entity_accounts(validated, runtime_paths)
    identity = build_tool_execution_identity(
        channel="headless",
        agent_name=agent,
        runtime_paths=runtime_paths,
        requester_id=requester,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=f"headless:{uuid4().hex}",
    )
    authority = DetachedRequesterContext(
        requester_id=requester,
        config=validated,
        runtime_paths=runtime_paths,
        agent_reply_memberships=AgentReplyMembershipIndex(),
        config_provider=lambda: validated,
        budget_monitor=None,
    )
    model = resolve_turn_model(validated, runtime_paths, ModelScope(entity=agent), TurnBudget(None, requester))
    turn = detached_turn_context(
        agent,
        session_id=identity.session_id,
        execution_identity=identity,
        model_name=model.model_name,
        prompt=prompt,
    )
    with detached_requester_context(authority), detached_tool_scope(identity), tool_workdir(workdir):
        result = await closing_model_connections(
            ai_response(replace(turn, show_tool_calls=False), runtime_paths, validated),
        )
    metadata = result.run_metadata
    return HeadlessResult(
        text=result.text,
        completed=result.completed is not None,
        failed=result.failed is not None,
        tool_calls=metadata.get(AI_RUN_METADATA_KEY, {}).get("tools", {}).get("count", 0),
        run_metadata=metadata,
    )
