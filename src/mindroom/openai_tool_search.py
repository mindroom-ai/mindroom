"""OpenAI server-side tool search (defer_loading) for MindRoom deferred tools.

On supported OpenAI Responses providers, authored ``defer: true`` tools are
handled by OpenAI's server-side tool search instead of MindRoom's homegrown
dynamic-tool loading. Every request sends each deferred tool's full definition
with ``defer_loading: true`` plus a hosted ``tool_search`` entry, so deferred
schemas stay out of the model's rendered context up front.
Discovered tools load at the END of the context window (the opposite
mechanism from Anthropic's inline tool_reference expansion, with the same
effect), so tool discovery never invalidates the cached prompt prefix.

:class:`~mindroom.openai_models.MindRoomOpenAIResponses` owns the wire
seams and calls into this module: :func:`request_params_with_deferred_tool_search` tags the
registered tools and injects the search entry with a deterministic order
(search tool, then non-deferred tools, then deferred sorted by name) so the
cached prefix stays byte-stable. Ordered output capture and replay live in
:mod:`mindroom.openai_response_replay`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, cast

from mindroom.llm_request_logging import record_llm_request_tools
from mindroom.model_defaults import OPENAI_TOOL_SEARCH_MIN_GPT_VERSION
from mindroom.model_instance_checks import isinstance_of_loaded

if TYPE_CHECKING:
    from agno.models.openai import OpenAIResponses

_OPENAI_RESPONSES_CLASS = ("agno.models.openai.responses", "OpenAIResponses")

_DEFERRED_TOOL_NAMES_ATTR = "_mindroom_openai_deferred_tool_names"
_NATIVE_TOOL_SEARCH_PROVIDERS = frozenset({"codex", "openai", "openai_codex"})
_OPENAI_API_BASE_URL = "https://api.openai.com/v1"
# LLM-plugin-style `openai-codex/gpt-N.M` ids match the same way as bare or
# `-codex`-suffixed ids, so no prefix normalization is needed before the search.
# A missing minor version counts as .0, so a major-only future release gates
# native while `gpt-5` stays homegrown.
_GPT_VERSION_PATTERN = re.compile(r"gpt-(\d+)(?:\.(\d+))?")


def openai_native_tool_search_supported(provider: str, model_id: str, *, base_url: object = None) -> bool:
    """Return whether one authored provider/model pair supports server-side tool search.

    Tool search is a Responses-API feature on gpt-5.4 and later, so the gate
    covers the OpenAI API and Codex providers and parses the ``gpt-N.M``
    version from the model id instead of keeping an allowlist that goes stale
    with each release.
    """
    canonical_provider = provider.strip().lower().replace("-", "_")
    if canonical_provider not in _NATIVE_TOOL_SEARCH_PROVIDERS:
        return False
    if (
        canonical_provider == "openai"
        and base_url not in (None, "")
        and (not isinstance(base_url, str) or base_url.rstrip("/") != _OPENAI_API_BASE_URL)
    ):
        return False
    version_match = _GPT_VERSION_PATTERN.search(model_id)
    if version_match is None:
        return False
    version = (int(version_match.group(1)), int(version_match.group(2) or 0))
    return version >= OPENAI_TOOL_SEARCH_MIN_GPT_VERSION


def install_openai_deferred_tool_search(model: object, *, deferred_tool_names: frozenset[str]) -> None:
    """Register wire tool names for OpenAI server-side tool search on one model.

    Every request built by
    :class:`~mindroom.openai_models.MindRoomOpenAIResponses` sends the
    named tools with ``defer_loading: true`` plus the hosted
    ``tool_search`` entry, so their schemas stay out of the rendered context
    and tool discovery never invalidates the prompt cache. No-op for
    non-Responses models and empty name sets.
    """
    if not isinstance_of_loaded(model, _OPENAI_RESPONSES_CLASS) or not deferred_tool_names:
        return
    vars(model)[_DEFERRED_TOOL_NAMES_ATTR] = frozenset(deferred_tool_names)


def model_deferred_tool_names(model: OpenAIResponses) -> frozenset[str]:
    """Return the wire tool names registered for deferred loading on one model."""
    deferred_tool_names = vars(model).get(_DEFERRED_TOOL_NAMES_ATTR)
    return deferred_tool_names if isinstance(deferred_tool_names, frozenset) else frozenset()


def request_params_with_deferred_tool_search(
    request_params: dict[str, Any],
    deferred_tool_names: frozenset[str],
) -> dict[str, Any]:
    """Tag deferred function tools with defer_loading and inject the search tool.

    Every deferred tool's full definition still ships on every request; the
    API keeps deferred schemas out of the rendered context and loads
    discovered tools at the end of the context window. The tools array is
    ordered deterministically (search tool, then the remaining non-deferred
    tools, then deferred tools sorted by name) so the cached prefix stays
    byte-stable across requests. The search tool is injected only when at
    least one deferred tool is present in the request.
    """
    tools = request_params.get("tools")
    if not deferred_tool_names or not isinstance(tools, list):
        record_llm_request_tools(tools)
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
        record_llm_request_tools(tools)
        return request_params
    deferred_tools.sort(key=lambda tool: str(tool.get("name")))
    prepared_params = dict(request_params)
    prepared_params["tools"] = [{"type": "tool_search"}, *non_deferred_tools, *deferred_tools]
    record_llm_request_tools(prepared_params["tools"])
    return prepared_params


def _as_dict(value: object) -> dict[str, Any] | None:
    """Return the value as a string-keyed dict when possible."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None
