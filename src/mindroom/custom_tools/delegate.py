"""Agent delegation tools for MindRoom agents.

Allows an agent to start configured subagents and continue their sessions.
Each turn runs independently and returns its response as the tool result.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from mindroom.agent_cli.minimal_mode_preflight import minimal_subagent_candidates
from mindroom.agent_descriptions import describe_agent
from mindroom.ai import run_delegated_child_response
from mindroom.delegation.cost import receive_subagent_cost
from mindroom.delegation.direct import run_direct_child_turn
from mindroom.delegation.execution import delegation_call_job
from mindroom.delegation.lifecycle import authorize_delegation, prepare_child_turn
from mindroom.delegation.personas import list_profiles, render_profile_listing
from mindroom.delegation.sessions import SubagentSessionError
from mindroom.delegation.state import CONTINUE_SUBAGENT, RUN_SUBAGENT
from mindroom.delegation.targets import minimal_mode_refusal, resolve_follow_up_target, resolve_fresh_target
from mindroom.delegation.workers import (
    dedicated_worker_refusal,
    resource_profiles_available,
    sandbox_workers_available,
    validate_sandbox_target,
)
from mindroom.tool_jobs.runtime import ManagedJobToolkit, get_background_runtime
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.invocation import current_tool_call
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import (
    current_budget_monitor,
    current_execution_identity,
    get_tool_runtime_context,
)
from mindroom.tool_system.tool_schema import closed_parameters
from mindroom.workers.models import ScriptResourceProfileName  # noqa: TC001 - the tool schema reads it at runtime.

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild, SubagentPersona
    from mindroom.delegation.targets import DelegationTarget
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.tool_jobs.runtime import JobSpec, ToolJobRuntime
    from mindroom.tool_system.tool_function import ToolFunction
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def _model_choice(name: str, model: ModelConfig) -> str:
    """Format one model name, compactly listing reasoning levels with `*` on the default."""
    if not model.reasoning:
        return name
    default = model.reasoning.default
    levels = "|".join(f"*{level}" if level == default else level for level in model.reasoning.levels)
    return f"{name}[{levels}]"


class DelegateTools(Toolkit, ManagedJobToolkit):
    """Tools that let an agent run configured agents as fresh subagents."""

    def __init__(
        self,
        agent_name: str,
        runtime_paths: RuntimePaths,
        config: Config,
        workspace_root: Path,
        execution_identity: ToolExecutionIdentity | None = None,
        delegation_depth: int = 0,
        refresh_scheduler: KnowledgeRefreshScheduler | None = None,
        persona: SubagentPersona | None = None,
        calls_as_jobs: bool = False,
    ) -> None:
        self._agent_name = agent_name
        self._delegate_to = config.get_agent(agent_name).delegate_to
        self._runtime_paths = runtime_paths
        self._config = config
        self._execution_identity = execution_identity
        self._delegation_depth = delegation_depth
        # Whether the agent's build lets these calls become jobs.
        self._calls_as_jobs = calls_as_jobs
        self._refresh_scheduler = refresh_scheduler
        self._minimal_targets = minimal_subagent_candidates(
            config,
            runtime_paths,
            self._delegate_to,
            execution_identity,
        )
        self._workspace_root = workspace_root
        # An authored subagent's own tools, skills, and knowledge bases cap the copies it authors, on every channel.
        self._persona = persona
        # A caller that may run itself may also author that copy's prompt, tools, skills, knowledge, or profile.
        self._authoring = agent_name in self._delegate_to
        # Whether this caller may run a child in a worker of its own, and size that worker.
        self._dedicated_workers = (
            dedicated_worker_refusal(agent_name, config, runtime_paths, resource_profile=None) is None
        )
        self._resource_profiles = self._dedicated_workers and resource_profiles_available(runtime_paths)
        self._sandbox_workers = sandbox_workers_available(agent_name, config, runtime_paths)
        self._minimal_sandbox = (
            self._sandbox_workers and execution_identity is not None and execution_identity.channel == "matrix"
        )

        super().__init__(
            name="delegate",
            instructions=self._build_instructions(),
            tools=[self.run_subagent, self.continue_subagent],
        )

    def _offers_minimal(self) -> bool:
        return bool(self._minimal_targets or self._minimal_sandbox)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Describe ``run_subagent`` with this caller's targets, offering no option the caller cannot use."""
        if function.name != RUN_SUBAGENT:
            return function
        hidden = {
            *(() if self._offers_minimal() else ("minimal",)),
            *(() if self._authoring else ("system_prompt", "tools", "skills", "knowledge", "profile")),
            *(() if self._dedicated_workers else ("dedicated_worker",)),
            *(() if self._resource_profiles else ("resource_profile",)),
            *(() if self._sandbox_workers else ("sandbox", "mount_dir", "mount_read_only")),
        }
        description = self._build_run_subagent_description()
        properties = {name: value for name, value in function.parameters["properties"].items() if name not in hidden}
        # Models always name the run with a string; only a call without one, such as one stored before it, runs untitled.
        properties["title"] = {"type": "string", "description": properties["title"]["description"]}
        parameters = closed_parameters(self.run_subagent, {**function.parameters, "properties": properties})
        parameters["required"] = [*parameters["required"], "title"]
        return replace(function, description=description, parameters=parameters)

    def _build_instructions(self) -> str:
        """Build toolkit instructions listing available delegation targets."""
        # The caller's own entry never repeats its configured role, which an authored copy must not see.
        tools = config_plugins(self._config, self._runtime_paths).tools
        lines = [
            f"{target_name}\n  - Yourself, run as a fresh copy."
            if target_name == self._agent_name
            else describe_agent(target_name, self._config, tools)
            for target_name in self._delegate_to
        ]
        instructions = self._config.render_prompt(
            "DELEGATE_TOOLKIT_INSTRUCTIONS_TEMPLATE",
            agent_descriptions="\n\n".join(lines),
        )
        if self._background_jobs_available():
            instructions += "\n" + self._config.get_prompt("DELEGATE_BACKGROUND_JOB_INSTRUCTIONS")
        return instructions

    def _background_jobs_available(self) -> bool:
        return (
            self._calls_as_jobs
            and self._execution_identity is not None
            and self._execution_identity.channel == "matrix"
            and get_background_runtime(self._runtime_paths) is not None
        )

    def _build_run_subagent_description(self) -> str:
        """Build the model-facing function description with this caller's allowlist."""
        available_targets = ", ".join(self._delegate_to)
        minimal_guidance = (
            "Minimal mode: set minimal=true for a short system prompt + single Bash tool (discovers/calls tools on "
            "demand; lower tokens, extra discovery steps). Prefer for self-contained tasks when the child's full "
            "role, instructions, and tool docs are not important in its system prompt; use standard mode when they "
            "matter or the child needs approval-gated tools (unsupported in minimal). "
            f"Subagents that support minimal mode: {', '.join(self._minimal_targets)}.\n"
            if self._minimal_targets
            else ""
        )
        background = self._background_jobs_available()
        background_guidance = (
            "Managed Matrix calls accept background_after (null waits or yields the wait on a newer message you "
            "answer while the child keeps working; 0 returns a Job ID immediately; >0 waits N seconds before "
            "backgrounding). "
            if background
            else ""
        )
        has_reasoning = any(model.reasoning for model in self._config.models.values())
        models = ", ".join(_model_choice(name, model) for name, model in sorted(self._config.models.items()))
        models_header = (
            "Available models (append @<level> for reasoning effort; * = default)"
            if has_reasoning
            else "Available models"
        )
        return (
            "Run one allowed configured agent as a fresh subagent and wait for its result.\n"
            f"Allowed subagents for this caller: {available_targets}.\n"
            "Use only these agent names. Include full context, constraints, and expected output in task; "
            "the child does not inherit this conversation, but keeps its configured tools, workspace, and memory. "
            "Selecting your own name starts a fresh copy of yourself (omit/null agent_name also selects you if "
            "allowed).\n"
            f"{background_guidance}"
            f"{models_header}: {models}. Omit/null model for normal selection. "
            f"{'This' if background else 'The caller waits; this'} does not create a Matrix thread. "
            "Use continue_subagent(subagent_id) for follow-ups (keeps model and mode).\n"
            f"{minimal_guidance}"
            f"{self._dedicated_worker_guidance()}"
            f"{self._authoring_guidance()}"
            "In Matrix, approval-required child tools pause for the user's approval before continuing. "
            "Returns the child's answer, stable subagent ID, and child-scoped audit reference."
        )

    def _dedicated_worker_guidance(self) -> str:
        if not self._dedicated_workers:
            return ""
        profiles = " Set resource_profile (small/standard/large) to size it." if self._resource_profiles else ""
        minimal_sandbox = " (or Bash-only with minimal=true)" if self._offers_minimal() else ""
        sandbox = (
            " Set sandbox=true (or mount_dir with optional mount_read_only=true on a self-copy) to isolate the child "
            f"with only shell/filesystem{minimal_sandbox}, no credentials, memory, skills, knowledge, "
            "or workspace outside mount_dir."
            if self._sandbox_workers
            else ""
        )
        return (
            "Dedicated worker: set dedicated_worker=true to run shell/file/python/worker tools in an isolated worker "
            "(avoids sharing with you or sibling subagents; same workspace; first call waits for startup; excludes "
            f"packages installed in the shared worker).{profiles}{sandbox} Follow-ups reuse it until idle-timeout "
            "expiry, then start a clean one.\n"
        )

    def _authoring_guidance(self) -> str:
        if not self._authoring:
            return ""
        minimal = " or minimal=true" if self._agent_name in self._minimal_targets else ""
        return (
            "Authoring: pass system_prompt to replace your role/instructions on a fresh copy of yourself, plus "
            "optional subsets of tools (toolkit or toolkit.function), skills, and knowledge (omitting tools keeps "
            "all; listing tools restricts skills/knowledge to those listed; keeps workspace, credentials, approvals). "
            "Or pass profile for a workspace subagents/<name>.md (YAML frontmatter description + optional "
            f"tools/skills/knowledge/model/mode, body = system_prompt); explicit model{minimal} overrides profile. "
            "Follow-ups keep the initial prompt.\n"
            f"{self._profile_listing()}"
        )

    def _profile_listing(self) -> str:
        listing = render_profile_listing(list_profiles(self._workspace_root))
        return f"Saved profiles:\n{listing}\n" if listing else ""

    async def run_subagent(
        self,
        task: str,
        agent_name: str | None = None,
        model: str | None = None,
        minimal: bool = False,
        system_prompt: str | None = None,
        tools: list[str] | None = None,
        skills: list[str] | None = None,
        knowledge: list[str] | None = None,
        profile: str | None = None,
        dedicated_worker: bool | None = None,
        resource_profile: ScriptResourceProfileName | None = None,
        sandbox: bool | None = None,
        mount_dir: str | None = None,
        mount_read_only: bool | None = None,
        *,
        title: str | None = None,
    ) -> str:
        """Run a fresh subagent and wait for its response and audit reference.

        The runtime-generated tool description lists caller-specific allowed
        targets, model guidance, and subagents that support minimal mode. Passing
        ``mindroom_output_path`` writes the returned text answer in the caller's
        own workspace on the primary (in-sandbox tool output redirection remains
        disabled).

        Args:
            task: Self-contained task with context, constraints, and expected output.
            agent_name: Allowed subagent name (omit/null selects yourself if allowed).
            model: Model name or name@level override (omit/null for default).
            minimal: Run in token-efficient minimal mode (or Bash-only with sandbox=true).
            system_prompt: Custom prompt replacing your role/instructions on a self-copy.
            tools: Subset of your toolkits or toolkit.function entries (omit to keep all).
            skills: Subset of your skills (omit keeps all only if tools is also omitted).
            knowledge: Subset of your knowledge base IDs (omit keeps all only if tools is also omitted).
            profile: Saved workspace subagents/<name>.md profile (mutually exclusive with system_prompt/tools/skills/knowledge).
            dedicated_worker: Run worker tools in an isolated worker for parallel/heavy work (implied by sandbox).
            resource_profile: Worker size: small, standard, or large (omit for default).
            sandbox: Run in an isolated Kubernetes sandbox pod with only shell/filesystem and no credentials, memory, skills, or knowledge.
            mount_dir: Workspace-relative subdirectory to mount into the sandbox (self-copy only; implies sandbox=true).
            mount_read_only: Mount mount_dir read-only (requires mount_dir).
            title: 3-6 word task title shown to observers.

        Returns:
            The delegated agent's response, or an error message if delegation failed.

        """
        return await self._run_direct(
            RUN_SUBAGENT,
            {
                "task": task,
                "title": title,
                "agent_name": agent_name,
                "model": model,
                "minimal": minimal,
                "system_prompt": system_prompt,
                "tools": tools,
                "skills": skills,
                "knowledge": knowledge,
                "profile": profile,
                "dedicated_worker": dedicated_worker,
                "resource_profile": resource_profile,
                "sandbox": sandbox,
                "mount_dir": mount_dir,
                "mount_read_only": mount_read_only,
            },
        )

    def _caller_identity(self) -> ToolExecutionIdentity:
        """Return who delegates: the running call's identity, for execution, ownership, and audit."""
        identity = current_execution_identity()
        if identity is None:
            msg = "Delegation requires a caller execution identity"
            raise RuntimeError(msg)
        return identity

    async def continue_subagent(self, subagent_id: str, message: str) -> str:
        """Send a follow-up to an existing subagent and wait for its answer.

        Reuses history, tools, workspace, and memory; `subagent_id` belongs to this caller and conversation and persists across restarts (wait for prior turn/approvals first; current permissions apply). Returns answer, same subagent ID, and new audit ref.

        Args:
            subagent_id: Subagent ID (or unique >=8-hex prefix) from run_subagent/continue_subagent.
            message: Follow-up instructions or question.

        Returns:
            The child's answer, stable subagent ID, and this turn's audit reference, or an error.

        """
        return await self._run_direct(CONTINUE_SUBAGENT, {"subagent_id": subagent_id, "message": message})

    async def managed_job(
        self,
        function_name: str,
        arguments: dict[str, Any],
        *,
        job_id: str,
        runtime: ToolJobRuntime,
    ) -> JobSpec | str:
        """Prepare the child a managed call of ``function_name`` runs as ``runtime``'s job ``job_id``, or say why not.

        The child's turn starts only once the job is admitted; the job starts and settles the child it is known by.
        """
        prepared = await self._prepare(function_name, arguments)
        if isinstance(prepared, str):
            return prepared
        child, config, parent_run_id = prepared
        if function_name == CONTINUE_SUBAGENT:
            arguments = {**arguments, "subagent_id": child.subagent_id}
        return delegation_call_job(
            child,
            job_id=job_id,
            runtime=runtime,
            owner=self._caller_identity(),
            parent_run_id=parent_run_id,
            tool_name=function_name,
            arguments=arguments,
            run_child=run_delegated_child_response,
            config=config,
            runtime_paths=self._runtime_paths,
            refresh_scheduler=self._refresh_scheduler,
        )

    async def _target(self, function_name: str, arguments: Mapping[str, object]) -> DelegationTarget | str:
        """Resolve the child a call of ``function_name`` starts or continues, or return why it cannot."""
        if function_name == CONTINUE_SUBAGENT:
            return await resolve_follow_up_target(
                arguments,
                owner=self._caller_identity(),
                config=self._config,
                runtime_paths=self._runtime_paths,
                depth=self._delegation_depth,
            )
        return resolve_fresh_target(
            arguments,
            caller_name=self._agent_name,
            config=self._config,
            runtime_paths=self._runtime_paths,
            depth=self._delegation_depth,
            workspace_root=lambda: self._workspace_root,
            cap=self._persona,
        )

    async def _prepare(
        self,
        function_name: str,
        arguments: Mapping[str, object],
    ) -> tuple[DelegationChild, Config, str | None] | str:
        """Resolve and authorize one call's child and prepare its turn: the child, its live config, and parent run."""
        target = await self._target(function_name, arguments)
        if isinstance(target, str):
            return target
        config = authorize_delegation(
            self._agent_name,
            target.agent_name,
            target.task,
            config=self._config,
            runtime_paths=self._runtime_paths,
            execution_identity=self._execution_identity,
            depth=self._delegation_depth,
            model=target.model,
        )
        if isinstance(config, str):
            return config
        owner = self._caller_identity()
        effective_mode = target.previous_child.agent_mode if target.previous_child is not None else target.agent_mode
        minimal_refusal = minimal_mode_refusal(
            effective_mode,
            execution_identity=owner,
            runtime_context=get_tool_runtime_context(),
        )
        if minimal_refusal is not None:
            return minimal_refusal
        sandbox_refusal = validate_sandbox_target(
            caller_name=self._agent_name,
            target=target,
            previous_child=target.previous_child,
            owner=owner,
            config=config,
            runtime_paths=self._runtime_paths,
            delegation_depth=self._delegation_depth,
        )
        if sandbox_refusal is not None:
            return sandbox_refusal
        # The parent's run and tool call are the framework's, never the model's arguments.
        parent = current_tool_call()
        child = prepare_child_turn(
            self._agent_name,
            target.agent_name,
            target.task,
            owner=owner,
            config=config,
            runtime_paths=self._runtime_paths,
            depth=self._delegation_depth,
            budget_monitor=current_budget_monitor(),
            model=target.model,
            agent_mode=target.agent_mode,
            previous=target.previous_child,
            parent_tool_call_id=(parent.context.call_id or "") if parent is not None else "",
            title=target.title,
            persona=target.persona,
            dedicated_worker=target.dedicated_worker,
            worker_resource_profile=target.resource_profile,
            worker_sandbox=target.sandbox,
        )
        return child, config, parent.context.run_id if parent is not None else None

    async def _run_direct(self, function_name: str, arguments: Mapping[str, object]) -> str:
        """Run one call's child inside this call, settled by the shared lifecycle owner."""
        prepared = await self._prepare(function_name, arguments)
        if isinstance(prepared, str):
            return prepared
        child, config, parent_run_id = prepared
        try:
            result = await run_direct_child_turn(
                child,
                owner=self._caller_identity(),
                parent_run_id=parent_run_id,
                config=config,
                runtime_paths=self._runtime_paths,
                refresh_scheduler=self._refresh_scheduler,
            )
        except SubagentSessionError as error:
            return str(error)
        # The turn that made this call receives what the settled child cost.
        receive_subagent_cost(child.delegation_id, child.cost_usd)
        # The stable child record reference follows the child's answer.
        return f"{result.text}\n\n{result.receipt}" if result.receipt else result.text
