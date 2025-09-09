#!/usr/bin/env python3
"""Sync config.yaml with saas-platform/default-config.yaml.

This script ensures that config.yaml uses the same OpenRouter models
as defined in the SaaS platform default configuration, while preserving
local customizations like specific agents, rooms, and settings.
"""

import sys
from pathlib import Path
from typing import Any

import yaml


def load_yaml(path: Path) -> dict[str, Any]:
    """Load YAML file."""
    with path.open() as f:
        return yaml.safe_load(f)


def save_yaml(path: Path, data: dict[str, Any]) -> None:
    """Save YAML file."""
    with path.open("w") as f:
        yaml.dump(
            data,
            f,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
            width=120,
        )


def sync_models(local_config: dict[str, Any], default_config: dict[str, Any]) -> bool:
    """Sync model configurations from default to local config.

    Returns True if changes were made.
    """
    changed = False

    # Sync the entire models section
    if local_config.get("models") != default_config.get("models"):
        local_config["models"] = default_config["models"].copy()
        changed = True
        print("  ✓ Synced models configuration")

    # Sync router model to use default
    if "router" in local_config and local_config["router"].get("model") != "default":
        local_config["router"]["model"] = "default"
        changed = True
        print("  ✓ Updated router to use default model")

    # Sync memory LLM configuration if it exists
    if "memory" in local_config and "llm" in local_config["memory"]:
        default_memory_llm = default_config.get("memory", {}).get("llm", {})
        if local_config["memory"]["llm"] != default_memory_llm:
            local_config["memory"]["llm"] = default_memory_llm.copy()
            changed = True
            print("  ✓ Synced memory LLM configuration")

    return changed


def sync_agent_models(local_config: dict[str, Any], default_config: dict[str, Any]) -> bool:
    """Ensure all agents use valid model names.

    Returns True if changes were made.
    """
    changed = False
    valid_models = set(default_config.get("models", {}).keys())

    # Check agents
    for agent_name, agent_config in local_config.get("agents", {}).items():
        model = agent_config.get("model", "default")
        if model not in valid_models:
            # Map to a sensible default
            if "gpt" in model.lower() or "claude" in model.lower() or "sonnet" in model.lower():
                agent_config["model"] = "sonnet"
            else:
                agent_config["model"] = "default"
            changed = True
            print(f"  ✓ Updated {agent_name} model from {model} to {agent_config['model']}")

    # Check teams
    for team_name, team_config in local_config.get("teams", {}).items():
        model = team_config.get("model")
        if model and model not in valid_models:
            if "gpt" in model.lower() or "claude" in model.lower() or "sonnet" in model.lower():
                team_config["model"] = "sonnet"
            else:
                team_config["model"] = "default"
            changed = True
            print(f"  ✓ Updated {team_name} team model from {model} to {team_config['model']}")

    # Check room models
    room_models = local_config.get("room_models", {})
    for room, model in list(room_models.items()):
        if model not in valid_models:
            if "gpt" in model.lower() or "claude" in model.lower() or "sonnet" in model.lower():
                room_models[room] = "sonnet"
            else:
                room_models[room] = "default"
            changed = True
            print(f"  ✓ Updated room {room} model from {model} to {room_models[room]}")

    return changed


def main() -> int:
    """Main entry point."""
    # Define paths
    root_dir = Path(__file__).parent.parent
    local_config_path = root_dir / "config.yaml"
    default_config_path = root_dir / "saas-platform" / "default-config.yaml"

    # Check if files exist
    if not local_config_path.exists():
        print(f"❌ Local config not found: {local_config_path}", file=sys.stderr)
        return 1

    if not default_config_path.exists():
        print(f"❌ Default config not found: {default_config_path}", file=sys.stderr)
        return 1

    print("Syncing config.yaml with saas-platform/default-config.yaml...")

    # Load configurations
    local_config = load_yaml(local_config_path)
    default_config = load_yaml(default_config_path)

    # Sync configurations
    changed = False

    # Sync model configurations
    if sync_models(local_config, default_config):
        changed = True

    # Ensure all agents/teams use valid models
    if sync_agent_models(local_config, default_config):
        changed = True

    # Save if changed
    if changed:
        save_yaml(local_config_path, local_config)
        print(f"\n✅ Successfully synced {local_config_path}")
        print("\nModel configuration synced with SaaS platform defaults:")
        print("  • All models now use OpenRouter")
        print("  • Default model: google/gemini-2.5-flash")
        print("  • High-quality model: anthropic/claude-sonnet-4")
        return 0
    print("✅ Config already in sync, no changes needed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
