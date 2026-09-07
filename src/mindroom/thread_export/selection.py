"""Matrix room and account selection for thread exports."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import MissingManagedEntityAccountError
from mindroom.matrix.client_visible_messages import trusted_visible_sender_ids
from mindroom.matrix.invited_rooms_store import invited_room_entity_names, invited_rooms_path, load_invited_rooms
from mindroom.matrix.state import MatrixRoom, matrix_state_for_runtime
from mindroom.thread_export.models import (
    ThreadExportGroup,
    ThreadExportRoom,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


def export_rooms(runtime_paths: RuntimePaths, room_filter: str | None) -> list[ThreadExportRoom]:
    """Return persisted Matrix rooms selected for export."""
    rooms = matrix_state_for_runtime(runtime_paths).rooms
    selected_rooms: list[ThreadExportRoom] = []
    normalized_filter = room_filter.strip() if isinstance(room_filter, str) and room_filter.strip() else None
    for room_key, room in rooms.items():
        if normalized_filter is not None and not _room_matches_filter(room_key, room, normalized_filter):
            continue
        selected_rooms.append(
            ThreadExportRoom(
                key=room_key,
                room_id=room.room_id,
                alias=room.alias,
                name=room.name,
            ),
        )
    return selected_rooms


def _room_matches_filter(room_key: str, room: MatrixRoom, room_filter: str) -> bool:
    """Return whether one persisted room matches a CLI filter."""
    normalized_filter = room_filter.casefold()
    return any(
        normalized_filter in candidate.casefold()
        for candidate in (room_key, room.room_id, room.alias, room.name)
        if candidate
    )


def invited_export_rooms(
    config: Config,
    runtime_paths: RuntimePaths,
    room_filter: str | None,
    *,
    known_room_ids: set[str],
) -> list[tuple[str, list[ThreadExportRoom]]]:
    """Return invited rooms grouped by the entity whose account is a member."""
    normalized_filter = room_filter.strip().casefold() if isinstance(room_filter, str) and room_filter.strip() else None
    grouped: list[tuple[str, list[ThreadExportRoom]]] = []
    for entity_name in invited_room_entity_names(config):
        entity_rooms: list[ThreadExportRoom] = []
        for room_id in sorted(load_invited_rooms(invited_rooms_path(runtime_paths.storage_root, entity_name))):
            if room_id in known_room_ids:
                continue
            if normalized_filter is not None and normalized_filter not in room_id.casefold():
                continue
            known_room_ids.add(room_id)
            entity_rooms.append(
                ThreadExportRoom(
                    key=room_id,
                    room_id=room_id,
                    alias="",
                    name="",
                    invited=True,
                ),
            )
        if entity_rooms:
            grouped.append((entity_name, entity_rooms))
    return grouped


def trusted_sender_ids_for_export(config: Config, runtime_paths: RuntimePaths) -> frozenset[str]:
    """Return trusted senders when Matrix accounts have already been prepared."""
    try:
        return trusted_visible_sender_ids(config, runtime_paths)
    except MissingManagedEntityAccountError:
        return frozenset()


def build_export_groups(
    *,
    state_rooms: Sequence[ThreadExportRoom],
    invited_groups: Sequence[tuple[str, list[ThreadExportRoom]]],
) -> list[ThreadExportGroup]:
    """Use the router for configured rooms and each invited room's own entity."""
    groups = [ThreadExportGroup(entity_name=ROUTER_AGENT_NAME, rooms=tuple(state_rooms))] if state_rooms else []
    groups.extend(ThreadExportGroup(entity_name=name, rooms=tuple(rooms)) for name, rooms in invited_groups)
    return groups
