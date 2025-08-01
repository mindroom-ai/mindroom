"""Matrix room management functions."""

from .config import MatrixConfig, MatrixRoom


def load_rooms() -> dict[str, MatrixRoom]:
    """Load room configuration from YAML file."""
    config = MatrixConfig.load()
    return config.rooms


def get_room_aliases() -> dict[str, str]:
    """Get mapping of room aliases to room IDs."""
    config = MatrixConfig.load()
    return config.get_room_aliases()


def get_room_id(room_key: str) -> str | None:
    """Get room ID for a given room key/alias."""
    config = MatrixConfig.load()
    room = config.get_room(room_key)
    return room.room_id if room else None


def add_room(room_key: str, room_id: str, alias: str, name: str) -> None:
    """Add a new room to the configuration."""
    config = MatrixConfig.load()
    config.add_room(room_key, room_id, alias, name)
    config.save()


def remove_room(room_key: str) -> bool:
    """Remove a room from the configuration."""
    config = MatrixConfig.load()
    if room_key in config.rooms:
        del config.rooms[room_key]
        config.save()
        return True
    return False
