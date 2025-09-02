"""Pydantic models for Matrix state."""

from datetime import UTC, datetime
from typing import Self

import yaml
from pydantic import BaseModel, Field, field_serializer

from mindroom.constants import MATRIX_STATE_FILE


class MatrixAccount(BaseModel):
    """Represents a Matrix account (user or agent)."""

    username: str
    password: str


class MatrixRoom(BaseModel):
    """Represents a Matrix room state."""

    room_id: str
    alias: str
    name: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_serializer("created_at")
    def serialize_datetime(self, dt: datetime) -> str:
        """Serialize datetime to ISO format string."""
        return dt.isoformat()


class MatrixState(BaseModel):
    """Complete Matrix state including accounts and rooms."""

    accounts: dict[str, MatrixAccount] = Field(default_factory=dict)
    rooms: dict[str, MatrixRoom] = Field(default_factory=dict)

    @classmethod
    def load(cls) -> Self:
        """Load state from file."""
        if not MATRIX_STATE_FILE.exists():
            return cls()

        with MATRIX_STATE_FILE.open() as f:
            data = yaml.safe_load(f) or {}

        return cls.model_validate(data)

    def save(self) -> None:
        """Save state to file."""
        # Use Pydantic's model_dump with custom serializer for datetime
        data = self.model_dump(mode="json")

        MATRIX_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with MATRIX_STATE_FILE.open("w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def get_account(self, key: str) -> MatrixAccount | None:
        """Get an account by key."""
        return self.accounts.get(key)

    def add_account(self, key: str, username: str, password: str) -> None:
        """Add or update an account."""
        self.accounts[key] = MatrixAccount(username=username, password=password)

    def get_room(self, key: str) -> MatrixRoom | None:
        """Get a room by key."""
        return self.rooms.get(key)

    def add_room(self, key: str, room_id: str, alias: str, name: str) -> None:
        """Add or update a room."""
        self.rooms[key] = MatrixRoom(room_id=room_id, alias=alias, name=name, created_at=datetime.now(tz=UTC))

    def get_room_aliases(self) -> dict[str, str]:
        """Get mapping of room aliases to room IDs."""
        return {key: room.room_id for key, room in self.rooms.items()}
