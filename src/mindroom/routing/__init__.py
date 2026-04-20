"""Routing domain facade."""

# ruff: noqa: F401,F403,F405
from .core import *
from .core import Agent, _AgentSuggestion, get_model_instance

__all__ = [
    "suggest_agent",
    "suggest_agent_for_message",
]
