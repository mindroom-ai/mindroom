"""Matrix room and account selection for thread exports."""

from __future__ import annotations

import stat
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import MissingManagedEntityAccountError, configured_routable_entity_names_for_room
from mindroom.matrix.client_visible_messages import trusted_visible_sender_ids
from mindroom.matrix.identity import MatrixID, managed_account_key
from mindroom.matrix.invited_rooms_store import invited_room_entity_names, invited_rooms_path, load_invited_room_claims
from mindroom.matrix.state import MatrixRoom, matrix_state_for_runtime
from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY, INTERNAL_USER_AGENT_NAME, AgentMatrixUser
from mindroom.matrix_identifiers import extract_server_name_from_homeserver
from mindroom.thread_export.models import (
    InvitedRoomConflict,
    InvitedRoomSelection,
    ThreadExportGroup,
    ThreadExportGroupFailure,
    ThreadExportGroupResult,
    ThreadExportRoom,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.state import MatrixAccount


@dataclass(frozen=True)
class _PersistedInvitedRoomClaim:
    """One current or retired entity-state claim over invited rooms."""

    entity_name: str | None
    label: str
    room_ids: frozenset[str]


def export_rooms(
    config: Config,
    runtime_paths: RuntimePaths,
    room_filter: str | None,
) -> list[ThreadExportRoom]:
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
                source_entity_names=_configured_room_source_entity_names(
                    config,
                    runtime_paths,
                    room,
                ),
            ),
        )
    return selected_rooms


def _configured_room_source_entity_names(
    config: Config,
    runtime_paths: RuntimePaths,
    room: MatrixRoom,
) -> tuple[str, ...]:
    """Return entities whose authored configuration owns one managed room."""
    configured_names = configured_routable_entity_names_for_room(
        config,
        room.room_id,
        runtime_paths,
        room_aliases=(room.alias,),
    )
    if configured_names:
        return tuple(sorted(configured_names))
    return (ROUTER_AGENT_NAME,)


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
    state_rooms: Sequence[ThreadExportRoom],
) -> InvitedRoomSelection:
    """Add current-reader variants not already represented by persisted state."""
    normalized_filter = room_filter.strip().casefold() if isinstance(room_filter, str) and room_filter.strip() else None
    entity_names = invited_room_entity_names(config)
    state_rooms_by_id = {room.room_id: room for room in state_rooms}
    claimants_by_room: dict[str, list[_PersistedInvitedRoomClaim]] = {}
    for claim in _persisted_invited_room_claims(config, runtime_paths):
        for room_id in claim.room_ids:
            if normalized_filter is not None and normalized_filter not in room_id.casefold():
                continue
            claimants_by_room.setdefault(room_id, []).append(claim)

    rooms_by_entity: dict[str, list[ThreadExportRoom]] = {}
    conflicts: list[InvitedRoomConflict] = []
    for room_id, claims in sorted(claimants_by_room.items()):
        state_room = state_rooms_by_id.get(room_id)
        configured_names = (
            state_room.source_entity_names
            if state_room is not None
            else tuple(
                sorted(
                    configured_routable_entity_names_for_room(
                        config,
                        room_id,
                        runtime_paths,
                    ),
                ),
            )
        )
        current_claimants = tuple(sorted(claim.entity_name for claim in claims if claim.entity_name is not None))
        room = ThreadExportRoom(
            key=room_id,
            room_id=room_id,
            alias="",
            name="",
            invited=True,
            source_entity_names=(),
        )
        if state_room is None and configured_names:
            rooms_by_entity.setdefault(configured_names[0], []).append(
                replace(room, source_entity_names=configured_names),
            )
        for entity_name in current_claimants:
            if entity_name not in configured_names:
                rooms_by_entity.setdefault(entity_name, []).append(
                    replace(state_room or room, invited=True, source_entity_names=(entity_name,)),
                )
        if state_room is not None or configured_names or current_claimants:
            continue
        conflicts.append(
            InvitedRoomConflict(
                room=room,
                claimant_labels=tuple(sorted(claim.label for claim in claims)),
            ),
        )

    groups = tuple(
        (entity_name, tuple(entity_rooms))
        for entity_name in entity_names
        if (entity_rooms := rooms_by_entity.get(entity_name))
    )
    return InvitedRoomSelection(
        groups=groups,
        conflicts=tuple(conflicts),
    )


