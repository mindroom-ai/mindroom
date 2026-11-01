"""Tests for the per-agent contacts configuration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mindroom.config.agent import AgentConfig
from mindroom.config.contacts import CONTACT_CONTROL_TOOLS, ContactsConfig
from mindroom.config.main import Config


def test_contacts_are_off_by_default() -> None:
    """An agent without a contacts object offers no contacts."""
    assert AgentConfig(display_name="A").contacts is None
    assert ContactsConfig() == ContactsConfig(rules=[], tools=[], judge=None)


@pytest.mark.parametrize("name", sorted(CONTACT_CONTROL_TOOLS))
def test_contacts_config_refuses_control_tools(name: str) -> None:
    """A contact may not drive tools that act with the owner's authority beyond the conversation."""
    with pytest.raises(ValidationError, match=f"contacts.tools may not include {name}"):
        ContactsConfig(tools=["calculator", name])


def test_contacts_config_accepts_ordinary_tools_and_rejects_unknown_fields() -> None:
    """Ordinary tool names pass, and a misspelled field fails instead of being ignored."""
    assert ContactsConfig(tools=["calculator"], rules=["Be brief."]).tools == ["calculator"]
    with pytest.raises(ValidationError, match="rule"):
        ContactsConfig.model_validate({"rule": ["Be brief."]})


def test_judge_model_must_exist() -> None:
    """A misspelled judge model alias fails configuration like participation.judgment does."""
    with pytest.raises(ValidationError, match="Unknown judgment model for agent 'helper': 'missing'"):
        Config.model_validate(
            {
                "agents": {
                    "helper": {
                        "display_name": "Helper",
                        "contacts": {"judge": {"provider": "llm", "model": "missing"}},
                    },
                },
            },
        )
    config = Config.model_validate(
        {
            "agents": {
                "helper": {
                    "display_name": "Helper",
                    "contacts": {"judge": {"provider": "llm", "model": "fast"}},
                },
            },
            "models": {"fast": {"provider": "openai", "id": "fast-model"}},
        },
    )
    assert config.agents["helper"].contacts is not None
    assert config.agents["helper"].contacts.judge is not None
