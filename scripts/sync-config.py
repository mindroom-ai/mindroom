#!/usr/bin/env python3
"""Sync models from config.yaml to saas-platform/default-config.yaml."""

import sys
from pathlib import Path

import yaml


def main() -> int:
    """Copy models from root config.yaml to saas-platform default."""
    root_dir = Path(__file__).parent.parent
    source_path = root_dir / "config.yaml"
    target_path = root_dir / "saas-platform" / "default-config.yaml"

    # Load both configs
    with source_path.open() as f:
        source = yaml.safe_load(f)
    with target_path.open() as f:
        target = yaml.safe_load(f)

    # Copy models from source to target
    if target.get("models") != source.get("models"):
        target["models"] = source["models"]
        
        # Also sync memory LLM and router if they exist
        if "memory" in source and "llm" in source["memory"]:
            if "memory" not in target:
                target["memory"] = {}
            target["memory"]["llm"] = source["memory"]["llm"]
        if "router" in source:
            if "router" not in target:
                target["router"] = {}
            target["router"]["model"] = source["router"]["model"]

        # Save
        with target_path.open("w") as f:
            yaml.dump(target, f, default_flow_style=False, sort_keys=False, allow_unicode=True, width=120)

        print(f"✅ Synced models from {source_path.name} to {target_path.relative_to(root_dir)}")
        return 0

    print("✅ Already in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())