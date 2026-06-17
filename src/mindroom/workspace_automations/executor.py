"""Workspace automation check execution."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from mindroom.agents import build_agent_toolkit, resolve_runtime_worker_tools
from mindroom.hooks import EVENT_TOOL_AFTER_CALL, EVENT_TOOL_BEFORE_CALL
from mindroom.tool_approval import evaluate_tool_approval
from mindroom.tool_system.dynamic_toolkits import visible_tool_surface
from mindroom.tool_system.worker_routing import build_tool_execution_identity

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import HookRegistry
    from mindroom.workspace_automations.models import LoadedWorkspaceAutomation
    from mindroom.workspace_automations.targets import WorkspaceAutomationTarget

_STRUCTURED_SHELL_FUNCTION_NAME = "run_shell_command_structured"
_PUBLIC_SHELL_FUNCTION_NAME = "run_shell_command"


@dataclass(frozen=True)
class ShellCheckResult:
    """Machine-readable result from one workspace automation shell check."""

    automation_id: str
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    raw_output: str
    timed_out: bool
    error: str | None


async def run_shell_check(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    hook_registry: HookRegistry,
    target: WorkspaceAutomationTarget,
    automation: LoadedWorkspaceAutomation,
) -> ShellCheckResult:
    """Run one workspace automation shell check through the agent shell toolkit."""
    try:
        if hook_registry.has_hooks(EVENT_TOOL_BEFORE_CALL) or hook_registry.has_hooks(EVENT_TOOL_AFTER_CALL):
            return _failed_result(
                automation.automation_id,
                "Workspace automation shell checks cannot run while tool call hooks are registered.",
            )
        approval_error = await _tool_approval_error(
            config=config,
            runtime_paths=runtime_paths,
            target=target,
            automation=automation,
        )
        if approval_error is not None:
            return _failed_result(automation.automation_id, approval_error)

        execution_identity = target.agent_runtime.execution_identity
        if execution_identity is None:
            execution_identity = build_tool_execution_identity(
                channel="matrix",
                agent_name=target.agent_name,
                transport_agent_name=target.agent_name,
                runtime_paths=runtime_paths,
                requester_id=None,
                room_id=None,
                thread_id=None,
                resolved_thread_id=None,
                session_id=f"workspace-automation:{target.agent_name}:{automation.automation_id}",
            )
        worker_tools = resolve_runtime_worker_tools(
            target.agent_name,
            config,
            runtime_paths,
            ["shell"],
        )
        toolkit = build_agent_toolkit(
            "shell",
            agent_name=target.agent_name,
            config=config,
            runtime_paths=runtime_paths,
            worker_tools=worker_tools,
            runtime_overrides=config.get_agent_tool_runtime_overrides(target.agent_name, "shell"),
            agent_runtime=target.agent_runtime,
            tool_config_overrides=_shell_tool_config_overrides(config, target.agent_name),
            execution_identity=execution_identity,
        )
        if toolkit is None:
            return _failed_result(automation.automation_id, "Shell toolkit was not available.")

        function = toolkit.async_functions.get(_STRUCTURED_SHELL_FUNCTION_NAME)
        if function is None or function.entrypoint is None:
            return _failed_result(automation.automation_id, "Shell toolkit did not expose structured execution.")

        result = function.entrypoint(
            automation.check.command,
            tail=automation.check.tail,
            timeout=automation.check.timeout_seconds,
            max_output_bytes=target.policy.max_output_bytes,
        )
        if inspect.isawaitable(result):
            result = await result
        return _result_from_payload(automation.automation_id, result)
    except Exception as exc:
        return _failed_result(automation.automation_id, f"{type(exc).__name__}: {exc}")


def _result_from_payload(automation_id: str, payload: object) -> ShellCheckResult:
    if not isinstance(payload, Mapping):
        return _failed_result(automation_id, "Structured shell execution returned a non-mapping result.")

    typed_payload = cast("Mapping[str, object]", payload)
    exit_code_value = typed_payload.get("exit_code")
    exit_code = exit_code_value if type(exit_code_value) is int else None
    error_value = typed_payload.get("error")
    return ShellCheckResult(
        automation_id=automation_id,
        ok=typed_payload.get("ok") is True,
        exit_code=exit_code,
        stdout=_payload_text(typed_payload.get("stdout")),
        stderr=_payload_text(typed_payload.get("stderr")),
        raw_output=_payload_text(typed_payload.get("raw_output")),
        timed_out=typed_payload.get("timed_out") is True,
        error=None if error_value is None else str(error_value),
    )


def _shell_tool_config_overrides(config: Config, agent_name: str) -> dict[str, object] | None:
    tool_surface = visible_tool_surface(
        agent_name=agent_name,
        config=config,
        session_id=None,
        enable_dynamic_tools_manager=False,
    )
    for entry in tool_surface.runtime_tool_configs:
        if entry.name == "shell":
            return dict(entry.tool_config_overrides)
    return None


async def _tool_approval_error(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    target: WorkspaceAutomationTarget,
    automation: LoadedWorkspaceAutomation,
) -> str | None:
    arguments = {
        "args": automation.check.command,
        "tail": automation.check.tail,
        "timeout": automation.check.timeout_seconds,
        "max_output_bytes": target.policy.max_output_bytes,
    }
    for tool_name in (_STRUCTURED_SHELL_FUNCTION_NAME, _PUBLIC_SHELL_FUNCTION_NAME):
        requires_approval, _timeout_seconds = await evaluate_tool_approval(
            config,
            runtime_paths,
            tool_name,
            arguments,
            target.agent_name,
        )
        if requires_approval:
            return "Workspace automation shell checks cannot run when shell tool approval is required."
    return None


def _payload_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _failed_result(automation_id: str, error: str) -> ShellCheckResult:
    return ShellCheckResult(
        automation_id=automation_id,
        ok=False,
        exit_code=None,
        stdout="",
        stderr="",
        raw_output="",
        timed_out=False,
        error=error,
    )


__all__ = ["ShellCheckResult", "run_shell_check"]
