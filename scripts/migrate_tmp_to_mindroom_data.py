#!/usr/bin/env python
"""Migrate from old tmp/ structure to new mindroom_data/ structure.

This script migrates existing MindRoom data from the old tmp/ folder structure
to the new, more organized mindroom_data/ structure.

Old structure:
    tmp/
    ├── matrix_state.yaml
    ├── response_tracking/
    │   └── {agent}/responded_events.json
    └── {agent}.db

New structure:
    mindroom_data/
    ├── state/
    │   ├── matrix/
    │   │   └── sync_state.yaml
    │   └── agents/
    │       ├── sessions/
    │       │   └── {agent}.db
    │       └── tracking/
    │           └── {agent}/responded_events.json
"""

import shutil
import sys
from pathlib import Path


def migrate_data(old_base: Path = Path("tmp"), new_base: Path = Path("mindroom_data")) -> None:  # noqa: C901, PLR0912
    """Migrate data from old tmp/ structure to new mindroom_data/ structure.

    Args:
        old_base: Path to old tmp directory
        new_base: Path to new mindroom_data directory

    """
    if not old_base.exists():
        print(f"✓ No old data directory found at '{old_base}' - nothing to migrate")
        return

    print(f"Starting migration from '{old_base}' to '{new_base}'...")

    # Create new directory structure
    (new_base / "state" / "matrix").mkdir(parents=True, exist_ok=True)
    (new_base / "state" / "agents" / "sessions").mkdir(parents=True, exist_ok=True)
    (new_base / "state" / "agents" / "tracking").mkdir(parents=True, exist_ok=True)

    migrated_items = []

    # Migrate matrix_state.yaml -> state/matrix/sync_state.yaml
    old_matrix_state = old_base / "matrix_state.yaml"
    if old_matrix_state.exists():
        new_matrix_state = new_base / "state" / "matrix" / "sync_state.yaml"
        shutil.move(str(old_matrix_state), str(new_matrix_state))
        migrated_items.append(f"  ✓ Matrix state: {old_matrix_state} → {new_matrix_state}")

    # Migrate response_tracking/{agent}/ -> state/agents/tracking/{agent}/
    old_tracking = old_base / "response_tracking"
    if old_tracking.exists():
        for agent_dir in old_tracking.iterdir():
            if agent_dir.is_dir():
                new_agent_tracking = new_base / "state" / "agents" / "tracking" / agent_dir.name
                shutil.move(str(agent_dir), str(new_agent_tracking))
                migrated_items.append(f"  ✓ Response tracking: {agent_dir} → {new_agent_tracking}")

    # Migrate *.db files -> state/agents/sessions/
    for db_file in old_base.glob("*.db"):
        new_db_path = new_base / "state" / "agents" / "sessions" / db_file.name
        shutil.move(str(db_file), str(new_db_path))
        migrated_items.append(f"  ✓ Agent database: {db_file} → {new_db_path}")

    # Migrate any chroma/ directory (vector database)
    old_chroma = old_base / "chroma"
    if old_chroma.exists():
        new_chroma = new_base / "state" / "memory" / "chroma"
        new_chroma.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old_chroma), str(new_chroma))
        migrated_items.append(f"  ✓ Chroma database: {old_chroma} → {new_chroma}")

    # Clean up old directories if empty
    try:
        if old_tracking.exists() and not any(old_tracking.iterdir()):
            old_tracking.rmdir()
        if not any(old_base.iterdir()):
            old_base.rmdir()
            migrated_items.append(f"  ✓ Removed empty directory: {old_base}")
    except (OSError, PermissionError) as e:
        print(f"  ⚠ Could not remove old directory: {e}")

    # Print summary
    if migrated_items:
        print("\nMigration completed successfully!")
        print("Migrated items:")
        for item in migrated_items:
            print(item)
    else:
        print(f"✓ No data found to migrate in '{old_base}'")

    print(f"\n✓ New data structure ready at '{new_base}'")


def main() -> None:
    """Main entry point for migration script."""
    # Check if we're in a deployment environment
    if Path("/app/tmp").exists():
        # Docker environment
        migrate_data(Path("/app/tmp"), Path("/app/mindroom_data"))
    elif Path("deploy/instance_data").exists():
        # Deployment instances
        instance_data = Path("deploy/instance_data")
        for instance_dir in instance_data.iterdir():
            if instance_dir.is_dir():
                old_tmp = instance_dir / "tmp"
                new_data = instance_dir / "mindroom_data"
                if old_tmp.exists():
                    print(f"\n=== Migrating instance: {instance_dir.name} ===")
                    migrate_data(old_tmp, new_data)

                    # After migration, remove the old tmp directory if empty
                    try:
                        if old_tmp.exists() and not any(old_tmp.iterdir()):
                            old_tmp.rmdir()
                            print(f"  ✓ Removed empty tmp directory for instance {instance_dir.name}")
                    except (OSError, PermissionError) as e:
                        print(f"  ⚠ Could not remove tmp directory: {e}")
    else:
        # Local development
        migrate_data()


if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except Exception as e:
        print(f"❌ Migration failed: {e}", file=sys.stderr)
        sys.exit(1)
