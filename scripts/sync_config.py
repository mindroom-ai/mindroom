#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml"]
# ///
"""Sync config.yaml to saas-platform, but override models with OpenRouter."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import yaml

ROOT_DIR = Path(__file__).parent.parent
_MODEL_DEFAULTS_PATH = ROOT_DIR / "src" / "mindroom" / "model_defaults.py"


def _load_model_defaults() -> ModuleType:
    """Load model defaults without importing the full mindroom package in the uv-script environment."""
    spec = importlib.util.spec_from_file_location("_mindroom_model_defaults", _MODEL_DEFAULTS_PATH)
    if spec is None or spec.loader is None:
        msg = f"Could not load model defaults from {_MODEL_DEFAULTS_PATH}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


model_defaults = _load_model_defaults()
SAAS_MODELS = {name: preset.to_config_dict() for name, preset in model_defaults.SAAS_MODEL_PRESETS.items()}


def main() -> int:
    """Copy entire config but override models for SaaS."""
    source_path = ROOT_DIR / "config.yaml"

    # Write directly to the k8s instance directory
    target_path = ROOT_DIR / "cluster" / "k8s" / "instance" / "default-config.yaml"

    # Load source config
    with source_path.open() as f:
        config = yaml.safe_load(f)

    # Override models with OpenRouter versions
    config["models"] = SAAS_MODELS

    # Remove sleepy_paws agent for SaaS
    if "agents" in config and "sleepy_paws" in config["agents"]:
        del config["agents"]["sleepy_paws"]

    # Override memory configuration for SaaS
    if "memory" in config:
        # Override LLM to use OpenAI (mem0 doesn't support OpenRouter)
        if "llm" in config["memory"]:
            config["memory"]["llm"] = {
                "provider": "openai",
                "config": {
                    "model": model_defaults.OPENAI_GPT_NANO,
                    "temperature": 0.1,
                    "top_p": 1,
                },
            }

        # Override embedder to use OpenAI's default embedding model.
        if "embedder" in config["memory"]:
            config["memory"]["embedder"] = {
                "provider": "openai",
                "config": {
                    "model": model_defaults.OPENAI_EMBEDDING_SMALL,
                },
            }

    # Override router to use gpt5nano model for better structured output support
    if "router" in config:
        config["router"]["model"] = "gpt5nano"

    if "voice" in config and "stt" in config["voice"]:
        config["voice"]["stt"]["model"] = model_defaults.OPENAI_TRANSCRIPTION

    # Save to target location
    target_path.parent.mkdir(parents=True, exist_ok=True)

    with target_path.open("w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True, width=120)

    print(f"✅ Synced config with OpenRouter models to {target_path.relative_to(ROOT_DIR)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
