"""Tests for the top-level `slack:` config section."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from mindroom.config import slack as slack_config
from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.config.slack import SlackAnsweringAgent, SlackConfig, has_personal_slack, slack_answering_agents
from mindroom.constants import resolve_runtime_paths
from mindroom.slack.api import SLACK_CHANNEL_ID_RE

if TYPE_CHECKING:
    from pathlib import Path

_MAPPING_ERROR = "Email mapping requires a valid Matrix template and explicit email domain"
_MAPPING = {"email_domain": "example.com", "matrix_user_id_template": "@{localpart}:example.org"}


def _config_data(agents: dict[str, object] | None = None, **slack: object) -> dict[str, object]:
    return {
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
        "router": {"model": "default"},
        "slack": slack,
        "agents": agents or {},
    }


def _config(agents: dict[str, object] | None = None, **slack: object) -> Config:
    return Config.model_validate(_config_data(agents, **slack))


def _agent(*, rooms: list[str] | None = None, **slack_overrides: object) -> dict[str, object]:
    return {"display_name": "Agent", "rooms": rooms or [], "tools": [{"slack": slack_overrides}]}


def test_slack_config_defaults_to_no_mapping() -> None:
    """A config without a `slack:` section maps no Slack emails to Matrix users."""
    config = Config.model_validate(
        {"models": {"default": {"provider": "openai", "id": "gpt-6-astra"}}, "router": {"model": "default"}},
    )

    assert config.slack == SlackConfig()
    assert config.slack.email_domain is None
    assert config.slack.matrix_user_id_template is None


def test_slack_config_accepts_a_complete_mapping() -> None:
    """A domain plus a template with one localpart placeholder is a valid mapping."""
    config = _config(email_domain="example.com", matrix_user_id_template="@{localpart}:example.org")

    assert config.slack.email_domain == "example.com"
    assert config.slack.matrix_user_id_template == "@{localpart}:example.org"


@pytest.mark.parametrize(
    "fields",
    [
        {"email_domain": "example.com"},
        {"matrix_user_id_template": "@{localpart}:example.org"},
    ],
)
def test_slack_config_requires_both_fields(fields: dict[str, str]) -> None:
    """Setting only the domain or only the template is rejected."""
    with pytest.raises(ValidationError, match="Set email_domain and matrix_user_id_template together"):
        _config(**fields)


@pytest.mark.parametrize(
    "template",
    [
        "@user:example.org",
        "@{localpart}{localpart}:example.org",
        "@{localpart}:{server}",
        "{localpart}",
    ],
)
def test_template_requires_single_localpart(template: str) -> None:
    """The Matrix template must hold exactly one `{localpart}` and form a valid Matrix ID."""
    with pytest.raises(ValidationError, match=_MAPPING_ERROR):
        _config(email_domain="example.com", matrix_user_id_template=template)


def test_slack_config_rejects_unknown_fields() -> None:
    """The section is closed: misspelled keys fail instead of being ignored."""
    with pytest.raises(ValidationError, match="email_domian"):
        _config(email_domian="example.com")


def test_room_without_channels_is_rejected() -> None:
    """A personal agent has no Slack channel to answer in."""
    with pytest.raises(ValidationError, match=r"agents\.helper\.tools\.slack\.room needs channels"):
        _config({"helper": _agent(rooms=["lobby"], room="lobby")})


def test_room_must_be_one_of_the_agents_rooms() -> None:
    """A mention can only start a thread in a room the agent is configured for."""
    agents = {"helper": _agent(rooms=["lobby"], channels=["C0123ABCD"], room="ops")}

    with pytest.raises(ValidationError, match=r"agents\.helper\.tools\.slack\.room must be one of the agent's rooms"):
        _config(agents, **_MAPPING)


def test_room_needs_write() -> None:
    """An answering agent always posts its replies to Slack, so it cannot be read-only."""
    agents = {"helper": _agent(rooms=["lobby"], channels=["C0123ABCD"], room="lobby", write=False)}

    with pytest.raises(
        ValidationError,
        match=r"agents\.helper\.tools\.slack\.room needs write; an answering agent posts its replies to Slack",
    ):
        _config(agents, **_MAPPING)

    reader = _config({"helper": _agent(channels=["C0123ABCD"], write=False)}, **_MAPPING)
    assert slack_answering_agents(reader) == ()
    answering = _config(
        {"helper": _agent(rooms=["lobby"], channels=["C0123ABCD"], room="lobby", write=True)},
        **_MAPPING,
    )
    assert [agent.agent for agent in slack_answering_agents(answering)] == ["helper"]


def test_a_channel_has_one_answering_agent() -> None:
    """Two agents cannot both answer mentions in one channel, even when they list other channels too."""
    agents = {
        "first": _agent(rooms=["lobby"], channels=["C0123ABCD"], room="lobby"),
        "second": _agent(rooms=["ops"], channels=["C0456EFGH", "C0123ABCD"], room="ops"),
    }

    with pytest.raises(ValidationError, match="Slack channel C0123ABCD is answered by both first and second"):
        _config(agents, **_MAPPING)


def test_answering_needs_the_user_mapping() -> None:
    """Without the email-to-Matrix mapping a Slack mention has no requester to answer."""
    agents = {"helper": _agent(rooms=["lobby"], channels=["C0123ABCD"], room="lobby")}

    with pytest.raises(
        ValidationError,
        match=r"Answering Slack mentions needs slack\.email_domain and slack\.matrix_user_id_template",
    ):
        _config(agents)


@pytest.mark.parametrize(
    "channel",
    ["general", "c0123abcd", "C0123", "D0123ABCD", " C0123ABCD", "C0123ABCD,C0456EFGH", 42],
)
def test_channel_ids_must_look_like_slack_ids(channel: object) -> None:
    """Names, short IDs, direct-message IDs and padded values are not channel IDs."""
    with pytest.raises(ValidationError, match="Slack channel IDs look like C0123ABCD"):
        _config({"helper": _agent(channels=[channel])})


def test_config_channel_ids_are_the_slack_clients() -> None:
    """Config keeps its own copy of the channel ID pattern, which must stay the Slack client's."""
    assert slack_config._CHANNEL_ID.pattern == SLACK_CHANNEL_ID_RE.pattern


