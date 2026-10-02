"""Bind one response's CLI owner and grant to a freshly prepared tool catalog."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING

from mindroom.agent_cli.session import MAX_CLI_GRANT_LIFETIME_NS, cli_turn_owner
from mindroom.agent_cli.shell_access import agent_cli_shell_env, minimal_shell_problems
from mindroom.agent_cli.turn import LiveTurnTools
from mindroom.approval_tools import authorize_prepared_tool_call

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.agent_cli.lifetime import CliTurnLifetime
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.response_turn import ResponseTurnContext
    from mindroom.tool_system.agent_tool_calls import PreparedAgentToolBinding, PreparedAgentToolCatalog
    from mindroom.tool_system.output_files import ToolOutputFilePolicy
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_access import ToolKey

__all__ = ["bind_response_owner"]


def bind_response_owner(
    catalog: PreparedAgentToolCatalog,
    *,
    runtime: ToolRuntimeContext,
    lifetime: CliTurnLifetime,
    response_context: ResponseTurnContext,
    run_id: str | None,
    context_documents: Mapping[str, str],
    output_file_policy: ToolOutputFilePolicy | None,
    delegation_depth: int,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    failure_message: Callable[[str], str],
) -> LiveTurnTools:
    """Register the response's owner and grant on its first attempt, or rebind its catalog after a rebuild."""
    assert runtime.orchestrator is not None
    owner = lifetime.owner
    if owner is not None:
        owner.bind_catalog(catalog, context=context_documents)
        return owner
    # Check that Bash can reach MindRoom before any grant exists.
    if problems := minimal_shell_problems(runtime.config, runtime.runtime_paths, runtime.agent_name):
        raise RuntimeError(" ".join(problems))
    expected_target = runtime.resolve_worker_target()
    bindings: dict[tuple[int, ToolKey], PreparedAgentToolBinding] = {}

    async def authorize(key: ToolKey, _arguments: dict[str, object]) -> None:
        current = new_owner.catalog
        if current.runtime_context.current_config != current.runtime_context.config:
            raise PermissionError(failure_message("Current configuration no longer permits this CLI catalog"))
        cache_key = (id(current), key)
        binding = bindings.get(cache_key)
        if binding is None:
            binding = await current.bind(key)
            bindings[cache_key] = binding
        await authorize_prepared_tool_call(binding, expected_worker_target=expected_target)

    new_owner = LiveTurnTools(
        cli_turn_owner(runtime, replace(response_context, run_id=run_id)),
        catalog=catalog,
        authorize=authorize,
        context=context_documents,
        output_file_policy=output_file_policy,
        delegation_depth=delegation_depth,
        refresh_scheduler=refresh_scheduler,
    )
    lifetime.register(new_owner, runtime.orchestrator.agent_cli_registry)
    now = time.time_ns()
    lifetime.grant_expires_at_ns = now + MAX_CLI_GRANT_LIFETIME_NS
    grant = new_owner.issue(now_ns=now, expires_at_ns=lifetime.grant_expires_at_ns)
    new_owner.shell_env = agent_cli_shell_env(
        runtime.config,
        runtime.runtime_paths,
        runtime.agent_name,
        grant.raw_token,
    )
    return new_owner
