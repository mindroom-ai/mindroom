"""The configured team a team run compacts and stores as, and the history settings it runs under."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.history.types import ResolvedHistorySettings


def configured_team(config: Config, team_name: str | None) -> str | None:
    """Return the configured team ``team_name`` names, or None for an ad hoc team.

    A saved approval names the team as the entity whose account handles it, which for an ad hoc team is a member;
    an ad hoc team compacts and stores as no configured entity, under the defaults.
    """
    return team_name if team_name in config.teams else None


def team_history_settings(config: Config, team_name: str | None) -> ResolvedHistorySettings:
    """Return the history settings of the configured team, or the defaults for an ad hoc one."""
    return config.resolve_entity(configured_team(config, team_name)).history_settings


__all__ = [
    "configured_team",
    "team_history_settings",
]
