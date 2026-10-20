"""Search and call deferred tools on models without native tool search."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from agno.tools import Toolkit

from mindroom.tool_system.tool_schema_cache import processed_function_definition

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from mindroom.tool_system.dynamic_toolkits import DeferredToolkit


_WORD_RE = re.compile(r"[a-z0-9]+")
_MAX_RESULTS_LIMIT = 20
_LISTING_BYTE_LIMIT = 16 * 1024
_LISTED_DESCRIPTION_LENGTH = 256
# Room for the status, usage, and truncation message around the listed functions.
_LISTING_ENVELOPE_BYTES = 512
_CALL_TOOL_DESCRIPTION = "Call a deferred function found with tool_search, passing its arguments as a JSON object."
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
        call_tool.description = _CALL_TOOL_DESCRIPTION
        call_tool.parameters = deepcopy(_CALL_TOOL_PARAMETERS)
        call_tool.skip_entrypoint_processing = True

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        return json.dumps({"status": status, "tool": "dynamic_tools", **kwargs}, sort_keys=True)

    def tool_search(self, query: str = "", max_results: int = 5) -> str:
        """Search deferred functions by name or keywords and return their full definitions, or list them.

        Call a returned function with call_tool(name, arguments).

        Args:
            query: Function name, toolkit name, or keywords describing the needed capability.
                Leave it empty to list the deferred functions without their definitions.
            max_results: Maximum number of function definitions to return, at most 20.

        """
        raw_query = query.strip().lower()
        query_tokens = _tokens(raw_query)
        # No function name lacks word characters, so a query without them can only browse.
        if not query_tokens:
            return self._listing()
        scored: list[tuple[int, int, DeferredToolkit, dict[str, Any]]] = []
        for deferred in self._deferred_toolkits:
            for definition in _function_definitions(deferred):
                score = _search_score(deferred, definition, query_tokens, raw_query)
                if score > 0:
                    scored.append((score, len(scored), deferred, definition))
        scored.sort(key=lambda item: (-item[0], item[1]))
        matches = scored[: max(1, min(max_results, _MAX_RESULTS_LIMIT))]
        instructions = _domain_instructions(deferred for _score, _order, deferred, _definition in matches)
        payload: dict[str, object] = {
            "matches": [_match(deferred, definition) for _score, _order, deferred, definition in matches],
            "usage": "Call a match with call_tool(name, arguments), passing arguments as a JSON object.",
        }
        if instructions:
            payload["instructions"] = instructions
        if not matches:
            payload["message"] = (
                "No deferred function matched. Try other keywords or a toolkit name from the system prompt, "
                "or call tool_search() without a query to list them."
            )
        elif len(scored) > len(matches):
            if len(matches) < _MAX_RESULTS_LIMIT:
                hint = (
                    f"Raise max_results (at most {_MAX_RESULTS_LIMIT}) or use more specific keywords to see the others."
                )
            else:
                hint = "Use more specific keywords to see the others."
            payload["message"] = f"{len(scored)} functions matched; these are the best {len(matches)}. {hint}"
        return self._payload("ok", **payload)

    def _listing(self) -> str:
        """List each deferred function's name, toolkit, and description, leaving out descriptions, then functions, to fit."""
        described = [
            {
                "name": definition["name"],
                "toolkit": deferred.domain_name,
                "description": definition["description"][:_LISTED_DESCRIPTION_LENGTH],
            }
            for deferred in self._deferred_toolkits
            for definition in _function_definitions(deferred)
        ]
        listed = _within_listing_limit(described)
        payload: dict[str, object] = {
            "usage": "Call tool_search with a function's exact name to get its definition, then call it with call_tool.",
        }
        if len(listed) < len(described):
            listed = _within_listing_limit([{"name": item["name"], "toolkit": item["toolkit"]} for item in described])
            payload["message"] = (
                "Descriptions were left out to fit the listing."
                if len(listed) == len(described)
                else f"Listed {len(listed)} of {len(described)} deferred functions, without descriptions; "
                "search by keywords or a toolkit name to find the others."
            )
        payload["functions"] = listed
        return self._payload("ok", **payload)

    def call_tool(self, name: str = "", arguments: object = None, **_extra: object) -> str:
        """Report a wrapper call that could not be routed to a deferred function.

        Resolvable calls never reach this method: they run the named function directly.
        """
        del arguments
        subject = f"'{name}' was not called" if name else "Nothing was called"
        return self._payload(
            "error",
            message=f"{subject}. Use an exact function name from tool_search and pass arguments as a JSON object.",
        )


def _within_listing_limit(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    """Return the leading entries that fit in one listing result."""
    listed: list[dict[str, str]] = []
    size = _LISTING_ENVELOPE_BYTES
    for entry in entries:
        size += len(json.dumps(entry)) + 2
        if size > _LISTING_BYTE_LIMIT:
            break
        listed.append(entry)
    return listed


def _domain_instructions(matched: Iterable[DeferredToolkit]) -> dict[str, str]:
    """Join the instructions of the matched toolkits per authored domain, each text once."""
    # One authored entry can expand to several toolkits, so each domain joins its toolkits' instructions.
    texts: dict[str, dict[str, None]] = {}
    for deferred in matched:
        if deferred.instructions:
            texts.setdefault(deferred.domain_name, {})[deferred.instructions] = None
    return {domain: "\n\n".join(domain_texts) for domain, domain_texts in texts.items()}


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
    if raw_query == definition["name"].lower():
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
