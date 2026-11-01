"""Focused tests for shared config validation helpers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel, ValidationError

from mindroom.agent_policy import PRIVATE_KNOWLEDGE_BASE_ID_PREFIX
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.calls import LiveCallProfile, RealtimeCallProfile
from mindroom.config.judgment import OpenAIDecisionsJudgmentConfig
from mindroom.config.knowledge import KnowledgeRerankerConfig
from mindroom.config.main import Config, config_validation_issues
from mindroom.config.models import DefaultsConfig, EmbedderConfig, ToolConfigEntry
from mindroom.config.validation import duplicate_items, validate_history_limit_choice
from mindroom.config.voice import SpeechServiceConfig, VoiceSTTConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.matrix.state import update_matrix_state
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


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


@pytest.mark.parametrize(
    ("private_extra", "memory_backend"),
    [
        ({}, "file"),
        ({"template_dir": "./mind_template"}, "none"),
    ],
)
def test_git_backed_private_knowledge_rejects_the_memory_notes_tree(
    private_extra: dict[str, str],
    memory_backend: str,
) -> None:
    """File memory and a private template each reserve the memory notes tree on their own."""
    private = {
        "per": "user",
        "root": "mind_data",
        "knowledge": {"path": "memory/notes", "git": {"repo_url": "https://github.com/example/repo"}},
        **private_extra,
    }
    data = {
        "models": {"default": {"provider": "openai", "id": "default-model"}},
        "agents": {"mind": {"display_name": "Mind", "private": private, "memory_backend": memory_backend}},
    }
    with pytest.raises(ValueError, match=r"git-backed private knowledge at 'memory/notes'.*dedicated subtree"):
        Config.model_validate(data)


def test_unknown_knowledge_base_assignments_are_listed_sorted() -> None:
    """Unknown knowledge base assignments are reported sorted by agent and base ID."""
    data = {
        "models": {"default": {"provider": "openai", "id": "default-model"}},
        "agents": {
            "zed": {"display_name": "Zed", "knowledge_bases": ["notes"]},
            "amy": {"display_name": "Amy", "knowledge_bases": ["wiki", "docs"]},
        },
    }
    with pytest.raises(
        ValueError,
        match=re.escape("Agents reference unknown knowledge bases: amy -> docs, amy -> wiki, zed -> notes"),
    ):
        Config.model_validate(data)


def test_unknown_knowledge_base_lookup_reports_it_is_not_configured() -> None:
    """Looking up a base ID that is neither configured nor a private agent base names that ID."""
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "default-model"}},
            "agents": {"code": {"display_name": "Code"}},
        },
    )
    for base_id in ("missing", f"{PRIVATE_KNOWLEDGE_BASE_ID_PREFIX}code", f"{PRIVATE_KNOWLEDGE_BASE_ID_PREFIX}ghost"):
        with pytest.raises(ValueError, match=re.escape(f"Knowledge base '{base_id}' is not configured")):
            config.get_knowledge_base_config(base_id)


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


def test_speech_service_blank_key_and_host_mean_unset() -> None:
    """Blank speech form fields are omitted settings, and a set host is trimmed before its URL check."""
    blank = SpeechServiceConfig.model_validate(
        {"model": "whisper-1", "credentials_service": "openai", "api_key": "  ", "host": " "},
    )
    assert (blank.api_key, blank.host) == (None, None)
    trimmed = SpeechServiceConfig.model_validate({"model": "whisper-1", "api_key": " k ", "host": " http://stt/v1/ "})
    assert (trimmed.api_key, trimmed.host) == ("k", "http://stt/v1")


@pytest.mark.parametrize(("model", "backend"), [(RealtimeCallProfile, "realtime"), (LiveCallProfile, "live")])
def test_required_call_credential_service_references_are_validated(model: type[BaseModel], backend: str) -> None:
    """A call profile's required credentials_service is trimmed and rejects an invalid service name."""

    def service(value: str) -> object:
        profile = {"backend": backend, "model": "m", "voice": "marin", "credentials_service": value}
        return model.model_validate(profile).model_dump()["credentials_service"]

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


