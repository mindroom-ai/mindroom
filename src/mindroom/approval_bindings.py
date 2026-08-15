"""Durable exact-call bindings for native approval continuations."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agno.models.response import ToolExecution
    from agno.run.requirement import RunRequirement

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

_OAUTH_RESET_BINDING_KEY = "oauth_reset"
_OAUTH_RESET_TOOL_NAME = "reset_oauth_connection"


def canonical_tool_arguments(arguments: object) -> str:
    """Encode one tool argument object into deterministic JSON."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        msg = "Approval tool arguments must be an object"
        raise TypeError(msg)
    try:
        return json.dumps(arguments, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        msg = "Approval tool arguments are not JSON serializable"
        raise RuntimeError(msg) from exc


async def build_approval_tool_bindings(
    identified_tools: Sequence[tuple[ToolExecution, str, str, str]],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> dict[str, dict[str, object]]:
    """Freeze exact identity, arguments, owner, and sensitive target for every paused call."""
    from mindroom.oauth.reset import build_oauth_reset_approval_bindings  # noqa: PLC0415

    reset_calls = tuple(item for item in identified_tools if item[2] == _OAUTH_RESET_TOOL_NAME)
    if reset_calls and len(identified_tools) != 1:
        msg = "OAuth reset must be the only tool call in its approval generation"
        raise RuntimeError(msg)
    reset_bindings = await build_oauth_reset_approval_bindings(
        identified_tools,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )
    bindings: dict[str, dict[str, object]] = {}
    for tool, tool_call_id, tool_name, invoking_agent in identified_tools:
        binding: dict[str, object] = {
            "tool_name": tool_name,
            "arguments_json": canonical_tool_arguments(tool.tool_args),
            "invoking_agent": invoking_agent,
        }
        reset_binding = reset_bindings.get(tool_call_id)
        if reset_binding is not None:
            binding[_OAUTH_RESET_BINDING_KEY] = reset_binding
        bindings[tool_call_id] = binding
    return bindings


def validate_exact_approval_requirements(
    requirements: Sequence[RunRequirement],
    *,
    bindings: Mapping[str, Mapping[str, object]],
    default_agent_name: str,
) -> None:
    """Fail closed unless the persisted run still contains every approved exact call."""
    actual_ids: set[str] = set()
    for requirement in requirements:
        tool = requirement.tool_execution
        if tool is None or not tool.tool_call_id or not tool.tool_name:
            msg = "Paused tools no longer match the approval continuation"
            raise RuntimeError(msg)
        tool_call_id = tool.tool_call_id
        if tool_call_id in actual_ids:
            msg = "Paused tools no longer match the approval continuation"
            raise RuntimeError(msg)
        actual_ids.add(tool_call_id)
        binding = bindings.get(tool_call_id)
        if binding is None or (
            binding.get("tool_name") != tool.tool_name
            or binding.get("arguments_json") != canonical_tool_arguments(tool.tool_args)
            or binding.get("invoking_agent") != (requirement.member_agent_name or default_agent_name)
        ):
            msg = "Paused tools no longer match the approval continuation"
            raise RuntimeError(msg)
    if actual_ids != set(bindings):
        msg = "Paused tools no longer match the approval continuation"
        raise RuntimeError(msg)
