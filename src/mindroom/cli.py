"""Mindroom CLI - Simplified multi-agent Matrix bot system."""

import asyncio
import os
import sys
from pathlib import Path

import nio
import typer
import yaml
from rich.console import Console

from mindroom.matrix import MATRIX_HOMESERVER

app = typer.Typer(help="Mindroom: Multi-agent Matrix bot system")
console = Console()

HOMESERVER = MATRIX_HOMESERVER or "http://localhost:8008"
CREDENTIALS_FILE = Path("matrix_users.yaml")


def load_credentials() -> dict:
    """Load credentials from matrix_users.yaml."""
    if not CREDENTIALS_FILE.exists():
        return {}
    with open(CREDENTIALS_FILE) as f:
        return yaml.safe_load(f) or {}


def save_credentials(creds: dict) -> None:
    """Save credentials to matrix_users.yaml."""
    with open(CREDENTIALS_FILE, "w") as f:
        yaml.dump(creds, f, default_flow_style=False, sort_keys=False)


async def _register_user(username: str, password: str, display_name: str) -> None:
    """Register a new Matrix user."""
    client = nio.AsyncClient(HOMESERVER)

    try:
        # Try to register
        response = await client.register(
            username=username,
            password=password,
            device_name="mindroom-cli",
        )

        if isinstance(response, nio.RegisterResponse):
            # Set display name
            await client.login(password=password)
            await client.set_displayname(display_name)
            console.print(f"✅ Registered user: @{username}:localhost")
        elif isinstance(response, nio.RegisterError) and response.status_code == "M_USER_IN_USE":
            console.print(f"ℹ️  User @{username}:localhost already exists")
        else:
            console.print(f"❌ Failed to register {username}: {response}")
    finally:
        await client.close()


async def _create_room_and_invite_agents(room_key: str, room_name: str, user_client: nio.AsyncClient) -> str | None:
    """Create a room and invite all configured agents."""
    from mindroom.matrix_room_manager import add_room

    # Create room
    response = await user_client.room_create(
        name=room_name,
        alias=room_key,
        topic=f"Mindroom {room_name}",
        preset="public_chat",
    )

    if isinstance(response, nio.RoomCreateResponse):
        room_id: str = response.room_id
        console.print(f"✅ Created room: {room_name} ({room_id})")

        # Save room info
        add_room(room_key, room_id, f"#{room_key}:localhost", room_name)

        # Invite agents based on agents.yaml
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


@app.command()
def run():
    """Run the mindroom multi-agent system.

    This command automatically:
    - Creates a user account if needed
    - Creates all agent accounts
    - Creates all rooms defined in agents.yaml
    - Starts the multi-agent system
    """
    asyncio.run(_run())


async def _run() -> None:
    """Run the multi-agent system with automatic setup."""
    from mindroom.agent_loader import load_config
    from mindroom.bot import main

    console.print("🚀 Starting Mindroom multi-agent system...\n")

    # Check if we have a user account
    creds = load_credentials()
    if not creds or "user" not in creds:
        console.print("📝 Creating user account...")

        # Generate credentials
        user_username = "mindroom_user"
        user_password = f"user_password_{os.urandom(8).hex()}"

        # Register user
        await _register_user(user_username, user_password, "Mindroom User")

        # Save credentials
        creds = load_credentials()
        creds["user"] = {"username": user_username, "password": user_password}
        save_credentials(creds)

        console.print(f"✅ User account ready: @{user_username}:localhost")

    # Load agent configuration
    config = load_config()
    required_rooms = set()

    # Collect all rooms mentioned in agents.yaml
    for agent_config in config.agents.values():
        required_rooms.update(agent_config.rooms)

    if required_rooms:
        # Check which rooms need to be created
        from mindroom.matrix_room_manager import load_rooms

        existing_rooms = load_rooms()
        missing_rooms = required_rooms - set(existing_rooms.keys())

        if missing_rooms:
            console.print(f"\n🏗️  Creating {len(missing_rooms)} rooms...")

            # Login as user to create rooms
            username = f"@{creds['user']['username']}:localhost"
            password = creds["user"]["password"]
            client = nio.AsyncClient(HOMESERVER, username)

            try:
                response = await client.login(password=password)
                if isinstance(response, nio.LoginResponse):
                    for room_key in missing_rooms:
                        room_name = room_key.replace("_", " ").title()
                        await _create_room_and_invite_agents(room_key, room_name, client)
                else:
                    console.print(f"❌ Failed to login: {response}")
                    sys.exit(1)
            finally:
                await client.close()

    # Agent accounts are created automatically by the bot system
    console.print("\n🤖 Starting agents...")
    console.print("Press Ctrl+C to stop\n")

    try:
        await main()
    except KeyboardInterrupt:
        console.print("\n✋ Stopped")


