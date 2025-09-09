#!/usr/bin/env python3
"""Sync config.yaml models with saas-platform/default-config.yaml."""

import sys
from pathlib import Path

import yaml


def main() -> int:
    """Replace models in config.yaml with defaults from saas-platform."""
    root_dir = Path(__file__).parent.parent
    config_path = root_dir / "config.yaml"
    default_path = root_dir / "saas-platform" / "default-config.yaml"

    # Load both configs
    with config_path.open() as f:
        config = yaml.safe_load(f)
    with default_path.open() as f:
        default = yaml.safe_load(f)

    # Replace models section entirely
    if config.get("models") != default.get("models"):
        config["models"] = default["models"]

        # Also sync memory LLM and router if they exist
        if "memory" in config and "llm" in config["memory"]:
            config["memory"]["llm"] = default.get("memory", {}).get("llm", {})
        if "router" in config:
            config["router"]["model"] = "default"

        # Save
        with config_path.open("w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True, width=120)

        print("✅ Synced models")
        return 0

    print("✅ Already in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
