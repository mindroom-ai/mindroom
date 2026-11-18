"""Reasoning levels: the per-model ``reasoning`` block, ``name@level`` references, and how a level reaches a route."""
# ruff: noqa: D103

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.config.models import ModelConfig, ModelTable
from mindroom.constants import resolve_runtime_paths
from mindroom.providers.reasoning import apply_reasoning_level
from mindroom.thread_summary.generation import _summary_provider

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_LEVELS = {"levels": ["low", "medium", "high"], "default": "medium"}


def _config(**models: dict[str, Any]) -> Config:
    return Config.model_validate({"models": models})


def _sol(**fields: Any) -> dict[str, Any]:  # noqa: ANN401 - model fields
    return {"provider": "codex", "id": "gpt-6.2-sol", "display_name": "Sol", "reasoning": _LEVELS, **fields}


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    return resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "data", process_env={})


# --- The reasoning block ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reasoning",
    [
        {"levels": ["low", "hihg"], "default": "low"},
        {"levels": ["low", "low"], "default": "low"},
        {"levels": [], "default": "low"},
        {"levels": ["low", "high"], "default": "medium"},
        {"levels": ["low", "high"]},
    ],
    ids=["unknown-word", "duplicate", "empty", "default-not-listed", "no-default"],
)
def test_a_malformed_reasoning_block_fails_config_load(reasoning: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _config(sol=_sol(reasoning=reasoning))


@pytest.mark.parametrize("provider", ["groq", "cerebras", "synthetic"])
def test_a_route_that_takes_no_level_rejects_the_block(provider: str, runtime_paths: RuntimePaths) -> None:
    data = {"models": {"fast": {"provider": provider, "id": "m", "reasoning": _LEVELS}}}

    with pytest.raises(ConfigRuntimeValidationError, match=rf"models\.fast: .*{provider}.*reasoning"):
        Config.validate_with_runtime(data, runtime_paths)


def test_every_route_with_a_level_key_accepts_the_block_at_every_level(runtime_paths: RuntimePaths) -> None:
    levels = {"levels": ["off", "minimal", "low", "medium", "high", "xhigh", "max"], "default": "low"}
    providers = ["openai", "codex", "azure", "openrouter", "deepseek", "zai", "llama_cpp", "kimi", "anthropic"]
    providers += ["vertexai_claude", "bedrock_claude", "google", "ollama"]
    models = {provider: {"provider": provider, "id": "m", "reasoning": levels} for provider in providers}

    Config.validate_with_runtime({"models": models}, runtime_paths)


# --- References ---------------------------------------------------------------------------------------------


def test_a_reference_resolves_to_the_model_at_that_level() -> None:
    config = _config(sol=_sol(), plain={"provider": "openai", "id": "gpt-6"})

    high = config.models["sol@high"]

    assert "sol@high" in config.models
    assert config.models.get("sol@high") == high
    assert high.reasoning is not None
    assert high.reasoning.default == "high"
    assert high.display_name == "Sol (high)"
    assert config.models["sol@medium"] == config.models["sol"]
    for missing in ("sol@xhigh", "plain@high", "nope@high", "sol@", "@high"):
        assert missing not in config.models
        assert config.models.get(missing) is None
        with pytest.raises(KeyError):
            config.models[missing]


def test_the_model_table_lists_and_saves_only_authored_models() -> None:
    config = _config(sol=_sol(), plain={"provider": "openai", "id": "gpt-6"})

    assert list(config.models) == ["sol", "plain"]
    assert len(config.models) == 2
    assert sorted(config.models.items()) == sorted(
        {"sol": config.models["sol"], "plain": config.models["plain"]}.items(),
    )
    assert set(config.authored_model_dump()["models"]) == {"sol", "plain"}
    assert config.authored_model_dump()["models"]["sol"]["reasoning"] == _LEVELS


def test_the_model_table_survives_copies_and_revalidation() -> None:
    config = _config(sol=_sol())

    for copied in (
        copy.deepcopy(config),
        config.model_copy(),
        Config.model_validate(config.authored_model_dump()),
    ):
        assert copied.models["sol@high"].display_name == "Sol (high)"


def test_model_names_still_forbid_the_level_separator() -> None:
    with pytest.raises(ValidationError, match="Model names"):
        _config(**{"sol@high": _sol()})


def test_a_model_reference_at_a_level_passes_every_model_reference_check(runtime_paths: RuntimePaths) -> None:
    def data(model: str) -> dict[str, Any]:
        return {"models": {"default": _sol()}, "agents": {"code": {"display_name": "Code", "model": model}}}

    assert Config.validate_with_runtime(data("default@high"), runtime_paths).agents["code"].model == "default@high"
    with pytest.raises(ConfigRuntimeValidationError, match="default@xhigh"):
        Config.validate_with_runtime(data("default@xhigh"), runtime_paths)


# --- Applying a level to a route ----------------------------------------------------------------------------


def _applied(provider: str, level: str, extra_kwargs: dict[str, Any] | None = None) -> dict[str, Any]:
    levels = {"levels": [level], "default": level}
    model = ModelConfig(provider=provider, id="m", reasoning=levels, extra_kwargs=extra_kwargs)
    return apply_reasoning_level(model).extra_kwargs or {}


@pytest.mark.parametrize(
    ("provider", "level", "expected"),
    [
        ("openai", "high", {"reasoning_effort": "high"}),
        ("openai", "off", {"reasoning_effort": "none"}),
        ("codex", "xhigh", {"reasoning_effort": "xhigh"}),
        ("openai_codex", "off", {"reasoning_effort": "none"}),
        ("azure", "low", {"reasoning_effort": "low"}),
        ("openrouter", "max", {"reasoning_effort": "max"}),
        ("zai", "high", {"reasoning_effort": "high"}),
        ("zai", "off", {"extra_body": {"thinking": {"type": "disabled"}}}),
        ("llama_cpp", "medium", {"reasoning_effort": "medium"}),
        ("kimi", "high", {"reasoning_effort": "high"}),
        ("deepseek", "high", {"reasoning_effort": "high", "use_thinking": True}),
        ("deepseek", "off", {"use_thinking": False}),
        ("anthropic", "low", {"output_config": {"effort": "low"}}),
        ("vertexai_claude", "off", {"thinking": {"type": "disabled"}}),
        ("bedrock_claude", "max", {"output_config": {"effort": "max"}}),
        ("google", "minimal", {"thinking_level": "minimal"}),
        ("gemini", "off", {"thinking_budget": 0}),
        ("ollama", "high", {"think": "high"}),
        ("ollama", "off", {"think": False}),
    ],
)
def test_each_route_takes_the_level_in_its_own_key(provider: str, level: str, expected: dict[str, Any]) -> None:
    assert _applied(provider, level) == expected


def test_the_level_wins_over_the_same_key_and_keeps_every_other_one() -> None:
    assert _applied("codex", "low", {"reasoning_effort": "high", "reasoning": {"summary": "auto"}}) == {
        "reasoning_effort": "low",
        "reasoning": {"summary": "auto"},
    }
    assert _applied("anthropic", "low", {"output_config": {"effort": "high", "task_budget": 5}, "max_tokens": 9}) == {
        "output_config": {"effort": "low", "task_budget": 5},
        "max_tokens": 9,
    }
    assert _applied("deepseek", "off", {"reasoning_effort": "high"}) == {"use_thinking": False}
    assert _applied("google", "low", {"thinking_budget": 512}) == {"thinking_level": "low"}
    assert _applied("google", "off", {"thinking_level": "high", "include_thoughts": True}) == {
        "thinking_budget": 0,
        "include_thoughts": True,
    }


def test_a_claude_level_drops_the_authored_setting_it_conflicts_with() -> None:
    """``off`` drops an authored effort, which Claude rejects beside disabled thinking; a level drops disabled thinking."""
    output_config = {"effort": "xhigh", "format": {"type": "json_schema"}}
    assert _applied("anthropic", "off", {"output_config": output_config, "max_tokens": 9}) == {
        "output_config": {"format": {"type": "json_schema"}},
        "thinking": {"type": "disabled"},
        "max_tokens": 9,
    }
    assert _applied("anthropic", "off", {"output_config": {"effort": "low"}}) == {"thinking": {"type": "disabled"}}
    assert _applied("vertexai_claude", "low", {"thinking": {"type": "disabled"}, "max_tokens": 9}) == {
        "output_config": {"effort": "low"},
        "max_tokens": 9,
    }
    assert _applied("anthropic", "low", {"thinking": {"type": "enabled", "budget_tokens": 2048}}) == {
        "thinking": {"type": "enabled", "budget_tokens": 2048},
        "output_config": {"effort": "low"},
    }
    raw = {"request_params": {"thinking": {"type": "disabled"}}}
    assert _applied("anthropic", "low", raw) == {**raw, "output_config": {"effort": "low"}}


def test_zai_off_keeps_the_authored_extra_body() -> None:
    assert _applied(
        "zai",
        "off",
        {"reasoning_effort": "high", "extra_body": {"thinking": {"type": "enabled"}, "x": 1}},
    ) == {
        "extra_body": {"thinking": {"type": "enabled"}, "x": 1},
    }
    assert _applied("zai", "off", {"extra_body": {"x": 1}}) == {
        "extra_body": {"thinking": {"type": "disabled"}, "x": 1},
    }


@pytest.mark.parametrize("output_config", ["high", True])
def test_a_claude_output_config_that_is_no_mapping_fails_config_load_naming_it(
    output_config: object,
    runtime_paths: RuntimePaths,
) -> None:
    model = {"provider": "anthropic", "id": "m", "reasoning": _LEVELS, "extra_kwargs": {"output_config": output_config}}

    with pytest.raises(
        ConfigRuntimeValidationError,
        match=r"models\.claude: extra_kwargs\.output_config must be a mapping",
    ):
        Config.validate_with_runtime({"models": {"claude": model}}, runtime_paths)


def test_a_model_without_the_block_is_unchanged() -> None:
    model = ModelConfig(provider="openai", id="m", extra_kwargs={"reasoning_effort": "high"})

    assert apply_reasoning_level(model) is model


def test_a_config_without_models_still_reads_references() -> None:
    config = Config()

    assert isinstance(config.models, ModelTable)
    config.models["sol"] = ModelConfig(**_sol())
    assert config.models["sol@high"].display_name == "Sol (high)"


def test_a_thread_summary_at_a_level_drops_its_model_s_temperature(
    runtime_paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[ModelConfig] = []
    monkeypatch.setattr(
        "mindroom.providers.registry.build_model_provider",
        lambda config, _paths, model_name: built.append(config.models[model_name]),
    )
    config = _config(sol=_sol(extra_kwargs={"temperature": 0.9, "reasoning_summary": "auto"}))

    _summary_provider(config, runtime_paths, "sol@high")

    assert isinstance(built[0], ModelConfig)
    assert built[0].extra_kwargs == {"reasoning_summary": "auto"}
    assert built[0].reasoning is not None
    assert built[0].reasoning.default == "high"
