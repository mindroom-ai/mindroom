"""Load Claude Code's optional SDK only when building a selected model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.dependencies import ensure_optional_deps, optional_dependency_environment

if TYPE_CHECKING:
    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths
    from mindroom.providers.claude_code.adapter import ClaudeCodeProvider
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def build_provider(
    model_config: ModelConfig,
    runtime_paths: RuntimePaths,
    *,
    execution_identity: ToolExecutionIdentity | None,
    auto_install: bool = True,
) -> ClaudeCodeProvider:
    """Build the Claude Code adapter, loading its SDK before the first request."""
    ensure_optional_deps(["claude-agent-sdk"], "claude_agent", runtime_paths, auto_install=auto_install)
    with optional_dependency_environment():
        from mindroom.providers.claude_code.adapter import build_adapter  # noqa: PLC0415 - optional SDK

    return build_adapter(model_config, runtime_paths, execution_identity=execution_identity)
