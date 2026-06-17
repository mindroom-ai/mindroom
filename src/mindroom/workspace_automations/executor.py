"""Workspace automation check execution."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from mindroom.agents import build_agent_toolkit, resolve_runtime_worker_tools
from mindroom.tool_system.worker_routing import build_tool_execution_identity

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.workspace_automations.models import LoadedWorkspaceAutomation
    from mindroom.workspace_automations.targets import WorkspaceAutomationTarget

_STRUCTURED_SHELL_FUNCTION_NAME = "run_shell_command_structured"


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
    target: WorkspaceAutomationTarget,
    automation: LoadedWorkspaceAutomation,
) -> ShellCheckResult:
    """Run one workspace automation shell check through the agent shell toolkit."""
    try:
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
    for entry in config.get_agent_tool_configs(agent_name):
        if entry.name == "shell":
            return dict(entry.tool_config_overrides)
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
