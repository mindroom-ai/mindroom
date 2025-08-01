"""Mindroom CLI - Simplified multi-agent Matrix bot system."""

import asyncio
import os
import sys
from pathlib import Path
from typing import NamedTuple

import nio
import typer
from rich.console import Console

from mindroom.matrix import MATRIX_HOMESERVER, MatrixConfig, matrix_client

app = typer.Typer(help="Mindroom: Multi-agent Matrix bot system")
console = Console()

HOMESERVER = MATRIX_HOMESERVER or "http://localhost:8008"


class InviteResult(NamedTuple):
    """Result of inviting an agent to a room."""

    success: bool
    already_member: bool


async def _register_user(username: str, password: str, display_name: str) -> None:
    """Register a new Matrix user."""
    async with matrix_client(HOMESERVER) as client:
        # Try to register
        response = await client.register(
            username=username,
            password=password,
            device_name="mindroom-cli",
        )

        if isinstance(response, nio.RegisterResponse):
            # Set display name - use the user_id from the response
            client.user = response.user_id
            client.access_token = response.access_token
            client.device_id = response.device_id
            # No need to login again - we already have access token from registration
            await client.set_displayname(display_name)
            console.print(f"✅ Registered user: @{username}:localhost")
        elif isinstance(response, nio.responses.RegisterErrorResponse) and response.status_code == "M_USER_IN_USE":
            console.print(f"ℹ️  User @{username}:localhost already exists")
        else:
            console.print(f"❌ Failed to register {username}: {response}")


async def _get_room_members(client: nio.AsyncClient, room_id: str, room_key: str) -> set[str]:
    """Get the current members of a room."""
    try:
        members_response = await client.joined_members(room_id)
        if isinstance(members_response, nio.JoinedMembersResponse):
            # members is a list of RoomMember objects
            return set(member.user_id for member in members_response.members)
        else:
            console.print(f"⚠️  Could not check members for {room_key}")
            return set()
    except Exception as e:
        console.print(f"⚠️  Error checking members for {room_key}: {e}")
        return set()


async def _invite_agent_to_room(client: nio.AsyncClient, room_id: str, room_key: str, agent_id: str) -> InviteResult:
    """Invite an agent to a room. Returns InviteResult."""
    try:
        response = await client.room_invite(room_id, agent_id)
        if isinstance(response, nio.RoomInviteResponse):
            console.print(f"✅ Invited {agent_id} to {room_key}")
            return InviteResult(success=True, already_member=False)
        else:
            console.print(f"❌ Failed to invite {agent_id} to {room_key}: {response}")
            return InviteResult(success=False, already_member=False)
    except Exception as e:
        console.print(f"❌ Error inviting {agent_id} to {room_key}: {e}")
        return InviteResult(success=False, already_member=False)


async def _ensure_agents_in_rooms(client: nio.AsyncClient, required_rooms: set[str]) -> None:
    """Ensure all agents are invited to their configured rooms."""
    from mindroom.agent_loader import load_config
    from mindroom.matrix import load_rooms

    console.print("\n🔄 Checking agent room access...")

    config = load_config()
    existing_rooms = load_rooms()

    successful_invites = 0
    failed_invites = 0
    already_in_room = 0

    for room_key in required_rooms:
        if room_key not in existing_rooms:
            continue

        room = existing_rooms[room_key]
        room_members = await _get_room_members(client, room.room_id, room_key)

        # Check each agent for this room
        for agent_name, agent_config in config.agents.items():
            if room_key not in agent_config.rooms:
                continue

            agent_id = f"@mindroom_{agent_name}:localhost"

            # Skip if already in room
            if agent_id in room_members:
                already_in_room += 1
                console.print(f"✓ {agent_id} already in {room_key}")
                continue

            # Invite if not in room
            result = await _invite_agent_to_room(client, room.room_id, room_key, agent_id)
            if result.success:
                successful_invites += 1
            else:
                failed_invites += 1

    console.print(
        f"\n📊 Room access summary: {successful_invites} invited, "
        f"{already_in_room} already present, {failed_invites} failed"
    )


async def _create_room_and_invite_agents(room_key: str, room_name: str, user_client: nio.AsyncClient) -> str | None:
    """Create a room and invite all configured agents."""
    from mindroom.matrix import add_room

    # Create room
    response = await user_client.room_create(
        name=room_name,
        alias=room_key,
        topic=f"Mindroom {room_name}",
        preset=nio.RoomPreset.public_chat,
    )

    if isinstance(response, nio.RoomCreateResponse):
        room_id: str = response.room_id
        console.print(f"✅ Created room: {room_name} ({room_id})")

        # Save room info
        add_room(room_key, room_id, f"#{room_key}:localhost", room_name)

        # Invite agents based on config.yaml
        from mindroom.agent_loader import load_config

        config = load_config()

        invited_count = 0
        for agent_name, agent_config in config.agents.items():
            if room_key in agent_config.rooms:
                agent_id = f"@mindroom_{agent_name}:localhost"
                try:
                    await user_client.room_invite(room_id, agent_id)
                    invited_count += 1
                except Exception:
                    pass  # Agent might not exist yet

        if invited_count > 0:
            console.print(f"   Invited {invited_count} agents to the room")

        return room_id
    console.print(f"❌ Failed to create room {room_name}: {response}")
    return None


