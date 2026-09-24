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
    _warn_about_unrestricted_file_access_with_worker_code_tools(config, runtime_paths)


def _warn_about_unrestricted_file_access_with_worker_code_tools(config: Config, runtime_paths: RuntimePaths) -> None:
    """Log one warning per agent that isolates code tools in a worker but lets path tools read anything."""
    ensure_tool_registry_loaded(runtime_paths, config)
    for agent_name in config.agents:
        entity = config.resolve_entity(agent_name)
        if entity.file_access != "unrestricted":
            continue
        tool_names = [entry.name for entry in entity.tool_configs]
        worker_tools = resolve_runtime_worker_tools(
            agent_name,
            config,
            runtime_paths,
            tool_names,
            tool_registry_preloaded=True,
        )
        worker_code_tools = sorted(
            name
            for name in worker_tools
            if name in TOOL_METADATA and TOOL_METADATA[name].file_access is ToolFileAccess.UNRESTRICTED
        )
        if worker_code_tools:
            logger.warning(
                "Agent routes code tools to a worker but has file_access 'unrestricted'; "
                "its primary-process path tools can read runtime secrets",
                agent=agent_name,
                worker_code_tools=worker_code_tools,
            )
