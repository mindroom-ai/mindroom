"""Mindroom CLI - Simplified multi-agent Matrix bot system."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import nio
import typer
from rich.console import Console

from mindroom.matrix import (
    MATRIX_HOMESERVER,
    MatrixState,
    extract_server_name_from_homeserver,
    matrix_client,
)

if TYPE_CHECKING:
    pass
app = typer.Typer(help="Mindroom: Multi-agent Matrix bot system")
console = Console()

HOMESERVER = MATRIX_HOMESERVER or "http://localhost:8008"
SERVER_NAME = extract_server_name_from_homeserver(HOMESERVER)


async def _ensure_user_account() -> MatrixState:
    """Ensure a user account exists, creating one if necessary."""
    state = MatrixState.load()

    user_account = state.get_account("user")

    # If we have stored credentials, try to login first
    if user_account:
        async with matrix_client(HOMESERVER, f"@{user_account.username}:{SERVER_NAME}") as client:
            response = await client.login(password=user_account.password)
            if isinstance(response, nio.LoginResponse):
                console.print(f"✅ User account ready: @{user_account.username}:{SERVER_NAME}")
                return state
            else:
                console.print("⚠️  Stored credentials invalid, creating new account...")
                state.accounts.pop("user", None)

    # No valid account, create a new one
    console.print("📝 Creating user account...")

    # Generate credentials
    user_username = "mindroom_user"
    user_password = f"mindroom_password_{os.urandom(16).hex()}"

    # Register user
    from mindroom.matrix.client import register_user

    try:
        user_id = await register_user(HOMESERVER, user_username, user_password, "Mindroom User")
        console.print(f"✅ Registered user: {user_id}")
    except ValueError as e:
        error_msg = str(e)
        if "M_USER_IN_USE" in error_msg:
            console.print(f"ℹ️  User @{user_username}:{SERVER_NAME} already exists")
        else:
            console.print(f"❌ Failed to register {user_username}: {error_msg}")

    # Save credentials
    state.add_account("user", user_username, user_password)
    state.save()

    console.print(f"✅ User account ready: @{user_username}:{SERVER_NAME}")

    return state


async def _ensure_rooms_exist(client: nio.AsyncClient, required_rooms: set[str]) -> None:
    """Ensure all required rooms exist.

    With the new self-managing agent pattern, agents handle their own room
    memberships. This function only ensures the rooms exist.
    """
    from mindroom.agent_config import get_agent_ids_for_room, load_config
    from mindroom.matrix import ensure_room_exists, load_rooms

    console.print("\n🔄 Ensuring required rooms exist...")

    config = load_config()
    existing_rooms = load_rooms()

    # Track statistics
    rooms_created = 0
    rooms_existed = 0

    for room_key in required_rooms:
        # Skip if this is a room ID (starts with !)
        if room_key.startswith("!"):
            continue

        # Check if room already exists
        if room_key in existing_rooms:
            rooms_existed += 1
            continue

        # Get power users for this room
        power_users = get_agent_ids_for_room(room_key, config, client.homeserver)

        # Create the room using the shared function
        room_id = await ensure_room_exists(
            client=client,
            room_key=room_key,
            power_users=power_users,
        )

        if room_id:
            rooms_created += 1
            console.print(f"   ✅ Created room {room_key.replace('_', ' ').title()}")
            console.print("   ℹ️  Agents will join automatically when they start")
        else:
            console.print(f"❌ Failed to create room {room_key}")

    console.print(f"\n📊 Room setup: {rooms_created} created, {rooms_existed} already existed")


@app.command()
def version():
    """Show the current version of Mindroom."""
    from mindroom import __version__

    console.print(f"Mindroom version: [bold]{__version__}[/bold]")
    console.print("Multi-agent Matrix bot system")


@app.command()
def run(
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        "-l",
        help="Set the logging level (DEBUG, INFO, WARNING, ERROR)",
        case_sensitive=False,
    ),
    storage_path: Path = typer.Option(  # noqa: B008
        Path("tmp"),
        "--storage-path",
        "-s",
        help="Base directory for storing agent data (response tracking, etc.)",
    ),
) -> None:
    """Run the mindroom multi-agent system.

    This command automatically:
    - Creates a user account if needed
    - Creates all agent accounts
    - Creates all rooms defined in config.yaml
    - Starts the multi-agent system
    """
    asyncio.run(_run(log_level=log_level.upper(), storage_path=storage_path))


async def _run(log_level: str, storage_path: Path) -> None:
    """Run the multi-agent system with automatic setup."""
    from mindroom.agent_config import load_config
    from mindroom.bot import main

    console.print(f"🚀 Starting Mindroom multi-agent system (log level: {log_level})...\n")

    # Ensure we have a user account
    state = await _ensure_user_account()

    # Load agent configuration and collect required rooms
    agent_config = load_config()
    required_rooms = set(room for agent_cfg in agent_config.agents.values() for room in agent_cfg.rooms)

    # Handle room creation and agent invitations if needed
    if required_rooms:
        # Login as user for room operations
        user_account = state.get_account("user")
        if not user_account:
            console.print("❌ No user account found")
            sys.exit(1)

        username = f"@{user_account.username}:{SERVER_NAME}"
        password = user_account.password

        async with matrix_client(HOMESERVER, username) as client:
            response = await client.login(password=password)
            if isinstance(response, nio.LoginResponse):
                # Create missing rooms and ensure all agents are invited
                await _ensure_rooms_exist(client, required_rooms)
            else:
                console.print(f"❌ Failed to login: {response}")
                sys.exit(1)

    # Agent accounts are created automatically by the bot system
    console.print("\n🤖 Starting agents...")
    console.print("Press Ctrl+C to stop\n")

    try:
        await main(log_level=log_level, storage_path=storage_path)
    except KeyboardInterrupt:
        console.print("\n✋ Stopped")


def main():
    """Main entry point that shows help by default."""
    import sys

    # Handle -h flag by replacing with --help
    for i, arg in enumerate(sys.argv):
        if arg == "-h":
            sys.argv[i] = "--help"
            break

    # If no arguments provided, show help
    if len(sys.argv) == 1:
        # Show help by appending --help to argv
        sys.argv.append("--help")

    app()


if __name__ == "__main__":
    main()