@pytest.mark.parametrize(
    "second_room",
    ["voice", "#voice:localhost"],
    ids=["same-spelling-model-validator", "alias-spelling-runtime-validation"],
)
def test_one_rule_reports_one_location_and_message_from_either_validator(tmp_path: Path, second_room: str) -> None:
    """A call room shared by spelling or by alias fails with the same issue, named at `config`, without Pydantic's prefix."""
    data = {
        "agents": {
            "one": {"display_name": "One", "rooms": ["voice"]},
            "two": {"display_name": "Two", "rooms": [second_room]},
        },
        "calls": {
            "enabled": True,
            "profiles": {
                "voice": {
                    "backend": "realtime",
                    "model": "gpt-realtime",
                    "credentials_service": "openai",
                    "voice": "marin",
                },
            },
            "agents": {"one": "voice", "two": "voice"},
        },
    }
    with pytest.raises(ValueError, match="configures multiple agents") as raised:
        Config.validate_with_runtime(data, test_runtime_paths(tmp_path))

    assert config_validation_issues(raised.value) == [
        (("config",), "calls.agents configures multiple agents for room(s): voice (one, two)"),
    ]


_OUTSIDE_ALIAS = "#ideas:example.org"
_FILE_MEMORY_AGENT = {"display_name": "General", "rooms": ["lobby"], "memory_backend": "file"}
_MODELS = {"default": {"provider": "openai", "id": "test-model"}}


@pytest.mark.parametrize(
    ("authored", "location", "room"),
    [
        (
            {"agents": {"general": {"display_name": "General", "rooms": [_OUTSIDE_ALIAS]}}},
            "agents.general.rooms",
            _OUTSIDE_ALIAS,
        ),
        (
            {
                "agents": {"code": {"display_name": "Code", "rooms": ["lobby"]}},
                "teams": {
                    "ops": {"display_name": "Ops", "role": "Work", "agents": ["code"], "rooms": [_OUTSIDE_ALIAS]},
                },
            },
            "teams.ops.rooms",
            _OUTSIDE_ALIAS,
        ),
        (
            {"rooms": {_OUTSIDE_ALIAS: {}}, "agents": {"general": _FILE_MEMORY_AGENT}},
            "rooms",
            _OUTSIDE_ALIAS,
        ),
        (
            {"agents": {"general": {**_FILE_MEMORY_AGENT, "room_thread_modes": {_OUTSIDE_ALIAS: "room"}}}},
            "agents.general.room_thread_modes",
            _OUTSIDE_ALIAS,
        ),
        (
            {
                "agents": {
                    "general": {
                        **_FILE_MEMORY_AGENT,
                        "automations": [{"name": "prompt_curation", "room": _OUTSIDE_ALIAS}],
                    },
                },
            },
            "agents.general.automations.prompt_curation.room",
            _OUTSIDE_ALIAS,
        ),
        (
            {
                "defaults": {"automations": [{"name": "prompt_curation", "room": "ideas"}]},
                "agents": {"general": _FILE_MEMORY_AGENT},
            },
            "defaults.automations.prompt_curation.room",
            "ideas",
        ),
        (
            {"agents": {"general": _FILE_MEMORY_AGENT}, "room_models": {"lobbby": "default"}},
            "room_models",
            "lobbby",
        ),
        (
            {"agents": {"general": _FILE_MEMORY_AGENT}, "room_thread_summary_models": {_OUTSIDE_ALIAS: "default"}},
            "room_thread_summary_models",
            _OUTSIDE_ALIAS,
        ),
    ],
    ids=[
        "agent-rooms",
        "team-rooms",
        "top-level-rooms",
        "room-thread-modes",
        "agent-automation",
        "unlisted-key",
        "room-models",
        "room-thread-summary-models",
    ],
)
def test_a_room_reference_that_names_no_room_fails_naming_where(
    tmp_path: Path,
    authored: dict[str, object],
    location: str,
    room: str,
) -> None:
    """An outside alias or an unlisted key resolves to no room at runtime, so loading the config names it."""
    with pytest.raises(ValueError, match="room ID") as raised:
        Config.validate_with_runtime({**authored, "models": _MODELS}, test_runtime_paths(tmp_path))

    assert config_validation_issues(raised.value) == [
        (
            ("config",),
            "Each room reference must be a room key MindRoom manages, that room's full alias, or a ! room ID: "
            f"{location} -> {room}",
        ),
    ]


