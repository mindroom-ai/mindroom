"""Build agent-context and registry-backed toolkits for one agent build."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.timing import timed
from mindroom.tool_system.catalog import get_tool_by_name, safe_tool_init_override_fields
from mindroom.tool_system.construction import ToolConstruction, tool_config_signature
from mindroom.tool_system.output_files import ToolOutputFilePolicy, current_tool_workdir
from mindroom.tool_system.skills import agent_workspace_skills_root
from mindroom.tool_system.toolkits import configure_toolkit_functions
from mindroom.workers.models import SANDBOX_ALLOWED_TOOLKITS

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom import constants
    from mindroom.delegation.state import SubagentPersona
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.runtime_resolution import ResolvedAgentRuntime
    from mindroom.tool_system.agent_tools import AgentTools
    from mindroom.tool_system.authoring import Toolkit
    from mindroom.tool_system.dynamic_toolkits import DeferredToolkit
    from mindroom.tool_system.sandbox_proxy import ToolPlacement
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity
    from mindroom.workers.models import WorkerSandboxConfig

# Skill management keeps its own output handling; dynamic tool search returns inline.
_DIRECT_OUTPUT_TOOLKITS = frozenset({"skill_manage", "dynamic_tools"})


@timed("system_prompt_assembly.agent_create.toolkit_build")
def build_agent_toolkit(  # noqa: C901, PLR0912 - flat match dispatch over per-agent context toolkits
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
    worker_sandbox: WorkerSandboxConfig | None = None,
) -> Toolkit | None:
    """Build one of the toolkits ``agent_tools`` offers the agent, where ``placement`` runs it.

    ``persona`` is an authored subagent's persona; its ``delegate`` toolkit keeps
    the copies it authors within the persona's tools, skills, and knowledge bases.
    ``calls_as_jobs`` says whether the agent's build lets this toolkit's calls become background jobs.
    ``workspace_in_worker`` says whether the build's workspace-path toolkits run in a worker, where attachments save.

    ``deferred_toolkits`` returns the wire-deferred toolkits that ``dynamic_tools`` searches, as the agent's runs offer
    them; without it ``dynamic_tools`` is skipped and None is returned.
    """
    if worker_sandbox is not None and tool_name not in SANDBOX_ALLOWED_TOOLKITS:
        return None
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
    workspace_root = agent_runtime.workspace.root
    toolkit: Toolkit
    match tool_name:
        case "memory":
            from mindroom.custom_tools.memory import MemoryTools  # noqa: PLC0415

            toolkit = MemoryTools(agent_name, config, runtime_paths, execution_identity)
        case "knowledge_graph":
            from mindroom.custom_tools.knowledge_graph import KnowledgeGraphTools  # noqa: PLC0415

            toolkit = KnowledgeGraphTools(agent_name, config, runtime_paths, execution_identity)
        case "delegate":
            # Imported lazily to avoid a cycle: delegate -> ai -> agents -> toolkit_builder -> delegate.
            from mindroom.custom_tools.delegate import DelegateTools  # noqa: PLC0415

            toolkit = DelegateTools(
                agent_name=agent_name,
                runtime_paths=runtime_paths,
                config=config,
                execution_identity=execution_identity,
                delegation_depth=delegation_depth,
                refresh_scheduler=refresh_scheduler,
                workspace_root=workspace_root,
                persona=persona,
                calls_as_jobs=calls_as_jobs,
            )
        case "self_config":
            from mindroom.custom_tools.self_config import SelfConfigTools  # noqa: PLC0415

            toolkit = SelfConfigTools(agent_name, runtime_paths)
        case "agent_manager":
            from mindroom.custom_tools.agent_manager import AgentManagerTools  # noqa: PLC0415

            can_create = config.get_agent(agent_name).created_agents_per_owner > 0
            info = config.generated_agent(agent_name)
            toolkit = AgentManagerTools(
                agent_name,
                can_create=can_create,
                generated=info is not None,
                created=info is not None and info.created,
            )
        case "contacts":
            from mindroom.custom_tools.contacts import ContactsTools  # noqa: PLC0415

            toolkit = ContactsTools(agent_name)
        case "skill_manage":
            from mindroom.custom_tools.skill_manage import SkillManageTools  # noqa: PLC0415

            toolkit = SkillManageTools(agent_name, config, runtime_paths, agent_workspace_skills_root(workspace_root))
        case "compact_context":
            from mindroom.custom_tools.compact_context import CompactContextTools  # noqa: PLC0415

            toolkit = CompactContextTools(agent_name, config, runtime_paths, execution_identity)
        case "report_publishing":
            from mindroom.custom_tools.report_publishing import ReportPublishingTools  # noqa: PLC0415

            toolkit = ReportPublishingTools()
        case "dynamic_tools":
            if deferred_toolkits is None:
                return None
            from mindroom.custom_tools.dynamic_tools import DynamicToolsToolkit  # noqa: PLC0415

            toolkit = DynamicToolsToolkit(deferred_toolkits=deferred_toolkits)
        case _:
            sandboxed = worker_sandbox is not None
            worker_scope = agent_runtime.execution.execution_scope
            base_dir_override: dict[str, object] | None = (
                None
                if sandboxed
                else (
                    {"base_dir": str(current_tool_workdir() or workspace_root)}
                    if "base_dir" in safe_tool_init_override_fields(tool_name, tool_metadata=agent_tools.tools.entries)
                    else None
                )
            )
            return get_tool_by_name(
                tool_name,
                runtime_paths,
                tools=agent_tools.tools,
                credentials_manager=get_runtime_credentials_manager(runtime_paths),
                runtime_config=config,
                tool_config_overrides=tool_config_overrides,
                tool_init_overrides=base_dir_override,
                runs_in_worker=agent_tools.runs_in_worker(tool_name, placement),
                allowed_shared_services=(
                    frozenset()
                    if sandboxed
                    else (config.get_worker_grantable_credentials() if worker_scope is not None else None)
                ),
                tool_output_workspace_root=None if sandboxed else workspace_root,
                agent_state_root=agent_runtime.state_root,
                tool_output_auto_save_threshold_bytes=config.defaults.tool_output_auto_save_threshold_bytes,
                workspace_in_worker=workspace_in_worker,
                sandbox=sandboxed,
                worker_target=agent_runtime.execution.worker_target,
            )

    if tool_name not in _DIRECT_OUTPUT_TOOLKITS:
        policy = ToolOutputFilePolicy.for_current_tools(
            workspace_root,
            runtime_paths,
            auto_save_threshold_bytes=config.defaults.tool_output_auto_save_threshold_bytes,
        )
        configure_toolkit_functions(toolkit, output_files=policy)
    toolkit.tool_construction = ToolConstruction(tool_name, None, tool_config_signature(tool_config_overrides))
    return toolkit


__all__ = ["build_agent_toolkit"]
