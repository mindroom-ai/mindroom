"""Narrow durable ownership and welcome receipts for personal rooms."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

import nio
from pydantic import BaseModel, ConfigDict, model_validator

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.durable_write import write_json_file_durable
from mindroom.tool_system.worker_routing import agent_state_root_path

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


class PersonalRoomAdoption(BaseModel):
    """Operator attestation for one existing room; never inferred from an alias."""

    model_config = ConfigDict(extra="forbid")

    creator_user_id: str
    agent_user_id: str
    router_user_id: str | None = None


class PersonalRoomRecord(BaseModel):
    """One user-owned room and its immutable welcome transaction."""

    model_config = ConfigDict(extra="forbid")

    user_id: str
    alias: str
    source_room_id: str
    room_id: str | None = None
    welcome_content: dict[str, Any] | None = None
    welcome_event_id: str | None = None
    welcome_completed: bool = False
    welcome_device_id: str | None = None
    confirmation_content: dict[str, Any] | None = None
    confirmation_device_id: str | None = None
    confirmation_event_id: str | None = None
    avatar_done: bool = False
    adoption: PersonalRoomAdoption | None = None

    @model_validator(mode="after")
    def require_adoption_room_id(self) -> PersonalRoomRecord:
        """Operator trust applies only to an explicit immutable room ID."""
        if self.adoption is not None and not self.room_id:
            msg = "Personal-room adoption requires an exact room_id"
            raise ValueError(msg)
        return self


def personal_room_digest(user_id: str) -> str:
    """Hash the full Matrix ID, including its homeserver."""
    return hashlib.sha256(user_id.encode()).hexdigest()


def personal_room_record_path(runtime_paths: RuntimePaths, agent_name: str, user_id: str) -> Path:
    """Keep lifecycle records outside requester-private agent workspaces."""
    return (
        agent_state_root_path(runtime_paths.storage_root, agent_name)
        / "personal_rooms"
        / f"{personal_room_digest(user_id)}.json"
    )


def read_personal_room(path: Path) -> PersonalRoomRecord | None:
    """Reject corrupt or misbound records instead of silently losing retention."""
    if not path.exists():
        return None
    record = PersonalRoomRecord.model_validate_json(path.read_bytes())
    if path.stem != personal_room_digest(record.user_id):
        msg = "Personal-room record has mismatched requester identity"
        raise ValueError(msg)
    return record


def write_personal_room(path: Path, record: PersonalRoomRecord) -> None:
    """Publish one lifecycle update with the existing durable-write primitive."""
    write_json_file_durable(path, record.model_dump(mode="json"), strict_atomic_replace=True)


def personal_room_records(runtime_paths: RuntimePaths, agent_name: str) -> list[PersonalRoomRecord]:
    """Read only one agent's room lifecycle records."""
    directory = personal_room_record_path(runtime_paths, agent_name, "").parent
    return [record for path in directory.glob("*.json") if (record := read_personal_room(path)) is not None]


def retained_personal_rooms(runtime_paths: RuntimePaths, agent_name: str, *, user_id: str | None = None) -> set[str]:
    """Protect existing rooms even when onboarding has subsequently been disabled."""
    records = personal_room_records(runtime_paths, agent_name)
    if agent_name == ROUTER_AGENT_NAME and user_id is not None:
        directory = agent_state_root_path(runtime_paths.storage_root, agent_name).parent
        records.extend(
            record
            for path in directory.glob("*/personal_rooms/*.json")
            if (record := read_personal_room(path)) is not None
            and record.adoption is not None
            and record.adoption.router_user_id == user_id
        )
    return {record.room_id for record in records if record.room_id is not None}


async def personal_room_cleanup_exclusions(
    client: nio.AsyncClient,
    runtime_paths: RuntimePaths,
    agent_name: str,
) -> set[str]:
    """Conservatively preserve interrupted creates without adopting or joining aliases."""
    rooms = retained_personal_rooms(runtime_paths, agent_name, user_id=client.user_id)
    for record in personal_room_records(runtime_paths, agent_name):
        if record.room_id is not None:
            rooms.add(record.room_id)
            continue
        response = await client.room_resolve_alias(record.alias)
        if isinstance(response, nio.RoomResolveAliasResponse):
            rooms.add(response.room_id)
        elif not isinstance(response, nio.RoomResolveAliasError) or response.status_code != "M_NOT_FOUND":
            msg = "Cannot safely clean up rooms while a personal-room create is unresolved"
            raise RuntimeError(msg)
    return rooms
