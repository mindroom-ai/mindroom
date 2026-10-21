"""OpenAI server-side tool search (defer_loading) for MindRoom deferred tools.

On supported OpenAI Responses providers, authored ``defer: true`` tools are
handled by OpenAI's server-side tool search instead of MindRoom's portable
tool search. Every request sends each deferred tool's full definition
with ``defer_loading: true`` plus a hosted ``tool_search`` entry, so deferred
schemas stay out of the model's rendered context up front.
Discovered tools load at the END of the context window (the opposite
mechanism from Anthropic's inline tool_reference expansion, with the same
effect), so tool discovery never invalidates the cached prompt prefix.

:class:`~mindroom.providers.openai_models.MindRoomOpenAIResponses` owns the wire
seams and calls into this module: :func:`request_params_with_deferred_tool_search` tags the
registered tools and injects the search entry with a deterministic order
(search tool, then non-deferred tools, then deferred sorted by name) so the
cached prefix stays byte-stable. Ordered output capture and replay live in
:mod:`mindroom.providers.openai_response_replay`.
"""

from __future__ import annotations

from typing import Any

from mindroom.llm_request_logging import record_llm_request_tools
from mindroom.providers.model_instance_checks import isinstance_of_loaded
from mindroom.providers.openai.tool_search import openai_native_tool_search_supported
from mindroom.providers.openai.tool_search import request_params_with_deferred_tool_search as tag_deferred_tools

__all__ = [
    "install_openai_deferred_tool_search",
    "model_deferred_tool_names",
    "openai_native_tool_search_supported",
    "request_params_with_deferred_tool_search",
]

_OPENAI_RESPONSES_CLASS = ("agno.models.openai.responses", "OpenAIResponses")
_DEFERRED_TOOL_NAMES_ATTR = "_mindroom_openai_deferred_tool_names"


def install_openai_deferred_tool_search(model: object, *, deferred_tool_names: frozenset[str]) -> None:
    """Register wire tool names for OpenAI server-side tool search on one model.

    Every request built by
    :class:`~mindroom.providers.openai_models.MindRoomOpenAIResponses` sends the
    named tools with ``defer_loading: true`` plus the hosted
    ``tool_search`` entry, so their schemas stay out of the rendered context
    and tool discovery never invalidates the prompt cache. No-op for
    non-Responses models and empty name sets.
    """
    if not isinstance_of_loaded(model, _OPENAI_RESPONSES_CLASS) or not deferred_tool_names:
        return
    vars(model)[_DEFERRED_TOOL_NAMES_ATTR] = frozenset(deferred_tool_names)


def model_deferred_tool_names(model: object) -> frozenset[str]:
    """Return the wire tool names registered for deferred loading on one model."""
    deferred_tool_names = vars(model).get(_DEFERRED_TOOL_NAMES_ATTR)
    return deferred_tool_names if isinstance(deferred_tool_names, frozenset) else frozenset()


def request_params_with_deferred_tool_search(
    request_params: dict[str, Any],
    deferred_tool_names: frozenset[str],
) -> dict[str, Any]:
    """Tag deferred tools and inject the search tool, recording the final wire tools for the request log."""
    prepared_params = tag_deferred_tools(request_params, deferred_tool_names)
    record_llm_request_tools(prepared_params.get("tools"))
    return prepared_params
