"""Create an attempt's tool catalog, and bind the response's CLI owner and grant to it once prepared."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING

from mindroom.agent_cli.session import MAX_CLI_GRANT_LIFETIME_NS, CliCatalogReloadedError, cli_turn_owner
from mindroom.agent_cli.shell_access import agent_cli_shell_env, minimal_shell_problems
from mindroom.agent_cli.turn import LiveTurnTools
from mindroom.approval.tool_owners import authorize_prepared_tool_call, check_prepared_catalog_authority
from mindroom.tool_jobs.execution import agent_tool_middleware
from mindroom.tool_system.agent_tool_calls import PreparedAgentToolCatalog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from mindroom.agent_cli.lifetime import CliTurnLifetime
    from mindroom.agent_loop.build import AgentBuild
    from mindroom.agent_loop.spec import RunContext
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.llm.runs import RunRecord
    from mindroom.response_turn import ResponseTurnContext
    from mindroom.run_storage import RunStorage
    from mindroom.tool_system.agent_tool_calls import PreparedAgentToolBinding
    from mindroom.tool_system.output_files import ToolOutputFilePolicy
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_access import ToolKey
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

__all__ = ["bind_response_owner", "tool_catalog"]

_RELOADED = (
    "This MindRoom call did not run: the configuration was reloaded after this turn's tools were built. "
    "A new turn's calls run under the reloaded configuration."
)


def tool_catalog(
    build: AgentBuild,
    *,
    run: RunContext,
    runtime: ToolRuntimeContext,
    storage: RunStorage,
    record: RunRecord | None = None,
) -> PreparedAgentToolCatalog:
    """Return an empty catalog for ``build``'s tools in run ``run``, under the build's tool-call policy and call limit.

    ``record`` is the stored run an approval recovery continues.
    In minimal mode the batch checkpoint sets it for a live turn.
    ``runtime`` must be the build's agent's, since the catalog's generated functions run under it.
    """
    if runtime.agent_name != build.spec.id:
        msg = f"A tool catalog for agent {build.spec.id!r} needs its runtime context, not {runtime.agent_name!r}'s"
        raise ValueError(msg)
    return PreparedAgentToolCatalog(
        run=run,
        runtime_context=runtime,
        store=storage.store,
        middleware=agent_tool_middleware(build),
        max_tool_calls_per_turn=build.spec.max_tool_calls_per_turn,
        record=record,
    )


def _build_cli_authorizer(
    expected_target: ResolvedWorkerTarget,
    *,
    reload_ends_run: bool,
    sandbox_only: bool,
) -> Callable[[LiveTurnTools, ToolKey | str, dict[str, object]], Awaitable[None]]:
    bindings: dict[tuple[int, ToolKey], PreparedAgentToolBinding] = {}

    async def authorize(owner: LiveTurnTools, key: ToolKey | str, _arguments: dict[str, object]) -> None:
        current = owner.catalog

        def check_authority(target: ToolKey | str) -> None:
            try:
                check_prepared_catalog_authority(current.runtime_context, key=target)
            except PermissionError as exc:
                if reload_ends_run:
                    owner.end_run_for_reload()
                raise CliCatalogReloadedError(_RELOADED) from exc

        if isinstance(key, str) and (prepared := current.prepared_single_key(key)) is not None:
            key = prepared
        # Before binding, so a refused catalog builds no deferred toolkit.
        check_authority(key)
        binding = None if isinstance(key, str) else bindings.get((id(current), key))
        if binding is None:
            binding = await current.bind(key, check_current=lambda: check_authority(key))
            bindings[(id(current), binding.key)] = binding
        # Again after the bind's awaits, so the same check in the call below, which runs first, never refuses.
        check_authority(binding.key)
        if sandbox_only and current.runtime_context.resolve_worker_target().placement != expected_target.placement:
            msg = "Sandboxed minimal subagent placement changed during execution"
            raise RuntimeError(msg)
        await authorize_prepared_tool_call(binding, expected_worker_target=expected_target)

    return authorize


def bind_response_owner(
    catalog: PreparedAgentToolCatalog,
    *,
    lifetime: CliTurnLifetime,
    response_context: ResponseTurnContext,
    context_documents: Mapping[str, str],
    output_file_policy: ToolOutputFilePolicy,
    delegation_depth: int,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    reload_ends_run: bool,
    sandbox_only: bool = False,
) -> LiveTurnTools:
    """Register the response's owner and grant on its first attempt, or rebind its catalog after a rebuild.

    The owner runs as the catalog's tool runtime, for the catalog's run. A config reload that changes what the
    catalog was built from refuses its calls; with ``reload_ends_run``, a minimal turn's, the first refusal also stops
    the run after its batch, since no later call of the turn could run. With ``sandbox_only``, no CLI token is
    minted and no registry entry is created: Bash runs purely inside the isolated sandbox pod.
    """
    runtime = catalog.runtime_context
    if not sandbox_only:
        assert runtime.orchestrator is not None
    if (existing := lifetime.owner) is not None:
        existing.bind_catalog(catalog, context=context_documents)
        return existing
    if not sandbox_only and (
        problems := minimal_shell_problems(runtime.config, runtime.runtime_paths, runtime.agent_name)
    ):
        # Check that Bash can reach MindRoom before any grant exists.
        raise RuntimeError(" ".join(problems))
    authorize = _build_cli_authorizer(
        runtime.resolve_worker_target(),
        reload_ends_run=reload_ends_run,
        sandbox_only=sandbox_only,
    )

    new_owner = LiveTurnTools(
        cli_turn_owner(runtime, replace(response_context, run_id=catalog.run.run_id)),
        catalog=catalog,
        authorize=authorize,
        context=context_documents,
        output_file_policy=output_file_policy,
        delegation_depth=delegation_depth,
        refresh_scheduler=refresh_scheduler,
        sandbox_only=sandbox_only,
    )
    if sandbox_only:
        lifetime.register(new_owner, None)
        return new_owner
    assert runtime.orchestrator is not None
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
