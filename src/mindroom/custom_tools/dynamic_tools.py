"""Search and call deferred tools on models without native tool search."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, NamedTuple

from mindroom.config.tool_aliases import canonical_tool_name
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.llm.tool_search import arguments_object
from mindroom.tool_system.authoring import Toolkit, passthrough_tool, tool
from mindroom.tool_system.tool_access import tool_definition

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from mindroom.tool_system.dynamic_toolkits import DeferredToolkit


_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_UPPER_RE = re.compile(r"([A-Z]+)([A-Z][a-z])")
_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "with",
    },
)
_BM25_K1 = 1.2
_BM25_B = 0.75
_NAME_WEIGHT = 3.0
_DOMAIN_WEIGHT = 1.5
_NAMESPACE_WEIGHT = 0.5
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


def _split_words(text: str) -> list[str]:
    split = _CAMEL_UPPER_RE.sub(r"\1 \2", _CAMEL_BOUNDARY_RE.sub(r"\1 \2", text))
    return _WORD_RE.findall(split.lower())


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return f"{word[:-3]}y"
    if len(word) > 4 and word.endswith(("ches", "shes", "sses", "xes", "zes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _tokenize(text: str) -> list[str]:
    return [_stem(word) for word in _split_words(text) if word not in _STOP_WORDS]


def _query_term_groups(query: str) -> list[frozenset[str]]:
    raw_words = _split_words(query)
    if not raw_words:
        return []
    words = [word for word in raw_words if word not in _STOP_WORDS] or raw_words
    groups: list[frozenset[str]] = []
    for word in words:
        stemmed = _stem(word)
        terms = {stemmed}
        # A toolkit's other names find it too.
        for candidate in (word, stemmed):
            if (canonical := canonical_tool_name(candidate)) != candidate:
                terms.update(_tokenize(canonical))
        if (group := frozenset(terms)) not in groups:
            groups.append(group)
    return groups


class _IndexedFunction(NamedTuple):
    deferred: DeferredToolkit
    definition: dict[str, Any]
    name_terms: frozenset[str]
    domain_terms: frozenset[str]
    detail_counts: Counter[str]
    detail_length: int
    namespace_terms: frozenset[str]
    all_terms: frozenset[str]


class _SearchMatch(NamedTuple):
    doc: _IndexedFunction
    exact: bool
    covered: int
    has_name_or_domain_hit: bool
    score: float


class DynamicToolsToolkit(Toolkit):
    """Return deferred function definitions on request; deferred calls arrive through ``call_tool``.

    ``deferred_toolkits`` returns the deferred toolkits as the agent's runs offer them, which the agent knows only once
    all of its toolkits are assembled, after this one.
    """

    def __init__(self, *, deferred_toolkits: Callable[[], Sequence[DeferredToolkit]]) -> None:
        self._deferred_toolkits = deferred_toolkits
        # Fresh instance per turn, so dedup state stays strictly within-turn.
        self._returned_functions: set[str] = set()
        self._returned_instructions: dict[str, str] = {}
        super().__init__(name="dynamic_tools")

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        return custom_tool_payload("dynamic_tools", status, **kwargs)

    def _render_matches(self, matches: Sequence[_SearchMatch]) -> tuple[list[dict[str, Any]], dict[str, str], bool]:
        rendered: list[dict[str, Any]] = []
        exact_domains: set[str] = set()
        fresh_domains: set[str] = set()
        any_stubbed = False
        for match in matches:
            definition = match.doc.definition
            name = definition["name"]
            domain = match.doc.deferred.deferral.domain_name
            if not match.exact and name in self._returned_functions:
                any_stubbed = True
                rendered.append({"name": name, "toolkit": domain, "already_returned": True})
                continue
            rendered.append({**definition, "toolkit": domain})
            self._returned_functions.add(name)
            (exact_domains if match.exact else fresh_domains).add(domain)

        instructions: dict[str, str] = {}
        for domain, text in _domain_instructions(match.doc.deferred for match in matches).items():
            if domain in exact_domains or (domain in fresh_domains and self._returned_instructions.get(domain) != text):
                instructions[domain] = text
                self._returned_instructions[domain] = text
        return rendered, instructions, any_stubbed

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
        query_groups = _query_term_groups(query)
        # No function name lacks word characters, so a query without them can only browse.
        if not query_groups:
            return self._listing()
        scored = _rank_functions(_index_functions(self._deferred_toolkits()), query_groups, raw_query)
        matches = scored[: max(1, min(max_results, _MAX_RESULTS_LIMIT))]
        rendered_matches, instructions, any_stubbed = self._render_matches(matches)
        usage = "Call a match with call_tool(name, arguments), passing arguments as a JSON object."
        if any_stubbed:
            usage += (
                " Matches with already_returned=true were shown earlier in this turn; "
                "search by exact name to re-fetch the full schema."
            )
        payload: dict[str, object] = {"matches": rendered_matches, "usage": usage}
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


def _index_functions(deferred_toolkits: Sequence[DeferredToolkit]) -> list[_IndexedFunction]:
    docs: list[_IndexedFunction] = []
    for deferred in deferred_toolkits:
        domain_terms = frozenset(_tokenize(deferred.deferral.domain_name))
        namespace_terms = frozenset(_tokenize(deferred.deferral.description))
        for definition in _function_definitions(deferred):
            name_terms = frozenset(_tokenize(definition["name"]))
            detail_tokens = _tokenize(definition["description"])
            for parameter_name in definition["parameters"].get("properties", {}):
                detail_tokens.extend(_tokenize(parameter_name))
            detail_counts = Counter(detail_tokens)
            all_terms = name_terms | domain_terms | frozenset(detail_counts) | namespace_terms
            docs.append(
                _IndexedFunction(
                    deferred,
                    definition,
                    name_terms,
                    domain_terms,
                    detail_counts,
                    len(detail_tokens),
                    namespace_terms,
                    all_terms,
                ),
            )
    return docs


def _bm25_term_score(doc: _IndexedFunction, term: str, idf: float, avg_detail_length: float) -> float:
    weight = (
        (_NAME_WEIGHT if term in doc.name_terms else 0.0)
        + (_DOMAIN_WEIGHT if term in doc.domain_terms else 0.0)
        + (_NAMESPACE_WEIGHT if term in doc.namespace_terms else 0.0)
    )
    if tf := doc.detail_counts.get(term, 0):
        denom = tf + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * (doc.detail_length / avg_detail_length))
        weight += (tf * (_BM25_K1 + 1.0)) / denom
    return idf * weight


def _rank_functions(
    docs: Sequence[_IndexedFunction],
    query_groups: Sequence[frozenset[str]],
    raw_query: str,
) -> list[_SearchMatch]:
    if not docs:
        return []
    n = len(docs)
    doc_freq: Counter[str] = Counter()
    for doc in docs:
        doc_freq.update(doc.all_terms)
    avg_detail_length = (sum(doc.detail_length for doc in docs) / n) or 1.0
    query_terms = {term for group in query_groups for term in group}
    idfs = {term: math.log(1.0 + (n - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5)) for term in query_terms}

    matches: list[_SearchMatch] = []
    for doc in docs:
        fn_name = doc.definition["name"].lower()
        exact = raw_query in {fn_name, f"{doc.deferred.deferral.domain_name.lower()}.{fn_name}"}
        covered = 0
        has_name_or_domain_hit = False
        score = 0.0
        for group in query_groups:
            if group.isdisjoint(doc.all_terms):
                continue
            covered += 1
            if not group.isdisjoint(doc.name_terms) or not group.isdisjoint(doc.domain_terms):
                has_name_or_domain_hit = True
            score += max(_bm25_term_score(doc, term, idfs[term], avg_detail_length) for term in group)
        if exact or covered > 0:
            matches.append(_SearchMatch(doc, exact, covered, has_name_or_domain_hit, score))

    if len(query_groups) >= 2 and any(match.exact or match.covered >= 2 for match in matches):
        matches = [match for match in matches if match.exact or match.covered >= 2 or match.has_name_or_domain_hit]
    # Stable sort preserves original registration order on ties.
    matches.sort(key=lambda match: (match.exact, match.covered, match.score), reverse=True)
    return matches
