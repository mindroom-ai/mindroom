"""Pydantic models for Matrix state."""

from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_serializer

from mindroom import constants


class _MatrixAccount(BaseModel):
    """Represents a Matrix account (user or agent)."""

    username: str
    password: str
    domain: str | None = None
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

    @classmethod
    def load(cls, runtime_paths: constants.RuntimePaths) -> "MatrixState":
        """Load state from file."""
        state_file = constants.matrix_state_file(runtime_paths=runtime_paths)
        return _load_matrix_state_file(
            state_file,
            current_domain=_current_runtime_domain(runtime_paths),
        )

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
        domain: str | None = None,
        device_id: str | None = None,
        access_token: str | None = None,
    ) -> None:
        """Add or update an account."""
        existing_account = self.accounts.get(key)
        effective_domain = domain if domain is not None else existing_account.domain if existing_account else None
        self.accounts[key] = _MatrixAccount(
            username=username,
            password=password,
            domain=effective_domain,
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


def managed_account_usernames(runtime_paths: constants.RuntimePaths) -> dict[str, str]:
    """Return current persisted managed Matrix usernames keyed by state account key."""
    state_file = constants.matrix_state_file(runtime_paths=runtime_paths)
    state = _load_matrix_state_file_for_accounts(
        *_matrix_state_cache_key(state_file),
        current_domain=_current_runtime_domain(runtime_paths),
    )
    return {key: account.username for key, account in state.accounts.items() if key.startswith("agent_")}


def _matrix_state_cache_key(state_file: Path) -> tuple[Path, int | None, int | None]:
    """Return one cache key that invalidates when the state file changes."""
    if not state_file.exists():
        return state_file, None, None
    stat = state_file.stat()
    return state_file, stat.st_mtime_ns, stat.st_size


@lru_cache(maxsize=64)
def _load_matrix_state_file_for_accounts(
    state_file: Path,
    mtime_ns: int | None,
    size: int | None,
    *,
    current_domain: str,
) -> MatrixState:
    """Load Matrix state through a file-change-sensitive cache for account reads."""
    del mtime_ns, size
    return _load_matrix_state_file(state_file, current_domain=current_domain)


def _current_runtime_domain(runtime_paths: constants.RuntimePaths) -> str:
    """Return the current Matrix server name for one runtime context."""
    if server_name := constants.runtime_matrix_server_name(runtime_paths):
        return server_name

    homeserver = constants.runtime_matrix_homeserver(runtime_paths)
    server_part = homeserver.split("://", 1)[1] if "://" in homeserver else homeserver
    return server_part.split(":", 1)[0]


def _migrate_accounts_to_current_schema(state: MatrixState, *, current_domain: str) -> bool:
    """Normalize persisted accounts to the current on-disk schema."""
    changed = False
    for account in state.accounts.values():
        if account.domain != current_domain:
            account.domain = current_domain
            changed = True
    return changed


def _load_matrix_state_file(state_file: Path, *, current_domain: str) -> MatrixState:
    """Load one Matrix state file from disk."""
    if not state_file.exists():
        return MatrixState()
    with state_file.open() as f:
        data = yaml.safe_load(f) or {}
    state = MatrixState.model_validate(data)
    migrated = _migrate_accounts_to_current_schema(state, current_domain=current_domain)
    normalized_data = state.model_dump(mode="json")
    if migrated or data != normalized_data:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with state_file.open("w") as f:
            yaml.dump(normalized_data, f, default_flow_style=False, sort_keys=False)
    return state
