"""OpenAI server-side tool search (``defer_loading``) for MindRoom's deferred tools.

On supported OpenAI Responses routes, authored ``defer: true`` tools are handled by OpenAI's server-side tool
search instead of MindRoom's portable tool search. Every request sends each deferred tool's full definition with
``defer_loading: true`` plus a hosted ``tool_search`` entry, so deferred schemas stay out of the model's rendered
context up front. Discovered tools load at the end of the context window, so tool discovery never invalidates
the cached prompt prefix.
"""

from __future__ import annotations

import re
from typing import Any, cast

from mindroom.model_defaults import OPENAI_BASE_URL_DEFAULT, OPENAI_TOOL_SEARCH_MIN_GPT_VERSION
from mindroom.providers.names import provider_route

__all__ = ["openai_native_tool_search_supported", "request_params_with_deferred_tool_search"]

_NATIVE_TOOL_SEARCH_PROVIDERS = frozenset({"codex", "openai"})
# LLM-plugin-style ``openai-codex/gpt-N.M`` ids match the same way as bare or ``-codex``-suffixed ids, so no
# prefix normalization is needed before the search. A missing minor version counts as .0, so a major-only future
# release gates native while ``gpt-5`` stays on portable tool search.
_GPT_VERSION_PATTERN = re.compile(r"gpt-(\d+)(?:\.(\d+))?")


def openai_native_tool_search_supported(provider: str, model_id: str, *, base_url: object = None) -> bool:
    """Return whether one authored provider/model pair supports server-side tool search.

    Tool search is a Responses-API feature on gpt-5.4 and later, so the gate covers the OpenAI API and Codex
    providers and parses the ``gpt-N.M`` version from the model id instead of keeping an allowlist that goes stale
    with each release.
    """
    canonical = provider_route(provider)
    if canonical not in _NATIVE_TOOL_SEARCH_PROVIDERS:
        return False
    if (
        canonical == "openai"
        and base_url not in (None, "")
        and (not isinstance(base_url, str) or base_url.rstrip("/") != OPENAI_BASE_URL_DEFAULT)
    ):
        return False
    version_match = _GPT_VERSION_PATTERN.search(model_id)
    if version_match is None:
        return False
    version = (int(version_match.group(1)), int(version_match.group(2) or 0))
    return version >= OPENAI_TOOL_SEARCH_MIN_GPT_VERSION


def _as_dict(value: object) -> dict[str, Any] | None:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def request_params_with_deferred_tool_search(
    request_params: dict[str, Any],
    deferred_tool_names: frozenset[str],
) -> dict[str, Any]:
    """Tag deferred function tools with ``defer_loading`` and inject the search tool.

    Every deferred tool's full definition still ships on every request; the API keeps deferred schemas out of the
    rendered context and loads discovered tools at the end of the context window. The tools array is ordered
    deterministically (search tool, then the remaining non-deferred tools, then deferred tools sorted by name) so
    the cached prefix stays byte-stable across requests. The search tool is injected only when at least one
    deferred tool is present in the request.
    """
    tools = request_params.get("tools")
    if not deferred_tool_names or not isinstance(tools, list):
        return request_params
    non_deferred_tools: list[Any] = []
    deferred_tools: list[dict[str, Any]] = []
    for tool in tools:
        tool_dict = _as_dict(tool)
        if (
            tool_dict is not None
            and tool_dict.get("type") == "function"
            and tool_dict.get("name") in deferred_tool_names
        ):
            deferred_tools.append({**tool_dict, "defer_loading": True})
        else:
            non_deferred_tools.append(tool)
    if not deferred_tools:
        return request_params
    deferred_tools.sort(key=lambda tool: str(tool.get("name")))
    return {**request_params, "tools": [{"type": "tool_search"}, *non_deferred_tools, *deferred_tools]}
