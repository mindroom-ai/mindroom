"""Matrix room management functions."""

from .state import MatrixRoom, MatrixState


def load_rooms() -> dict[str, MatrixRoom]:
    """Load room state from YAML file."""
    state = MatrixState.load()
    return state.rooms


def get_room_aliases() -> dict[str, str]:
    """Get mapping of room aliases to room IDs."""
    state = MatrixState.load()
    return state.get_room_aliases()


def get_room_id(room_key: str) -> str | None:
    """Get room ID for a given room key/alias."""
    state = MatrixState.load()
    room = state.get_room(room_key)
    return room.room_id if room else None


def add_room(room_key: str, room_id: str, alias: str, name: str) -> None:
    """Add a new room to the state."""
    state = MatrixState.load()
    state.add_room(room_key, room_id, alias, name)
    state.save()


def remove_room(room_key: str) -> bool:
    """Remove a room from the state."""
    state = MatrixState.load()
    if room_key in state.rooms:
        del state.rooms[room_key]
        state.save()
        return True
    return False


def resolve_room_aliases(room_list: list[str]) -> list[str]:
    """Resolve room aliases to room IDs.

    Args:
        room_list: List of room aliases or IDs

    Returns:
        List of room IDs (aliases resolved to IDs, IDs passed through)
    """
    room_aliases = get_room_aliases()
    return [room_aliases.get(room, room) for room in room_list]


def get_room_alias_from_id(room_id: str) -> str | None:
    """Get room alias from room ID (reverse lookup).

    Args:
        room_id: Matrix room ID

    Returns:
        Room alias if found, None otherwise
    """
    room_aliases = get_room_aliases()
    for alias, rid in room_aliases.items():
        if rid == room_id:
            return alias
    return None
