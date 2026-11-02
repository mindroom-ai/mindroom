"""Matrix identifier helpers that do not depend on Matrix runtime modules."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from mindroom.constants import RuntimePaths, runtime_matrix_server_name, runtime_mindroom_namespace

if TYPE_CHECKING:
    from collections.abc import Iterable

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


def namespaced_username_localpart(localpart: str, runtime_paths: RuntimePaths) -> str:
    """Append the installation namespace, when one is set, to a Matrix username localpart."""
    namespace = _mindroom_namespace(runtime_paths)
    if namespace:
        return f"{localpart}_{namespace}"
    return localpart


def agent_username_localpart(agent_name: str, runtime_paths: RuntimePaths) -> str:
    """Build the Matrix username localpart for an agent-like entity."""
    return namespaced_username_localpart(f"{_AGENT_USERNAME_PREFIX}{agent_name}", runtime_paths)


def unnamespaced_agent_name_from_username_localpart(username_localpart: str) -> str | None:
    """Extract an unnamespaced generated agent name from a Matrix username localpart."""
    if not username_localpart.lower().startswith(_AGENT_USERNAME_PREFIX):
        return None
    agent_name = username_localpart[len(_AGENT_USERNAME_PREFIX) :]
    return agent_name or None


def is_managed_room_key(room_key: str) -> bool:
    """Return whether a configured room reference names a managed room, not a Matrix room ID or alias."""
    return not room_key.startswith(("!", "#"))


def managed_room_alias_localpart(room_key: str, runtime_paths: RuntimePaths) -> str:
    """Build the managed room alias localpart for a room key."""
    namespace = _mindroom_namespace(runtime_paths)
    if not namespace:
        return room_key
    return f"{room_key}_{namespace}"


def managed_room_alias(room_key: str, server_name: str, runtime_paths: RuntimePaths) -> str:
    """Build the full alias MindRoom gives the managed room for a room key, as its record stores it."""
    return f"#{managed_room_alias_localpart(room_key, runtime_paths)}:{server_name}"


def managed_space_alias_localpart(runtime_paths: RuntimePaths) -> str:
    """Build the reserved alias localpart for the root MindRoom Space."""
    return managed_room_alias_localpart("_mindroom_root_space", runtime_paths)


def server_emoji_alias_localpart(runtime_paths: RuntimePaths) -> str:
    """Build the alias localpart of the server custom emoji room."""
    return managed_room_alias_localpart("emoji", runtime_paths)


def managed_room_key_for_alias(room_alias: str, server_name: str, runtime_paths: RuntimePaths) -> str | None:
    """Return the room key whose managed room alias is exactly ``room_alias``, or None for any other reference."""
    alias_localpart = room_alias[1:].split(":", 1)[0]
    namespace = _mindroom_namespace(runtime_paths)
    room_key = alias_localpart.removesuffix(f"_{namespace}") if namespace else alias_localpart
    return room_key if managed_room_alias(room_key, server_name, runtime_paths) == room_alias else None


def is_concrete_matrix_user_id(user_id: str) -> bool:
    """Return whether this string is a concrete Matrix user ID (no wildcards or placeholders)."""
    if not user_id.startswith("@") or "*" in user_id or "?" in user_id:
        return False
    if any(character.isspace() for character in user_id):
        return False
    localpart, separator, domain = user_id[1:].partition(":")
    return bool(separator) and bool(localpart) and bool(domain)


def split_concrete_matrix_user_ids(user_ids: Iterable[str]) -> tuple[list[str], list[str]]:
    """Split user IDs into deduplicated concrete Matrix IDs and sorted skipped entries."""
    unique_ids = list(dict.fromkeys(user_ids))
    concrete = [user_id for user_id in unique_ids if is_concrete_matrix_user_id(user_id)]
    skipped = sorted(user_id for user_id in unique_ids if not is_concrete_matrix_user_id(user_id))
    return concrete, skipped


def extract_server_name_from_homeserver(homeserver: str, runtime_paths: RuntimePaths) -> str:
    """Extract the Matrix server name from a homeserver URL."""
    if server_name := runtime_matrix_server_name(runtime_paths):
        return server_name

    server_part = homeserver.split("://", 1)[1] if "://" in homeserver else homeserver
    if ":" in server_part:
        return server_part.split(":", 1)[0]
    return server_part
