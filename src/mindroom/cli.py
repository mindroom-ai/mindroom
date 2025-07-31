#!/usr/bin/env python3
"""Minimal mindroom CLI for Matrix bot management."""

import asyncio
import os
import sys
from pathlib import Path

import nio
import typer
import yaml
from rich.console import Console

app = typer.Typer(
    help="Mindroom Matrix bot management CLI",
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
console = Console()


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Show help if no command is provided."""
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit()


CREDENTIALS_FILE = Path("matrix_users.yaml")
ENV_FILE = Path(".env")
HOMESERVER = "http://localhost:8008"


def load_credentials():
    """Load credentials from matrix_users.yaml."""
    if CREDENTIALS_FILE.exists():
        with open(CREDENTIALS_FILE) as f:
            return yaml.safe_load(f)
    return {}


def save_credentials(creds):
    """Save credentials to matrix_users.yaml."""
    with open(CREDENTIALS_FILE, "w") as f:
        yaml.dump(creds, f, default_flow_style=False, sort_keys=False)


@app.command()
def setup():
    """Set up bot and test users (run this first)."""
    asyncio.run(_setup())


async def _setup():
    """Create bot and test user."""
    console.print("🚀 Setting up mindroom users...")

    # Load existing credentials
    creds = load_credentials()

    # Create bot user
    bot_username = "mindroom_bot"
    bot_password = "bot_password_123"

    client = nio.AsyncClient(HOMESERVER, f"@{bot_username}:localhost")
    try:
        response = await client.register(username=bot_username, password=bot_password)
        if isinstance(response, nio.RegisterResponse):
            console.print(f"✅ Created bot: @{bot_username}:localhost")
            creds["bot"] = {"username": bot_username, "password": bot_password}
        else:
            console.print(f"ℹ️  Bot already exists: @{bot_username}:localhost")
            creds["bot"] = {"username": bot_username, "password": bot_password}
    finally:
        await client.close()

    # Create test user
    user_username = "mindroom_user"
    user_password = "user_password_123"

    client = nio.AsyncClient(HOMESERVER, f"@{user_username}:localhost")
    try:
        response = await client.register(username=user_username, password=user_password)
        if isinstance(response, nio.RegisterResponse):
            console.print(f"✅ Created user: @{user_username}:localhost")
            creds["user"] = {"username": user_username, "password": user_password}
        else:
            console.print(f"ℹ️  User already exists: @{user_username}:localhost")
            creds["user"] = {"username": user_username, "password": user_password}
    finally:
        await client.close()

    # Save credentials
    save_credentials(creds)

    # Update .env file
    if not ENV_FILE.exists():
        ENV_FILE.write_text(f"""# Matrix Server Details
MATRIX_HOMESERVER={HOMESERVER}
MATRIX_USER_ID=@{bot_username}:localhost
MATRIX_PASSWORD={bot_password}

# Agno AI Configuration
AGNO_MODEL=ollama:devstral:24b

# AI Caching Configuration
ENABLE_AI_CACHE=true
AI_CACHE_DIR=tmp/.ai_cache

# API Keys (optional)
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
OLLAMA_HOST=http://pc.local:11434
""")
        console.print("✅ Created .env file")

    console.print("\n✨ Setup complete!")
    console.print(f"\n🤖 Bot: @{bot_username}:localhost")
    console.print(f"👤 User: @{user_username}:localhost (password: {user_password})")


@app.command()
def run():
    """Run the mindroom bot."""
    from mindroom.bot import main

    creds = load_credentials()
    if not creds or "bot" not in creds:
        console.print("❌ No bot credentials found! Run: mindroom setup")
        sys.exit(1)

    # Set environment variables
    os.environ["MATRIX_HOMESERVER"] = HOMESERVER
    os.environ["MATRIX_USER_ID"] = f"@{creds['bot']['username']}:localhost"
    os.environ["MATRIX_PASSWORD"] = creds["bot"]["password"]

    console.print(f"🤖 Starting mindroom bot as @{creds['bot']['username']}:localhost")
    console.print("Press Ctrl+C to stop\n")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n✋ Bot stopped")


@app.command()
def test():
    """Create a test room and invite the bot."""
    asyncio.run(_test())


@app.command()
def invite_agents():
    """Invite all agents to an existing room."""
    room_id = typer.prompt("Enter room ID to invite agents to")
    asyncio.run(_invite_agents(room_id))


async def _test():
    """Test bot connection."""
    creds = load_credentials()
    if not creds:
        console.print("❌ No credentials found! Run: mindroom setup")
        return

    if "user" not in creds or "bot" not in creds:
        console.print("❌ Missing user or bot credentials! Run: mindroom setup")
        return

    username = f"@{creds['user']['username']}:localhost"
    password = creds["user"]["password"]
    bot_id = f"@{creds['bot']['username']}:localhost"

    client = nio.AsyncClient(HOMESERVER, username)

    try:
        # Login
        response = await client.login(password=password)
        if not isinstance(response, nio.LoginResponse):
            console.print(f"❌ Failed to login: {response}")
            return

        console.print(f"✅ Logged in as {username}")

        # Create room
        response = await client.room_create(name="Mindroom Test")
        if isinstance(response, nio.RoomCreateResponse):
            room_id = response.room_id
            console.print(f"✅ Created room: {room_id}")

            # Invite bot
            await client.room_invite(room_id, bot_id)
            console.print(f"✅ Invited {bot_id}")
            console.print(f"\n💬 Send a message mentioning {bot_id} to test!")

    finally:
        await client.close()


async def _invite_agents(room_id: str) -> None:
    """Invite all agents to a room."""
    creds = load_credentials()
    if not creds or "user" not in creds:
        console.print("❌ No user credentials found! Run: mindroom setup")
        return

    username = f"@{creds['user']['username']}:localhost"
    password = creds["user"]["password"]

    client = nio.AsyncClient(HOMESERVER, username)

    try:
        # Login
        response = await client.login(password=password)
        if not isinstance(response, nio.LoginResponse):
            console.print(f"❌ Failed to login: {response}")
            return

        console.print(f"✅ Logged in as {username}")

        # Load all agent credentials
        agent_count = 0
        for key, value in creds.items():
            if key.startswith("agent_"):
                agent_username = value["username"]
                agent_id = f"@{agent_username}:localhost"

                try:
                    await client.room_invite(room_id, agent_id)
                    console.print(f"✅ Invited {agent_id}")
                    agent_count += 1
                except Exception as e:
                    console.print(f"❌ Failed to invite {agent_id}: {e}")

        console.print(f"\n✨ Invited {agent_count} agents to room {room_id}")

    finally:
        await client.close()


@app.command()
def create_agent_room(
    room_alias: str = typer.Argument(..., help="Room alias (e.g., 'lobby', 'dev', 'research')"),
    room_name: str = typer.Option(None, help="Display name for the room"),
    invite_agents: bool = typer.Option(True, help="Automatically invite all agents to the room"),
) -> None:
    """Create a new room and optionally invite all agents."""
    asyncio.run(_create_agent_room(room_alias, room_name, invite_agents))


async def _create_agent_room(room_alias: str, room_name: str | None, invite_agents: bool) -> None:
    """Create a new room and optionally invite all agents."""
    creds = load_credentials()
    if not creds or "user" not in creds:
        console.print("❌ No user credentials found! Run: mindroom setup")
        return

    username = f"@{creds['user']['username']}:localhost"
    password = creds["user"]["password"]

    client = nio.AsyncClient(HOMESERVER, username)

    try:
        # Login
        response = await client.login(password=password)
        if not isinstance(response, nio.LoginResponse):
            console.print(f"❌ Failed to login: {response}")
            return

        console.print(f"✅ Logged in as {username}")

        # If no room name provided, use the alias
        if not room_name:
            room_name = room_alias.capitalize()

        # Create the room
        room_config = {
            "name": room_name,
            "alias": room_alias,  # This creates #room_alias:localhost
            "visibility": nio.RoomVisibility.private,
            "preset": nio.RoomPreset.private_chat,
        }

        response = await client.room_create(**room_config)

        if isinstance(response, nio.RoomCreateResponse):
            room_id = response.room_id
            console.print(f"✅ Created room: {room_name} (#{room_alias}:localhost) -> {room_id}")

            # Save room to matrix_rooms.yaml
            from mindroom.matrix_room_manager import add_room

            add_room(room_alias, room_id, f"#{room_alias}:localhost", room_name)

            if invite_agents:
                # Invite all agents
                agent_count = 0
                for key, value in creds.items():
                    if key.startswith("agent_"):
                        agent_username = value["username"]
                        agent_id = f"@{agent_username}:localhost"

                        try:
                            await client.room_invite(room_id, agent_id)
                            console.print(f"✅ Invited {agent_id}")
                            agent_count += 1
                        except Exception as e:
                            console.print(f"❌ Failed to invite {agent_id}: {e}")

                console.print(f"\n✨ Created room '{room_name}' with {agent_count} agents!")
        else:
            console.print(f"❌ Failed to create room: {response}")

    finally:
        await client.close()


@app.command()
def create_all_rooms() -> None:
    """Create all rooms defined in agents.yaml and invite relevant agents."""
    asyncio.run(_create_all_rooms())


async def _create_all_rooms() -> None:
    """Create all rooms defined in agents.yaml and invite relevant agents."""
    import sys

    from mindroom.agent_loader import load_config

    sys.path.append(str(Path(__file__).parent))

    creds = load_credentials()
    if not creds or "user" not in creds:
        console.print("❌ No user credentials found! Run: mindroom setup")
        return

    username = f"@{creds['user']['username']}:localhost"
    password = creds["user"]["password"]

    # Load agent configuration to get all unique rooms
    config = load_config()
    from mindroom.matrix_room_manager import get_room_aliases

    room_aliases = get_room_aliases()
    all_rooms = set()
    room_to_agents: dict[str, list[str]] = {}

    # Collect all unique rooms and which agents belong to each
    for agent_name, agent_config in config.agents.items():
        for room in agent_config.rooms:
            # Resolve room alias to actual room ID if needed
            resolved_room = room_aliases.get(room, room)
            all_rooms.add(resolved_room)
            if resolved_room not in room_to_agents:
                room_to_agents[resolved_room] = []
            room_to_agents[resolved_room].append(agent_name)

    client = nio.AsyncClient(HOMESERVER, username)

    try:
        # Login
        response = await client.login(password=password)
        if not isinstance(response, nio.LoginResponse):
            console.print(f"❌ Failed to login: {response}")
            return

        console.print(f"✅ Logged in as {username}")

        # Create each room
        room_mapping = {}  # Maps room IDs from config to actual room IDs
        for room_ref in all_rooms:
            # Extract room alias from the reference (e.g., "!lobby:localhost" -> "lobby")
            room_alias = room_ref[1 : room_ref.index(":")] if room_ref.startswith("!") and ":" in room_ref else room_ref

            room_name = room_alias.capitalize()

            # Create the room
            room_config = {
                "name": room_name,
                "alias": room_alias,
                "visibility": nio.RoomVisibility.private,
                "preset": nio.RoomPreset.private_chat,
            }

            response = await client.room_create(**room_config)

            if isinstance(response, nio.RoomCreateResponse):
                room_id = response.room_id
                room_mapping[room_ref] = room_id
                console.print(f"✅ Created room: {room_name} (#{room_alias}:localhost) -> {room_id}")

                # Get agents that should be in this room
                agents_for_room = room_to_agents.get(room_ref, [])
                console.print(f"   Agents for this room: {', '.join(agents_for_room)}")

                # Invite agents to this room
                for agent_name in agents_for_room:
                    agent_key = f"agent_{agent_name}"
                    if agent_key in creds:
                        agent_username = creds[agent_key]["username"]
                        agent_id = f"@{agent_username}:localhost"
                        try:
                            await client.room_invite(room_id, agent_id)
                            console.print(f"   ✅ Invited {agent_name} ({agent_id})")
                        except Exception as e:
                            console.print(f"   ❌ Failed to invite {agent_name}: {e}")
            else:
                console.print(f"❌ Failed to create room {room_alias}: {response}")

    finally:
        await client.close()

    console.print("\n✨ Room creation complete!")


@app.command()
def info():
    """Show current credentials and status."""
    creds = load_credentials()
    if not creds:
        console.print("❌ No credentials found! Run: mindroom setup")
        return

    console.print("🔑 Mindroom Credentials")
    console.print("=" * 40)

    if "bot" in creds:
        console.print(f"\n🤖 Bot: @{creds['bot']['username']}:localhost")

    if "user" in creds:
        console.print(f"👤 User: {creds['user']['username']} (password: {creds['user']['password']}")

    # Show all agents
    agent_count = 0
    console.print("\n🤖 Agents:")
    for key, value in creds.items():
        if key.startswith("agent_"):
            agent_name = key.replace("agent_", "")
            agent_username = value["username"]
            console.print(f"  • {agent_name}: @{agent_username}:localhost")
            agent_count += 1

    if agent_count == 0:
        console.print("  (No agents found)")

    # Show rooms
    if "rooms" in creds:
        console.print("\n🏠 Rooms:")
        for room_key, room_data in creds["rooms"].items():
            room_alias = room_data.get("alias", room_key)
            room_name = room_data.get("name", room_key)
            console.print(f"  • {room_name} ({room_alias})")

    console.print(f"\n🌐 Server: {HOMESERVER}")


if __name__ == "__main__":
    app()
