"""Team domain facade."""

import sys
from types import ModuleType

# ruff: noqa: F401,F403,F405
from . import core as _core
from .core import *
from .core import (
    Agent,
    Team,
    _create_team_instance,
    _ensure_request_team_knowledge_managers,
    _get_response_content,
    _materialize_team_members,
    _select_team_mode,
    _team_response_stream_raw,
    _TeamModeDecision,
    create_agent,
    get_agent_knowledge,
    get_model_instance,
    get_user_friendly_error_message,
    prepare_bound_team_execution_context,
)


class _FacadeModule(ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if hasattr(_core, name):
            setattr(_core, name, value)


sys.modules[__name__].__class__ = _FacadeModule

__all__ = [
    "TeamIntent",
    "TeamMemberStatus",
    "TeamMode",
    "TeamOutcome",
    "TeamResolution",
    "TeamResolutionMember",
    "build_materialized_team_instance",
    "decide_team_formation",
    "format_team_response",
    "materialize_exact_team_members",
    "prepare_materialized_team_execution",
    "resolve_configured_team",
    "resolve_live_shared_agent_names",
    "select_model_for_team",
    "team_response",
    "team_response_stream",
]
