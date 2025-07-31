"""Manager for Matrix room configuration and persistence."""

import os
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class MatrixRoom(BaseModel):
    """Represents a Matrix room configuration."""

    room_id: str
    alias: str
    name: str
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")


MATRIX_ROOMS_FILE = Path(os.environ.get("MATRIX_ROOMS_FILE", "matrix_rooms.yaml"))


def load_rooms() -> dict[str, MatrixRoom]:
    """Load room configuration from YAML file."""
    if not MATRIX_ROOMS_FILE.exists():
        return {}

    with open(MATRIX_ROOMS_FILE) as f:
        data = yaml.safe_load(f) or {}

    rooms = {}
    for room_key, room_data in data.get("rooms", {}).items():
        rooms[room_key] = MatrixRoom(**room_data)

    return rooms


def save_rooms(rooms: dict[str, MatrixRoom]) -> None:
    """Save room configuration to YAML file."""
    data = {
        "rooms": {
            key: {
                "room_id": room.room_id,
                "alias": room.alias,
                "name": room.name,
                "created_at": room.created_at,
            }
            for key, room in rooms.items()
        }
    }

    # Add header comment
    yaml_content = """# Matrix room configuration
# This file tracks all Matrix rooms created for the multi-agent system
# It is automatically updated when rooms are created or deleted

"""
    yaml_content += yaml.dump(data, default_flow_style=False, sort_keys=False)

    with open(MATRIX_ROOMS_FILE, "w") as f:
        f.write(yaml_content)


def get_room_aliases() -> dict[str, str]:
    """Get mapping of room aliases to room IDs."""
    rooms = load_rooms()
    return {key: room.room_id for key, room in rooms.items()}


def get_room_id(room_key: str) -> str | None:
    """Get room ID for a given room key/alias."""
    rooms = load_rooms()
    room = rooms.get(room_key)
    return room.room_id if room else None


def add_room(room_key: str, room_id: str, alias: str, name: str) -> None:
    """Add a new room to the configuration."""
    rooms = load_rooms()
    rooms[room_key] = MatrixRoom(
        room_id=room_id,
        alias=alias,
        name=name,
    )
    save_rooms(rooms)


def remove_room(room_key: str) -> bool:
    """Remove a room from the configuration."""
    rooms = load_rooms()
    if room_key in rooms:
        del rooms[room_key]
        save_rooms(rooms)
        return True
    return False
