#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml"]
# ///
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
    "gpt5nano": {
        "provider": "openai",
        "id": "gpt-5-nano-2025-08-07",
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

    # Write directly to the k8s instance directory
    target_path = root_dir / "cluster" / "k8s" / "instance" / "default-config.yaml"

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
                    "model": "gpt-5-nano-2025-08-07",
                    "temperature": 0.1,
                    "top_p": 1,
                },
            }

        # Override embedder to use OpenAI's text-embedding-3-small
        if "embedder" in config["memory"]:
            config["memory"]["embedder"] = {
                "provider": "openai",
                "config": {
                    "model": "text-embedding-3-small",
                },
            }

    # Override router to use gpt5nano model for better structured output support
    if "router" in config:
        config["router"]["model"] = "gpt5nano"

    # Save to target location
    target_path.parent.mkdir(parents=True, exist_ok=True)

    with target_path.open("w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True, width=120)

    print(f"✅ Synced config with OpenRouter models to {target_path.relative_to(root_dir)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
