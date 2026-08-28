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

from pydantic import TypeAdapter, ValidationError

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
_LEGACY_BOOLEAN_ADAPTER = TypeAdapter(bool)
_LEGACY_STRING_LIST_ADAPTER = TypeAdapter(list[str])
_LEGACY_MATRIX_ACCESS_MODES = frozenset({"multi_user", "single_user_private"})
_LEGACY_MULTI_USER_JOIN_RULES = frozenset({"knock", "public"})
_REPLY_POLICY_FIELDS = frozenset({"joined_rooms", "users"})


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


def _legacy_boolean(value: object, *, field_name: str) -> bool:
    """Apply the retired schema's Pydantic boolean coercion before migration."""
    try:
        return _LEGACY_BOOLEAN_ADAPTER.validate_python(value)
    except ValidationError as exc:
        msg = f"Automatic access migration cannot convert {field_name}; fix the retired value before retrying"
        raise AccessMigrationError(msg) from exc


def _legacy_string_list(value: object, *, field_name: str) -> list[str]:
    """Validate one list with the same shape required by the retired schema."""
    try:
        return _LEGACY_STRING_LIST_ADAPTER.validate_python(value)
    except ValidationError as exc:
        msg = f"Automatic access migration cannot convert {field_name}; fix the retired value before retrying"
        raise AccessMigrationError(msg) from exc


def _legacy_mapping(value: object, *, field_name: str) -> dict[Any, Any]:
    if not isinstance(value, dict):
        msg = f"Automatic access migration cannot convert {field_name}; fix the retired value before retrying"
        raise AccessMigrationError(msg)
    return value


def _stable_union(left: object, right: object, *, field_name: str) -> list[str]:
    left_items = [] if left is None else _legacy_string_list(left, field_name=field_name)
    right_items = [] if right is None else _legacy_string_list(right, field_name=field_name)
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


def _normalized_reply_policy(raw_policy: object, *, field_name: str) -> dict[str, Any]:
    if isinstance(raw_policy, list):
        return {
            "users": _legacy_string_list(raw_policy, field_name=f"{field_name}.users"),
            "joined_rooms": [],
        }
    if not isinstance(raw_policy, dict):
        msg = f"Automatic access migration cannot convert {field_name}; fix the retired value before retrying"
        raise AccessMigrationError(msg)
    policy = cast("dict[str, object]", raw_policy)
    unknown_fields = sorted(str(key) for key in policy if key not in _REPLY_POLICY_FIELDS)
    if unknown_fields:
        msg = f"Automatic access migration cannot convert {field_name}.{unknown_fields[0]}; remove the unknown field"
        raise AccessMigrationError(msg)
    return {
        "users": _legacy_string_list(policy.get("users", []), field_name=f"{field_name}.users"),
        "joined_rooms": _legacy_string_list(
            policy.get("joined_rooms", []),
            field_name=f"{field_name}.joined_rooms",
        ),
    }


def _normalized_reply_permissions(reply_permissions: object) -> dict[str, dict[str, Any]]:
    policies = _legacy_mapping(
        reply_permissions,
        field_name="authorization.agent_reply_permissions",
    )
    return {
        str(entity_name): _normalized_reply_policy(
            raw_policy,
            field_name=f"authorization.agent_reply_permissions.{entity_name}",
        )
        for entity_name, raw_policy in policies.items()
    }


def _normalized_matrix_access(matrix_access: object) -> dict[str, Any]:
    access = _legacy_mapping(matrix_access, field_name="matrix_room_access")
    mode = access.get("mode", "single_user_private")
    if mode not in _LEGACY_MATRIX_ACCESS_MODES:
        msg = "Automatic access migration cannot convert matrix_room_access.mode; fix the retired value before retrying"
        raise AccessMigrationError(msg)
    join_rule = access.get("multi_user_join_rule", "public")
    if join_rule not in _LEGACY_MULTI_USER_JOIN_RULES:
        msg = (
            "Automatic access migration cannot convert matrix_room_access.multi_user_join_rule; "
            "fix the retired value before retrying"
        )
        raise AccessMigrationError(msg)
    return {
        **access,
        "mode": mode,
        "multi_user_join_rule": join_rule,
        "publish_to_room_directory": _legacy_boolean(
            access.get("publish_to_room_directory", False),
            field_name="matrix_room_access.publish_to_room_directory",
        ),
        "reconcile_existing_rooms": _legacy_boolean(
            access.get("reconcile_existing_rooms", False),
            field_name="matrix_room_access.reconcile_existing_rooms",
        ),
        "encrypt_managed_rooms": _legacy_boolean(
            access.get("encrypt_managed_rooms", False),
            field_name="matrix_room_access.encrypt_managed_rooms",
        ),
        "invite_only_rooms": _legacy_string_list(
            access.get("invite_only_rooms", []),
            field_name="matrix_room_access.invite_only_rooms",
        ),
        "room_admins": _legacy_string_list(
            access.get("room_admins", []),
            field_name="matrix_room_access.room_admins",
        ),
    }


def _apply_reply_policy(
    entity_data: dict[str, Any],
    policy: dict[str, Any] | None,
    *,
    entity_field_name: str,
    migrate_credential_managers: bool,
) -> None:
    if policy is None:
        return
    users = policy["users"]
    existing_access = entity_data.get("access")
    if existing_access is not None and not isinstance(existing_access, dict):
        msg = f"Automatic access migration cannot combine malformed {entity_field_name}.access"
        raise AccessMigrationError(msg)
    authored_access = existing_access if isinstance(existing_access, dict) else {}
    entity_data["access"] = {
        "current_room_members": authored_access.get("current_room_members", False),
        "members_of_rooms": _stable_union(
            authored_access.get("members_of_rooms"),
            policy["joined_rooms"],
            field_name=f"{entity_field_name}.access.members_of_rooms",
        ),
        "users": _stable_union(
            authored_access.get("users"),
            users,
            field_name=f"{entity_field_name}.access.users",
        ),
    }
    if migrate_credential_managers:
        concrete_users, _skipped_users = split_concrete_matrix_user_ids(users)
        entity_data["credential_managers"] = _stable_union(
            entity_data.get("credential_managers"),
            concrete_users,
            field_name=f"{entity_field_name}.credential_managers",
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
    migrated["administrators"] = _stable_union(
        migrated.get("administrators"),
        global_users,
        field_name="administrators",
    )
    room_defaults = migrated.setdefault("room_defaults", {})
    if isinstance(room_defaults, dict):
        room_defaults["invite_users"] = _stable_union(
            room_defaults.get("invite_users"),
            global_users,
            field_name="room_defaults.invite_users",
        )


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
                    entity_field_name=f"{section_name}.{entity_name}",
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
                entity_field_name="router",
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
                room["invite_users"] = _stable_union(
                    room.get("invite_users"),
                    invite_users,
                    field_name=f"rooms.{room_key}.invite_users",
                )


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
            field_name="room_defaults.admins",
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
    matrix_access = (
        _normalized_matrix_access(migrated.pop("matrix_room_access")) if "matrix_room_access" in migrated else None
    )
    authorization = migrated.get("authorization")
    if isinstance(authorization, dict):
        global_users = _legacy_string_list(
            authorization.pop("global_users", []),
            field_name="authorization.global_users",
        )
        reply_permissions = _normalized_reply_permissions(authorization.pop("agent_reply_permissions", {}))
        room_permissions = _legacy_mapping(
            authorization.pop("room_permissions", {}),
            field_name="authorization.room_permissions",
        )
        default_room_access = _legacy_boolean(
            authorization.pop("default_room_access", False),
            field_name="authorization.default_room_access",
        )
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
