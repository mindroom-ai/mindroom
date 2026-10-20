"""Search and call deferred tools on models without native tool search."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from agno.tools import Toolkit

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agno.tools.function import Function

    from mindroom.tool_system.dynamic_toolkits import DeferredToolkit


_WORD_RE = re.compile(r"[a-z0-9]+")
_MAX_RESULTS_LIMIT = 20


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


class DynamicToolsToolkit(Toolkit):
    """Return deferred function definitions on request; deferred calls arrive through ``call_tool``."""

    def __init__(self, *, deferred_toolkits: Sequence[DeferredToolkit]) -> None:
        self._deferred_toolkits = tuple(deferred_toolkits)
        super().__init__(name="dynamic_tools", tools=[self.tool_search, self.call_tool])

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        return json.dumps({"status": status, "tool": "dynamic_tools", **kwargs}, sort_keys=True)

    def tool_search(self, query: str, max_results: int = 5) -> str:
        """Search deferred functions by name or keywords and return their full definitions.

        Call a returned function with call_tool(name, arguments).
        """
        raw_query = query.strip().lower()
        query_tokens = _tokens(raw_query)
        scored: list[tuple[int, int, DeferredToolkit, dict[str, Any]]] = []
        for deferred in self._deferred_toolkits:
            for definition in _function_definitions(deferred):
                score = _search_score(deferred, definition, query_tokens, raw_query)
                if score > 0:
                    scored.append((score, len(scored), deferred, definition))
        scored.sort(key=lambda item: (-item[0], item[1]))
        matches = scored[: max(1, min(max_results, _MAX_RESULTS_LIMIT))]
        instructions = {
            deferred.domain_name: deferred.instructions
            for _score, _order, deferred, _definition in matches
            if deferred.instructions
        }
        payload: dict[str, object] = {
            "matches": [{**definition, "toolkit": deferred.domain_name} for _s, _o, deferred, definition in matches],
            "instructions": instructions,
            "usage": "Call a match with call_tool(name, arguments), passing arguments as a JSON object.",
        }
        if not matches:
            payload["message"] = "No deferred function matched. Try other keywords or a toolkit name from the list."
        return self._payload("ok", **payload)

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Call a deferred function found with tool_search, passing its arguments as a JSON object."""
        del arguments
        return self._payload(
            "error",
            message=(
                f"'{name}' was not called. Use an exact function name from tool_search "
                "and pass arguments as a JSON object."
            ),
        )


def _function_definitions(deferred: DeferredToolkit) -> list[dict[str, Any]]:
    functions: dict[str, Function] = {**deferred.toolkit.get_async_functions(), **deferred.toolkit.get_functions()}
    definitions = []
    for name in dict.fromkeys(deferred.wire_function_names):
        function = functions[name]._per_run_copy()
        function.process_entrypoint(strict=False)
        definitions.append(
            {"name": name, "description": function.description or "", "parameters": function.parameters},
        )
    return definitions


def _search_score(
    deferred: DeferredToolkit,
    definition: dict[str, Any],
    query_tokens: set[str],
    raw_query: str,
) -> int:
    if raw_query == definition["name"]:
        return 100
    if not query_tokens:
        return 0
    detail_tokens = _tokens(definition["description"])
    for parameter_name in definition["parameters"].get("properties", {}):
        detail_tokens |= _tokens(parameter_name)
    for score, tokens in (
        (80, _tokens(definition["name"])),
        (70, _tokens(deferred.domain_name)),
        (40, detail_tokens),
        (20, _tokens(deferred.description)),
    ):
        if query_tokens & tokens:
            return score
    return 0
