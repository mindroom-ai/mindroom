"""Run one configured agent's turn from another Python program, without a Matrix homeserver or ``mindroom run``."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.ai import ai_response
from mindroom.config.main import Config
from mindroom.config.resolution import agent_knowledge_base_ids, memory_backend, memory_search, uses_knowledge_graph
from mindroom.constants import AI_RUN_METADATA_KEY, resolve_runtime_paths
from mindroom.entity_resolution import persist_offline_entity_accounts
from mindroom.matrix.state import matrix_state_for_runtime
from mindroom.providers.model_http_client import aclose_model_http_client
from mindroom.response_turn import detached_turn_context
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.output_files import tool_workdir
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
    from collections.abc import Awaitable, Mapping
    from pathlib import Path

    from mindroom.constants import RuntimePaths


__all__ = ["HeadlessConfigError", "HeadlessResult", "run_turn", "validate_config"]


class HeadlessConfigError(ValueError):
    """A config, storage, workdir, or requester that a headless run cannot serve; ``problems`` names each reason."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))

    def __reduce__(self) -> tuple[type[HeadlessConfigError], tuple[list[str]]]:
        """Pickle by the problems, so the error keeps its message in another process, such as a process pool's."""
        return type(self), (self.problems,)


@dataclass(frozen=True)
class HeadlessResult:
    """How one headless turn ended: its reply text, and whether the turn completed or failed with that text."""

    text: str
    completed: bool
    failed: bool
    # The tool calls the agent itself made in the attempt that ended the turn; a subagent's calls are not counted.
    tool_calls: int
    run_metadata: Mapping[str, Any]


# How many headless turns each event loop is running; the last one to end closes the loop's pooled model connections.
_ACTIVE_TURNS: Counter[asyncio.AbstractEventLoop] = Counter()


async def _closing_connections_after_last_turn[T](work: Awaitable[T]) -> T:
    """Await one turn, then close the loop's pooled model connections unless another turn is still using them."""
    loop = asyncio.get_running_loop()
    _ACTIVE_TURNS[loop] += 1
    try:
        return await work
    finally:
        _ACTIVE_TURNS[loop] -= 1
        if not _ACTIVE_TURNS[loop]:
            del _ACTIVE_TURNS[loop]
            await aclose_model_http_client()


def _agent_problems(config: Config, runtime_paths: RuntimePaths, agent: str) -> list[str]:
    """Return why ``agent`` or a subagent it may delegate to needs a running MindRoom or a Matrix room."""
    tools = config_plugins(config, runtime_paths).tools
    placement = tool_placement(runtime_paths, tools)
    allow_shell = config.defaults.allow_shell_without_worker
    problems = []
    for name in sorted(config.get_agent_delegation_closure(agent)):
        if agent_knowledge_base_ids(config, name):
            problems.append(f"agent {name!r}: knowledge bases need a running MindRoom to index them")
        if memory_backend(config, name) == "file" and memory_search(config, name).mode == "semantic":
            problems.append(f"agent {name!r}: semantic memory_search needs a running MindRoom to index memory")
        if uses_knowledge_graph(config, name):
            problems.append(f"agent {name!r}: the knowledge graph needs a running MindRoom")
        agent_tools = resolve_agent_tools(config, name, tools)
        mcp = [tool for tool in agent_tools.tool_names if tools.mcp_server_id(tool)]
        if mcp:
            problems.append(f"agent {name!r}: MCP tools need a running MindRoom: {', '.join(mcp)}")
        routed = [
            tool for tool in agent_tools.tool_names if tool not in mcp and agent_tools.runs_in_worker(tool, placement)
        ]
        if routed:
            problems.append(
                f"agent {name!r} runs {', '.join(routed)} in a worker; set its worker_tools: [] to run them here",
            )
        if "shell" in agent_tools.tool_names and not allow_shell:
            problems.append(
                f"agent {name!r}: shell runs in this process; set defaults.allow_shell_without_worker: true",
            )
    return problems


def _problems(
    config: Config,
    runtime_paths: RuntimePaths,
    agent: str,
    requester_id: str | None,
    workdir: Path | None,
) -> list[str]:
    problems = []
    if agent in config.teams:
        problems.append(f"{agent!r} is a team; headless runs serve agents only")
    elif agent not in config.agents:
        problems.append(f"no agent named {agent!r} is configured")
    else:
        problems.extend(_agent_problems(config, runtime_paths, agent))
    if requester_id is None:
        problems.append("administrators is empty; add the requester to it")
    elif requester_id not in config.administrators:
        problems.append(f"requester {requester_id!r} is not in administrators")
    if any(entry.enabled for entry in config.plugins):
        problems.append("plugins need a running MindRoom to load them")
    if any(
        account.password or account.access_token
        for account in matrix_state_for_runtime(runtime_paths).accounts.values()
    ):
        problems.append(
            f"storage {runtime_paths.storage_root} holds a MindRoom instance's Matrix accounts; "
            "give headless runs a storage of their own",
        )
    if workdir is not None and not workdir.is_dir():
        problems.append(f"workdir {workdir} is not an existing directory")
    return problems


def _validated(
    config: Mapping[str, Any],
    *,
    agent: str,
    storage: Path,
    workdir: Path | None,
    requester_id: str | None,
) -> tuple[Config, RuntimePaths, Path | None, str]:
    """Return the validated config, its runtime paths, the workdir, and the requester, or raise ``HeadlessConfigError``.

    The path tools open their base directory without following a link, so the workdir is the directory itself.
    """
    storage = storage.expanduser().resolve()
    runtime_paths = resolve_runtime_paths(config_path=storage / "config.yaml", storage_path=storage)
    validated = Config.validate_with_runtime(dict(config), runtime_paths)
    workdir = workdir.expanduser().resolve() if workdir is not None else None
    requester = requester_id if requester_id is not None else next(iter(validated.administrators), None)
    if problems := _problems(validated, runtime_paths, agent, requester, workdir):
        raise HeadlessConfigError(problems)
    assert requester is not None, "a missing requester is one of the problems"
    return validated, runtime_paths, workdir, requester


def validate_config(
    config: Mapping[str, Any],
    *,
    agent: str,
    storage: Path,
    workdir: Path | None = None,
    requester_id: str | None = None,
) -> Config:
    """Validate ``config`` as ``run_turn`` would run it, raising ``HeadlessConfigError`` for what it cannot serve."""
    return _validated(config, agent=agent, storage=storage, workdir=workdir, requester_id=requester_id)[0]


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
    validated, runtime_paths, workdir, requester = _validated(
        config,
        agent=agent,
        storage=storage,
        workdir=workdir,
        requester_id=requester_id,
    )
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
        result = await _closing_connections_after_last_turn(
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
