"""Fail-closed authorization gate for reading cross-room thread fork prefixes."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Literal

import nio

from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.contacts.store import is_contact_room
from mindroom.entity_resolution import entity_identity_registry
from mindroom.matrix.client_delivery import (
    MatrixDeliveryFailure,
    MatrixDeliveryFailureKind,
    cached_room,
    resolve_room_encryption_outcome,
)
from mindroom.matrix.client_room_admin import get_room_members
from mindroom.matrix.history_visibility import read_room_history
from mindroom.matrix.room_membership import (
    cached_joined_member_ids,
    cached_member_ids,
    ensure_room_membership_synced,
    room_membership_is_complete,
)
from mindroom.matrix.state_failures import StateFailure, state_failure
from mindroom.matrix.thread_forks import (
    CrossRoomForkAuthorization,
    CrossRoomForkDecision,
    CrossRoomForkHop,
)
from mindroom.requester_identity import equivalent_requester_ids, is_human_requester_id, is_managed_entity_id

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

_SAFE_OUTPUT_HISTORY_VISIBILITIES = frozenset({"shared", "invited", "joined"})
_FULL_PARENT_HISTORY_VISIBILITIES = frozenset({"shared", "world_readable"})


def _fork_root_author(
    fork_root: ResolvedVisibleMessage,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Return the authenticated human author of a cross-room fork root, or None."""
    registry = entity_identity_registry(config, runtime_paths)
    author = fork_root.sender
    if registry.current_entity_name_for_user_id(author) is not None:
        relayed = fork_root.content.get(ORIGINAL_SENDER_KEY)
        if not isinstance(relayed, str) or not relayed:
            return None
        author = relayed
    return author if is_human_requester_id(author, config, runtime_paths) else None


def _involves_contact_room(
    runtime_paths: RuntimePaths,
    hop: CrossRoomForkHop,
    *,
    output_room_id: str,
) -> bool:
    try:
        return any(
            is_contact_room(runtime_paths, room_id)
            for room_id in {hop.target_room_id, hop.child_room_id, hop.parent_room_id, output_room_id}
        )
    except (OSError, ValueError):
        return True


def _target_invites_complete(config: Config, room: nio.MatrixRoom) -> bool:
    """Return whether ``room.invited_users`` is known to hold every current invite."""
    if room.members_synced or config.matrix_sync.mode == "classic":
        return True
    summary = room.summary
    return (
        summary is not None
        and summary.invited_member_count is not None
        and summary.invited_member_count <= len(room.invited_users)
    )


def _history_visibility_known_in_cache(config: Config, room: nio.MatrixRoom | None) -> bool:
    """Return whether ``room.history_visibility`` was delivered authoritatively by sync."""
    # Simplified sliding sync does not request m.room.history_visibility, so nio leaves it at its "shared" default.
    return room is not None and config.matrix_sync.mode == "classic"


