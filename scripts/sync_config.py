#!/usr/bin/env python3
"""Sync config.yaml to saas-platform, but override models with OpenRouter."""

import sys
from pathlib import Path

import yaml

# OpenRouter models to use in SaaS platform
SAAS_MODELS = {
    "default": {
        "provider": "openrouter",
        "id": "google/gemini-2.5-flash",
    },
    "sonnet": {
        "provider": "openrouter",
        "id": "anthropic/claude-sonnet-4",
    },
    "deepseek": {
        "provider": "openrouter",
        "id": "deepseek/deepseek-chat-v3.1:free",
    },
    "gemini_flash": {
        "provider": "openrouter",
        "id": "google/gemini-2.5-flash",
    },
    "glm45": {
        "provider": "openrouter",
        "id": "z-ai/glm-4.5",
    },
    # Map any other model names to OpenRouter equivalents
    "gpt5mini": {
        "provider": "openrouter",
        "id": "google/gemini-2.5-flash",
    },
    "haiku": {
        "provider": "openrouter",
        "id": "google/gemini-2.5-flash",
    },
    "opus": {
        "provider": "openrouter",
        "id": "anthropic/claude-sonnet-4",
    },
    "gpt4o": {
        "provider": "openrouter",
        "id": "anthropic/claude-sonnet-4",
    },
    "gpt_oss_120b": {
        "provider": "openrouter",
        "id": "google/gemini-2.5-flash",
    },
}


def main() -> int:
    """Copy entire config but override models for SaaS."""
    root_dir = Path(__file__).parent.parent
    source_path = root_dir / "config.yaml"
    target_path = root_dir / "saas-platform" / "default-config.yaml"

    # Load source config
    with source_path.open() as f:
        config = yaml.safe_load(f)

    # Override models with OpenRouter versions
    config["models"] = SAAS_MODELS

    # Override memory LLM to use OpenRouter
    if "memory" in config and "llm" in config["memory"]:
        config["memory"]["llm"] = {
            "provider": "openrouter",
            "config": {
                "model": "google/gemini-2.5-flash",
                "temperature": 0.1,
                "top_p": 1,
            },
        }

    # Override router to use default model
    if "router" in config:
        config["router"]["model"] = "default"

    # Save to target
    with target_path.open("w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True, width=120)

    print(f"✅ Synced config with OpenRouter models to {target_path.relative_to(root_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
