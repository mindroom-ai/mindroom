"""OpenAI server-side tool search (``defer_loading``) for MindRoom's deferred tools.

On supported OpenAI Responses routes, authored ``defer: true`` tools are handled by OpenAI's server-side tool
search instead of MindRoom's portable tool search. Every request sends each deferred tool's full definition with
``defer_loading: true`` plus a hosted ``tool_search`` entry, so deferred schemas stay out of the model's rendered
context up front. Discovered tools load at the end of the context window, so tool discovery never invalidates
the cached prompt prefix.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from mindroom.model_defaults import OPENAI_BASE_URL_DEFAULT, OPENAI_TOOL_SEARCH_MIN_GPT_VERSION

__all__ = ["openai_native_tool_search_supported", "request_params_with_deferred_tool_search"]

# A missing minor version counts as .0, so a major-only future release gates native while ``gpt-5`` stays on
# portable tool search.
_GPT_VERSION_PATTERN = re.compile(r"gpt-(\d+)(?:\.(\d+))?")


def openai_native_tool_search_supported(route: Literal["openai", "codex"], model_id: str, *, base_url: str) -> bool:
    """Return whether a model on the ``openai`` or ``codex`` route supports server-side tool search.

    Tool search is a Responses-API feature on gpt-5.4 and later, so the gate parses the ``gpt-N.M`` version from the
    model id instead of keeping an allowlist that goes stale with each release. On the ``openai`` route only the
    public API endpoint at ``base_url`` offers it.
    """
    if route == "openai" and base_url.rstrip("/") != OPENAI_BASE_URL_DEFAULT:
        return False
    version_match = _GPT_VERSION_PATTERN.search(model_id)
    if version_match is None:
        return False
    version = (int(version_match.group(1)), int(version_match.group(2) or 0))
    return version >= OPENAI_TOOL_SEARCH_MIN_GPT_VERSION


def request_params_with_deferred_tool_search(
    request_params: dict[str, Any],
    deferred_tool_names: frozenset[str],
) -> dict[str, Any]:
    """Tag deferred function tools with ``defer_loading`` and add the search tool when any is present.

    The tools are ordered search tool, non-deferred tools, then deferred tools by name, so the cached prefix stays
    byte-stable across requests.
    """
    tools = request_params.get("tools")
    if not deferred_tool_names or not isinstance(tools, list):
        return request_params
    non_deferred_tools: list[Any] = []
    deferred_tools: list[dict[str, Any]] = []
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "function" and tool.get("name") in deferred_tool_names:
            deferred_tools.append({**tool, "defer_loading": True})
        else:
            non_deferred_tools.append(tool)
    if not deferred_tools:
        return request_params
    deferred_tools.sort(key=lambda tool: str(tool.get("name")))
    return {**request_params, "tools": [{"type": "tool_search"}, *non_deferred_tools, *deferred_tools]}
