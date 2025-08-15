#!/usr/bin/env uv run
"""Set avatars for existing Matrix rooms.

This script:
1. Connects to the Matrix server
2. Finds all configured rooms
3. Sets avatars for rooms that have avatar files

Usage:
    uv run scripts/set_room_avatars.py

Requires:
    - Matrix server running
    - Room avatar files in avatars/rooms/
"""
# /// script
# dependencies = [
#   "pyyaml",
#   "matrix-nio",
#   "python-dotenv",
#   "rich",
# ]
# ///

import asyncio

# Add the src directory to the path
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
from rich.console import Console

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mindroom.constants import MATRIX_HOMESERVER
from mindroom.matrix.client import check_and_set_room_avatar
from mindroom.matrix.rooms import get_room_id
from mindroom.matrix.users import login_admin_user

console = Console()

# Load environment variables from .env file
load_dotenv()


def get_project_root() -> Path:
    """Get the project root directory."""
    return Path(__file__).parent.parent


def load_config() -> dict:
    """Load the configuration from config.yaml."""
    config_path = get_project_root() / "config.yaml"
    with config_path.open() as f:
        return yaml.safe_load(f)


async def set_room_avatars() -> None:
    """Set avatars for all configured rooms."""
    config = load_config()

    # Get all unique rooms from agents
    all_rooms = set()
    agents = config.get("agents", {})
    for agent_data in agents.values():
        rooms = agent_data.get("rooms", [])
        all_rooms.update(rooms)

    if not all_rooms:
        console.print("[yellow]No rooms found in configuration[/yellow]")
        return

    console.print(f"[cyan]Found {len(all_rooms)} rooms in configuration[/cyan]")

    # Login as admin
    console.print("\n[yellow]Logging in as admin...[/yellow]")
    client = await login_admin_user(MATRIX_HOMESERVER)
    if not client:
        console.print("[red]Failed to login as admin[/red]")
        return

    console.print("[green]✓ Logged in successfully[/green]")

    # Process each room
    avatars_dir = get_project_root() / "avatars" / "rooms"
    success_count = 0
    skip_count = 0
    fail_count = 0

    for room_name in sorted(all_rooms):
        avatar_path = avatars_dir / f"{room_name}.png"

        if not avatar_path.exists():
            console.print(f"[dim]⊘ No avatar file for room '{room_name}'[/dim]")
            skip_count += 1
            continue

        # Get room ID
        room_id = get_room_id(room_name)
        if not room_id:
            console.print(f"[yellow]⚠ Room '{room_name}' not found in Matrix state[/yellow]")
            fail_count += 1
            continue

        # Set avatar
        console.print(f"[yellow]Setting avatar for room '{room_name}'...[/yellow]")
        if await check_and_set_room_avatar(client, room_id, avatar_path):
            console.print(f"[green]✓ Set avatar for room '{room_name}'[/green]")
            success_count += 1
        else:
            console.print(f"[red]✗ Failed to set avatar for room '{room_name}'[/red]")
            fail_count += 1

    # Close client
    await client.close()

    # Summary
    console.print("\n[bold]Summary:[/bold]")
    console.print(f"  [green]✓ Success: {success_count}[/green]")
    console.print(f"  [dim]⊘ Skipped: {skip_count}[/dim]")
    if fail_count > 0:
        console.print(f"  [red]✗ Failed: {fail_count}[/red]")


async def main() -> None:
    """Main entry point."""
    console.print("[bold cyan]Room Avatar Setter[/bold cyan]\n")

    try:
        await set_room_avatars()
        console.print("\n[bold green]✨ Done![/bold green]")
    except Exception as e:
        console.print(f"\n[red]Error: {e}[/red]")
        raise


if __name__ == "__main__":
    asyncio.run(main())
