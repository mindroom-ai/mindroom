"""Startup and reload warnings about risky but allowed config choices.

See docs/architecture/security-posture.md for the trust model behind the file access check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.agents import resolve_runtime_worker_tools
from mindroom.logging_config import get_logger
from mindroom.orchestration.rooms import warn_about_foreign_homeserver_authorities
from mindroom.tool_system.catalog import TOOL_METADATA, ensure_tool_registry_loaded
from mindroom.tool_system.declarations import ToolFileAccess

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)


def warn_about_config_risks(config: Config, runtime_paths: RuntimePaths) -> None:
    """Log every risky-but-allowed choice in one loaded config."""
    warn_about_foreign_homeserver_authorities(config, runtime_paths)
    _warn_about_unconfined_primary_tools_next_to_worker_code_tools(config, runtime_paths)


def _primary_tool_is_unconfined(tool_name: str, file_access: str) -> bool:
    """Return whether a primary-process tool can reach files beyond the agent workspace."""
    metadata = TOOL_METADATA[tool_name]
    if metadata.file_access is ToolFileAccess.UNCONFINED:
        return True
    return metadata.file_access is ToolFileAccess.AGENT and file_access == "unrestricted"


def _warn_about_unconfined_primary_tools_next_to_worker_code_tools(config: Config, runtime_paths: RuntimePaths) -> None:
    """Log one warning per agent that isolates code tools in a worker while primary-process tools stay unconfined."""
    # Code-execution tools are built in; loading plugins here would re-import plugin modules mid-reload.
    ensure_tool_registry_loaded(runtime_paths)
    for agent_name in config.agents:
        entity = config.resolve_entity(agent_name)
        tool_names = [entry.name for entry in entity.tool_configs if entry.name in TOOL_METADATA]
        routed = set(
            resolve_runtime_worker_tools(
                agent_name,
                config,
                runtime_paths,
                tool_names,
                tool_registry_preloaded=True,
            ),
        )
        worker_tools = {
            name for name in tool_names if name in routed and not TOOL_METADATA[name].requires_primary_runtime
        }
        worker_code_tools = sorted(name for name in worker_tools if TOOL_METADATA[name].executes_code)
        unconfined_primary_tools = sorted(
            name
            for name in tool_names
            if name not in worker_tools and _primary_tool_is_unconfined(name, entity.file_access)
        )
        if worker_code_tools and unconfined_primary_tools:
            logger.warning(
                "Agent isolates code tools in a worker, but primary-process tools that are not confined "
                "by file_access can read runtime secrets",
                agent=agent_name,
                worker_code_tools=worker_code_tools,
                unconfined_primary_tools=unconfined_primary_tools,
            )
