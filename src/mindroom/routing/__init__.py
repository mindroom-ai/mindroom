"""Routing domain facade."""

import sys
from types import ModuleType

# ruff: noqa: F401,F403,F405
from . import core as _core
from .core import *
from .core import Agent, _AgentSuggestion, get_model_instance


class _FacadeModule(ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if hasattr(_core, name):
            setattr(_core, name, value)


sys.modules[__name__].__class__ = _FacadeModule

__all__ = [
    "suggest_agent",
    "suggest_agent_for_message",
]
