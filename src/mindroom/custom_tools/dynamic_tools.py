"""Search and call deferred tools on models without native tool search."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from agno.tools import Toolkit

from mindroom.tool_system.tool_schema_cache import processed_function_definition

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.tool_system.dynamic_toolkits import DeferredToolkit


_WORD_RE = re.compile(r"[a-z0-9]+")
_MAX_RESULTS_LIMIT = 20
_CALL_TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Exact function name from tool_search."},
        "arguments": {"type": "object", "description": "The function's arguments as a JSON object."},
    },
    "required": ["name"],
}
_EXACT_NAME_SCORE = 1000


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


class DynamicToolsToolkit(Toolkit):
    """Return deferred function definitions on request; deferred calls arrive through ``call_tool``."""

    def __init__(self, *, deferred_toolkits: Sequence[DeferredToolkit]) -> None:
        self._deferred_toolkits = tuple(deferred_toolkits)
        super().__init__(name="dynamic_tools", tools=[self.tool_search, self.call_tool])
        # Agno would render the argument values of a typed dict as empty objects, and Gemini conversion drops the
        # description of an optional object, so the wrapper declares the same shape as the MCP call_tool bridge.
        call_tool = self.functions["call_tool"]
        call_tool.parameters = deepcopy(_CALL_TOOL_PARAMETERS)
        call_tool.skip_entrypoint_processing = True

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        return json.dumps({"status": status, "tool": "dynamic_tools", **kwargs}, sort_keys=True)

    def tool_search(self, query: str, max_results: int = 5) -> str:
        """Search deferred functions by name or keywords and return their full definitions.

        Call a returned function with call_tool(name, arguments).

        Args:
            query: Function name, toolkit name, or keywords describing the needed capability.
            max_results: Maximum number of function definitions to return.

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
            "matches": [_match(deferred, definition) for _score, _order, deferred, definition in matches],
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


def _match(deferred: DeferredToolkit, definition: dict[str, Any]) -> dict[str, Any]:
    match = {**definition, "toolkit": deferred.domain_name}
    if function_instructions := deferred.function_instructions.get(definition["name"]):
        match["instructions"] = function_instructions
    return match


def _function_definitions(deferred: DeferredToolkit) -> list[dict[str, Any]]:
    # Async runs execute the async implementation when a toolkit registers both, so describe that one.
    functions = deferred.toolkit.get_async_functions()
    return [processed_function_definition(functions[name]) for name in dict.fromkeys(deferred.wire_function_names)]


def _search_score(
    deferred: DeferredToolkit,
    definition: dict[str, Any],
    query_tokens: set[str],
    raw_query: str,
) -> int:
    """Score one function by the query words it matches, each counted once in its best field."""
    if raw_query == definition["name"]:
        return _EXACT_NAME_SCORE
    detail_tokens = _tokens(definition["description"])
    for parameter_name in definition["parameters"].get("properties", {}):
        detail_tokens |= _tokens(parameter_name)
    fields = (
        (8, _tokens(definition["name"])),
        (4, _tokens(deferred.domain_name)),
        (2, detail_tokens),
        (1, _tokens(deferred.description)),
    )
    return sum(max((weight for weight, tokens in fields if token in tokens), default=0) for token in query_tokens)
