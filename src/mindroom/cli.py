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
    MatrixID,
    MatrixState,
    extract_server_name_from_homeserver,
    invite_to_room,
    matrix_client,
)

if TYPE_CHECKING:
    from mindroom.agent_config import Config
app = typer.Typer(help="Mindroom: Multi-agent Matrix bot system")
console = Console()

HOMESERVER = MATRIX_HOMESERVER or "http://localhost:8008"
SERVER_NAME = extract_server_name_from_homeserver(HOMESERVER)


async def _ensure_agent_in_room(
    client: nio.AsyncClient,
    room_id: str,
    room_key: str,
    agent_id: str,
    room_members: set[str],
    agent_name: str | None = None,
) -> tuple[int, int, int]:  # Returns (successful_invites, already_in_room, failed_invites)
    """Ensure an agent is in a room, returning invitation statistics."""
    display_name = agent_name or agent_id

    if agent_id not in room_members:
        success = await invite_to_room(client, room_id, agent_id)
        if success:
            return (1, 0, 0)  # successful_invites=1
        else:
            return (0, 0, 1)  # failed_invites=1
    else:
        console.print(f"✓ {display_name} already in {room_key}")
        return (0, 1, 0)  # already_in_room=1


async def _create_room_and_invite_agents(room_key: str, room_name: str, user_client: nio.AsyncClient) -> str | None:
    """Create a room and invite all configured agents."""
    from mindroom.agent_config import get_agent_ids_for_room, load_config
    from mindroom.matrix import add_room, create_room, invite_to_room

    config = load_config()
    power_users = get_agent_ids_for_room(room_key, config, user_client.homeserver)

    # Create room with power levels
    room_id = await create_room(
        client=user_client,
        name=room_name,
        alias=room_key,
        topic=f"Mindroom {room_name}",
        power_users=power_users,
    )

    if room_id:
        # Save room info
        add_room(room_key, room_id, f"#{room_key}:{SERVER_NAME}", room_name)

        invited_count = 0

        # Always invite the router first
        router_id = MatrixID.from_agent("router", SERVER_NAME).full_id
        success = await invite_to_room(user_client, room_id, router_id)
        if success:
            invited_count += 1
            console.print(f"  ✅ Invited router to {room_name}")
        else:
            console.print("  ❌ Failed to invite router")

        # Invite agents based on config.yaml
        from mindroom.agent_config import load_config

        config = load_config()

        for agent_name, agent_config in config.agents.items():
            if room_key in agent_config.rooms:
                agent_id = MatrixID.from_agent(agent_name, SERVER_NAME).full_id
                invite_response = await user_client.room_invite(room_id, agent_id)
                if isinstance(invite_response, nio.RoomInviteResponse):
                    invited_count += 1
                # Ignore failures - agent might not exist yet

        if invited_count > 0:
            console.print(f"   Invited {invited_count} agents to the room")

        return room_id
    console.print(f"❌ Failed to create room {room_name}")
    return None


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


async def _ensure_rooms_and_agents(client: nio.AsyncClient, required_rooms: set[str]) -> None:
    """Ensure all required rooms exist and all agents (including router) are invited."""
    from mindroom.agent_config import load_config
    from mindroom.matrix import get_room_members, load_rooms

    console.print("\n🔄 Setting up rooms and agent access...")

    config = load_config()
    existing_rooms = load_rooms()

    # Track statistics
    rooms_created = 0
    successful_invites = 0
    failed_invites = 0
    already_in_room = 0

    for room_key in required_rooms:
        room_name = room_key.replace("_", " ").title()

        # Create room if it doesn't exist
        if room_key not in existing_rooms:
            room_id = await _create_room_and_invite_all_agents(room_key, room_name, client, config)
            if room_id:
                rooms_created += 1
                # Reload rooms to include the newly created one
                existing_rooms = load_rooms()
            else:
                console.print(f"❌ Failed to create room {room_key}, skipping agent invites")
                continue

        # Ensure all agents are in the room (for existing rooms)
        if room_key in existing_rooms:
            room = existing_rooms[room_key]
            room_members = await get_room_members(client, room.room_id)

            # Always invite router to ALL rooms first
            router_id = MatrixID.from_agent("router", SERVER_NAME).full_id
            s, a, f = await _ensure_agent_in_room(client, room.room_id, room_key, router_id, room_members, "Router")
            successful_invites += s
            already_in_room += a
            failed_invites += f

            # Invite configured agents to their assigned rooms
            for agent_name, agent_config in config.agents.items():
                if room_key not in agent_config.rooms:
                    continue

                agent_id = MatrixID.from_agent(agent_name, SERVER_NAME).full_id
                s, a, f = await _ensure_agent_in_room(client, room.room_id, room_key, agent_id, room_members, agent_id)
                successful_invites += s
                already_in_room += a
                failed_invites += f

    console.print(
        f"\n📊 Setup summary: {rooms_created} rooms created, {successful_invites} invited, "
        f"{already_in_room} already present, {failed_invites} failed"
    )


