"""Build agent-context and registry-backed toolkits for one agent build."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.timing import timed
from mindroom.tool_system.catalog import get_tool_by_name
from mindroom.tool_system.construction import ToolConstruction, bind_toolkit_construction, tool_config_signature
from mindroom.tool_system.output_files import ToolOutputFilePolicy, current_tool_workdir
from mindroom.tool_system.skills import agent_workspace_skills_root
from mindroom.tool_system.toolkits import configure_toolkit_functions

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from mindroom import constants
    from mindroom.config.main import Config
    from mindroom.delegation.state import SubagentPersona
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.runtime_resolution import ResolvedAgentRuntime
    from mindroom.tool_system.agent_tools import AgentTools
    from mindroom.tool_system.authoring import Toolkit
    from mindroom.tool_system.dynamic_toolkits import DeferredToolkit
    from mindroom.tool_system.registry import ToolRegistry
    from mindroom.tool_system.sandbox_proxy import ToolPlacement
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass(frozen=True, slots=True)
class _AgentToolkitBuildContext:
    agent_name: str
    config: Config
    runtime_paths: constants.RuntimePaths
    workspace_root: Path
    execution_identity: ToolExecutionIdentity | None
    delegation_depth: int
    refresh_scheduler: KnowledgeRefreshScheduler | None
    persona: SubagentPersona | None
    deferred_toolkits: Callable[[], Sequence[DeferredToolkit]] | None
    calls_as_jobs: bool

    def _memory(self) -> Toolkit:
        from mindroom.custom_tools.memory import MemoryTools  # noqa: PLC0415

        return MemoryTools(self.agent_name, self.config, self.runtime_paths, self.execution_identity)

    def _knowledge_graph(self) -> Toolkit:
        from mindroom.custom_tools.knowledge_graph import KnowledgeGraphTools  # noqa: PLC0415

        return KnowledgeGraphTools(self.agent_name, self.config, self.runtime_paths, self.execution_identity)

    def _delegate(self) -> Toolkit:
        # Imported lazily to avoid a cycle: delegate -> ai -> agents -> toolkit_builder -> delegate.
        from mindroom.custom_tools.delegate import DelegateTools  # noqa: PLC0415

        return DelegateTools(
            agent_name=self.agent_name,
            runtime_paths=self.runtime_paths,
            config=self.config,
            execution_identity=self.execution_identity,
            delegation_depth=self.delegation_depth,
            refresh_scheduler=self.refresh_scheduler,
            workspace_root=self.workspace_root,
            persona=self.persona,
            calls_as_jobs=self.calls_as_jobs,
        )

    def _self_config(self) -> Toolkit:
        from mindroom.custom_tools.self_config import SelfConfigTools  # noqa: PLC0415

        return SelfConfigTools(self.agent_name, self.runtime_paths)

    def _agent_manager(self) -> Toolkit:
        from mindroom.custom_tools.agent_manager import AgentManagerTools  # noqa: PLC0415

        can_create = self.config.get_agent(self.agent_name).created_agents_per_owner > 0
        info = self.config.generated_agent(self.agent_name)
        return AgentManagerTools(
            self.agent_name,
            can_create=can_create,
            generated=info is not None,
            created=info is not None and info.created,
        )

    def _contacts(self) -> Toolkit:
        from mindroom.custom_tools.contacts import ContactsTools  # noqa: PLC0415

        return ContactsTools(self.agent_name)

    def _skill_manage(self) -> Toolkit:
        from mindroom.custom_tools.skill_manage import SkillManageTools  # noqa: PLC0415

        return SkillManageTools(
            self.agent_name,
            self.config,
            self.runtime_paths,
            agent_workspace_skills_root(self.workspace_root),
        )

    def _compact_context(self) -> Toolkit:
        from mindroom.custom_tools.compact_context import CompactContextTools  # noqa: PLC0415

        return CompactContextTools(self.agent_name, self.config, self.runtime_paths, self.execution_identity)

    def _report_publishing(self) -> Toolkit:
        from mindroom.custom_tools.report_publishing import ReportPublishingTools  # noqa: PLC0415

        return ReportPublishingTools()

    def _dynamic_tools(self) -> Toolkit | None:
        if self.deferred_toolkits is None:
            return None
        from mindroom.custom_tools.dynamic_tools import DynamicToolsToolkit  # noqa: PLC0415

        return DynamicToolsToolkit(deferred_toolkits=self.deferred_toolkits)


# Skill management keeps its own output handling; dynamic tool search returns inline.
_DIRECT_OUTPUT_TOOLKITS = frozenset({"skill_manage", "dynamic_tools"})

# Toolkits whose constructors require per-agent build context rather than generic registry metadata.
_CONTEXT_TOOLKIT_BUILDERS: dict[str, Callable[[_AgentToolkitBuildContext], Toolkit | None]] = {
    "memory": _AgentToolkitBuildContext._memory,
    "knowledge_graph": _AgentToolkitBuildContext._knowledge_graph,
    "delegate": _AgentToolkitBuildContext._delegate,
    "self_config": _AgentToolkitBuildContext._self_config,
    "agent_manager": _AgentToolkitBuildContext._agent_manager,
    "contacts": _AgentToolkitBuildContext._contacts,
    "skill_manage": _AgentToolkitBuildContext._skill_manage,
    "compact_context": _AgentToolkitBuildContext._compact_context,
    "report_publishing": _AgentToolkitBuildContext._report_publishing,
    "dynamic_tools": _AgentToolkitBuildContext._dynamic_tools,
}


def _tool_base_dir_override(tool_name: str, *, tools: ToolRegistry, workspace_path: Path) -> dict[str, object] | None:
    """Point a registered tool that exposes a ``base_dir`` config field at the bound workdir or the agent's workspace."""
    metadata = tools.get(tool_name)
    config_fields = (metadata.config_fields if metadata is not None else None) or ()
    if not any(field.name == "base_dir" for field in config_fields):
        return None
    return {"base_dir": str(current_tool_workdir() or workspace_path)}


