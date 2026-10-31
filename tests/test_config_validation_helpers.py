"""Focused tests for shared config validation helpers."""

from __future__ import annotations

import re

import pytest
from pydantic import BaseModel, ValidationError

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.judgment import OpenAIDecisionsJudgmentConfig
from mindroom.config.knowledge import KnowledgeRerankerConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, EmbedderConfig, ToolConfigEntry
from mindroom.config.validation import duplicate_items, validate_history_limit_choice
from mindroom.config.voice import SpeechServiceConfig, VoiceSTTConfig


def test_duplicate_items_preserves_first_duplicate_encounter_order() -> None:
    """Duplicate reporting should follow the first repeated occurrence."""
    assert duplicate_items(["alpha", "beta", "gamma", "beta", "alpha", "beta"]) == ["beta", "alpha"]


@pytest.mark.parametrize(
    ("compaction_model", "message"),
    [
        (
            "missing",
            "Compaction model references unknown models: agents.code.compaction.model -> missing, "
            "defaults.compaction.model -> missing, teams.ops.compaction.model -> missing",
        ),
        (
            "windowless",
            "Explicit compaction model references require a model with context_window: "
            "agents.code.compaction.model -> windowless, defaults.compaction.model -> windowless, "
            "teams.ops.compaction.model -> windowless",
        ),
    ],
)
def test_compaction_model_reference_errors_list_every_scope_sorted(compaction_model: str, message: str) -> None:
    """Compaction model errors name each scope's reference in sorted order, not authoring order."""
    compaction = {"model": compaction_model}
    data = {
        "models": {
            "default": {"provider": "openai", "id": "default-model", "context_window": 128_000},
            "windowless": {"provider": "openai", "id": "windowless-model"},
        },
        "defaults": {"compaction": compaction},
        "agents": {"code": {"display_name": "Code", "compaction": compaction}},
        "teams": {"ops": {"display_name": "Ops", "role": "Work", "agents": ["code"], "compaction": compaction}},
    }
    with pytest.raises(ValueError, match=re.escape(message)):
        Config.model_validate(data)


def test_history_limit_choice_rejects_both_limits_with_existing_message() -> None:
    """Shared history validation should keep the existing user-facing message."""
    with pytest.raises(ValueError, match="num_history_runs and num_history_messages are mutually exclusive"):
        validate_history_limit_choice(num_history_runs=2, num_history_messages=10)


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (
            lambda: ToolConfigEntry(name="shell", initial=True),
            "Tool entry initial=true requires defer=true",
        ),
        (
            lambda: AgentConfig(
                display_name="Code",
                knowledge_bases=["docs", "runbooks", "docs", "runbooks"],
            ),
            "Duplicate knowledge bases are not allowed: docs, runbooks",
        ),
        (
            lambda: TeamConfig(
                display_name="Team",
                role="Work",
                agents=["code", "research", "code", "research"],
            ),
            "Duplicate agents are not allowed in a team: code, research",
        ),
        (
            lambda: AuthorizationConfig(
                aliases={
                    "@alice:example.com": ["@telegram_1:example.com", "@discord_1:example.com"],
                    "@bob:example.com": ["@telegram_1:example.com", "@discord_1:example.com"],
                },
            ),
            "Duplicate bridge aliases are not allowed: @telegram_1:example.com, @discord_1:example.com",
        ),
    ],
)
def test_duplicate_validators_preserve_error_order(factory: object, match: str) -> None:
    """Config duplicate validators should preserve encounter-ordered duplicate names."""
    with pytest.raises(ValueError, match=match):
        factory()


@pytest.mark.parametrize(
    ("model", "required"),
    [
        (EmbedderConfig, {}),
        (KnowledgeRerankerConfig, {"provider": "cohere", "model": "rerank-v3.5"}),
        (OpenAIDecisionsJudgmentConfig, {"provider": "openai_decisions"}),
        (SpeechServiceConfig, {"model": "whisper-1", "api_key": "key"}),
        (VoiceSTTConfig, {"api_key": "key"}),
    ],
)
def test_optional_credential_service_references_are_validated(
    model: type[BaseModel],
    required: dict[str, object],
) -> None:
    """Every optional credentials_service may stay unset, is trimmed, and rejects an invalid service name."""

    def service(value: str | None) -> object:
        return model.model_validate({**required, "credentials_service": value}).model_dump()["credentials_service"]

    assert service(None) is None
    assert service(" ops ") == "ops"
    with pytest.raises(ValidationError, match="Service name can only include"):
        service("bad name")


@pytest.mark.parametrize(
    "aliases",
    [
        {"@alice:example.com": ["@bridge:example.com"], "@carol:example.com": ["@alice:example.com"]},
        {"@alice:example.com": ["@bob:example.com"], "@bob:example.com": ["@alice:example.com"]},
        {"@alice:example.com": ["@alice:example.com"]},
    ],
    ids=["chain", "cycle", "self-alias"],
)
def test_authorization_aliases_reject_canonical_ids_as_aliases(aliases: dict[str, list[str]]) -> None:
    """Alias resolution must remain idempotent at every authorization boundary."""
    with pytest.raises(ValueError, match="Canonical Matrix user IDs cannot also be bridge aliases"):
        AuthorizationConfig(aliases=aliases)


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (
            lambda: DefaultsConfig(num_history_runs=2, num_history_messages=10),
            "num_history_runs and num_history_messages are mutually exclusive",
        ),
        (
            lambda: AgentConfig(display_name="Code", num_history_runs=2, num_history_messages=10),
            "num_history_runs and num_history_messages are mutually exclusive",
        ),
        (
            lambda: TeamConfig(
                display_name="Team",
                role="Work",
                agents=["code"],
                num_history_runs=2,
                num_history_messages=10,
            ),
            "num_history_runs and num_history_messages are mutually exclusive",
        ),
    ],
)
def test_history_limit_validators_preserve_error_message(factory: object, match: str) -> None:
    """Defaults, agents, and teams should reject ambiguous history limits consistently."""
    with pytest.raises(ValueError, match=match):
        factory()


@pytest.mark.parametrize(
    ("authored", "message"),
    [
        ({"mode": "broadcast"}, "Input should be 'coordinate' or 'collaborate'"),
        ({"instructions": ["Be brief."]}, "Extra inputs are not permitted"),
    ],
    ids=["unknown-mode", "misplaced-key"],
)
def test_a_team_entry_with_an_unknown_mode_or_key_fails_naming_it(authored: dict[str, object], message: str) -> None:
    """An invalid mode or a key a team does not have fails at load, naming the key, instead of running collaborate."""
    with pytest.raises(ValueError, match=message) as raised:
        TeamConfig.model_validate({"display_name": "Team", "role": "Work", "agents": ["code"], **authored})
    assert next(iter(authored)) in str(raised.value)
