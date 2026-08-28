"""Raw retired access inputs for tests exercising automatic model migration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mindroom.config.auth import AuthorizationConfig
    from mindroom.config.main import Config


def retired_authorization(**values: object) -> Any:
    """Return raw authorization data so the root config migrator receives it."""
    return dict(values)


def retired_reply_permission(
    *,
    users: list[str] | None = None,
    joined_rooms: list[str] | None = None,
) -> dict[str, object]:
    """Return one raw retired responder policy for migration-focused setup."""
    policy: dict[str, object] = {}
    if users is not None:
        policy["users"] = users
    if joined_rooms is not None:
        policy["joined_rooms"] = joined_rooms
    return policy


def apply_retired_authorization(config: Config, **values: object) -> AuthorizationConfig:
    """Migrate retired access inputs onto an existing config used by a test."""
    from mindroom.config.main import Config  # noqa: PLC0415

    payload = config.authored_model_dump()
    payload["authorization"] = dict(values)
    migrated = Config.model_validate(payload)
    config.administrators = migrated.administrators
    config.room_defaults = migrated.room_defaults
    config.rooms = migrated.rooms
    config.authorization = migrated.authorization
    config.router.access = migrated.router.access
    for agent_name, agent in config.agents.items():
        agent.access = migrated.agents[agent_name].access
        agent.credential_managers = migrated.agents[agent_name].credential_managers
    for team_name, team in config.teams.items():
        team.access = migrated.teams[team_name].access
    return config.authorization
