"""Matrix mention utilities."""

from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass
from functools import partial
from itertools import islice
from typing import TYPE_CHECKING, Any

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import current_entity_id, entity_identity_registry
from mindroom.matrix.identity import MatrixID, matrix_user_id_prefix_candidates, parse_current_matrix_user_id
from mindroom.matrix.message_builder import build_message_content, markdown_fenced_code_ranges, markdown_to_html
from mindroom.matrix_identifiers import unnamespaced_agent_name_from_username_localpart
from mindroom.tool_system.events import build_tool_trace_content, ensure_visible_tool_marker_spacing

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Iterator, Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.entity_resolution import EntityIdentityRegistry
    from mindroom.tool_system.events import ToolTraceEntry

_ENTITY_MENTION_PATTERN = re.compile(r"(?<![\w])@(?P<localpart>\w+)(?::[^\s]+)?", flags=re.IGNORECASE)
_FULL_MATRIX_ID_CANDIDATE_PATTERN = re.compile(r"(?<![-A-Za-z0-9._=/+])@\S+")
_EXPLICIT_MENTION_BOUNDARY = r"(?<![-A-Za-z0-9._=/+])"
_ALIAS_LOCALPART_PATTERN = re.compile(r"(?<![\w])@(\w+)")
_NON_WHITESPACE_RUN = re.compile(r"\S+")
# Each scanner resolves at most this many @ tokens per body, so one message costs bounded CPU on the shared loop.
# A body that reaches the budget is searched once more, in C, for mentions of configured entities only.
_MAX_MENTION_TOKENS_PER_SCANNER = 256


@dataclass(frozen=True)
class _MentionToken:
    start: int
    end: int
    localpart: str
    has_server_name: bool = False
    explicit_user_id: str | None = None


@dataclass(frozen=True)
class _MentionScan:
    """Budgeted mention tokens from one body, with its prose ranges and whether a budget ran out."""

    tokens: list[_MentionToken]
    prose_ranges: list[tuple[int, int]]
    budget_exhausted: bool


@dataclass(frozen=True)
class _DisjointSpans:
    """Sorted, pairwise-disjoint text spans with logarithmic overlap checks."""

    starts: list[int]
    ends: list[int]

    @classmethod
    def from_spans(cls, spans: Iterable[tuple[int, int]]) -> _DisjointSpans:
        ordered = sorted(spans)
        return cls(starts=[start for start, _end in ordered], ends=[end for _start, end in ordered])

    def overlaps(self, start: int, end: int) -> bool:
        """Return whether one span overlaps any stored span."""
        # Sorted disjoint spans also have sorted ends, so only the last span starting before `end` can reach `start`.
        index = bisect_left(self.starts, end) - 1
        return index >= 0 and self.ends[index] > start


@dataclass(frozen=True)
class _MentionResolution:
    plain_text: str
    markdown_text: str
    user_id: str


@dataclass(frozen=True)
class _MentionReplacement(_MentionResolution):
    start: int
    end: int


def parse_mentions_in_text(
    text: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    allow_generated_agent_localparts: bool = True,
) -> tuple[str, list[str], str]:
    """Parse text for agent/team mentions and return processed text with user IDs.

    Args:
        text: Text that may contain @entity_name mentions
        config: Application configuration
        runtime_paths: Explicit runtime context for namespace-aware mention resolution
        allow_generated_agent_localparts: Whether generated localparts like @mindroom_agent are aliases

    Returns:
        Tuple of (plain_text, list_of_mentioned_user_ids, markdown_text_with_links)

    """
    replacements = _mention_replacements(
        text,
        config,
        runtime_paths,
        allow_generated_agent_localparts=allow_generated_agent_localparts,
    )
    if not replacements:
        return text, [], text
    return (
        _apply_replacements(text, replacements, use_markdown=False),
        _mentioned_user_ids_from_replacements(replacements),
        _apply_replacements(text, replacements, use_markdown=True),
    )


