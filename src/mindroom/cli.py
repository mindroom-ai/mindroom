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
    from mindroom.agent_config import Config
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
    from mindroom.agent_config import load_config
    from mindroom.matrix import load_rooms

    console.print("\n🔄 Ensuring required rooms exist...")

    config = load_config()
    existing_rooms = load_rooms()

    # Track statistics
    rooms_created = 0
    rooms_existed = 0

    for room_key in required_rooms:
        room_name = room_key.replace("_", " ").title()

        # Create room if it doesn't exist
        if room_key not in existing_rooms:
            room_id = await _create_room_simple(room_key, room_name, client, config)
            if room_id:
                rooms_created += 1
                # Reload rooms to include the newly created one
                existing_rooms = load_rooms()
            else:
                console.print(f"❌ Failed to create room {room_key}")
        else:
            rooms_existed += 1

    console.print(f"\n📊 Room setup: {rooms_created} created, {rooms_existed} already existed")


async def _create_room_simple(
    room_key: str,
    room_name: str,
    client: nio.AsyncClient,
    config: Config,
) -> str | None:
    """Create a room.

    With the new self-managing agent pattern, agents will join rooms themselves.
    This function only creates the room with appropriate power levels.
    """
    from mindroom.agent_config import get_agent_ids_for_room
    from mindroom.matrix import add_room, create_room

    # Get all agents for this room to grant power levels
    power_users = get_agent_ids_for_room(room_key, config, client.homeserver)

    # Create room with power levels
    room_id = await create_room(
        client=client,
        name=room_name,
        alias=room_key,
        topic=f"Mindroom {room_name}",
        power_users=power_users,
    )

    if room_id:
        # Save room info
        add_room(room_key, room_id, f"#{room_key}:{SERVER_NAME}", room_name)
        console.print(f"   ✅ Created room {room_name}")
        console.print("   ℹ️  Agents will join automatically when they start")
        return room_id

    console.print(f"❌ Failed to create room {room_name}")
    return None


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


@app.command()
def info():
    """Show current system status."""
    state = MatrixState.load()
    if not state.accounts and not state.rooms:
        console.print("❌ No configuration found. Run: mindroom run")
        return

    console.print("🔑 Mindroom Status")
    console.print("=" * 40)

    # User info
    user_account = state.get_account("user")
    if user_account:
        console.print(f"\n👤 User: @{user_account.username}:{SERVER_NAME}")

    # Agent info
    agent_accounts = [(key, acc) for key, acc in state.accounts.items() if key.startswith("agent_")]
    if agent_accounts:
        console.print(f"\n🤖 Agents: {len(agent_accounts)} registered")
        for key, account in agent_accounts:
            agent_name = key.replace("agent_", "")
            console.print(f"  • {agent_name}: @{account.username}:{SERVER_NAME}")

    # Room info
    if state.rooms:
        console.print(f"\n🏠 Rooms: {len(state.rooms)} created")
        for _room_key, room in state.rooms.items():
            console.print(f"  • {room.name} ({room.alias})")

    console.print(f"\n🌐 Server: {HOMESERVER}")


@app.command()
def create_room(
    room_alias: str = typer.Argument(..., help="Room alias (e.g., 'testing', 'dev2')"),
    room_name: str = typer.Option(None, help="Display name for the room"),
) -> None:
    """Create a new room. Agents will join automatically when they start."""
    asyncio.run(_create_room(room_alias, room_name))


async def _create_room(room_alias: str, room_name: str | None) -> None:
    """Create a room implementation."""
    if room_name is None:
        room_name = room_alias.replace("_", " ").title()

    # Ensure we have a user account
    state = MatrixState.load()
    user_account = state.get_account("user")
    if not user_account:
        console.print("❌ No user account found. Run: mindroom run")
        return

    username = f"@{user_account.username}:{SERVER_NAME}"
    password = user_account.password

    async with matrix_client(HOMESERVER, username) as client:
        response = await client.login(password=password)
        if isinstance(response, nio.LoginResponse):
            from mindroom.agent_config import load_config

            config = load_config()
            room_id = await _create_room_simple(room_alias, room_name, client, config)
            if room_id:
                console.print(f"\n✅ Room created successfully: {room_id}")
                console.print("ℹ️  Agents will join automatically when they start")
        else:
            console.print(f"❌ Failed to login: {response}")


@app.command()
def room_info(room_id: str = typer.Argument(..., help="Room ID or alias to get info about")) -> None:
    """Get information about a room (replaces invite_agents command).

    With the new self-managing agent pattern, agents handle their own room
    memberships. Use this command to check room status.
    """
    asyncio.run(_room_info(room_id))


async def _room_info(room_id: str) -> None:
    """Get room information."""
    state = MatrixState.load()
    user_account = state.get_account("user")
    if not user_account:
        console.print("❌ No user account found. Run: mindroom run")
        return

    username = f"@{user_account.username}:{SERVER_NAME}"
    password = user_account.password

    async with matrix_client(HOMESERVER, username) as client:
        response = await client.login(password=password)
        if isinstance(response, nio.LoginResponse):
            # Get room members
            members_response = await client.joined_members(room_id)
            if isinstance(members_response, nio.JoinedMembersResponse):
                console.print(f"\n🏠 Room: {room_id}")
                console.print(f"👥 Members: {len(members_response.members)}")

                agents = []
                users = []
                for user_id in members_response.members:
                    if user_id.startswith("@mindroom_"):
                        agents.append(user_id)
                    else:
                        users.append(user_id)

                if agents:
                    console.print("\n🤖 Agents:")
                    for agent_id in sorted(agents):
                        console.print(f"  • {agent_id}")

                if users:
                    console.print("\n👤 Users:")
                    for user_id in sorted(users):
                        console.print(f"  • {user_id}")

                console.print("\nℹ️  Agents manage their own room memberships automatically")
            else:
                console.print(f"❌ Failed to get room info: {members_response}")
        else:
            console.print(f"❌ Failed to login: {response}")


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
