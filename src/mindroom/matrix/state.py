"""Matrix account and room state, persisted in ``matrix_state.yaml``."""

import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from time import monotonic

from pydantic import BaseModel, Field, field_serializer

from mindroom import constants, yaml_io
from mindroom.atomic_file import atomic_write_bytes_at
from mindroom.matrix.legacy_state import normalize_legacy_matrix_state
from mindroom.matrix_identifiers import extract_server_name_from_homeserver

_MATRIX_STATE_STAT_TTL_SECONDS = 1.0
_matrix_state_write_generation = 0
# Serializes every write of the file across threads, including the legacy
# rewrite a read can trigger; the event loop gets the same guarantee from no
# writer ever awaiting. Reentrant because an update reads through the loader.
_matrix_state_write_lock = threading.RLock()


class MatrixAccount(BaseModel):
    """Represents a Matrix account (user or agent)."""

    username: str
    password: str | None = None
    requested_username: str | None = None
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

    accounts: dict[str, MatrixAccount] = Field(default_factory=dict)
    rooms: dict[str, MatrixRoom] = Field(default_factory=dict)
    space_room_id: str | None = None
    server_emoji_room_id: str | None = None

    @classmethod
    def load(cls, runtime_paths: constants.RuntimePaths) -> "MatrixState":
        """Return a private copy of the cached state, for a reader that edits it locally.

        Changes to the copy never reach the file; ``update_matrix_state`` is the
        only writer.
        """
        return matrix_state_for_runtime(runtime_paths).model_copy(deep=True)

    def get_account(self, key: str) -> MatrixAccount | None:
        """Get an account by key."""
        return self.accounts.get(key)

    def add_account(
        self,
        key: str,
        username: str,
        password: str | None,
        *,
        requested_username: str | None = None,
        domain: str | None = None,
        device_id: str | None = None,
        access_token: str | None = None,
    ) -> None:
        """Add or update an account."""
        existing_account = self.accounts.get(key)
        effective_domain = domain if domain is not None else existing_account.domain if existing_account else None
        effective_requested_username = (
            requested_username
            if requested_username is not None
            else existing_account.requested_username
            if existing_account
            else None
        )
        self.accounts[key] = MatrixAccount(
            username=username,
            password=password,
            requested_username=effective_requested_username,
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
        """Get mapping of room keys and full aliases to room IDs."""
        aliases: dict[str, str] = {}
        for key, room in self.rooms.items():
            aliases[key] = room.room_id
            if room.alias:
                aliases[room.alias] = room.room_id
        return aliases

    def set_space_room_id(self, room_id: str | None) -> None:
        """Set the root Matrix Space room ID."""
        self.space_room_id = room_id

    def set_server_emoji_room_id(self, room_id: str) -> None:
        """Set the server custom emoji room ID."""
        self.server_emoji_room_id = room_id


def update_matrix_state(runtime_paths: constants.RuntimePaths, mutate: Callable[[MatrixState], object]) -> None:
    """Apply one change to the persisted state and save it.

    The file is read fresh and saved before anything else can run, because
    every writer replaces the whole file: a copy held across an await would
    overwrite whatever another writer saved meanwhile, such as the device and
    token of a bot that logged in while the root space was being created.
    ``mutate`` must not block or wait on anything.
    """
    state_file = constants.matrix_state_file(runtime_paths=runtime_paths)
    with _matrix_state_write_lock:
        state = _load_matrix_state_file(state_file, current_domain=_current_runtime_domain(runtime_paths))
        mutate(state)
        _write_matrix_state_file(state_file, state.model_dump(mode="json"))


def matrix_state_for_runtime(runtime_paths: constants.RuntimePaths) -> MatrixState:
    """Return persisted Matrix state cached by write generation and file signature.

    The returned object is shared across callers and must not be mutated;
    ``update_matrix_state`` changes the file.
    """
    state_file = constants.matrix_state_file(runtime_paths=runtime_paths)
    return _load_matrix_state_file_cached(
        *_matrix_state_cache_key(state_file),
        current_domain=_current_runtime_domain(runtime_paths),
    )


def load_rooms(runtime_paths: constants.RuntimePaths) -> dict[str, MatrixRoom]:
    """Load room state from YAML file.

    Returns an isolated copy of the rooms map so callers may mutate the dict
    or its ``MatrixRoom`` values without corrupting cached state used by other
    readers.
    """
    return MatrixState.load(runtime_paths=runtime_paths).rooms


def _room_aliases(runtime_paths: constants.RuntimePaths) -> dict[str, str]:
    """Return a read-only alias map with the same freshness as cached state."""
    state_file = constants.matrix_state_file(runtime_paths=runtime_paths)
    return _room_aliases_cached(
        *_matrix_state_cache_key(state_file),
        current_domain=_current_runtime_domain(runtime_paths),
    )


@lru_cache(maxsize=64)
def _room_aliases_cached(
    state_file: Path,
    write_generation: int,
    mtime_ns: int | None,
    size: int | None,
    *,
    current_domain: str,
) -> dict[str, str]:
    """Build the alias map once per persisted state snapshot."""
    state = _load_matrix_state_file_cached(
        state_file,
        write_generation,
        mtime_ns,
        size,
        current_domain=current_domain,
    )
    return state.get_room_aliases()


def get_room_id(room_key: str, runtime_paths: constants.RuntimePaths) -> str | None:
    """Get room ID for a given room key/alias."""
    room = matrix_state_for_runtime(runtime_paths).get_room(room_key)
    return room.room_id if room else None


def resolve_room_aliases(
    room_list: list[str],
    runtime_paths: constants.RuntimePaths,
) -> list[str]:
    """Resolve room aliases to room IDs."""
    aliases = _room_aliases(runtime_paths)
    return [aliases.get(room, room) for room in room_list]


def resolve_room_id(room_id_or_alias: str, runtime_paths: constants.RuntimePaths) -> str:
    """Resolve one room alias to its Matrix room ID when known."""
    return resolve_room_aliases([room_id_or_alias], runtime_paths=runtime_paths)[0]


def get_room_alias_from_id(room_id: str, runtime_paths: constants.RuntimePaths) -> str | None:
    """Get room alias from room ID."""
    for alias, resolved_room_id in _room_aliases(runtime_paths).items():
        if resolved_room_id == room_id:
            return alias
    return None


def _matrix_state_cache_key(state_file: Path) -> tuple[Path, int, int | None, int | None]:
    """Return a key with eager local invalidation and bounded external staleness."""
    stat_ttl_bucket = int(monotonic() / _MATRIX_STATE_STAT_TTL_SECONDS)
    mtime_ns, size = _matrix_state_file_signature(state_file, stat_ttl_bucket)
    return state_file, _matrix_state_write_generation, mtime_ns, size


@lru_cache(maxsize=64)
def _matrix_state_file_signature(state_file: Path, stat_ttl_bucket: int) -> tuple[int | None, int | None]:
    """Stat one state file at most once per TTL bucket."""
    del stat_ttl_bucket
    try:
        stat = state_file.stat()
    except OSError:
        return None, None
    return stat.st_mtime_ns, stat.st_size


@lru_cache(maxsize=64)
def _load_matrix_state_file_cached(
    state_file: Path,
    write_generation: int,
    mtime_ns: int | None,
    size: int | None,
    *,
    current_domain: str,
) -> MatrixState:
    """Load Matrix state through a local-write and external-file-sensitive cache."""
    del write_generation, mtime_ns, size
    return _load_matrix_state_file(state_file, current_domain=current_domain)


def _current_runtime_domain(runtime_paths: constants.RuntimePaths) -> str:
    """Return the current Matrix server name for one runtime context."""
    return extract_server_name_from_homeserver(constants.runtime_matrix_homeserver(runtime_paths), runtime_paths)


def _load_matrix_state_file(state_file: Path, *, current_domain: str) -> MatrixState:
    """Load one Matrix state file from disk."""
    with _matrix_state_write_lock:
        if not state_file.exists():
            return MatrixState()
        with state_file.open(encoding="utf-8") as f:
            data = yaml_io.safe_load(f) or {}
        state = MatrixState.model_validate(data)
        normalized_data = normalize_legacy_matrix_state(state, data, current_domain=current_domain)
        if normalized_data is not None:
            _write_matrix_state_file(state_file, normalized_data)
        return state


def _write_matrix_state_file(state_file: Path, data: dict[str, object]) -> None:
    """Atomically replace the state file and invalidate this process's cached reads."""
    global _matrix_state_write_generation

    state_file.parent.mkdir(parents=True, exist_ok=True)
    payload = yaml_io.safe_dump(data, default_flow_style=False, sort_keys=False).encode("utf-8")
    directory_fd = os.open(state_file.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        atomic_write_bytes_at(directory_fd, state_file.name, payload, temp_prefix=f".{state_file.name}.")
    finally:
        os.close(directory_fd)
    _matrix_state_write_generation += 1
