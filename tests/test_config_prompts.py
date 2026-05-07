"""Tests for configurable built-in prompt overrides."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mindroom.config.main import Config


def test_config_accepts_known_prompt_override() -> None:
    """Prompt overrides accept known globals and return configured text."""
    config = Config.model_validate(
        {
            "prompts": {
                "AGENT_IDENTITY_CONTEXT_TEMPLATE": "Custom identity for {display_name}.",
            },
        },
    )

    assert config.get_prompt("AGENT_IDENTITY_CONTEXT_TEMPLATE") == "Custom identity for {display_name}."


def test_config_rejects_unknown_prompt_override() -> None:
    """Unknown prompt override names fail config validation."""
    with pytest.raises(ValidationError, match="Unknown prompt override"):
        Config.model_validate(
            {
                "prompts": {
                    "NOT_A_REAL_PROMPT": "Custom prompt.",
                },
            },
        )


def test_config_rejects_prompt_override_with_unsupported_template_field() -> None:
    """Prompt template overrides must use fields supplied by their call site."""
    with pytest.raises(ValidationError, match="Unsupported template field"):
        Config.model_validate(
            {
                "prompts": {
                    "ROUTER_AGENT_SELECTION_PROMPT_TEMPLATE": "{agents_info} {message} {thread_context}",
                },
            },
        )


@pytest.mark.parametrize(
    "template",
    [
        "{message.nope}",
        "{message[999]}",
        "{message:{message.nope}}",
    ],
)
def test_config_rejects_prompt_override_with_compound_template_field(template: str) -> None:
    """Prompt template overrides must not use compound field access."""
    with pytest.raises(ValidationError, match="Compound template fields are not supported"):
        Config.model_validate(
            {
                "prompts": {
                    "ROUTER_AGENT_SELECTION_PROMPT_TEMPLATE": template,
                },
            },
        )