@pytest.mark.parametrize(
    ("namespace", "alias", "valid"),
    [
        ("", "#lobby:localhost", True),
        ("team1", "#lobby_team1:localhost", True),
        ("team1", "#lobby:localhost", False),
        ("", "#lobby:example.org", False),
    ],
    ids=["plain", "namespaced", "namespace-missing", "other-server"],
)
def test_only_the_exact_alias_mindroom_creates_for_a_room_key_names_that_room(
    tmp_path: Path,
    namespace: str,
    alias: str,
    *,
    valid: bool,
) -> None:
    """The full alias of a configured room key stays valid; the same localpart on another server or namespace does not."""
    runtime_paths = test_runtime_paths(tmp_path)
    runtime_paths = resolve_runtime_paths(
        config_path=runtime_paths.config_path,
        storage_path=runtime_paths.storage_root,
        process_env={**runtime_paths.process_env, "MINDROOM_NAMESPACE": namespace},
    )
    authored = {
        "models": _MODELS,
        "agents": {
            "one": {"display_name": "One", "rooms": ["lobby"]},
            "two": {
                "display_name": "Two",
                "rooms": [alias],
                "memory_backend": "file",
                "room_thread_modes": {alias: "room"},
                "automations": [{"name": "prompt_curation", "room": alias}],
            },
        },
    }
    if valid:
        assert Config.validate_with_runtime(authored, runtime_paths).agents["two"].rooms == [alias]
    else:
        with pytest.raises(ValueError, match=re.escape(f"agents.two.rooms -> {alias}")):
            Config.validate_with_runtime(authored, runtime_paths)


def test_room_model_maps_accept_rooms_and_summary_models_accept_entities(tmp_path: Path) -> None:
    """Model maps take a room key, its full alias, or a ! room ID, and summary models also take an agent or team."""
    rooms = {"lobby": "default", "#lobby:localhost": "default", "!outside:example.org": "default"}
    authored = {
        "models": _MODELS,
        "agents": {"general": _FILE_MEMORY_AGENT},
        "teams": {"crew": {"display_name": "Crew", "role": "Work", "agents": ["general"]}},
        "room_models": rooms,
        "room_thread_summary_models": {**rooms, "general": "default", "crew": "default"},
    }

    config = Config.validate_with_runtime(authored, test_runtime_paths(tmp_path))

    assert config.room_models == rooms


def test_a_room_mindroom_recorded_keeps_its_alias_and_key_after_its_key_leaves_the_config(tmp_path: Path) -> None:
    """MindRoom's record still maps a room it created, so its alias and key keep naming it once no entity lists the key."""
    runtime_paths = test_runtime_paths(tmp_path)
    update_matrix_state(
        runtime_paths,
        lambda state: state.add_room("lobby", "!lobby:localhost", "#lobby:localhost", "Lobby"),
    )
    authored = {
        "models": _MODELS,
        "agents": {
            "general": {
                **_FILE_MEMORY_AGENT,
                "rooms": ["#lobby:localhost"],
                "room_thread_modes": {"lobby": "room"},
            },
        },
    }

    assert Config.validate_with_runtime(authored, runtime_paths).agents["general"].rooms == ["#lobby:localhost"]