def _persisted_invited_room_claims(
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[_PersistedInvitedRoomClaim, ...]:
    """Load current claims and safe legacy claims left by retired entities."""
    configured_paths = {
        invited_rooms_path(runtime_paths.storage_root, entity_name): entity_name
        for entity_name in invited_room_entity_names(config)
    }
    agents_root = runtime_paths.storage_root / "agents"
    retired_paths = _retired_invited_room_claim_paths(agents_root, frozenset(configured_paths))

    claims = [
        claim
        for path, entity_name in configured_paths.items()
        if (claim := _load_invited_room_claim(path, entity_name=entity_name, label=entity_name)) is not None
    ]
    claims.extend(
        claim
        for path in retired_paths
        if (
            claim := _load_invited_room_claim(
                path,
                entity_name=None,
                label=f"retired state directory {path.parent.name!r}",
            )
        )
        is not None
    )
    return tuple(claims)


def _retired_invited_room_claim_paths(agents_root: Path, configured_paths: frozenset[Path]) -> tuple[Path, ...]:
    """Discover safe claim files belonging to removed or renamed entities."""
    if agents_root.is_symlink():
        msg = f"Unsafe invited-room state root: {agents_root}"
        raise RuntimeError(msg)
    try:
        state_roots = sorted(agents_root.iterdir(), key=lambda path: path.name)
    except FileNotFoundError:
        return ()
    except OSError as exc:
        msg = f"Unsafe invited-room state root: {agents_root}"
        raise RuntimeError(msg) from exc

    retired_paths: list[Path] = []
    for state_root in state_roots:
        path = state_root / "invited_rooms.json"
        if state_root.is_symlink():
            msg = f"Unsafe invited-room claim state root: {state_root}"
            raise RuntimeError(msg)
        if not state_root.is_dir():
            continue
        if _regular_claim_file_exists(path) and path not in configured_paths:
            retired_paths.append(path)
    return tuple(retired_paths)


def _regular_claim_file_exists(path: Path) -> bool:
    """Return whether a claim exists, rejecting unreadable or non-regular paths."""
    try:
        claim_mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    except OSError as exc:
        msg = f"Unsafe invited-room claim file: {path}"
        raise RuntimeError(msg) from exc
    if not stat.S_ISREG(claim_mode):
        msg = f"Unsafe invited-room claim file: {path}"
        raise RuntimeError(msg)
    return True


def _load_invited_room_claim(
    path: Path,
    *,
    entity_name: str | None,
    label: str,
) -> _PersistedInvitedRoomClaim | None:
    """Load one non-empty persisted ownership claim."""
    room_ids = load_invited_room_claims(path)
    if not room_ids:
        return None
    return _PersistedInvitedRoomClaim(
        entity_name=entity_name,
        label=label,
        room_ids=frozenset(room_ids),
    )


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
    """Select a persisted Matrix account for export reads.

    The router first, and the internal user last. Export reads thread bodies
    from the journal projection of whichever principal it logs in as, and only
    an account something is actually syncing has one: the internal user runs no
    bot, so choosing it would mean hydrating every thread from the homeserver on
    every pass. The router is the one managed entity that joins every configured
    room, which is what the rooms in ``matrix_state.yaml`` are.
    """
    state = matrix_state_for_runtime(runtime_paths)
    candidate_keys = [
        managed_account_key(ROUTER_AGENT_NAME),
        *(account_key for account_key in state.accounts if account_key != INTERNAL_USER_ACCOUNT_KEY),
        INTERNAL_USER_ACCOUNT_KEY,
    ]
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
