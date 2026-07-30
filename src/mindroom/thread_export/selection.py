"""Matrix room and account selection for thread exports."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import MissingManagedEntityAccountError
from mindroom.matrix.client_visible_messages import trusted_visible_sender_ids
from mindroom.matrix.identity import MatrixID, managed_account_key
from mindroom.matrix.invited_rooms_store import invited_room_entity_names, invited_rooms_path, load_invited_rooms
from mindroom.matrix.state import MatrixRoom, matrix_state_for_runtime
from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY, INTERNAL_USER_AGENT_NAME, AgentMatrixUser
from mindroom.matrix_identifiers import extract_server_name_from_homeserver
from mindroom.thread_export.models import (
    ThreadExportGroup,
    ThreadExportGroupFailure,
    ThreadExportGroupResult,
    ThreadExportRoom,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.state import MatrixAccount


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


def _account_user_from_state(
    *,
    account_key: str,
    account: MatrixAccount,
    homeserver: str,
    runtime_paths: RuntimePaths,
) -> AgentMatrixUser:
    """Build one login-ready Matrix user from persisted state credentials."""
    domain = account.domain or extract_server_name_from_homeserver(homeserver, runtime_paths=runtime_paths)
    entity_name = (
        INTERNAL_USER_AGENT_NAME if account_key == INTERNAL_USER_ACCOUNT_KEY else account_key.removeprefix("agent_")
    )
    return AgentMatrixUser(
        agent_name=entity_name,
        user_id=MatrixID.from_username(account.username, domain).full_id,
        display_name=entity_name,
        password=account.password,
        device_id=account.device_id,
        access_token=account.access_token,
    )


def select_export_account(runtime_paths: RuntimePaths, homeserver: str) -> AgentMatrixUser:
    """Select a persisted Matrix account for export reads."""
    state = matrix_state_for_runtime(runtime_paths)
    preferred_keys = [INTERNAL_USER_ACCOUNT_KEY, managed_account_key(ROUTER_AGENT_NAME)]
    candidate_keys = [*preferred_keys, *state.accounts]
    seen_keys: set[str] = set()

    for account_key in candidate_keys:
        if account_key in seen_keys:
            continue
        seen_keys.add(account_key)
        account = state.accounts.get(account_key)
        if account is None:
            continue
        return _account_user_from_state(
            account_key=account_key,
            account=account,
            homeserver=homeserver,
            runtime_paths=runtime_paths,
        )

    msg = "No persisted Matrix account found in matrix_state.yaml. Run MindRoom once before exporting threads."
    raise RuntimeError(msg)


def build_export_groups(
    *,
    runtime_paths: RuntimePaths,
    homeserver: str,
    state_rooms: Sequence[ThreadExportRoom],
    invited_groups: Sequence[tuple[str, list[ThreadExportRoom]]],
) -> list[ThreadExportGroupResult]:
    """Build account-specific export groups, retaining missing-account failures."""
    groups: list[ThreadExportGroupResult] = []
    if state_rooms:
        groups.append(
            ThreadExportGroup(
                user=select_export_account(runtime_paths, homeserver),
                rooms=tuple(state_rooms),
            ),
        )
    accounts = matrix_state_for_runtime(runtime_paths).accounts
    for entity_name, entity_rooms in invited_groups:
        account_key = managed_account_key(entity_name)
        account = accounts.get(account_key)
        if account is None:
            groups.append(
                ThreadExportGroupFailure(
                    rooms=tuple(entity_rooms),
                    error=f"No persisted Matrix account for invited-room entity '{entity_name}'",
                ),
            )
            continue
        groups.append(
            ThreadExportGroup(
                user=_account_user_from_state(
                    account_key=account_key,
                    account=account,
                    homeserver=homeserver,
                    runtime_paths=runtime_paths,
                ),
                rooms=tuple(entity_rooms),
            ),
        )
    return groups
