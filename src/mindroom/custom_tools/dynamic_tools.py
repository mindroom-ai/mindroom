"""Search and call deferred tools on models without native tool search."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from mindroom.config.tool_aliases import canonical_tool_name
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.llm.tool_search import arguments_object
from mindroom.tool_system.authoring import Toolkit, passthrough_tool, tool
from mindroom.tool_system.tool_access import tool_definition

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from mindroom.tool_system.dynamic_toolkits import DeferredToolkit


_WORD_RE = re.compile(r"[a-z0-9]+")
_MAX_RESULTS_LIMIT = 20
_LISTING_BYTE_LIMIT = 16 * 1024
_LISTED_DESCRIPTION_LENGTH = 256
# Room for the status, usage, and truncation message around the listed functions.
_LISTING_ENVELOPE_BYTES = 512
_CALL_TOOL_DESCRIPTION = "Call a deferred function from tool_search with top-level name and arguments object."
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
    """Return deferred function definitions on request; deferred calls arrive through ``call_tool``.

    ``deferred_toolkits`` returns the deferred toolkits as the agent's runs offer them, which the agent knows only once
    all of its toolkits are assembled, after this one.
    """

    def __init__(self, *, deferred_toolkits: Callable[[], Sequence[DeferredToolkit]]) -> None:
        self._deferred_toolkits = deferred_toolkits
        super().__init__(name="dynamic_tools")

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        return custom_tool_payload("dynamic_tools", status, **kwargs)

    @tool(offer_background_after=False)
    def tool_search(self, query: str = "", max_results: int = 5) -> str:
        """Search deferred functions by name or keywords and return their full definitions, or list them.

        Call a returned function with call_tool(name, arguments).

        Args:
            query: Function name, toolkit name, or keywords describing the needed capability.
                Leave it empty to list the deferred functions without their definitions.
            max_results: Maximum number of function definitions to return, at most 20.

        """
        raw_query = query.strip().lower()
        # A toolkit's other names find it too.
        query_tokens = {name for token in _tokens(raw_query) for name in (token, canonical_tool_name(token))}
        # No function name lacks word characters, so a query without them can only browse.
        if not query_tokens:
            return self._listing()
        scored: list[tuple[int, DeferredToolkit, dict[str, Any]]] = []
        for deferred in self._deferred_toolkits():
            for definition in _function_definitions(deferred):
                score = _search_score(deferred, definition, query_tokens, raw_query)
                if score > 0:
                    scored.append((score, deferred, definition))
        # A stable sort keeps equal scores in the order the toolkits list them.
        scored.sort(key=lambda item: -item[0])
        matches = scored[: max(1, min(max_results, _MAX_RESULTS_LIMIT))]
        instructions = _domain_instructions(deferred for _score, deferred, _definition in matches)
        payload: dict[str, object] = {
            "matches": [
                {**definition, "toolkit": deferred.deferral.domain_name} for _score, deferred, definition in matches
            ],
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
                "toolkit": deferred.deferral.domain_name,
                "description": definition["description"][:_LISTED_DESCRIPTION_LENGTH],
            }
            for deferred in self._deferred_toolkits()
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

    # A typed dict would render its argument values as empty objects, and Gemini conversion drops the description of
    # an optional object, so the wrapper declares the same shape as the MCP call_tool bridge.
    @passthrough_tool(description=_CALL_TOOL_DESCRIPTION, parameters=_CALL_TOOL_PARAMETERS)
    def call_tool(self, name: object = "", arguments: object = None, **_extra: object) -> str:
        """Report a wrapper call that could not be routed to a deferred function.

        Resolvable calls never reach this method: they run the named function directly.
        """
        return self._payload("error", message=_call_tool_error(name, arguments, self._deferred_toolkits()))


def _argument_keys(arguments: object) -> frozenset[str] | None:
    if isinstance(arguments, Mapping):
        return frozenset(key for key in arguments if isinstance(key, str))
    if isinstance(arguments, str) and (parsed := arguments_object(arguments)) is not None:
        return frozenset(key for key in parsed if isinstance(key, str))
    return None


def _dotted_name_hint(name: str, deferred_toolkits: Sequence[DeferredToolkit]) -> str | None:
    if "." not in name:
        return None
    prefix, _, candidate = name.rpartition(".")
    if not prefix or not candidate:
        return None
    for deferred in deferred_toolkits:
        if any(function.name == candidate for function in deferred.functions):
            owner = deferred.deferral.domain_name
            if prefix == owner:
                return (
                    f"'{name}' was not called. Use the exact unprefixed function name '{candidate}' from tool_search "
                    "and pass arguments as a JSON object."
                )
            return (
                f"'{name}' was not called ('{candidate}' belongs to toolkit '{owner}', not '{prefix}'). "
                "Use an exact function name from tool_search and pass arguments as a JSON object."
            )
    return None


def _call_tool_error(
    name: object,
    arguments: object,
    deferred_toolkits: Sequence[DeferredToolkit] = (),
) -> str:
    if name is None or name == "" or (isinstance(name, str) and not name.strip()):
        arg_keys = _argument_keys(arguments)
        if arg_keys is not None and {"name", "arguments"} <= arg_keys:
            message = (
                "Nothing was called: missing top-level 'name' (found 'name' and 'arguments' nested inside 'arguments'). "
                'Pass both at top level: call_tool(name="<function>", arguments={...}) using an exact name from tool_search.'
            )
        elif arg_keys is not None and "name" in arg_keys:
            message = (
                "Nothing was called: missing top-level 'name' (found 'name' inside 'arguments'). "
                "Move 'name' to the top level and keep only the function's parameters in 'arguments': "
                'call_tool(name="<function>", arguments={...}) using an exact name from tool_search.'
            )
        else:
            message = (
                "Nothing was called: missing top-level 'name'. "
                'Pass call_tool(name="<function>", arguments={...}) using an exact function name from tool_search '
                "and a JSON object for arguments."
            )
        return message
    if isinstance(name, Mapping | list | tuple | set | frozenset):
        return (
            "Nothing was called: top-level 'name' must be a string from tool_search. "
            'Pass call_tool(name="<function>", arguments={...}).'
        )
    if name == "call_tool":
        return (
            "'call_tool' was not called: pass the deferred function's exact name from tool_search, "
            "not 'call_tool', with arguments as a JSON object."
        )
    if arguments not in (None, "") and not isinstance(arguments, Mapping) and arguments_object(arguments) is None:
        return (
            f"'{name}' was not called: 'arguments' must be a JSON object. "
            "Use an exact function name from tool_search and pass arguments as a JSON object."
        )
    if isinstance(name, str) and (dotted_hint := _dotted_name_hint(name, deferred_toolkits)) is not None:
        return dotted_hint
    return f"'{name}' was not called. Use an exact function name from tool_search and pass arguments as a JSON object."


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
        if instructions := deferred.deferral.instructions:
            texts.setdefault(deferred.deferral.domain_name, {})[instructions] = None
    return {domain: "\n\n".join(domain_texts) for domain, domain_texts in texts.items()}


def _function_definitions(deferred: DeferredToolkit) -> list[dict[str, Any]]:
    return [tool_definition(function.definition()) for function in deferred.functions]


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
        (4, _tokens(deferred.deferral.domain_name)),
        (2, detail_tokens),
        (1, _tokens(deferred.deferral.description)),
    )
    return sum(max((weight for weight, tokens in fields if token in tokens), default=0) for token in query_tokens)