def _is_audience_member(
    user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Return whether ``user_id`` counts in the room audience (humans and non-managed bots)."""
    return not is_managed_entity_id(user_id, config, runtime_paths)


def _audience_joined_to_parent(
    user_ids: Iterable[str],
    parent_joined: frozenset[str],
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Return whether every human or non-managed bot in ``user_ids`` is currently joined to ``parent_joined``."""
    return all(
        not parent_joined.isdisjoint(equivalent_requester_ids(user_id, config, runtime_paths))
        for user_id in user_ids
        if _is_audience_member(user_id, config, runtime_paths)
    )


async def _room_encrypted_strict(client: nio.AsyncClient, room_id: str) -> bool | None:
    """Resolve room encryption strictly, returning None when encryption state is unknown or unreachable."""
    try:
        outcome = await resolve_room_encryption_outcome(client, room_id, operation="cross_room_thread_fork")
    except Exception:
        return None
    if isinstance(outcome, bool):
        return outcome
    if isinstance(outcome, MatrixDeliveryFailure) and outcome.kind is MatrixDeliveryFailureKind.ENCRYPTION_GUARD:
        return True
    return None


async def _read_room_history_visibility_strict(
    client: nio.AsyncClient,
    room_id: str,
) -> str | None:
    """Authoritatively read ``m.room.history_visibility`` for ``room_id``, or None on error."""
    try:
        response = await client.room_get_state_event(room_id, "m.room.history_visibility")
    except Exception:
        return None
    if isinstance(response, nio.RoomGetStateEventResponse):
        visibility = response.content.get("history_visibility") if isinstance(response.content, dict) else None
        return visibility if isinstance(visibility, str) else None
    if isinstance(response, nio.RoomGetStateEventError) and state_failure(response) is StateFailure.MISSING:
        return "shared"
    return None


async def _resolve_parent_joined(
    client: nio.AsyncClient,
    parent_room_id: str,
    parent_room: nio.MatrixRoom | None,
    *,
    requester_id: str,
    nonblocking: bool,
) -> frozenset[str] | CrossRoomForkDecision:
    """Return current joined members of ``parent_room_id``, or a denial/deferral."""
    if nonblocking:
        if parent_room is None:
            return CrossRoomForkDecision.DENIED
        return (
            cached_joined_member_ids(parent_room)
            if room_membership_is_complete(parent_room)
            else CrossRoomForkDecision.DEFERRED
        )

    if parent_room is not None:
        try:
            synced = await ensure_room_membership_synced(client, parent_room, sender_id=requester_id)
        except Exception:
            return CrossRoomForkDecision.DENIED
        return cached_joined_member_ids(parent_room) if synced else CrossRoomForkDecision.DENIED

    try:
        fetched = await get_room_members(client, parent_room_id)
    except Exception:
        return CrossRoomForkDecision.DENIED
    return CrossRoomForkDecision.DENIED if fetched is None else frozenset(fetched)


async def _resolve_encryption_decision(
    client: nio.AsyncClient,
    *,
    parent_room_id: str,
    parent_room: nio.MatrixRoom | None,
    destination_rooms: Sequence[tuple[str, nio.MatrixRoom | None]],
    nonblocking: bool,
) -> CrossRoomForkDecision:
    """Block encrypted (or unknown) parent -> unencrypted (or unknown) destination rooms."""
    if not destination_rooms:
        return CrossRoomForkDecision.ALLOWED
    parent_encrypted: bool | None
    if nonblocking:
        if parent_room is None:
            return CrossRoomForkDecision.DEFERRED
        parent_encrypted = bool(parent_room.encrypted)
    else:
        parent_encrypted = await _room_encrypted_strict(client, parent_room_id)
        if parent_encrypted is None:
            return CrossRoomForkDecision.DENIED

    deferred = False
    for dest_room_id, dest_room in destination_rooms:
        if nonblocking:
            if dest_room is None:
                deferred = True
                continue
            dest_encrypted: bool | None = bool(dest_room.encrypted)
        else:
            dest_encrypted = await _room_encrypted_strict(client, dest_room_id)
            if dest_encrypted is None:
                return CrossRoomForkDecision.DENIED
        if parent_encrypted and not dest_encrypted:
            return CrossRoomForkDecision.DENIED
    return CrossRoomForkDecision.DEFERRED if deferred else CrossRoomForkDecision.ALLOWED


async def _destination_members_from_room_state(
    client: nio.AsyncClient,
    room_id: str,
) -> frozenset[str] | CrossRoomForkDecision:
    """Fetch complete joined+invited members and history visibility directly from room state."""
    try:
        response = await client.room_get_state(room_id)
    except Exception:
        return CrossRoomForkDecision.DENIED
    if not isinstance(response, nio.RoomGetStateResponse):
        return CrossRoomForkDecision.DENIED
    visibility: object = "shared"
    members: set[str] = set()
    for event in response.events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        content = event.get("content")
        if not isinstance(content, dict):
            continue
        if event_type == "m.room.history_visibility" and event.get("state_key") == "":
            visibility = content.get("history_visibility")
        elif event_type == "m.room.member":
            state_key = event.get("state_key")
            if isinstance(state_key, str) and content.get("membership") in {"join", "invite"}:
                members.add(state_key)
    if not isinstance(visibility, str) or visibility not in _SAFE_OUTPUT_HISTORY_VISIBILITIES:
        return CrossRoomForkDecision.DENIED
    return frozenset(members)


def _resolve_destination_members_nonblocking(
    config: Config,
    room: nio.MatrixRoom | None,
) -> frozenset[str] | CrossRoomForkDecision:
    if room is None or not _history_visibility_known_in_cache(config, room):
        return CrossRoomForkDecision.DEFERRED
    complete = room_membership_is_complete(room) and _target_invites_complete(config, room)
    return cached_member_ids(room) if complete else CrossRoomForkDecision.DEFERRED


async def _resolve_destination_members_strict(
    client: nio.AsyncClient,
    config: Config,
    room_id: str,
    room: nio.MatrixRoom | None,
    *,
    requester_id: str,
) -> frozenset[str] | CrossRoomForkDecision:
    if room is None:
        return await _destination_members_from_room_state(client, room_id)
    try:
        synced = await ensure_room_membership_synced(client, room, sender_id=requester_id)
    except Exception:
        return CrossRoomForkDecision.DENIED
    if not synced:
        return CrossRoomForkDecision.DENIED
    if not _target_invites_complete(config, room):
        return await _destination_members_from_room_state(client, room_id)
    visibility = await _read_room_history_visibility_strict(client, room_id)
    if visibility is None or visibility not in _SAFE_OUTPUT_HISTORY_VISIBILITIES:
        return CrossRoomForkDecision.DENIED
    return cached_member_ids(room)


async def _resolve_destination_members(
    client: nio.AsyncClient,
    config: Config,
    room_id: str,
    room: nio.MatrixRoom | None,
    *,
    requester_id: str,
    nonblocking: bool,
) -> frozenset[str] | CrossRoomForkDecision:
    """Return all joined and invited members of ``room_id`` after verifying authoritative history visibility."""
    if room is not None and room.history_visibility not in _SAFE_OUTPUT_HISTORY_VISIBILITIES:
        return CrossRoomForkDecision.DENIED
    if nonblocking:
        return _resolve_destination_members_nonblocking(config, room)
    return await _resolve_destination_members_strict(client, config, room_id, room, requester_id=requester_id)


async def _allow_parent_segment_for_audience(
    client: nio.AsyncClient,
    segment: Sequence[ResolvedVisibleMessage],
    *,
    parent_room_id: str,
    parent_visibility: str,
    audience_parent_id_groups: tuple[frozenset[str], ...],
) -> CrossRoomForkDecision:
    """Verify every audience member can read ``segment`` under ``parent_room_id``'s history visibility."""
    if not segment:
        return CrossRoomForkDecision.ALLOWED
    earliest_timestamp = min(message.timestamp for message in segment)
    cached_since: dict[str, int | None | Literal[False]] = {}
    for parent_ids in audience_parent_id_groups:
        member_allowed = False
        for member_id in sorted(parent_ids):
            if member_id not in cached_since:
                try:
                    room_history = await read_room_history(client, parent_room_id, member_id)
                except Exception:
                    return CrossRoomForkDecision.DENIED
                cached_since[member_id] = False if room_history is None else room_history.since(parent_visibility)
            since = cached_since[member_id]
            if since is False:
                continue
            if since is None or since <= earliest_timestamp:
                member_allowed = True
                break
        if not member_allowed:
            return CrossRoomForkDecision.DENIED
    return CrossRoomForkDecision.ALLOWED


def _required_parent_humans(
    *,
    hop: CrossRoomForkHop,
    requester_id: str | None,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[str, ...] | None:
    """Return the human principals that must be joined to ``hop.parent_room_id``, or None if invalid."""
    if requester_id is None or not is_human_requester_id(requester_id, config, runtime_paths):
        return None
    if hop.child_room_id == hop.parent_room_id:
        return (requester_id,)
    fork_author = _fork_root_author(hop.fork_root, config, runtime_paths)
    return None if fork_author is None else (requester_id, fork_author)


def _destination_rooms(
    client: nio.AsyncClient,
    *,
    hop: CrossRoomForkHop,
    target_room: nio.MatrixRoom | None,
    output_room_id: str,
    output_room: nio.MatrixRoom | None,
) -> list[tuple[str, nio.MatrixRoom | None]]:
    """Return distinct destination rooms that differ from ``hop.parent_room_id``."""
    resolved_target = (
        target_room
        if target_room is not None and target_room.room_id == hop.target_room_id
        else cached_room(client, hop.target_room_id)
    )
    if output_room_id == hop.target_room_id:
        resolved_output = (
            output_room if output_room is not None and output_room.room_id == output_room_id else resolved_target
        )
        return [] if hop.target_room_id == hop.parent_room_id else [(hop.target_room_id, resolved_output)]
    resolved_output = (
        output_room
        if output_room is not None and output_room.room_id == output_room_id
        else cached_room(client, output_room_id)
    )
    rooms: list[tuple[str, nio.MatrixRoom | None]] = []
    if hop.target_room_id != hop.parent_room_id:
        rooms.append((hop.target_room_id, resolved_target))
    if output_room_id != hop.parent_room_id:
        rooms.append((output_room_id, resolved_output))
    return rooms


async def _collect_authorized_destination_audience(
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    parent_room_id: str,
    parent_room: nio.MatrixRoom | None,
    parent_joined: frozenset[str],
    destination_rooms: Sequence[tuple[str, nio.MatrixRoom | None]],
    requester_id: str,
    nonblocking: bool,
) -> set[str] | CrossRoomForkDecision:
    """Validate encryption, visibility, and audience for every destination room != ``parent_room_id``."""
    encryption_decision = await _resolve_encryption_decision(
        client,
        parent_room_id=parent_room_id,
        parent_room=parent_room,
        destination_rooms=destination_rooms,
        nonblocking=nonblocking,
    )
    if encryption_decision is not CrossRoomForkDecision.ALLOWED:
        return encryption_decision

    audience: set[str] = set()
    for dest_room_id, dest_room in destination_rooms:
        dest_members = await _resolve_destination_members(
            client,
            config,
            dest_room_id,
            dest_room,
            requester_id=requester_id,
            nonblocking=nonblocking,
        )
        if isinstance(dest_members, CrossRoomForkDecision):
            return dest_members
        if not _audience_joined_to_parent(dest_members, parent_joined, config, runtime_paths):
            return CrossRoomForkDecision.DENIED
        audience.update(user_id for user_id in dest_members if _is_audience_member(user_id, config, runtime_paths))
    return audience


async def _authorize_parent_history_visibility(
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    parent_room_id: str,
    parent_room: nio.MatrixRoom | None,
    parent_joined: frozenset[str],
    audience_members: set[str],
    nonblocking: bool,
) -> CrossRoomForkAuthorization:
    """Verify parent room history visibility, attaching a post-cut timestamp check for joined/invited rooms."""
    if nonblocking:
        if not _history_visibility_known_in_cache(config, parent_room):
            return CrossRoomForkAuthorization(CrossRoomForkDecision.DEFERRED)
        assert parent_room is not None
        decision = (
            CrossRoomForkDecision.ALLOWED
            if parent_room.history_visibility in _FULL_PARENT_HISTORY_VISIBILITIES
            else CrossRoomForkDecision.DEFERRED
        )
        return CrossRoomForkAuthorization(decision)

    parent_visibility = await _read_room_history_visibility_strict(client, parent_room_id)
    if parent_visibility is None:
        return CrossRoomForkAuthorization(CrossRoomForkDecision.DENIED)
    if parent_visibility in _FULL_PARENT_HISTORY_VISIBILITIES:
        return CrossRoomForkAuthorization(CrossRoomForkDecision.ALLOWED)

    audience_parent_id_groups = tuple(
        dict.fromkeys(
            frozenset(parent_joined & equivalent_requester_ids(user_id, config, runtime_paths))
            for user_id in audience_members
        ),
    )
    return CrossRoomForkAuthorization(
        CrossRoomForkDecision.ALLOWED,
        allow_segment=partial(
            _allow_parent_segment_for_audience,
            client,
            parent_room_id=parent_room_id,
            parent_visibility=parent_visibility,
            audience_parent_id_groups=audience_parent_id_groups,
        ),
    )


async def authorize_cross_room_fork_access(
    *,
    client: nio.AsyncClient | None,
    config: Config,
    runtime_paths: RuntimePaths,
    hop: CrossRoomForkHop,
    requester_id: str | None,
    nonblocking: bool,
    target_room: nio.MatrixRoom | None = None,
    output_room_id: str | None = None,
    output_room: nio.MatrixRoom | None = None,
) -> CrossRoomForkAuthorization:
    """Authorize reading one cross-room parent segment and return any required post-cut visibility check."""
    effective_output_room_id = hop.target_room_id if output_room_id is None else output_room_id
    required_humans = _required_parent_humans(
        hop=hop,
        requester_id=requester_id,
        config=config,
        runtime_paths=runtime_paths,
    )
    if (
        client is None
        or not client.user_id
        or requester_id is None
        or required_humans is None
        or _involves_contact_room(runtime_paths, hop, output_room_id=effective_output_room_id)
    ):
        return CrossRoomForkAuthorization(CrossRoomForkDecision.DENIED)

    parent_room = cached_room(client, hop.parent_room_id)
    parent_joined = await _resolve_parent_joined(
        client,
        hop.parent_room_id,
        parent_room,
        requester_id=requester_id,
        nonblocking=nonblocking,
    )
    if isinstance(parent_joined, CrossRoomForkDecision):
        return CrossRoomForkAuthorization(parent_joined)
    if client.user_id not in parent_joined or not _audience_joined_to_parent(
        required_humans,
        parent_joined,
        config,
        runtime_paths,
    ):
        return CrossRoomForkAuthorization(CrossRoomForkDecision.DENIED)

    destination_audience = await _collect_authorized_destination_audience(
        client,
        config,
        runtime_paths,
        parent_room_id=hop.parent_room_id,
        parent_room=parent_room,
        parent_joined=parent_joined,
        destination_rooms=_destination_rooms(
            client,
            hop=hop,
            target_room=target_room,
            output_room_id=effective_output_room_id,
            output_room=output_room,
        ),
        requester_id=requester_id,
        nonblocking=nonblocking,
    )
    if isinstance(destination_audience, CrossRoomForkDecision):
        return CrossRoomForkAuthorization(destination_audience)

    return await _authorize_parent_history_visibility(
        client,
        config,
        runtime_paths,
        parent_room_id=hop.parent_room_id,
        parent_room=parent_room,
        parent_joined=parent_joined,
        audience_members=set(required_humans) | destination_audience,
        nonblocking=nonblocking,
    )


async def authorize_cross_room_fork_hop(
    *,
    client: nio.AsyncClient | None,
    config: Config,
    runtime_paths: RuntimePaths,
    hop: CrossRoomForkHop,
    requester_id: str | None,
    nonblocking: bool,
    target_room: nio.MatrixRoom | None = None,
    output_room_id: str | None = None,
    output_room: nio.MatrixRoom | None = None,
) -> CrossRoomForkDecision:
    """Authorize reading one cross-room parent segment into ``output_room_id`` (fail closed)."""
    auth = await authorize_cross_room_fork_access(
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        hop=hop,
        requester_id=requester_id,
        nonblocking=nonblocking,
        target_room=target_room,
        output_room_id=output_room_id,
        output_room=output_room,
    )
    return auth.decision
