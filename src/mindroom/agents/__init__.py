"""Agent domain facade."""

import sys
from types import ModuleType

# ruff: noqa: F401,F403,F405
from . import core as _core
from .core import *
from .core import (
    _CULTURE_MANAGER_CACHE,
    _PRIVATE_CULTURE_MANAGER_CACHE,
    Agent,
    CultureManager,
    SqliteDb,
    _get_datetime_context,
    datetime,
    get_runtime_credentials_manager,
    get_tool_by_name,
    load_plugins,
    prepend_tool_hook_bridge,
)


class _FacadeModule(ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if hasattr(_core, name):
            setattr(_core, name, value)


sys.modules[__name__].__class__ = _FacadeModule

__all__ = [
    "build_agent_tool_init_context",
    "build_agent_toolkit",
    "create_agent",
    "create_session_storage",
    "create_state_storage_db",
    "describe_agent",
    "ensure_default_agent_workspaces",
    "get_agent_ids_for_room",
    "get_agent_runtime_sqlite_dbs",
    "get_agent_session",
    "get_agent_toolkit_names",
    "get_rooms_for_entity",
    "get_team_session",
    "remove_run_by_event_id",
    "show_tool_calls_for_agent",
]
