"""Slack integration settings."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mindroom.matrix.identity import validate_email_to_matrix_mapping

if TYPE_CHECKING:
    from mindroom.config.agent import AgentConfig
    from mindroom.config.main import Config

_SLACK_TOOL_NAME = "slack"
# Must match `SLACK_CHANNEL_ID_RE` in `mindroom.slack.api`, which config cannot import without the HTTP client.
_CHANNEL_ID = re.compile(r"[CG][A-Z0-9]{8,}")


class SlackConfig(BaseModel):
    """How Slack workspace members map to Matrix users."""

    model_config = ConfigDict(extra="forbid")

    email_domain: str | None = Field(
        default=None,
        description="Email domain of the Slack workspace's members, for example `example.com`; set with matrix_user_id_template",
    )
    matrix_user_id_template: str | None = Field(
        default=None,
        description="Matrix user ID a member's email localpart maps to, with one `{localpart}`, for example `@{localpart}:example.org`; set with email_domain",
    )

    @model_validator(mode="after")
    def validate_identity_mapping(self) -> SlackConfig:
        """Require both mapping fields or neither, and a valid pair."""
        if (self.email_domain is None) != (self.matrix_user_id_template is None):
            msg = "Set email_domain and matrix_user_id_template together"
            raise ValueError(msg)
        if self.matrix_user_id_template is not None:
            validate_email_to_matrix_mapping(self.matrix_user_id_template, self.email_domain)
        return self


@dataclass(frozen=True)
class SlackAnsweringAgent:
    """An agent that answers mentions of the MindRoom Slack app in its channels, in one of its Matrix rooms."""

    agent: str
    room: str
    channels: frozenset[str]


def _slack_overrides(agent: AgentConfig) -> dict[str, object]:
    """Return the agent's own `slack` tool overrides; `defaults.tools` cannot set `channels` or `room`."""
    return next((entry.overrides for entry in agent.tools if entry.name == _SLACK_TOOL_NAME), {})


def slack_answering_agents(config: Config) -> tuple[SlackAnsweringAgent, ...]:
    """List the agents with Slack channels and a room, in config order, from a validated config."""
    answering: list[SlackAnsweringAgent] = []
    for name, agent in config.agents.items():
        overrides = _slack_overrides(agent)
        channels = overrides.get("channels")
        room = overrides.get("room")
        if channels and room:
            answering.append(SlackAnsweringAgent(name, cast("str", room), frozenset(cast("list[str]", channels))))
    return tuple(answering)


def has_personal_slack(config: Config, agent_name: str) -> bool:
    """Return whether an agent has the `slack` tool in personal mode, which reads each requester's own Slack."""
    agent = config.agents.get(agent_name)
    return (
        agent is not None
        and _SLACK_TOOL_NAME in config.resolve_entity(agent_name).available_tools
        and not _slack_overrides(agent).get("channels")
    )


def _check_channels(path: str, channels: object) -> None:
    """Reject anything but a non-empty list of Slack channel IDs."""
    if not isinstance(channels, list):
        msg = f"{path}.channels must be a list of Slack channel IDs"
        raise ValueError(msg)  # noqa: TRY004
    if not channels:
        msg = f"{path}.channels must list at least one channel; omit it for personal mode"
        raise ValueError(msg)
    if not all(isinstance(channel, str) and _CHANNEL_ID.fullmatch(channel) for channel in channels):
        msg = f"{path}.channels: Slack channel IDs look like C0123ABCD"
        raise ValueError(msg)


def _effective_write(config: Config, name: str) -> object:
    """Return the `write` the agent's `slack` tool runs with: its own override over any from `defaults.tools`."""
    entries = config.resolve_entity(name).authored_tool_configs
    overrides = next((entry.tool_config_overrides for entry in entries if entry.name == _SLACK_TOOL_NAME), {})
    return overrides.get("write", True)


def _check_agent(config: Config, name: str, agent: AgentConfig) -> None:
    """Check one agent's `channels`, `room` and effective `write`."""
    overrides = _slack_overrides(agent)
    channels = overrides.get("channels")
    room = overrides.get("room")
    path = f"agents.{name}.tools.slack"
    if channels is not None:
        _check_channels(path, channels)
    if room is None:
        return
    if channels is None:
        msg = f"{path}.room needs channels"
        raise ValueError(msg)
    if _effective_write(config, name) is not True:
        msg = f"{path}.room needs write; an answering agent posts its replies to Slack"
        raise ValueError(msg)
    if room not in agent.rooms:
        msg = f"{path}.room must be one of the agent's rooms"
        raise ValueError(msg)
    if config.slack.matrix_user_id_template is None:
        msg = "Answering Slack mentions needs slack.email_domain and slack.matrix_user_id_template"
        raise ValueError(msg)


def validate_slack_agents(config: Config) -> None:
    """Check each agent's Slack `channels` and `room`, and that every channel has at most one answering agent."""
    for name, agent in config.agents.items():
        _check_agent(config, name, agent)
    answered_by: dict[str, str] = {}
    for answering in slack_answering_agents(config):
        for channel in sorted(answering.channels):
            first = answered_by.setdefault(channel, answering.agent)
            if first != answering.agent:
                msg = f"Slack channel {channel} is answered by both {first} and {answering.agent}"
                raise ValueError(msg)