async def _ensure_user_account() -> MatrixConfig:
    """Ensure a user account exists, creating one if necessary."""
    config = MatrixConfig.load()

    user_account = config.get_account("user")

    # If we have stored credentials, try to login first
    if user_account:
        async with matrix_client(HOMESERVER, f"@{user_account.username}:localhost") as client:
            response = await client.login(password=user_account.password)
            if isinstance(response, nio.LoginResponse):
                console.print(f"✅ User account ready: @{user_account.username}:localhost")
                return config
            else:
                console.print("⚠️  Stored credentials invalid, creating new account...")
                config.accounts.pop("user", None)

    # No valid account, create a new one
    console.print("📝 Creating user account...")

    # Generate credentials
    user_username = "mindroom_user"
    user_password = f"mindroom_password_{os.urandom(16).hex()}"

    # Register user
    await _register_user(user_username, user_password, "Mindroom User")

    # Save credentials
    config.add_account("user", user_username, user_password)
    config.save()

    console.print(f"✅ User account ready: @{user_username}:localhost")

    return config


async def _create_missing_rooms(client: nio.AsyncClient, required_rooms: set[str]) -> None:
    """Create any missing rooms from the required set."""
    from mindroom.matrix import load_rooms

    existing_rooms = load_rooms()
    missing_rooms = required_rooms - set(existing_rooms.keys())

    if missing_rooms:
        console.print(f"\n🏗️  Creating {len(missing_rooms)} rooms...")
        for room_key in missing_rooms:
            room_name = room_key.replace("_", " ").title()
            await _create_room_and_invite_agents(room_key, room_name, client)


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
    from mindroom.agent_loader import load_config
    from mindroom.bot import main

    console.print(f"🚀 Starting Mindroom multi-agent system (log level: {log_level})...\n")

    # Ensure we have a user account
    config = await _ensure_user_account()

    # Load agent configuration and collect required rooms
    agent_config = load_config()
    required_rooms = set(room for agent_cfg in agent_config.agents.values() for room in agent_cfg.rooms)

    # Handle room creation and agent invitations if needed
    if required_rooms:
        # Login as user for room operations
        user_account = config.get_account("user")
        if not user_account:
            console.print("❌ No user account found")
            sys.exit(1)

        username = f"@{user_account.username}:localhost"
        password = user_account.password

        async with matrix_client(HOMESERVER, username) as client:
            response = await client.login(password=password)
            if isinstance(response, nio.LoginResponse):
                # Create any missing rooms
                await _create_missing_rooms(client, required_rooms)

                # Ensure agents are invited to all required rooms
                await _ensure_agents_in_rooms(client, required_rooms)
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
    config = MatrixConfig.load()
    if not config.accounts and not config.rooms:
        console.print("❌ No configuration found. Run: mindroom run")
        return

    console.print("🔑 Mindroom Status")
    console.print("=" * 40)

    # User info
    user_account = config.get_account("user")
    if user_account:
        console.print(f"\n👤 User: @{user_account.username}:localhost")

    # Agent info
    agent_accounts = [(key, acc) for key, acc in config.accounts.items() if key.startswith("agent_")]
    if agent_accounts:
        console.print(f"\n🤖 Agents: {len(agent_accounts)} registered")
        for key, account in agent_accounts:
            agent_name = key.replace("agent_", "")
            console.print(f"  • {agent_name}: @{account.username}:localhost")

    # Room info
    if config.rooms:
        console.print(f"\n🏠 Rooms: {len(config.rooms)} created")
        for _room_key, room in config.rooms.items():
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
    config = MatrixConfig.load()
    user_account = config.get_account("user")
    if not user_account:
        console.print("❌ No user account found. Run: mindroom run")
        return

    username = f"@{user_account.username}:localhost"
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
    config = MatrixConfig.load()
    user_account = config.get_account("user")
    if not user_account:
        console.print("❌ No user account found. Run: mindroom run")
        return

    username = f"@{user_account.username}:localhost"
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
                from mindroom.agent_loader import load_config

                agent_config = load_config()

                invited_count = 0
                for agent_name, agent_cfg in agent_config.agents.items():
                    if room_key in agent_cfg.rooms:
                        agent_id = f"@mindroom_{agent_name}:localhost"
                        try:
                            await client.room_invite(room_id, agent_id)
                            console.print(f"✅ Invited {agent_id}")
                            invited_count += 1
                        except Exception as e:
                            console.print(f"❌ Failed to invite {agent_id}: {e}")

                console.print(f"\n✨ Invited {invited_count} agents to room")
            else:
                # Invite all agents if room not in config
                agent_count = 0
                for key, account in config.accounts.items():
                    if key.startswith("agent_"):
                        agent_id = f"@{account.username}:localhost"
                        try:
                            await client.room_invite(room_id, agent_id)
                            console.print(f"✅ Invited {agent_id}")
                            agent_count += 1
                        except Exception as e:
                            console.print(f"❌ Failed to invite {agent_id}: {e}")

                console.print(f"\n✨ Invited {agent_count} agents to room")
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
