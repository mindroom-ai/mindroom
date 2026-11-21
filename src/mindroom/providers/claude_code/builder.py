"""Load Claude Code's optional SDK only when building a selected model."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from mindroom.llm.provider import Capabilities
from mindroom.providers.claude_code.settings import ClaudeCodeSettings
from mindroom.providers.extra_kwargs import settings_from_extra_kwargs
from mindroom.providers.identity import model_identity
from mindroom.providers.prompt_cache_key import derive_session_routing_key
from mindroom.tool_system.dependencies import ensure_optional_deps, optional_dependency_environment

if TYPE_CHECKING:
    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths
    from mindroom.providers.claude_code.adapter import ClaudeCodeProvider
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


__all__ = ["build_provider"]


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
        from mindroom.providers.claude_code.adapter import ClaudeCodeProvider  # noqa: PLC0415 - optional SDK

    storage_root = runtime_paths.storage_root
    settings = settings_from_extra_kwargs(ClaudeCodeSettings, model_config.extra_kwargs, provider=model_config.provider)
    return ClaudeCodeProvider(
        identity=model_identity(model_config),
        capabilities=Capabilities(
            structured_output="json_mode",
            omitted_media=frozenset({"audio", "video", "file"}),
            provider_default_sampling=True,
        ),
        settings=settings,
        storage_root=storage_root,
        # Prefer the CLI the user keeps updated to the copy bundled with the SDK.
        cli_path=settings.cli_path or shutil.which("claude"),
        scope=None
        if execution_identity is None
        else derive_session_routing_key(execution_identity, storage_root=storage_root),
    )
