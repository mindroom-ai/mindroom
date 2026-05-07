"""Tests for configurable built-in prompt overrides."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mindroom.config.main import Config
from mindroom.prompts import PromptTemplateError, render_prompt_template


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


def test_config_render_prompt_replaces_bare_fields_and_escaped_braces() -> None:
    """Configured prompt templates render exact placeholders and escaped braces."""
    config = Config.model_validate(
        {
            "prompts": {
                "ROUTER_AGENT_SELECTION_PROMPT_TEMPLATE": "{{message}} {message} {agents_info}",
            },
        },
    )

    assert (
        config.render_prompt(
            "ROUTER_AGENT_SELECTION_PROMPT_TEMPLATE",
            agents_info="General: general assistant",
            message="hello",
        )
        == "{message} hello General: general assistant"
    )


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


def test_config_rejects_prompt_override_with_unsupported_placeholder() -> None:
    """Prompt placeholder overrides must use fields supplied by their call site."""
    with pytest.raises(ValidationError, match="Unsupported prompt placeholder"):
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
    ],
)
def test_config_rejects_prompt_override_with_compound_placeholder(template: str) -> None:
    """Prompt placeholder overrides must not use compound field access."""
    with pytest.raises(ValidationError, match="Compound prompt placeholders are not supported"):
        Config.model_validate(
            {
                "prompts": {
                    "ROUTER_AGENT_SELECTION_PROMPT_TEMPLATE": template,
                },
            },
        )


@pytest.mark.parametrize(
    "template",
    [
        "{message:.2f}",
        "{message:}",
        "{message:{agents_info}}",
        "{message:{message.nope}}",
    ],
)
def test_config_rejects_prompt_override_with_placeholder_format_spec(template: str) -> None:
    """Prompt placeholder overrides must not use field format specs."""
    match = (
        "Only exact \\{field_name\\} prompt placeholders are supported"
        if template == "{message:}"
        else "Prompt placeholder format specs are not supported"
    )
    with pytest.raises(ValidationError, match=match):
        Config.model_validate(
            {
                "prompts": {
                    "ROUTER_AGENT_SELECTION_PROMPT_TEMPLATE": template,
                },
            },
        )


def test_config_rejects_prompt_override_with_placeholder_conversion() -> None:
    """Prompt placeholder overrides must not use field conversion syntax."""
    with pytest.raises(ValidationError, match="Prompt placeholder conversions are not supported"):
        Config.model_validate(
            {
                "prompts": {
                    "ROUTER_AGENT_SELECTION_PROMPT_TEMPLATE": "{message!r}",
                },
            },
        )


def test_render_prompt_template_rejects_missing_field_value() -> None:
    """Runtime rendering fails clearly when a call site forgets a field value."""
    with pytest.raises(PromptTemplateError, match="Missing prompt placeholder value: agents_info"):
        render_prompt_template("{message} {agents_info}", message="hello")
