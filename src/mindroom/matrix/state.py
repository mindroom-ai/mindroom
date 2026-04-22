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
    known_user_ids: list[str] = Field(default_factory=list)
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
        return _load_matrix_state_file(state_file)

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
        known_user_ids: list[str] = []
        if existing_account is not None:
            known_user_ids.extend(_known_user_ids_for_account(existing_account))
        if effective_domain is not None:
            current_user_id = _matrix_user_id(username, effective_domain)
            if current_user_id not in known_user_ids:
                known_user_ids.append(current_user_id)
        self.accounts[key] = _MatrixAccount(
            username=username,
            password=password,
            domain=effective_domain,
            known_user_ids=known_user_ids,
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
    state = _load_matrix_state_file_for_accounts(*_matrix_state_cache_key(state_file))
    return {key: account.username for key, account in state.accounts.items() if key.startswith("agent_")}


def managed_account_sender_ids(runtime_paths: constants.RuntimePaths) -> dict[str, str]:
    """Return current persisted managed Matrix sender IDs keyed by state account key."""
    state_file = constants.matrix_state_file(runtime_paths=runtime_paths)
    state = _load_matrix_state_file_for_accounts(*_matrix_state_cache_key(state_file))
    return {
        key: sender_id
        for key, account in state.accounts.items()
        if key.startswith("agent_") and (sender_id := _current_sender_id(account)) is not None
    }


def managed_account_known_sender_ids(runtime_paths: constants.RuntimePaths) -> dict[str, tuple[str, ...]]:
    """Return all known persisted managed Matrix sender IDs keyed by state account key."""
    state_file = constants.matrix_state_file(runtime_paths=runtime_paths)
    state = _load_matrix_state_file_for_accounts(*_matrix_state_cache_key(state_file))
    return {
        key: tuple(_known_user_ids_for_account(account))
        for key, account in state.accounts.items()
        if key.startswith("agent_") and _known_user_ids_for_account(account)
    }


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
) -> MatrixState:
    """Load Matrix state through a file-change-sensitive cache for account reads."""
    del mtime_ns, size
    return _load_matrix_state_file(state_file)


def _matrix_user_id(username: str, domain: str) -> str:
    """Build a Matrix user ID from one username and domain."""
    return f"@{username}:{domain}"


def _current_sender_id(account: _MatrixAccount) -> str | None:
    """Return the current sender ID for one persisted account, if known."""
    if account.domain is None:
        return None
    return _matrix_user_id(account.username, account.domain)


def _known_user_ids_for_account(account: _MatrixAccount) -> list[str]:
    """Return all known sender IDs for one persisted account in stable order."""
    known_user_ids = list(dict.fromkeys(account.known_user_ids))
    current_sender_id = _current_sender_id(account)
    if current_sender_id is not None and current_sender_id not in known_user_ids:
        known_user_ids.append(current_sender_id)
    return known_user_ids


def _load_matrix_state_file(state_file: Path) -> MatrixState:
    """Load one Matrix state file from disk."""
    if not state_file.exists():
        return MatrixState()
    with state_file.open() as f:
        data = yaml.safe_load(f) or {}
    return MatrixState.model_validate(data)