@app.command()
def info():
    """Show current system status."""
    creds = load_credentials()
    if not creds:
        console.print("❌ No configuration found. Run: mindroom run")
        return

    console.print("🔑 Mindroom Status")
    console.print("=" * 40)

    # User info
    if "user" in creds:
        console.print(f"\n👤 User: @{creds['user']['username']}:localhost")

    # Agent info
    agent_count = sum(1 for key in creds if key.startswith("agent_"))
    if agent_count > 0:
        console.print(f"\n🤖 Agents: {agent_count} registered")
        for key, value in creds.items():
            if key.startswith("agent_"):
                agent_name = key.replace("agent_", "")
                console.print(f"  • {agent_name}: @{value['username']}:localhost")

    # Room info
    if "rooms" in creds:
        console.print(f"\n🏠 Rooms: {len(creds['rooms'])} created")
        for room_key, room_data in creds["rooms"].items():
            room_name = room_data.get("name", room_key)
            room_alias = room_data.get("alias", f"#{room_key}")
            console.print(f"  • {room_name} ({room_alias})")

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
    creds = load_credentials()
    if not creds or "user" not in creds:
        console.print("❌ No user account found. Run: mindroom run")
        return

    username = f"@{creds['user']['username']}:localhost"
    password = creds["user"]["password"]
    client = nio.AsyncClient(HOMESERVER, username)

    try:
        response = await client.login(password=password)
        if isinstance(response, nio.LoginResponse):
            await _create_room_and_invite_agents(room_alias, room_name, client)
        else:
            console.print(f"❌ Failed to login: {response}")
    finally:
        await client.close()


@app.command()
def invite_agents(room_id: str = typer.Argument(..., help="Room ID to invite agents to")) -> None:
    """Invite agents to an existing room."""
    asyncio.run(_invite_agents(room_id))


async def _invite_agents(room_id: str) -> None:
    """Invite agents to a room."""
    creds = load_credentials()
    if not creds or "user" not in creds:
        console.print("❌ No user account found. Run: mindroom run")
        return

    username = f"@{creds['user']['username']}:localhost"
    password = creds["user"]["password"]
    client = nio.AsyncClient(HOMESERVER, username)

    try:
        response = await client.login(password=password)
        if isinstance(response, nio.LoginResponse):
            # Get room name/alias for agent selection
            from mindroom.matrix_room_manager import load_rooms

            rooms = load_rooms()
            room_key = None
            for key, room in rooms.items():
                if room.room_id == room_id:
                    room_key = key
                    break

            if room_key:
                # Invite agents based on configuration
                from mindroom.agent_loader import load_config

                config = load_config()

                invited_count = 0
                for agent_name, agent_config in config.agents.items():
                    if room_key in agent_config.rooms:
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
                for key, value in creds.items():
                    if key.startswith("agent_"):
                        agent_id = f"@{value['username']}:localhost"
                        try:
                            await client.room_invite(room_id, agent_id)
                            console.print(f"✅ Invited {agent_id}")
                            agent_count += 1
                        except Exception as e:
                            console.print(f"❌ Failed to invite {agent_id}: {e}")

                console.print(f"\n✨ Invited {agent_count} agents to room")
        else:
            console.print(f"❌ Failed to login: {response}")
    finally:
        await client.close()


def main():
    """Main entry point that shows help by default."""
    import sys

    # If no arguments provided, show help
    if len(sys.argv) == 1:
        sys.argv.append("--help")

    # Replace -h with --help for consistency
    if "-h" in sys.argv:
        index = sys.argv.index("-h")
        sys.argv[index] = "--help"

    app()


if __name__ == "__main__":
    main()