@timed("system_prompt_assembly.agent_create.toolkit_build")
def build_agent_toolkit(
    tool_name: str,
    *,
    agent_tools: AgentTools,
    placement: ToolPlacement,
    runtime_paths: constants.RuntimePaths,
    agent_runtime: ResolvedAgentRuntime | None = None,
    tool_config_overrides: dict[str, object] | None = None,
    execution_identity: ToolExecutionIdentity | None,
    workspace_in_worker: bool = False,
    delegation_depth: int = 0,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    persona: SubagentPersona | None = None,
    deferred_toolkits: Callable[[], Sequence[DeferredToolkit]] | None = None,
    calls_as_jobs: bool = False,
) -> Toolkit | None:
    """Build one of the toolkits ``agent_tools`` offers the agent, where ``placement`` runs it.

    ``persona`` is an authored subagent's persona; its ``delegate`` toolkit keeps
    the copies it authors within the persona's tools, skills, and knowledge bases.
    ``calls_as_jobs`` says whether the agent's build lets this toolkit's calls become background jobs.
    ``workspace_in_worker`` says whether the build's workspace-path toolkits run in a worker, where attachments save.

    ``deferred_toolkits`` returns the wire-deferred toolkits that ``dynamic_tools`` searches, as the agent's runs offer
    them; without it ``dynamic_tools`` is skipped and None is returned.
    """
    config = agent_tools.config
    agent_name = agent_tools.agent_name
    if agent_runtime is None:
        agent_runtime = resolve_agent_runtime(
            agent_name,
            config,
            runtime_paths,
            execution_identity=execution_identity,
            create=True,
        )

    if (build := _CONTEXT_TOOLKIT_BUILDERS.get(tool_name)) is not None:
        toolkit = build(
            _AgentToolkitBuildContext(
                agent_name=agent_name,
                config=config,
                runtime_paths=runtime_paths,
                workspace_root=agent_runtime.workspace.root,
                execution_identity=execution_identity,
                delegation_depth=delegation_depth,
                refresh_scheduler=refresh_scheduler,
                persona=persona,
                deferred_toolkits=deferred_toolkits,
                calls_as_jobs=calls_as_jobs,
            ),
        )
        if toolkit is None:
            return None
        if tool_name not in _DIRECT_OUTPUT_TOOLKITS:
            policy = ToolOutputFilePolicy.for_current_tools(
                agent_runtime.workspace.root,
                runtime_paths,
                auto_save_threshold_bytes=config.defaults.tool_output_auto_save_threshold_bytes,
            )
            configure_toolkit_functions(toolkit, output_files=policy)
        return bind_toolkit_construction(
            toolkit,
            ToolConstruction(tool_name, None, tool_config_signature(tool_config_overrides)),
        )

    worker_scope = agent_runtime.execution.execution_scope
    return get_tool_by_name(
        tool_name,
        runtime_paths,
        tools=agent_tools.tools,
        credentials_manager=get_runtime_credentials_manager(runtime_paths),
        runtime_config=config,
        tool_config_overrides=tool_config_overrides,
        tool_init_overrides=_tool_base_dir_override(
            tool_name,
            tools=agent_tools.tools,
            workspace_path=agent_runtime.workspace.root,
        ),
        runs_in_worker=agent_tools.runs_in_worker(tool_name, placement),
        allowed_shared_services=config.get_worker_grantable_credentials() if worker_scope is not None else None,
        tool_output_workspace_root=agent_runtime.workspace.root,
        agent_state_root=agent_runtime.state_root,
        tool_output_auto_save_threshold_bytes=config.defaults.tool_output_auto_save_threshold_bytes,
        workspace_in_worker=workspace_in_worker,
        worker_target=agent_runtime.execution.worker_target,
    )


__all__ = ["build_agent_toolkit"]