def test_empty_channels_is_rejected_instead_of_meaning_personal() -> None:
    """An empty list would otherwise silently switch the agent to personal mode."""
    with pytest.raises(
        ValidationError,
        match=r"agents\.helper\.tools\.slack\.channels must list at least one channel; omit it for personal mode",
    ):
        _config({"helper": _agent(channels=[])})


def test_channels_must_be_a_list() -> None:
    """A bare string is not accepted for the channel list."""
    with pytest.raises(
        ValidationError,
        match=r"agents\.helper\.tools\.slack\.channels must be a list of Slack channel IDs",
    ):
        _config({"helper": _agent(channels="C0123ABCD")})


def test_valid_answering_config() -> None:
    """Answering agents are those with channels and a room; readers and personal agents are valid and silent."""
    config = _config(
        {
            "support": _agent(rooms=["lobby", "ops"], channels=["C0123ABCD", "G0456EFGH"], room="ops"),
            "reader": _agent(channels=["C0123ABCD"]),
            "personal": _agent(rooms=["lobby"]),
            "plain": {"display_name": "Plain", "tools": ["calculator"]},
            "docs": _agent(rooms=["docs"], channels=["C0789IJKL"], room="docs"),
        },
        **_MAPPING,
    )

    assert slack_answering_agents(config) == (
        SlackAnsweringAgent(agent="support", room="ops", channels=frozenset({"C0123ABCD", "G0456EFGH"})),
        SlackAnsweringAgent(agent="docs", room="docs", channels=frozenset({"C0789IJKL"})),
    )


def test_no_agent_answers_without_slack_tool_entries() -> None:
    """A config with no agents, or none using Slack, answers nothing."""
    assert slack_answering_agents(_config()) == ()
    assert slack_answering_agents(_config({"plain": {"display_name": "Plain"}})) == ()


def test_teams_and_defaults_tools_are_considered(tmp_path: Path) -> None:
    """Teams carry no tools, and `defaults.tools` cannot set the agent-only `channels` and `room`."""
    agents = {"helper": _agent(rooms=["lobby"], channels=["C0123ABCD"], room="lobby")}
    team = {"display_name": "Team", "role": "Work together", "agents": ["helper"], "rooms": ["lobby"]}

    with_team = Config.model_validate({**_config_data(agents, **_MAPPING), "teams": {"crew": team}})
    assert [answering.agent for answering in slack_answering_agents(with_team)] == ["helper"]
    with pytest.raises(ValidationError, match=r"crew\.tools"):
        Config.model_validate({**_config_data(agents, **_MAPPING), "teams": {"crew": {**team, "tools": ["slack"]}}})

    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    with pytest.raises(
        ConfigRuntimeValidationError,
        match=r"defaults\.tools\[0\]\.slack\.channels: unknown authored override field\(s\): channels",
    ):
        Config.validate_with_runtime(
            {"defaults": {"tools": [{"slack": {"channels": ["C0123ABCD"]}}]}},
            runtime_paths,
        )


def test_has_personal_slack_needs_the_tool_without_channels() -> None:
    """Only an agent with `slack` and no `channels` reads its requesters' own Slack; unknown names have none."""
    config = _config(
        {
            "personal": {"display_name": "Personal", "tools": ["slack"]},
            "project": _agent(rooms=["launch"], channels=["C0123ABCD"]),
            "none": {"display_name": "None"},
        },
        **_MAPPING,
    )

    assert has_personal_slack(config, "personal")
    assert not has_personal_slack(config, "project")
    assert not has_personal_slack(config, "none")
    assert not has_personal_slack(config, "missing")
