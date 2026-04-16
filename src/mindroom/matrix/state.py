"""Pydantic models for Matrix state."""

from datetime import UTC, datetime
from typing import Self

import yaml
from pydantic import BaseModel, Field, field_serializer

from mindroom import constants


class _MatrixAccount(BaseModel):
    """Represents a Matrix account (user or agent)."""

    username: str
    password: str
    device_id: str | None = None
    access_token: str | None = None


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

    accounts: dict[str, _MatrixAccount] = Field(default_factory=dict)
    rooms: dict[str, MatrixRoom] = Field(default_factory=dict)
    space_room_id: str | None = None
    router_ad_hoc_room_ids: set[str] = Field(default_factory=set)
    router_ad_hoc_inviter_ids: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def load(cls, runtime_paths: constants.RuntimePaths) -> Self:
        """Load state from file."""
        state_file = constants.matrix_state_file(runtime_paths=runtime_paths)
        if not state_file.exists():
            return cls()

        with state_file.open() as f:
            data = yaml.safe_load(f) or {}

        return cls.model_validate(data)

    def save(self, runtime_paths: constants.RuntimePaths) -> None:
        """Save state to file."""
        # Use Pydantic's model_dump with custom serializer for datetime
        data = self.model_dump(mode="json")

        state_file = constants.matrix_state_file(runtime_paths=runtime_paths)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with state_file.open("w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def get_account(self, key: str) -> _MatrixAccount | None:
        """Get an account by key."""
        return self.accounts.get(key)

    def add_account(
        self,
        key: str,
        username: str,
        password: str,
        *,
        device_id: str | None = None,
        access_token: str | None = None,
    ) -> None:
        """Add or update an account."""
        self.accounts[key] = _MatrixAccount(
            username=username,
            password=password,
            device_id=device_id,
            access_token=access_token,
        )

    def get_room(self, key: str) -> MatrixRoom | None:
        """Get a room by key."""
        return self.rooms.get(key)

    def add_room(self, key: str, room_id: str, alias: str, name: str) -> None:
        """Add or update a room."""
        self.rooms[key] = MatrixRoom(room_id=room_id, alias=alias, name=name, created_at=datetime.now(tz=UTC))

    def get_room_aliases(self) -> dict[str, str]:
        """Get mapping of room aliases to room IDs."""
        return {key: room.room_id for key, room in self.rooms.items()}

    def set_space_room_id(self, room_id: str | None) -> None:
        """Persist the root Matrix Space room ID."""
        self.space_room_id = room_id

    def add_router_ad_hoc_room(self, room_id: str) -> bool:
        """Persist one router-managed ad-hoc room id."""
        if room_id in self.router_ad_hoc_room_ids:
            return False
        self.router_ad_hoc_room_ids.add(room_id)
        return True

    def remove_router_ad_hoc_room(self, room_id: str) -> bool:
        """Remove one persisted router-managed ad-hoc room id."""
        changed = False
        if room_id in self.router_ad_hoc_room_ids:
            self.router_ad_hoc_room_ids.remove(room_id)
            changed = True
        if room_id in self.router_ad_hoc_inviter_ids:
            del self.router_ad_hoc_inviter_ids[room_id]
            changed = True
        return changed

    def remember_router_ad_hoc_inviter(self, room_id: str, inviter_id: str) -> bool:
        """Persist or refresh the latest actor for one pending ad-hoc room reconciliation."""
        if self.router_ad_hoc_inviter_ids.get(room_id) == inviter_id:
            return False
        self.router_ad_hoc_inviter_ids[room_id] = inviter_id
        return True

    def get_router_ad_hoc_inviter(self, room_id: str) -> str | None:
        """Return the persisted inviter for one pending ad-hoc room reconciliation."""
        return self.router_ad_hoc_inviter_ids.get(room_id)

    def clear_router_ad_hoc_inviter(self, room_id: str) -> bool:
        """Forget the persisted inviter for one ad-hoc room reconciliation."""
        if room_id not in self.router_ad_hoc_inviter_ids:
            return False
        del self.router_ad_hoc_inviter_ids[room_id]
        return True
