"""One-shot migration from retired access fields to the membership schema.

Delete this module after pre-membership configuration files are no longer expected to load.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from errno import EBUSY
from pathlib import Path
from typing import Any, cast

from mindroom import yaml_io
from mindroom.matrix_identifiers import split_concrete_matrix_user_ids

_RETIRED_AUTHORIZATION_FIELDS = frozenset(
    {
        "agent_reply_permissions",
        "default_room_access",
        "global_users",
        "room_permissions",
    },
)


@dataclass(frozen=True, slots=True)
class _AccessMigrationResult:
    """One migrated authored mapping and whether it differs from its input."""

    data: dict[str, Any]
    changed: bool


class AccessMigrationError(ValueError):
    """Raised when a retired access value has no safe membership equivalent."""


def _access_config_needs_migration(data: dict[str, Any]) -> bool:
    """Return whether authored config data contains any retired access field."""
    if "access_model" in data or "matrix_room_access" in data:
        return True
    authorization = data.get("authorization")
    return isinstance(authorization, dict) and bool(_RETIRED_AUTHORIZATION_FIELDS & authorization.keys())


def validate_access_migration_source(
    data: dict[str, Any],
    source_files: frozenset[Path],
    config_path: Path,
) -> None:
    """Reject a legacy access migration composed from more than one source file."""
    if _access_config_needs_migration(data) and source_files != frozenset({config_path.resolve()}):
        msg = "Automatic access migration does not support !include configurations"
        raise AccessMigrationError(msg)


def _access_migration_backup_path(path: Path) -> Path:
    """Return the fixed one-time backup path beside a migrated config file."""
    return path.with_name(f"{path.name}.pre-membership-access")


def _write_temp_file(path: Path, content: bytes, *, file_mode: int) -> Path:
    """Write and flush one sibling temporary file, returning its path."""
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temp_file:
        temp_path = Path(temp_file.name)
        temp_file.write(content)
        temp_file.flush()
        os.fsync(temp_file.fileno())
    temp_path.chmod(file_mode)
    return temp_path


def _create_backup_once(path: Path, content: bytes, *, file_mode: int) -> None:
    """Atomically publish an exact backup without replacing an earlier backup."""
    if path.exists():
        return
    temp_path = _write_temp_file(path, content, file_mode=file_mode)
    try:
        with suppress(FileExistsError):
            os.link(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def persist_access_migration(path: Path, original: bytes, migrated: dict[str, Any]) -> bytes:
    """Back up and atomically replace one validated monolithic config."""
    if path.read_bytes() != original:
        msg = "Configuration changed while access migration was being prepared; retry the load"
        raise AccessMigrationError(msg)
    file_mode = path.stat().st_mode & 0o777
    _create_backup_once(_access_migration_backup_path(path), original, file_mode=file_mode)
    rendered = yaml_io.safe_dump(
        migrated,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    try:
        yaml_io.write_text_atomic(path, rendered)
    except OSError as exc:
        if exc.errno != EBUSY:
            raise
        msg = (
            f"Automatic access migration cannot atomically replace {path} because a single-file bind mount cannot "
            "be replaced atomically. Run 'mindroom config migrate --path <host-config.yaml>' on the host before "
            "starting MindRoom"
        )
        raise AccessMigrationError(msg) from exc
    return rendered.encode("utf-8")


def _stable_union(left: object, right: object) -> list[Any]:
    left_items = left if isinstance(left, list) else []
    right_items = right if isinstance(right, list) else []
    return list(dict.fromkeys([*left_items, *right_items]))


def _validate_concrete_legacy_user_ids(user_ids: object, *, field_name: str) -> None:
    """Require a retired concrete-identity grant to contain only Matrix user IDs."""
    if not isinstance(user_ids, list) or any(not isinstance(user_id, str) for user_id in user_ids):
        msg = (
            f"Automatic access migration cannot convert {field_name}; replace its value with a list of concrete "
            "Matrix user IDs before retrying"
        )
        raise AccessMigrationError(msg)
    validated_user_ids = cast("list[str]", user_ids)
    _concrete_user_ids, invalid_user_ids = split_concrete_matrix_user_ids(validated_user_ids)
    if invalid_user_ids:
        msg = (
            f"Automatic access migration cannot convert {field_name} because it contains values that are not "
            f"concrete Matrix user IDs: {', '.join(invalid_user_ids)}. Replace them before retrying"
        )
        raise AccessMigrationError(msg)


def _normalized_reply_policy(raw_policy: object) -> dict[str, Any] | None:
    if isinstance(raw_policy, list):
        return {"users": raw_policy}
    return cast("dict[str, Any]", raw_policy) if isinstance(raw_policy, dict) else None


def _apply_reply_policy(
    entity_data: dict[str, Any],
    raw_policy: object,
    *,
    migrate_credential_managers: bool,
) -> None:
    policy = _normalized_reply_policy(raw_policy)
    if policy is None:
        return
    users = list(policy.get("users", []))
    existing_access = entity_data.get("access")
    authored_access = existing_access if isinstance(existing_access, dict) else {}
    entity_data["access"] = {
        "current_room_members": authored_access.get("current_room_members", False),
        "members_of_rooms": _stable_union(
            authored_access.get("members_of_rooms"),
            policy.get("joined_rooms"),
        ),
        "users": _stable_union(authored_access.get("users"), users),
    }
    if migrate_credential_managers:
        concrete_users, _skipped_users = split_concrete_matrix_user_ids(users)
        entity_data["credential_managers"] = _stable_union(
            entity_data.get("credential_managers"),
            concrete_users,
        )


def _managed_room_keys(config: dict[str, Any]) -> set[str]:
    room_keys = {
        room_key
        for room_key in config.get("rooms", {})
        if isinstance(room_key, str) and not room_key.startswith(("!", "#"))
    }
    for section_name in ("agents", "teams"):
        entities = config.get(section_name)
        if not isinstance(entities, dict):
            continue
        for entity_data in entities.values():
            if not isinstance(entity_data, dict):
                continue
            room_keys.update(
                room_key
                for room_key in entity_data.get("rooms", [])
                if isinstance(room_key, str) and not room_key.startswith(("!", "#"))
            )
    return room_keys


def _resolve_managed_room_key(
    room_reference: object,
    managed_room_keys: set[str],
    *,
    field_name: str,
) -> str:
    """Resolve one retired room reference when it identifies exactly one managed key."""
    if isinstance(room_reference, str):
        if room_reference in managed_room_keys:
            return room_reference
        if room_reference.startswith("#") and ":" in room_reference:
            alias_localpart = room_reference[1:].split(":", 1)[0]
            if alias_localpart in managed_room_keys:
                return alias_localpart
    msg = (
        f"Automatic access migration cannot resolve {field_name} room reference {room_reference!r} to a configured "
        "managed room key. Replace the reference with its managed room key before retrying"
    )
    raise AccessMigrationError(msg)


def _apply_default_room_access(entity_data: dict[str, Any]) -> None:
    if "access" not in entity_data:
        entity_data["access"] = {
            "current_room_members": True,
            "members_of_rooms": [],
            "users": [],
        }


def _migrate_global_users(migrated: dict[str, Any], global_users: list[Any]) -> None:
    if not global_users:
        return
    _validate_concrete_legacy_user_ids(global_users, field_name="authorization.global_users")
    migrated["administrators"] = _stable_union(migrated.get("administrators"), global_users)
    room_defaults = migrated.setdefault("room_defaults", {})
    if isinstance(room_defaults, dict):
        room_defaults["invite_users"] = _stable_union(room_defaults.get("invite_users"), global_users)


def _validate_reply_policy_entities(migrated: dict[str, Any], policies: dict[str, Any]) -> None:
    """Require every retired responder policy to target a configured entity."""
    known_entities = {"*", "router"}
    for section_name in ("agents", "teams"):
        entities = migrated.get(section_name)
        if isinstance(entities, dict):
            known_entities.update(entities)
    unknown_entities = sorted(str(entity_name) for entity_name in policies if entity_name not in known_entities)
    if unknown_entities:
        msg = f"authorization.agent_reply_permissions contains unknown entities: {', '.join(unknown_entities)}"
        raise AccessMigrationError(msg)


def _migrate_reply_permissions(
    migrated: dict[str, Any],
    reply_permissions: object,
    *,
    default_room_access: object,
) -> None:
    if isinstance(reply_permissions, dict):
        policies = cast("dict[str, Any]", reply_permissions)
        _validate_reply_policy_entities(migrated, policies)
        for section_name in ("agents", "teams"):
            entities = migrated.get(section_name)
            if not isinstance(entities, dict):
                continue
            for entity_name, entity_data in entities.items():
                if not isinstance(entity_data, dict):
                    continue
                raw_policy = policies.get(entity_name, policies.get("*"))
                _apply_reply_policy(
                    entity_data,
                    raw_policy,
                    migrate_credential_managers=section_name == "agents",
                )
                if default_room_access and raw_policy is None:
                    _apply_default_room_access(entity_data)
        router = migrated.get("router")
        if isinstance(router, dict):
            raw_policy = policies.get("router", policies.get("*"))
            _apply_reply_policy(
                router,
                raw_policy,
                migrate_credential_managers=False,
            )
            if default_room_access and raw_policy is None:
                _apply_default_room_access(router)


def _migrate_room_permissions(
    migrated: dict[str, Any],
    room_permissions: object,
    managed_room_keys: set[str],
) -> None:
    rooms = migrated.get("rooms")
    if room_permissions and not isinstance(rooms, dict):
        rooms = migrated.setdefault("rooms", {})
    if isinstance(rooms, dict) and isinstance(room_permissions, dict):
        for room_reference, invite_users in room_permissions.items():
            field_name = f"authorization.room_permissions.{room_reference}"
            _validate_concrete_legacy_user_ids(invite_users, field_name=field_name)
            room_key = _resolve_managed_room_key(
                room_reference,
                managed_room_keys,
                field_name="authorization.room_permissions",
            )
            room = rooms.setdefault(room_key, {})
            if isinstance(room, dict):
                room["invite_users"] = _stable_union(room.get("invite_users"), invite_users)


def _migrate_matrix_room_access(
    migrated: dict[str, Any],
    matrix_access: object,
    managed_room_keys: set[str],
) -> None:
    if not isinstance(matrix_access, dict):
        return
    access = cast("dict[str, Any]", matrix_access)
    room_admins = access.get("room_admins", [])
    _validate_concrete_legacy_user_ids(room_admins, field_name="matrix_room_access.room_admins")
    invite_only_rooms = [
        _resolve_managed_room_key(
            room_reference,
            managed_room_keys,
            field_name="matrix_room_access.invite_only_rooms",
        )
        for room_reference in access.get("invite_only_rooms", [])
    ]
    room_defaults = migrated.setdefault("room_defaults", {})
    if isinstance(room_defaults, dict):
        if access.get("mode", "single_user_private") == "multi_user":
            room_defaults.setdefault("join_policy", access.get("multi_user_join_rule", "public"))
            room_defaults.setdefault("listed", access.get("publish_to_room_directory", False))
        else:
            room_defaults.setdefault("join_policy", "invite")
            room_defaults.setdefault("listed", False)
        room_defaults.setdefault("encrypted", access.get("encrypt_managed_rooms", False))
        room_defaults["admins"] = _stable_union(
            room_defaults.get("admins"),
            room_admins,
        )

    rooms = migrated.get("rooms")
    if invite_only_rooms and not isinstance(rooms, dict):
        rooms = migrated.setdefault("rooms", {})
    if isinstance(rooms, dict):
        for room_key in invite_only_rooms:
            room = rooms.setdefault(room_key, {})
            if isinstance(room, dict):
                room.setdefault("join_policy", "invite")
                room.setdefault("listed", False)


def migrate_access_config_data(data: dict[str, Any]) -> _AccessMigrationResult:
    """Split retired access fields into explicit membership capabilities."""
    if not _access_config_needs_migration(data):
        return _AccessMigrationResult(data=data, changed=False)

    migrated = deepcopy(data)
    migrated.pop("access_model", None)
    matrix_access = migrated.pop("matrix_room_access", None)
    authorization = migrated.get("authorization")
    if isinstance(authorization, dict):
        global_users = list(authorization.pop("global_users", []))
        reply_permissions = authorization.pop("agent_reply_permissions", {})
        room_permissions = authorization.pop("room_permissions", {})
        default_room_access = authorization.pop("default_room_access", False)
        if not authorization:
            migrated.pop("authorization")
    else:
        global_users = []
        reply_permissions = {}
        room_permissions = {}
        default_room_access = False

    _migrate_global_users(migrated, global_users)
    _migrate_reply_permissions(
        migrated,
        reply_permissions,
        default_room_access=default_room_access,
    )
    managed_room_keys = _managed_room_keys(migrated)
    _migrate_room_permissions(migrated, room_permissions, managed_room_keys)
    _migrate_matrix_room_access(migrated, matrix_access, managed_room_keys)
    return _AccessMigrationResult(data=migrated, changed=migrated != data)