async def _invite_agent_to_room(
    client: nio.AsyncClient, room_id: str, agent_id: str, agent_name: str | None = None
) -> bool:
    """Invite a single agent to a room.

    Returns True if invitation was successful, False otherwise.
    """
    response = await client.room_invite(room_id, agent_id)

    display_name = agent_name or agent_id
    if isinstance(response, nio.RoomInviteResponse):
        console.print(f"  ✅ Invited {display_name}")
        return True
    else:
        console.print(f"  ❌ Failed to invite {display_name}: {response}")
        return False


async def _invite_agents_from_config(
    client: nio.AsyncClient, room_id: str, room_key: str, config: Config, include_router: bool = True
) -> int:
    """Invite agents to a room based on config.yaml room assignments.

    Returns the number of agents successfully invited.
    """
    invited_count = 0

    # Always invite the router first if requested
    if include_router:
        router_id = MatrixID.from_agent("router", SERVER_NAME).full_id
        if await _invite_agent_to_room(client, room_id, router_id, "router"):
            invited_count += 1

    # Invite configured agents
    for agent_name, agent_cfg in config.agents.items():
        if room_key in agent_cfg.rooms:
            agent_id = MatrixID.from_agent(agent_name, SERVER_NAME).full_id
            if await _invite_agent_to_room(client, room_id, agent_id, agent_name):
                invited_count += 1

    return invited_count


async def _invite_all_agents_to_room(
    client: nio.AsyncClient, room_id: str, state: MatrixState, include_router: bool = True
) -> int:
    """Invite all available agents to a room.

    Returns the number of agents successfully invited.
    """
    invited_count = 0

    # Always invite the router first if requested
    if include_router:
        router_id = MatrixID.from_agent("router", SERVER_NAME).full_id
        if await _invite_agent_to_room(client, room_id, router_id, "router"):
            invited_count += 1

    # Invite all other agents
    for key, account in state.accounts.items():
        if key.startswith("agent_"):
            agent_id = f"@{account.username}:{SERVER_NAME}"
            if await _invite_agent_to_room(client, room_id, agent_id):
                invited_count += 1

    return invited_count


async def _create_room_and_invite_all_agents(
    room_key: str,
    room_name: str,
    client: nio.AsyncClient,
    config: Config,
) -> str | None:
    """Create a room and invite router + all configured agents in one go."""
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

        # Invite agents based on config
        invited_count = await _invite_agents_from_config(client, room_id, room_key, config)

        if invited_count > 0:
            console.print(f"   Invited {invited_count} agents to the room")

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
                await _ensure_rooms_and_agents(client, required_rooms)
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
    """Create a new room and invite relevant agents."""
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
            await _create_room_and_invite_agents(room_alias, room_name, client)
        else:
            console.print(f"❌ Failed to login: {response}")


@app.command()
def invite_agents(room_id: str = typer.Argument(..., help="Room ID to invite agents to")) -> None:
    """Invite agents to an existing room."""
    asyncio.run(_invite_agents(room_id))


async def _invite_agents(room_id: str) -> None:
    """Invite agents to a room."""
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
            # Get room name/alias for agent selection
            from mindroom.matrix import load_rooms

            rooms = load_rooms()
            room_key = None
            for key, room in rooms.items():
                if room.room_id == room_id:
                    room_key = key
                    break

            if room_key:
                # Invite agents based on configuration
                from mindroom.agent_config import load_config

                agent_config = load_config()

                invited_count = await _invite_agents_from_config(client, room_id, room_key, agent_config)
                console.print(f"\n✨ Invited {invited_count} agents to room")
            else:
                # Invite all agents if room not in config
                invited_count = await _invite_all_agents_to_room(client, room_id, state)
                console.print(f"\n✨ Invited {invited_count} agents to room")
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
