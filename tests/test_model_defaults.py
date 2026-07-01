"""Tests for centralized default model identifiers."""

from __future__ import annotations

from pathlib import Path

import yaml

from mindroom import model_defaults


def test_default_model_strings_are_not_redeclared_in_source() -> None:
    """Runtime code should import default model IDs from mindroom.model_defaults."""
    repo_root = Path(__file__).resolve().parents[1]
    model_defaults_path = repo_root / "src" / "mindroom" / "model_defaults.py"
    ignored_paths = {
        model_defaults_path,
        repo_root / "src" / "mindroom" / "tools_metadata.json",
    }
    model_strings = {
        value
        for name, value in vars(model_defaults).items()
        if name.isupper()
        if isinstance(value, str) and any(marker in value for marker in ("-", ".", "/", ":"))
    }

    offenders: list[str] = []
    for root in (repo_root / "src" / "mindroom", repo_root / "scripts"):
        for path in root.rglob("*"):
            if path in ignored_paths or path.suffix not in {".py", ".yaml", ".yml", ".json"}:
                continue
            text = path.read_text(encoding="utf-8")
            offenders.extend(
                f"{path.relative_to(repo_root)} contains {model_string!r}"
                for model_string in model_strings
                if model_string in text
            )

    assert offenders == []


def test_saas_default_config_models_match_central_defaults() -> None:
    """The generated Helm default config should stay in sync with the central SaaS model defaults."""
    repo_root = Path(__file__).resolve().parents[1]
    config_path = repo_root / "cluster" / "k8s" / "instance" / "default-config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert config["models"] == {
        name: preset.to_config_dict() for name, preset in model_defaults.SAAS_MODEL_PRESETS.items()
    }
    assert config["memory"]["llm"]["config"]["model"] == model_defaults.OPENAI_GPT_NANO
    assert config["memory"]["embedder"]["config"]["model"] == model_defaults.OPENAI_EMBEDDING_SMALL
    assert config["voice"]["stt"]["model"] == model_defaults.OPENAI_TRANSCRIPTION


def test_saas_default_uses_current_gemini_flash() -> None:
    """The SaaS default and named Flash preset should use the current Gemini Flash model."""
    expected_model = "google/gemini-3.5-flash"
    old_preview_model = "google/gemini-3-flash-preview"

    assert model_defaults.SAAS_MODEL_PRESETS["default"].id == expected_model
    assert model_defaults.SAAS_MODEL_PRESETS["gemini_flash"].id == expected_model
    assert old_preview_model not in {preset.id for preset in model_defaults.SAAS_MODEL_PRESETS.values()}


def test_sonnet_presets_use_current_generation() -> None:
    """Sonnet presets should track the current provider-specific Sonnet generation."""
    bedrock_alternatives = dict(model_defaults.CONFIG_INIT_MODEL_ALTERNATIVES["bedrock_claude"])

    assert model_defaults.CONFIG_INIT_MODEL_PRESETS["anthropic"].id == "claude-sonnet-5"
    assert model_defaults.CONFIG_INIT_MODEL_PRESETS["vertexai_claude"].id == "claude-sonnet-5"
    assert model_defaults.CONFIG_INIT_MODEL_PRESETS["openrouter"].id == "anthropic/claude-sonnet-5"
    assert model_defaults.SAAS_MODEL_PRESETS["sonnet"].id == "anthropic/claude-sonnet-5"
    assert bedrock_alternatives["sonnet"].id == "global.anthropic.claude-sonnet-5"
    assert "claude-sonnet-4-6" not in {
        model_defaults.CONFIG_INIT_MODEL_PRESETS["anthropic"].id,
        model_defaults.CONFIG_INIT_MODEL_PRESETS["vertexai_claude"].id,
        model_defaults.CONFIG_INIT_MODEL_PRESETS["openrouter"].id,
        model_defaults.SAAS_MODEL_PRESETS["sonnet"].id,
        bedrock_alternatives["sonnet"].id,
    }


def test_config_init_openrouter_alternatives_cover_saas_openrouter_models() -> None:
    """Config init should comment every non-default OpenRouter model alias we expose elsewhere."""
    openrouter_saas_aliases = {
        name
        for name, preset in model_defaults.SAAS_MODEL_PRESETS.items()
        if preset.provider == "openrouter" and name not in {"default", "sonnet"}
    }
    openrouter_alternative_aliases = {
        name for name, _preset in model_defaults.CONFIG_INIT_MODEL_ALTERNATIVES["openrouter"]
    }

    assert openrouter_alternative_aliases == openrouter_saas_aliases
