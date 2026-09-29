"""Minimal provider presentation of the canonical MindRoom Agent."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any, NoReturn

from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

from mindroom.agent_cli.bash import MinimalBashTools
from mindroom.agent_cli.context import minimal_system_message
from mindroom.agent_cli.lifetime import current_cli_lifetime
from mindroom.agent_cli.session import MAX_CLI_GRANT_LIFETIME_NS, cli_turn_owner
from mindroom.agent_cli.turn import LiveTurnTools
from mindroom.agent_cli.worker import open_configured_cli_worker
from mindroom.agent_cli.worker_protocol import SHELL_OPERATION_NAMES, CliShellSettings
from mindroom.agent_knowledge_descriptions import KnowledgeToolDescribingAgent
from mindroom.approval_tools import authorize_prepared_tool_call
from mindroom.error_handling import MinimalModeUnavailableError, minimal_mode_failure_message
from mindroom.tool_system.agent_tool_calls import DeferredAgentToolkit, PreparedAgentToolCatalog
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.tool_system.tool_access import ToolKey
from mindroom.tools.shell import ShellRuntimeSettings

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from agno.run import RunContext
    from agno.run.agent import RunOutput
    from agno.session import AgentSession

    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.response_turn import ResponseTurnContext
    from mindroom.tool_system.agent_tool_calls import PreparedAgentToolBinding
    from mindroom.tool_system.output_files import ToolOutputFilePolicy


class MinimalAgent(KnowledgeToolDescribingAgent):
    """Keep complete agent state while presenting exactly one Bash function."""

    context_documents: dict[str, str]
    bootstrap_message: str = ""
    deferred_toolkits: tuple[DeferredAgentToolkit, ...] = ()
    response_context: ResponseTurnContext | None = None
    output_file_policy: ToolOutputFilePolicy | None = None
    delegation_depth: int = 0
    refresh_scheduler: KnowledgeRefreshScheduler | None = None

    def configure_minimal(
        self,
        *,
        instructions: Sequence[str],
        interactive_prompt: str,
        context_documents: Sequence[str],
        deferred_toolkits: tuple[DeferredAgentToolkit, ...],
        toolkit_names: Sequence[str],
        minimal_instructions: Sequence[str],
        context_files: Sequence[str],
        memory_root: Path | None,
        runtime_context: str,
        output_file_policy: ToolOutputFilePolicy | None,
        delegation_depth: int,
        refresh_scheduler: KnowledgeRefreshScheduler | None,
    ) -> None:
        """Configure presentation from the canonical factory's already-resolved inputs."""
        assert self.id is not None
        assert self.name is not None
        self.deferred_toolkits = deferred_toolkits
        self.output_file_policy = output_file_policy
        self.delegation_depth = delegation_depth
        self.refresh_scheduler = refresh_scheduler
        self.context_documents = {
            "role": self.role or "",
            "instructions": "\n\n".join(instructions),
            "interactive": interactive_prompt,
            "knowledge": "\n".join(f"{source.base_id}: {source.description}" for source in self.knowledge_sources),
        }
        for index, document in enumerate(context_documents):
            self.context_documents[f"context-{index + 1}"] = document
        if self.skills is not None:
            for index, skill in enumerate(self.skills.get_all_skills()):
                self.context_documents[f"skill-{index + 1}"] = f"{skill.name}\n{skill.instructions}"
        if isinstance(self.tools, list):
            for index, toolkit in enumerate(self.tools):
                if isinstance(toolkit, Toolkit) and toolkit.instructions:
                    self.context_documents[f"toolkit-{index + 1}"] = toolkit.instructions
        self.bootstrap_message = minimal_system_message(
            agent_name=self.id,
            display_name=self.name,
            toolkit_names=[*toolkit_names, *(item.name for item in deferred_toolkits)],
            instructions=minimal_instructions,
            context_files=context_files,
            memory_root=memory_root,
            runtime_context=(self.additional_context or "") + runtime_context,
        )
        self.system_message = self.bootstrap_message

    def _failure_message(self, reason: str) -> str:
        assert self.id is not None
        return minimal_mode_failure_message(reason, self.id)

    def _raise_failure(self, exc: BaseException) -> NoReturn:
        if not isinstance(exc, Exception):
            raise exc
        raise MinimalModeUnavailableError(self._failure_message(str(exc))) from exc

    def get_tools(
        self,
        run_response: RunOutput,  # noqa: ARG002 - pure SDK inspection
        run_context: RunContext,  # noqa: ARG002 - pure SDK inspection
        session: AgentSession,  # noqa: ARG002 - pure SDK inspection
        user_id: str | None = None,  # noqa: ARG002 - pure SDK inspection
    ) -> list[Any]:
        """Pure presentation for synchronous prompt inspection; never bind a worker."""
        return [MinimalBashTools()]

    @staticmethod
    def _validate_handlers(processed_tools: list[Any]) -> None:
        for tool in processed_tools:
            functions = list(tool.get_async_functions().values()) if isinstance(tool, Toolkit) else [tool]
            for function in functions:
                if isinstance(function, Function) and (
                    function.requires_user_input
                    or (function.external_execution and function.owning_toolkit != "delegate")
                ):
                    msg = f"Configured function {function.name!r} has no supported response continuation handler"
                    raise RuntimeError(msg)

    async def aget_execution_tools(
        self,
        run_response: RunOutput,
        run_context: RunContext,
        session: AgentSession,
        user_id: str | None = None,
        check_mcp_tools: bool = True,
    ) -> list[Any]:
        """Obtain the full filtered catalog, including requester-bound generated tools."""
        processed = await super().aget_tools(
            run_response,
            run_context,
            session,
            user_id=user_id,
            check_mcp_tools=check_mcp_tools,
        )
        if isinstance(self.tools, list):
            for toolkit in self.tools:
                if isinstance(toolkit, Toolkit) and not any(tool is toolkit for tool in processed):
                    msg = f"Configured toolkit {toolkit.name!r} failed connection"
                    raise RuntimeError(msg)
        self._validate_handlers(processed)
        return processed

    async def prepare_execution_catalog(
        self,
        run_response: RunOutput,
        run_context: RunContext,
        session: AgentSession,
        *,
        user_id: str | None,
        check_mcp_tools: bool = True,
    ) -> PreparedAgentToolCatalog:
        """Prepare hidden tools on this Agent, without starting a shell worker."""
        runtime = get_tool_runtime_context()
        if runtime is None or user_id != runtime.requester_id:
            msg = self._failure_message("Minimal catalog requires its authenticated requester")
            raise MinimalModeUnavailableError(msg)
        catalog = PreparedAgentToolCatalog(self, run_context, run_response, session, runtime)
        try:
            await catalog.prepare(
                await self.aget_execution_tools(
                    run_response,
                    run_context,
                    session,
                    user_id=user_id,
                    check_mcp_tools=check_mcp_tools,
                ),
            )
            loaded_names = {item["toolkit"] for item in catalog.metadata()}
            for deferred in self.deferred_toolkits:
                if deferred.name in loaded_names:
                    continue

                async def materialize(deferred: DeferredAgentToolkit = deferred) -> Toolkit:
                    toolkit = await deferred.materialize()
                    # Reuse this Agent's native connection and cleanup owner.
                    self.add_tool(toolkit)
                    available = await self.aget_execution_tools(
                        run_response,
                        run_context,
                        session,
                        user_id=user_id,
                        check_mcp_tools=check_mcp_tools,
                    )
                    if not any(tool is toolkit for tool in available):
                        msg = f"Configured toolkit {deferred.name!r} failed connection"
                        raise RuntimeError(msg)  # noqa: TRY301 - catalog cleanup owns failed materialization
                    return toolkit

                catalog.add_deferred(DeferredAgentToolkit(deferred.name, deferred.description, materialize))
            # Validate the effective prepared surface for startup, rebuilds, and
            # approval recovery before publishing a catalog or gaining authority.
            try:
                for name in SHELL_OPERATION_NAMES:
                    await catalog.bind(ToolKey("shell", name))
            except ValueError as exc:
                msg = "Minimal mode requires the agent's run, check, and kill shell permissions."
                raise RuntimeError(msg) from exc
            # The SDK calls this preparation before its module-level message builder;
            # overriding Agent.aget_system_message does not intercept live runs.
            bootstrap = self.system_message
            self.system_message = None
            try:
                full = await super().aget_system_message(
                    session,
                    run_context,
                    list(catalog.prepared_functions()),
                    self.add_session_state_to_context,
                    run_response.input.input_content if run_response.input is not None else None,
                )
            finally:
                self.system_message = bootstrap
            if full is not None and full.content is not None:
                self.context_documents["agent-context"] = str(full.content)
        except BaseException as exc:
            await catalog.close()
            self._raise_failure(exc)
        return catalog

    async def aget_tools(
        self,
        run_response: RunOutput,
        run_context: RunContext,
        session: AgentSession,
        user_id: str | None = None,
        check_mcp_tools: bool = True,
    ) -> list[Any]:
        """Bind the real managed turn lazily, then return only its Bash facade."""
        lifetime = current_cli_lifetime()
        runtime = get_tool_runtime_context()
        if lifetime is None or runtime is None or runtime.orchestrator is None or self.response_context is None:
            msg = self._failure_message("Minimal Bash requires an active managed response owner")
            raise MinimalModeUnavailableError(msg)
        catalog = await self.prepare_execution_catalog(
            run_response,
            run_context,
            session,
            user_id=user_id,
            check_mcp_tools=check_mcp_tools,
        )
        try:
            if self.output_file_policy is None:
                msg = "Minimal Bash requires a canonical agent workspace"
                raise RuntimeError(msg)  # noqa: TRY301 - shared failed-preparation cleanup
            shell_toolkits = (
                [tool for tool in self.tools if isinstance(tool, ShellRuntimeSettings)]
                if isinstance(self.tools, list)
                else []
            )
            if len(shell_toolkits) != 1:
                msg = "Canonical shell is missing its effective runtime settings"
                raise TypeError(msg)  # noqa: TRY301 - shared failed-preparation cleanup
            shell_settings = CliShellSettings(
                workspace=str(self.output_file_policy.workspace_root),
                shell_path_prepend=shell_toolkits[0].shell_path_prepend,
                output_max_bytes=self.output_file_policy.max_bytes,
                output_auto_save_threshold_bytes=self.output_file_policy.auto_save_threshold_bytes,
            )
            expected_target = runtime.resolve_worker_target()
            bindings: dict[tuple[int, ToolKey], PreparedAgentToolBinding] = {}

            async def authorize(key: ToolKey, _arguments: dict[str, object]) -> None:
                assert owner is not None
                current = owner.catalog
                if current.runtime_context.current_config != current.runtime_context.config:
                    msg = self._failure_message("Current configuration no longer permits this CLI catalog")
                    raise PermissionError(msg)  # noqa: TRY301 - callback runs after preparation
                cache_key = (id(current), key)
                binding = bindings.get(cache_key)
                if binding is None:
                    binding = await current.bind(key)
                    bindings[cache_key] = binding
                await authorize_prepared_tool_call(binding, expected_worker_target=expected_target)

            owner = lifetime.owner
            if owner is None:
                worker = await lifetime.enter_worker(open_configured_cli_worker(runtime))
                owner = LiveTurnTools(
                    cli_turn_owner(
                        runtime,
                        replace(self.response_context, run_id=run_context.run_id),
                        worker_id=worker.handle.worker_id,
                    ),
                    catalog=catalog,
                    worker=worker,
                    authorize=authorize,
                    context=self.context_documents,
                    output_file_policy=self.output_file_policy,
                    delegation_depth=self.delegation_depth,
                    refresh_scheduler=self.refresh_scheduler,
                )
                lifetime.register(owner, runtime.orchestrator.agent_cli_registry)
                now = time.time_ns()
                lifetime.grant_expires_at_ns = now + MAX_CLI_GRANT_LIFETIME_NS
                grant = owner.issue(now_ns=now, expires_at_ns=lifetime.grant_expires_at_ns)
                await worker.install_grant(owner, grant, shell=shell_settings)
            else:
                owner.bind_catalog(catalog, context=self.context_documents)

            def prepared(function: Function) -> None:
                lifetime.bind_provider(owner.checkpoint, function)

            facade = MinimalBashTools(execute=owner.execute_bash, on_prepare=prepared)
        except BaseException as exc:
            await catalog.close()
            self._raise_failure(exc)
        return [facade]
