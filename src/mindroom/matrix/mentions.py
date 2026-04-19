"""Matrix mention utilities."""

import ipaddress
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths
from mindroom.matrix.identity import MatrixID, agent_username_localpart, mindroom_namespace
from mindroom.matrix.message_builder import build_message_content, markdown_to_html
from mindroom.tool_system.events import build_tool_trace_content

if TYPE_CHECKING:
    from mindroom.tool_system.events import ToolTraceEntry

_AGENT_MENTION_PATTERN = re.compile(r"@(mindroom_)?(\w+)(?::[^\s]+)?", flags=re.IGNORECASE)
_FULL_MATRIX_ID_CANDIDATE_PATTERN = re.compile(r"(?<![-A-Za-z0-9._=/+])@\S+")
_DNS_LABEL_PATTERN = re.compile(r"[A-Za-z0-9-]+")
_MATRIX_USER_ID_LOCALPART_CHARACTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._=/-+")


@dataclass(frozen=True)
class _MentionReplacement:
    start: int
    end: int
    plain_text: str
    markdown_text: str
    user_id: str


def parse_mentions_in_text(
    text: str,
    sender_domain: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[str, list[str], str]:
    """Parse text for agent mentions and return processed text with user IDs.

    Args:
        text: Text that may contain @agent_name mentions
        sender_domain: Domain part of the sender's user ID (e.g., "localhost" from "@user:localhost")
        config: Application configuration
        runtime_paths: Explicit runtime context for namespace-aware mention resolution

    Returns:
        Tuple of (plain_text, list_of_mentioned_user_ids, markdown_text_with_links)

    """
    replacements = _collect_full_matrix_id_replacements(
        text,
        sender_domain=sender_domain,
        config=config,
        runtime_paths=runtime_paths,
    )
    replacements.extend(
        _collect_agent_mention_replacements(
            text,
            sender_domain=sender_domain,
            config=config,
            runtime_paths=runtime_paths,
            occupied_ranges=[(replacement.start, replacement.end) for replacement in replacements],
        ),
    )
    ordered_replacements = sorted(replacements, key=lambda replacement: replacement.start)

    mentioned_user_ids: list[str] = []
    for replacement in ordered_replacements:
        if replacement.user_id not in mentioned_user_ids:
            mentioned_user_ids.append(replacement.user_id)

    return (
        _apply_replacements(text, ordered_replacements, use_markdown=False),
        mentioned_user_ids,
        _apply_replacements(text, ordered_replacements, use_markdown=True),
    )


def _collect_agent_mention_replacements(
    text: str,
    *,
    sender_domain: str,
    config: Config,
    runtime_paths: RuntimePaths,
    occupied_ranges: list[tuple[int, int]],
) -> list[_MentionReplacement]:
    """Return replacement data for configured agent mentions in text."""
    replacements: list[_MentionReplacement] = []
    for match in _AGENT_MENTION_PATTERN.finditer(text):
        if _range_overlaps_existing(match.start(), match.end(), occupied_ranges):
            continue
        mention_info = _process_mention(match, config, sender_domain, runtime_paths)
        if mention_info is None:
            continue
        _original, user_id, display_name = mention_info
        replacements.append(
            _MentionReplacement(
                start=match.start(),
                end=match.end(),
                plain_text=user_id,
                markdown_text=f"[@{display_name}](https://matrix.to/#/{user_id})",
                user_id=user_id,
            ),
        )
    return replacements


def _collect_full_matrix_id_replacements(
    text: str,
    *,
    sender_domain: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[_MentionReplacement]:
    """Return replacement data for explicit full Matrix user IDs in text."""
    replacements: list[_MentionReplacement] = []
    for match in _FULL_MATRIX_ID_CANDIDATE_PATTERN.finditer(text):
        user_id = _extract_longest_valid_matrix_user_id(match.group(0))
        if user_id is None:
            continue
        matrix_id = MatrixID.parse(user_id)
        replacement = _replacement_for_explicit_matrix_id(
            matrix_id,
            sender_domain=sender_domain,
            config=config,
            runtime_paths=runtime_paths,
        )
        replacements.append(
            _MentionReplacement(
                start=match.start(),
                end=match.start() + len(user_id),
                plain_text=replacement.plain_text,
                markdown_text=replacement.markdown_text,
                user_id=replacement.user_id,
            ),
        )
    return replacements


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
        matrix_id = MatrixID.parse(candidate)
    except ValueError:
        return False

    if not matrix_id.username or len(candidate.encode("utf-8")) > 255:
        return False
    if any(character not in _MATRIX_USER_ID_LOCALPART_CHARACTERS for character in matrix_id.username):
        return False
    return _is_valid_matrix_server_name(matrix_id.domain)


def _is_valid_matrix_server_name(server_name: str) -> bool:
    """Return whether one Matrix server name matches hostname/IP plus optional port."""
    split = _split_server_name_and_port(server_name)
    if split is None:
        return False
    host, port = split
    if port is not None and (not port.isdigit() or len(port) > 5):
        return False
    if host.startswith("["):
        is_valid_ipv6_host = host.endswith("]")
        if is_valid_ipv6_host:
            try:
                ipaddress.IPv6Address(host[1:-1])
            except ValueError:
                is_valid_ipv6_host = False
        return is_valid_ipv6_host
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        is_valid_host = _is_valid_dns_name(host)
    else:
        is_valid_host = True
    return is_valid_host


def _split_server_name_and_port(server_name: str) -> tuple[str, str | None] | None:
    """Split a Matrix server name into host and optional port."""
    if not server_name:
        return None

    if server_name.startswith("["):
        closing_index = server_name.find("]")
        if closing_index == -1:
            return None
        host = server_name[: closing_index + 1]
        remainder = server_name[closing_index + 1 :]
        port = remainder[1:] if remainder.startswith(":") else None
        if remainder and port is None:
            return None
    elif ":" in server_name:
        host, port = server_name.rsplit(":", 1)
    else:
        host, port = server_name, None

    if not host or port == "":
        return None
    return host, port


def _is_valid_dns_name(host: str) -> bool:
    """Return whether one host string is a valid DNS name."""
    labels = host.split(".")
    return bool(host) and all(label and _DNS_LABEL_PATTERN.fullmatch(label) for label in labels)


def _replacement_for_explicit_matrix_id(
    matrix_id: MatrixID,
    *,
    sender_domain: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> _MentionReplacement:
    """Return replacement data for one explicit Matrix user ID."""
    user_id = matrix_id.full_id
    if agent_name := _agent_name_for_explicit_matrix_id(matrix_id, config, runtime_paths):
        agent_config = config.agents[agent_name]
        resolved_user_id = MatrixID.from_agent(agent_name, sender_domain, runtime_paths).full_id
        return _MentionReplacement(
            start=0,
            end=0,
            plain_text=resolved_user_id,
            markdown_text=f"[@{agent_config.display_name}](https://matrix.to/#/{resolved_user_id})",
            user_id=resolved_user_id,
        )
    return _MentionReplacement(
        start=0,
        end=0,
        plain_text=user_id,
        markdown_text=f"[{user_id}](https://matrix.to/#/{user_id})",
        user_id=user_id,
    )


def _agent_name_for_explicit_matrix_id(
    matrix_id: MatrixID,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Return the agent name for an explicit MXID only when the localpart is the full agent localpart."""
    matrix_username = matrix_id.username.lower()
    for agent_name in config.agents:
        if agent_username_localpart(agent_name, runtime_paths).lower() == matrix_username:
            return agent_name
    return None


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
        parts.append(text[last_end : replacement.start])
        parts.append(replacement.markdown_text if use_markdown else replacement.plain_text)
        last_end = replacement.end
    parts.append(text[last_end:])
    return "".join(parts)


def _process_mention(
    match: re.Match,
    config: Config,
    sender_domain: str,
    runtime_paths: RuntimePaths,
) -> tuple[str, str, str] | None:
    """Process a single mention match and return replacement data.

    Args:
        match: The regex match object
        config: The loaded config
        sender_domain: Domain for constructing Matrix IDs
        runtime_paths: Explicit runtime context for namespace-aware agent lookup

    Returns:
        Tuple of (original_text, matrix_user_id, display_name) or None if not a valid agent

    """
    original = match.group(0)
    name = match.group(2)

    # Skip user-like mentions (e.g. mindroom_user_*)
    if name.lower().startswith("user_"):
        return None

    agent_name = _find_matching_agent_name(match, config, runtime_paths)
    if agent_name is None:
        return None

    agent_config = config.agents[agent_name]
    user_id = MatrixID.from_agent(agent_name, sender_domain, runtime_paths).full_id
    return (original, user_id, agent_config.display_name)


def _find_matching_agent_name(
    match: re.Match,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Return the configured agent name matched by one mention, if any."""
    for candidate_name in _mention_candidate_names(match, runtime_paths):
        candidate_lower = candidate_name.lower()
        for config_agent_name in config.agents:
            if config_agent_name.lower() == candidate_lower:
                return config_agent_name
    return None


def _mention_candidate_names(match: re.Match, runtime_paths: RuntimePaths) -> list[str]:
    """Build ordered candidate agent names for one mention match."""
    name = match.group(2)
    prefix = match.group(1)

    # Prefer exact/base forms first, then prefix-reconstructed variants.
    candidate_names = [name]
    stripped_name: str | None = None

    namespace = mindroom_namespace(runtime_paths)
    if namespace:
        suffix = f"_{namespace}"
        if name.lower().endswith(suffix):
            stripped_name = name[: -len(suffix)]
            if stripped_name:
                candidate_names.append(stripped_name)
            else:
                stripped_name = None

    # When the regex captured a "mindroom_" prefix (group 1), the original mention
    # was e.g. "@mindroom_dev" but group(2) is just "dev". The config key might
    # be "mindroom_dev", so we must also try the un-stripped form. For namespaced
    # mentions like "@mindroom_dev_ns123", we also need the combined
    # prefix-plus-namespace-stripped candidate "mindroom_dev".
    if prefix:
        candidate_names.append(f"{prefix}{name}")
        if stripped_name:
            candidate_names.append(f"{prefix}{stripped_name}")
    return candidate_names


def format_message_with_mentions(
    config: Config,
    runtime_paths: RuntimePaths,
    text: str,
    sender_domain: str = "localhost",
    thread_event_id: str | None = None,
    reply_to_event_id: str | None = None,
    latest_thread_event_id: str | None = None,
    tool_trace: list["ToolTraceEntry"] | None = None,
    extra_content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse text for mentions and create properly formatted Matrix message.

    This is the universal function that should be used everywhere.

    Args:
        config: Application configuration
        runtime_paths: Explicit runtime context for mention parsing and HTML rendering
        text: Message text that may contain @agent_name mentions
        sender_domain: Domain part of the sender's user ID
        thread_event_id: Optional thread root event ID
        reply_to_event_id: Optional event ID to reply to (for genuine replies)
        latest_thread_event_id: Optional latest event ID in thread (for fallback compatibility)
        tool_trace: Optional structured tool trace metadata
        extra_content: Optional custom metadata fields merged into content

    Returns:
        Properly formatted content dict for room_send

    """
    plain_text, mentioned_user_ids, markdown_text = parse_mentions_in_text(text, sender_domain, config, runtime_paths)

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
