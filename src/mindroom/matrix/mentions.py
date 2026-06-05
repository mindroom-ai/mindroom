"""Matrix mention utilities."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import entity_identity_registry
from mindroom.matrix.identity import MatrixID, parse_current_matrix_user_id
from mindroom.matrix.message_builder import build_message_content, markdown_to_html
from mindroom.matrix_identifiers import unnamespaced_agent_name_from_username_localpart
from mindroom.tool_system.events import build_tool_trace_content, ensure_visible_tool_marker_spacing

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.entity_resolution import EntityIdentityRegistry
    from mindroom.tool_system.events import ToolTraceEntry

_ENTITY_MENTION_PATTERN = re.compile(r"(?<![\w])@(?P<localpart>\w+)(?::[^\s]+)?", flags=re.IGNORECASE)
_FULL_MATRIX_ID_CANDIDATE_PATTERN = re.compile(r"(?<![-A-Za-z0-9._=/+])@\S+")


@dataclass(frozen=True)
class _MentionToken:
    start: int
    end: int
    localpart: str
    has_server_name: bool = False
    explicit_user_id: str | None = None


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
    tokens = _scan_mention_tokens(text)
    if not tokens:
        return text, [], text

    registry = entity_identity_registry(config, runtime_paths)
    replacements = _resolve_mention_tokens(
        tokens,
        registry=registry,
        config=config,
        allow_generated_agent_localparts=allow_generated_agent_localparts,
    )

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
    tokens = _scan_mention_tokens(text)
    if not tokens:
        return []

    registry = entity_identity_registry(config, runtime_paths)
    replacements = _resolve_mention_tokens(
        tokens,
        registry=registry,
        config=config,
        allow_generated_agent_localparts=False,
    )
    return _mentioned_user_ids_from_replacements(replacements)


def _mentioned_user_ids_from_replacements(replacements: list[_MentionReplacement]) -> list[str]:
    """Return replacement user IDs without duplicates while preserving mention order."""
    mentioned_user_ids: list[str] = []
    for replacement in replacements:
        if replacement.user_id not in mentioned_user_ids:
            mentioned_user_ids.append(replacement.user_id)
    return mentioned_user_ids


def _scan_mention_tokens(text: str) -> list[_MentionToken]:
    """Return ordered mention tokens from one message body."""
    tokens = _scan_explicit_matrix_id_tokens(text)
    tokens.extend(
        _scan_entity_alias_tokens(
            text,
            occupied_ranges=[(token.start, token.end) for token in tokens],
        ),
    )
    return sorted(tokens, key=lambda token: token.start)


def _scan_explicit_matrix_id_tokens(text: str) -> list[_MentionToken]:
    """Return explicit full-MXID tokens from text."""
    tokens: list[_MentionToken] = []
    for match in _FULL_MATRIX_ID_CANDIDATE_PATTERN.finditer(text):
        user_id = _extract_longest_valid_matrix_user_id(match.group(0))
        if user_id is None:
            continue
        matrix_id = MatrixID.parse(user_id)
        tokens.append(
            _MentionToken(
                start=match.start(),
                end=match.start() + len(user_id),
                localpart=matrix_id.username,
                has_server_name=True,
                explicit_user_id=matrix_id.full_id,
            ),
        )
    return tokens


def _scan_entity_alias_tokens(
    text: str,
    *,
    occupied_ranges: list[tuple[int, int]],
) -> list[_MentionToken]:
    """Return non-overlapping alias-style mention tokens from text."""
    tokens: list[_MentionToken] = []
    for match in _ENTITY_MENTION_PATTERN.finditer(text):
        if _range_overlaps_existing(match.start(), match.end(), occupied_ranges):
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
    """Resolve scanned tokens into render-ready replacements."""
    replacements: list[_MentionReplacement] = []
    for token in tokens:
        resolution = _resolve_mention_token(
            token,
            registry=registry,
            config=config,
            allow_generated_agent_localparts=allow_generated_agent_localparts,
        )
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
    allow_generated_agent_localparts: bool,
) -> _MentionResolution | None:
    """Resolve one alias-style token to a local configured agent or team, if any."""
    if has_server_name:
        return None
    if entity_name := _find_matching_entity_name_for_localpart(
        localpart,
        config,
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
    entity_config = config.agents.get(entity_name) or config.teams[entity_name]
    resolved_user_id = registry.current_id(entity_name).full_id
    return _MentionResolution(
        plain_text=resolved_user_id,
        markdown_text=f"[@{entity_config.display_name}](https://matrix.to/#/{resolved_user_id})",
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
    for end in range(len(token), 0, -1):
        candidate = token[:end]
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


def _find_matching_entity_name_for_localpart(
    localpart: str,
    config: Config,
    *,
    allow_generated_agent_localparts: bool,
) -> str | None:
    """Return the configured agent or team name matched by one localpart string, if any."""
    entities = [*config.agents, *config.teams]

    if entity_name := _find_matching_entity_name(localpart, entities):
        return entity_name

    if not allow_generated_agent_localparts:
        return None

    generated_name = unnamespaced_agent_name_from_username_localpart(localpart)
    if generated_name is None or generated_name.lower().startswith("user_"):
        return None
    return _find_matching_entity_name(generated_name, entities)


def _find_matching_entity_name(
    localpart: str,
    entities: list[str],
) -> str | None:
    """Return the configured entity name matched by one localpart candidate."""
    lower_localpart = localpart.lower()
    for entity_name in entities:
        if entity_name == ROUTER_AGENT_NAME:
            continue
        if entity_name.lower() == lower_localpart:
            return entity_name
    return None


def resolve_entity_name_for_mention_localpart(
    localpart: str,
    config: Config,
    *,
    allow_generated_agent_localparts: bool = True,
) -> str | None:
    """Return the configured agent or team name matched by one Matrix mention localpart."""
    return _find_matching_entity_name_for_localpart(
        localpart,
        config,
        allow_generated_agent_localparts=allow_generated_agent_localparts,
    )


def _range_overlaps_existing(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    """Return whether one text span overlaps any existing replacement span."""
    return any(start < existing_end and end > existing_start for existing_start, existing_end in ranges)


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
    formatted_html = markdown_to_html(markdown_text)
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
