"""Matrix identifier helpers that do not depend on Matrix runtime modules."""

from __future__ import annotations

import re

from mindroom.constants import RuntimePaths, runtime_matrix_server_name, runtime_mindroom_namespace

_AGENT_USERNAME_PREFIX = "mindroom_"
_NAMESPACE_PATTERN = re.compile(r"^[a-z0-9]{4,32}$")


def _normalize_namespace(namespace: str | None) -> str | None:
    """Normalize and validate an installation namespace."""
    if namespace is None:
        return None
    normalized = namespace.strip().lower()
    if not normalized:
        return None
    if not _NAMESPACE_PATTERN.fullmatch(normalized):
        msg = f"MINDROOM_NAMESPACE must match ^[a-z0-9]{{4,32}}$ (got: {namespace!r})"
        raise ValueError(msg)
    return normalized


def _mindroom_namespace(runtime_paths: RuntimePaths) -> str | None:
    """Return the configured installation namespace for one explicit runtime context."""
    return _normalize_namespace(runtime_mindroom_namespace(runtime_paths))


def agent_username_localpart(agent_name: str, runtime_paths: RuntimePaths) -> str:
    """Build the Matrix username localpart for an agent-like entity."""
    namespace = _mindroom_namespace(runtime_paths)
    if namespace:
        return f"{_AGENT_USERNAME_PREFIX}{agent_name}_{namespace}"
    return f"{_AGENT_USERNAME_PREFIX}{agent_name}"


def unnamespaced_agent_name_from_username_localpart(username_localpart: str) -> str | None:
    """Extract an unnamespaced generated agent name from a Matrix username localpart."""
    if not username_localpart.lower().startswith(_AGENT_USERNAME_PREFIX):
        return None
    agent_name = username_localpart[len(_AGENT_USERNAME_PREFIX) :]
    return agent_name or None


def managed_room_alias_localpart(room_key: str, runtime_paths: RuntimePaths) -> str:
    """Build the managed room alias localpart for a room key."""
    namespace = _mindroom_namespace(runtime_paths)
    if not namespace:
        return room_key
    return f"{room_key}_{namespace}"


def managed_space_alias_localpart(runtime_paths: RuntimePaths) -> str:
    """Build the reserved alias localpart for the root MindRoom Space."""
    return managed_room_alias_localpart("_mindroom_root_space", runtime_paths)


def managed_room_key_from_alias_localpart(
    alias_localpart: str,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Extract the configured managed room key from an alias localpart."""
    namespace = _mindroom_namespace(runtime_paths)
    if not namespace:
        return alias_localpart

    suffix = f"_{namespace}"
    if not alias_localpart.endswith(suffix):
        return None
    room_key = alias_localpart[: -len(suffix)]
    return room_key or None


def room_alias_localpart(room_alias: str) -> str | None:
    """Extract the localpart from a room alias like '#lobby:example.com'."""
    if not room_alias.startswith("#") or ":" not in room_alias:
        return None
    return room_alias[1:].split(":", 1)[0]


def room_alias_identifier_candidates(room_alias: str, runtime_paths: RuntimePaths) -> list[str]:
    """Return alias, localpart, and managed-room key identifiers for one Matrix alias."""
    identifiers = [room_alias]
    localpart = room_alias_localpart(room_alias)
    if not localpart:
        return identifiers
    identifiers.append(localpart)
    managed_room_key = managed_room_key_from_alias_localpart(localpart, runtime_paths)
    if managed_room_key:
        identifiers.append(managed_room_key)
    return identifiers


def extract_server_name_from_homeserver(homeserver: str, runtime_paths: RuntimePaths) -> str:
    """Extract the Matrix server name from a homeserver URL."""
    if server_name := runtime_matrix_server_name(runtime_paths):
        return server_name

    server_part = homeserver.split("://", 1)[1] if "://" in homeserver else homeserver
    if ":" in server_part:
        return server_part.split(":", 1)[0]
    return server_part