def resolve_mentioned_user_ids_from_text(
    text: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[str]:
    """Resolve visible text mention tokens to Matrix user IDs."""
    replacements = _mention_replacements(text, config, runtime_paths, allow_generated_agent_localparts=False)
    return _mentioned_user_ids_from_replacements(replacements)


def _mention_replacements(
    text: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    allow_generated_agent_localparts: bool,
) -> list[_MentionReplacement]:
    """Return render-ready replacements for every resolvable mention in one body."""
    scan = _scan_mention_tokens(text)
    if not scan.tokens and not scan.budget_exhausted:
        return []

    registry = entity_identity_registry(config, runtime_paths)
    tokens = scan.tokens
    if scan.budget_exhausted:
        tokens = _with_uncapped_entity_tokens(
            text,
            scan,
            entity_user_ids=[current_id.full_id for current_id in registry.current_ids.values()],
            names_entity=partial(
                _entity_name_for_mention_localpart,
                entity_names=_entity_names_by_lowercase(config),
                allow_generated_agent_localparts=allow_generated_agent_localparts,
            ),
        )
    return _resolve_mention_tokens(
        tokens,
        registry=registry,
        config=config,
        allow_generated_agent_localparts=allow_generated_agent_localparts,
    )


def format_entity_mention(
    entity_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[str, list[str], str]:
    """Return one configured entity mention without resolving unrelated entities."""
    resolution = _entity_mention_resolution_from_user_id(
        entity_name,
        current_entity_id(entity_name, runtime_paths).full_id,
        config=config,
    )
    return resolution.plain_text, [resolution.user_id], resolution.markdown_text


def _mentioned_user_ids_from_replacements(replacements: list[_MentionReplacement]) -> list[str]:
    """Return replacement user IDs without duplicates while preserving mention order."""
    return list(dict.fromkeys(replacement.user_id for replacement in replacements))


def _scan_mention_tokens(text: str) -> _MentionScan:
    """Return ordered mention tokens from one message body, within the per-scanner token budget."""
    if "@" not in text:
        return _MentionScan(tokens=[], prose_ranges=[], budget_exhausted=False)

    # Fences span whole lines, so no token crosses a fence boundary and prose can be scanned on its own.
    prose_ranges = _ranges_outside(markdown_fenced_code_ranges(text), len(text))
    explicit_matches, explicit_exhausted = _budgeted_prose_matches(
        _FULL_MATRIX_ID_CANDIDATE_PATTERN,
        text,
        prose_ranges,
    )
    tokens = _scan_explicit_matrix_id_tokens(explicit_matches)
    alias_matches, alias_exhausted = _budgeted_prose_matches(_ENTITY_MENTION_PATTERN, text, prose_ranges)
    tokens.extend(
        _scan_entity_alias_tokens(
            alias_matches,
            occupied_spans=_DisjointSpans.from_spans((token.start, token.end) for token in tokens),
        ),
    )
    return _MentionScan(
        tokens=sorted(tokens, key=lambda token: token.start),
        prose_ranges=prose_ranges,
        budget_exhausted=explicit_exhausted or alias_exhausted,
    )


def _ranges_outside(ranges: list[tuple[int, int]], text_length: int) -> list[tuple[int, int]]:
    """Return the gaps around sorted, disjoint ranges within one text."""
    gaps: list[tuple[int, int]] = []
    cursor = 0
    for start, end in ranges:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = end
    if cursor < text_length:
        gaps.append((cursor, text_length))
    return gaps


def _prose_matches(pattern: re.Pattern[str], text: str, prose_ranges: list[tuple[int, int]]) -> Iterator[re.Match[str]]:
    """Yield pattern matches from prose ranges, in text order."""
    return (match for start, end in prose_ranges for match in pattern.finditer(text, start, end))


def _budgeted_prose_matches(
    pattern: re.Pattern[str],
    text: str,
    prose_ranges: list[tuple[int, int]],
) -> tuple[list[re.Match[str]], bool]:
    """Return at most the per-scanner token budget of prose matches, and whether more remained."""
    matches = list(islice(_prose_matches(pattern, text, prose_ranges), _MAX_MENTION_TOKENS_PER_SCANNER + 1))
    return matches[:_MAX_MENTION_TOKENS_PER_SCANNER], len(matches) > _MAX_MENTION_TOKENS_PER_SCANNER


def _scan_explicit_matrix_id_tokens(matches: list[re.Match[str]]) -> list[_MentionToken]:
    """Return explicit full-MXID tokens from candidate matches."""
    tokens: list[_MentionToken] = []
    for match in matches:
        user_id = _extract_longest_valid_matrix_user_id(match.group(0))
        if user_id is None:
            continue
        tokens.append(_explicit_token(match.start(), user_id))
    return tokens


def _explicit_token(start: int, user_id: str) -> _MentionToken:
    matrix_id = MatrixID.parse(user_id)
    return _MentionToken(
        start=start,
        end=start + len(user_id),
        localpart=matrix_id.username,
        has_server_name=True,
        explicit_user_id=matrix_id.full_id,
    )


def _scan_entity_alias_tokens(
    matches: list[re.Match[str]],
    *,
    occupied_spans: _DisjointSpans,
) -> list[_MentionToken]:
    """Return alias-style mention tokens from matches that do not overlap explicit tokens."""
    tokens: list[_MentionToken] = []
    for match in matches:
        if occupied_spans.overlaps(match.start(), match.end()):
            continue
        tokens.append(
            _MentionToken(
                start=match.start(),
                end=match.end(),
                localpart=_mention_localpart(match.group(0)),
                has_server_name=":" in match.group(0),
            ),
        )
    return tokens


def _with_uncapped_entity_tokens(
    text: str,
    scan: _MentionScan,
    *,
    entity_user_ids: Collection[str],
    names_entity: Callable[[str], str | None],
) -> list[_MentionToken]:
    """Add configured-entity mentions beyond the token budget, so junk tokens cannot hide a real one."""
    capped_explicit = [token for token in scan.tokens if token.explicit_user_id is not None]
    capped_explicit_spans = _DisjointSpans.from_spans((token.start, token.end) for token in capped_explicit)
    explicit = [
        *capped_explicit,
        *(
            token
            for token in _scan_entity_user_id_tokens(text, scan.prose_ranges, entity_user_ids)
            if not capped_explicit_spans.overlaps(token.start, token.end)
        ),
    ]
    explicit_spans = _DisjointSpans.from_spans((token.start, token.end) for token in explicit)
    aliases = [
        token
        for token in scan.tokens
        if token.explicit_user_id is None and not explicit_spans.overlaps(token.start, token.end)
    ]
    occupied_spans = _DisjointSpans.from_spans((token.start, token.end) for token in [*explicit, *aliases])
    aliases.extend(
        token
        for token in _scan_entity_alias_occurrences(text, scan.prose_ranges, names_entity)
        if not occupied_spans.overlaps(token.start, token.end)
    )
    return sorted([*explicit, *aliases], key=lambda token: token.start)


def _scan_entity_user_id_tokens(
    text: str,
    prose_ranges: list[tuple[int, int]],
    entity_user_ids: Collection[str],
) -> list[_MentionToken]:
    """Return explicit mentions of configured entity user IDs anywhere in prose."""
    if not entity_user_ids:
        return []
    pattern = re.compile(f"{_EXPLICIT_MENTION_BOUNDARY}(?:{_longest_first_alternation(entity_user_ids)})")
    tokens: list[_MentionToken] = []
    validations = 0
    for match in _prose_matches(pattern, text, prose_ranges):
        user_id = match.group(0)
        run = _NON_WHITESPACE_RUN.match(text, match.start())
        if run is not None and run.group(0) != user_id:
            # A longer token may extend the host or port, so it names this entity only if its longest valid prefix does.
            if validations == _MAX_MENTION_TOKENS_PER_SCANNER:
                continue
            validations += 1
            if _extract_longest_valid_matrix_user_id(run.group(0)) != user_id:
                continue
        tokens.append(_explicit_token(match.start(), user_id))
    return tokens


def _longest_first_alternation(values: Iterable[str]) -> str:
    """Return a regex alternation that prefers longer literals where one is a prefix of another."""
    ordered: list[str] = sorted(values, key=lambda value: len(value), reverse=True)
    return "|".join(re.escape(value) for value in ordered)


def _scan_entity_alias_occurrences(
    text: str,
    prose_ranges: list[tuple[int, int]],
    names_entity: Callable[[str], str | None],
) -> list[_MentionToken]:
    """Return alias-style mentions whose localpart names a configured entity, anywhere in prose."""
    found = {
        localpart.lower()
        for start, end in prose_ranges
        for localpart in set(_ALIAS_LOCALPART_PATTERN.findall(text, start, end))
    }
    mentioned = {localpart for localpart in found if names_entity(localpart) is not None}
    if not mentioned:
        return []
    pattern = re.compile(
        rf"(?<![\w])@(?P<localpart>{_longest_first_alternation(mentioned)})(?![\w])(?P<server>:[^\s]+)?",
        flags=re.IGNORECASE,
    )
    return [
        _MentionToken(start=match.start(), end=match.end(), localpart=match.group("localpart"))
        for match in _prose_matches(pattern, text, prose_ranges)
        if match.group("server") is None and match.group("localpart").lower() in mentioned
    ]


def _mention_localpart(mention_text: str) -> str:
    """Return the localpart-like segment from one raw mention token."""
    return mention_text[1:].split(":", 1)[0]


def _resolve_mention_tokens(
    tokens: list[_MentionToken],
    *,
    registry: EntityIdentityRegistry,
    config: Config,
    allow_generated_agent_localparts: bool,
) -> list[_MentionReplacement]:
    """Resolve scanned tokens into render-ready replacements, resolving each distinct token once."""
    entity_names = _entity_names_by_lowercase(config)
    resolutions: dict[tuple[str | None, str, bool], _MentionResolution | None] = {}
    replacements: list[_MentionReplacement] = []
    for token in tokens:
        key = (token.explicit_user_id, token.localpart, token.has_server_name)
        if key not in resolutions:
            resolutions[key] = _resolve_mention_token(
                token,
                registry=registry,
                config=config,
                entity_names=entity_names,
                allow_generated_agent_localparts=allow_generated_agent_localparts,
            )
        resolution = resolutions[key]
        if resolution is None:
            continue
        replacements.append(
            _MentionReplacement(
                start=token.start,
                end=token.end,
                plain_text=resolution.plain_text,
                markdown_text=resolution.markdown_text,
                user_id=resolution.user_id,
            ),
        )
    return replacements


def _resolve_mention_token(
    token: _MentionToken,
    *,
    registry: EntityIdentityRegistry,
    config: Config,
    entity_names: Mapping[str, str],
    allow_generated_agent_localparts: bool,
) -> _MentionResolution | None:
    """Resolve one scanned mention token into an entity or literal-user target."""
    if token.explicit_user_id is not None:
        return _resolve_explicit_matrix_id_token(
            token,
            registry=registry,
            config=config,
        )
    return _resolve_entity_alias_token(
        token.localpart,
        has_server_name=token.has_server_name,
        registry=registry,
        config=config,
        entity_names=entity_names,
        allow_generated_agent_localparts=allow_generated_agent_localparts,
    )


def _resolve_explicit_matrix_id_token(
    token: _MentionToken,
    *,
    registry: EntityIdentityRegistry,
    config: Config,
) -> _MentionResolution | None:
    """Resolve one explicit full MXID token."""
    explicit_user_id = token.explicit_user_id
    if explicit_user_id is None:
        msg = "Explicit MXID token is missing explicit_user_id"
        raise ValueError(msg)

    if entity_name := registry.current_entity_name_for_user_id(explicit_user_id, include_router=False):
        return _entity_mention_resolution(
            entity_name,
            registry=registry,
            config=config,
        )
    return _literal_user_resolution(explicit_user_id)


def _resolve_entity_alias_token(
    localpart: str,
    *,
    has_server_name: bool,
    registry: EntityIdentityRegistry,
    config: Config,
    entity_names: Mapping[str, str],
    allow_generated_agent_localparts: bool,
) -> _MentionResolution | None:
    """Resolve one alias-style token to a local configured agent or team, if any."""
    if has_server_name:
        return None
    if entity_name := _entity_name_for_mention_localpart(
        localpart,
        entity_names,
        allow_generated_agent_localparts=allow_generated_agent_localparts,
    ):
        return _entity_mention_resolution(
            entity_name,
            registry=registry,
            config=config,
        )
    return None


def _entity_mention_resolution(
    entity_name: str,
    *,
    registry: EntityIdentityRegistry,
    config: Config,
) -> _MentionResolution:
    """Return rendering data for one resolved local agent or team mention."""
    return _entity_mention_resolution_from_user_id(
        entity_name,
        registry.current_id(entity_name).full_id,
        config=config,
    )


def _entity_mention_resolution_from_user_id(
    entity_name: str,
    resolved_user_id: str,
    *,
    config: Config,
) -> _MentionResolution:
    """Return rendering data for one resolved entity user ID."""
    return _MentionResolution(
        plain_text=resolved_user_id,
        markdown_text=f"[@{config.entity_display_name(entity_name)}](https://matrix.to/#/{resolved_user_id})",
        user_id=resolved_user_id,
    )


def _literal_user_resolution(user_id: str) -> _MentionResolution:
    """Return rendering data for one literal Matrix user mention."""
    return _MentionResolution(
        plain_text=user_id,
        markdown_text=f"[{user_id}](https://matrix.to/#/{user_id})",
        user_id=user_id,
    )


def _extract_longest_valid_matrix_user_id(token: str) -> str | None:
    """Return the longest valid Matrix user ID prefix from one non-whitespace token."""
    for candidate in matrix_user_id_prefix_candidates(token):
        if _is_valid_explicit_matrix_user_id(candidate):
            return candidate
    return None


def _is_valid_explicit_matrix_user_id(candidate: str) -> bool:
    """Return whether one candidate string is a valid explicit Matrix user ID."""
    try:
        parse_current_matrix_user_id(candidate)
    except ValueError:
        return False
    return True


def _entity_names_by_lowercase(config: Config) -> dict[str, str]:
    """Map each lowercased configured agent or team name to its first configured spelling."""
    names: dict[str, str] = {}
    for entity_name in (*config.agents, *config.teams):
        if entity_name != ROUTER_AGENT_NAME:
            names.setdefault(entity_name.lower(), entity_name)
    return names


def _entity_name_for_mention_localpart(
    localpart: str,
    entity_names: Mapping[str, str],
    *,
    allow_generated_agent_localparts: bool,
) -> str | None:
    """Return the entity one mention localpart names, trying generated ``mindroom_<name>`` aliases second."""
    if entity_name := entity_names.get(localpart.lower()):
        return entity_name
    if not allow_generated_agent_localparts:
        return None
    generated_name = unnamespaced_agent_name_from_username_localpart(localpart)
    if generated_name is None or generated_name.lower().startswith("user_"):
        return None
    return entity_names.get(generated_name.lower())


def resolve_entity_name_for_mention_localpart(
    localpart: str,
    config: Config,
    *,
    allow_generated_agent_localparts: bool = True,
) -> str | None:
    """Return the configured agent or team name matched by one Matrix mention localpart."""
    return _entity_name_for_mention_localpart(
        localpart,
        _entity_names_by_lowercase(config),
        allow_generated_agent_localparts=allow_generated_agent_localparts,
    )


def _apply_replacements(
    text: str,
    replacements: list[_MentionReplacement],
    *,
    use_markdown: bool,
) -> str:
    """Apply collected mention replacements to text."""
    if not replacements:
        return text

    parts: list[str] = []
    last_end = 0
    for replacement in replacements:
        start = replacement.start
        end = replacement.end
        if _is_wrapped_in_single_backticks(text, replacement.start, replacement.end):
            start -= 1
            end += 1
        parts.append(text[last_end:start])
        parts.append(replacement.markdown_text if use_markdown else replacement.plain_text)
        last_end = end
    parts.append(text[last_end:])
    return "".join(parts)


def _is_wrapped_in_single_backticks(text: str, start: int, end: int) -> bool:
    """Return whether one replacement is wrapped as exactly one inline code token."""
    return start > 0 and end < len(text) and text[start - 1] == "`" and text[end] == "`"


def format_message_with_mentions(
    config: Config,
    runtime_paths: RuntimePaths,
    text: str,
    thread_event_id: str | None = None,
    reply_to_event_id: str | None = None,
    latest_thread_event_id: str | None = None,
    tool_trace: list[ToolTraceEntry] | None = None,
    extra_content: dict[str, Any] | None = None,
    *,
    markdown_renderer: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Parse text for mentions and create properly formatted Matrix message.

    This is the universal function that should be used everywhere.

    Args:
        config: Application configuration
        runtime_paths: Explicit runtime context for mention parsing and HTML rendering
        text: Message text that may contain @entity_name mentions
        thread_event_id: Optional thread root event ID
        reply_to_event_id: Optional event ID to reply to (for genuine replies)
        latest_thread_event_id: Optional latest event ID in thread (for fallback compatibility)
        tool_trace: Optional structured tool trace metadata
        extra_content: Optional custom metadata fields merged into content
        markdown_renderer: Optional response-scoped renderer for repeated Markdown input

    Returns:
        Properly formatted content dict for room_send

    """
    spaced_text = ensure_visible_tool_marker_spacing(text)
    plain_text, mentioned_user_ids, markdown_text = parse_mentions_in_text(
        spaced_text,
        config,
        runtime_paths,
    )

    # Convert markdown (with links) to HTML
    # The markdown converter will properly handle the [@DisplayName](url) format
    render_markdown = markdown_to_html if markdown_renderer is None else markdown_renderer
    formatted_html = render_markdown(markdown_text)
    tool_trace_content = build_tool_trace_content(tool_trace)
    merged_extra_content: dict[str, Any] = {}
    if tool_trace_content:
        merged_extra_content.update(tool_trace_content)
    if extra_content:
        merged_extra_content.update(extra_content)
    inherited_mentions = merged_extra_content.pop("m.mentions", None)
    inherited_user_ids = inherited_mentions.get("user_ids", []) if isinstance(inherited_mentions, dict) else []
    merged_mentioned_user_ids = list(mentioned_user_ids)
    for user_id in inherited_user_ids:
        if isinstance(user_id, str) and user_id not in merged_mentioned_user_ids:
            merged_mentioned_user_ids.append(user_id)

    return build_message_content(
        body=plain_text,
        formatted_body=formatted_html,
        mentioned_user_ids=merged_mentioned_user_ids,
        thread_event_id=thread_event_id,
        reply_to_event_id=reply_to_event_id,
        latest_thread_event_id=latest_thread_event_id,
        extra_content=merged_extra_content or None,
    )
