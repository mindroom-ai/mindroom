"""Generate and set avatars for all agents, teams, and rooms.

This script:
1. Reads all agents, teams, and rooms from config.yaml
2. Uses AI to generate custom prompts based on agent roles and room purposes
3. Generates consistent-style avatars using GPT Image 1
4. Stores avatars in avatars/ directory
5. Sets avatars in Matrix for agents and rooms
6. Only regenerates missing avatars (idempotent)

Usage:
    python scripts/generate_avatars.py [--set-only]

Options:
    --set-only    Skip generation and only set existing avatars in Matrix

Requires:
    OPENAI_API_KEY environment variable to be set (or in .env file)
"""

import asyncio
import base64
import os
import sys
from pathlib import Path

import aiofiles
import httpx
import yaml
from dotenv import load_dotenv
from openai import AsyncOpenAI
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.text import Text

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix import MATRIX_HOMESERVER
from mindroom.matrix.client import check_and_set_room_avatar
from mindroom.matrix.identity import MatrixID, extract_server_name_from_homeserver
from mindroom.matrix.rooms import get_room_id
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import AgentMatrixUser, login_agent_user

console = Console()

# Load environment variables from .env file
load_dotenv()

# Avatar generation prompts
BASE_STYLE = "adorable Pixar-style robot character portrait, big emotive eyes, soft rounded design, vibrant metallic colors, friendly smile, expressive antenna or unique head features, helper robot personality, warm lighting, 3D rendered look, approachable and huggable, centered composition, no text"

TEAM_SYSTEM_PROMPT = """You are an expert at creating visual descriptions for friendly Pixar-style robot team avatars.
Given a team's name, description, and the roles of its members, suggest 5-7 unique robot features and characteristics.
Think about: special attachments, unique colors, multiple connected robots, special tools or gadgets, distinctive shapes.
The avatar should show multiple robots working together or a single robot with features from all team members.
Output ONLY the visual elements as a comma-separated list, no other text.
Example: "multiple small robots holding hands, interconnected with glowing data streams, different colored robots in a group hug, modular robot with swappable parts, rainbow metallic finish"
"""

AGENT_SYSTEM_PROMPT = """You are an expert at creating visual descriptions for friendly Pixar-style robot avatars.
Given an agent's name and role, suggest 3-5 unique robot characteristics and features that match their personality.
Think about: special tools or attachments, unique antenna designs, eye shapes and colors, body modifications, special badges or emblems.
Output ONLY the visual elements as a comma-separated list, no other text.
Examples:
- For a calculator agent: "calculator screen chest display, number pad buttons, mathematical equation hologram, protractor antenna"
- For a research agent: "magnifying glass eye, book-shaped chest compartment, data scanner antenna, holographic display"
- For a code agent: "keyboard fingers, screen face with code scrolling, USB port accessories, binary code patterns"
"""

ROOM_SYSTEM_PROMPT = """You are an expert at creating visual descriptions for friendly Pixar-style robot meeting spaces and environments.
Given a room's name, suggest 5-7 environmental features that show this is a welcoming robot gathering space.
Think about: holographic displays, cozy charging stations, data streams, robot-friendly furniture, ambient lighting, tech decorations.
The avatar should depict a warm, inviting space where robots would gather and collaborate.
Output ONLY the visual elements as a comma-separated list, no other text.
Examples:
- For a lobby: "circular gathering space with soft blue lights, central hologram projector, comfortable charging pods, welcome banner with binary code, floating data orbs"
- For research room: "walls lined with data screens, floating holographic books, analysis stations, green scanning beams, knowledge crystals"
- For automation room: "conveyor belts, robotic arms on walls, gear decorations, orange industrial lighting, efficiency meters"
"""


def get_project_root() -> Path:
    """Get the project root directory."""
    return Path(__file__).parent.parent


def load_config() -> dict:
    """Load the configuration from config.yaml."""
    config_path = get_project_root() / "config.yaml"
    with config_path.open() as f:
        return yaml.safe_load(f)


def get_avatar_path(entity_type: str, entity_name: str) -> Path:
    """Get the path for an avatar file."""
    avatars_dir = get_project_root() / "avatars" / entity_type
    avatars_dir.mkdir(parents=True, exist_ok=True)
    return avatars_dir / f"{entity_name}.png"


async def generate_prompt(
    client: AsyncOpenAI,
    entity_type: str,
    entity_name: str,
    role: str,
    team_members: list[dict] | None = None,
) -> str:
    """Generate a DALL-E prompt based on the entity's role using AI."""
    # Use a simple AI model to generate visual themes based on the role
    if entity_type == "rooms":
        # For rooms, create a prompt for a robot gathering space
        system_prompt = ROOM_SYSTEM_PROMPT
        user_prompt = f"Room name: {entity_name}\nPurpose: {role}"
    elif entity_type == "teams" and team_members:
        # For teams, create a prompt that combines the team members' roles
        system_prompt = TEAM_SYSTEM_PROMPT
        members_info = "\n".join([f"- {m['name']}: {m['role']}" for m in team_members])
        user_prompt = f"Team name: {entity_name}\nTeam role: {role}\nTeam members:\n{members_info}"
    else:
        # For individual agents
        system_prompt = AGENT_SYSTEM_PROMPT
        user_prompt = f"Agent name: {entity_name}\nRole: {role}\nType: {entity_type}"

    # Use a cheaper/faster model for prompt generation
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
        max_tokens=150,
    )

    visual_elements = response.choices[0].message.content.strip()
    final_prompt = f"{BASE_STYLE}, {visual_elements}"

    # Print the prompt with rich formatting
    console.print(
        Panel(
            Text(final_prompt, style="cyan"),
            title=f"[bold yellow]{entity_type}/{entity_name}[/bold yellow]",
            border_style="green",
        ),
    )

    return final_prompt


async def download_image(url: str, save_path: Path) -> None:
    """Download an image from URL and save it."""
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        response.raise_for_status()

        async with aiofiles.open(save_path, "wb") as f:
            await f.write(response.content)


async def generate_avatar(
    client: AsyncOpenAI,
    entity_type: str,
    entity_name: str,
    entity_data: dict,
    all_agents: dict | None = None,
) -> None:
    """Generate an avatar for a single entity if it doesn't exist."""
    avatar_path = get_avatar_path(entity_type, entity_name)

    if avatar_path.exists():
        console.print(f"[green]✓[/green] Avatar already exists for [bold]{entity_type}/{entity_name}[/bold]")
        return

    role = entity_data.get("role", "AI assistant")
    console.print(f"\n[yellow]🎨 Generating avatar for {entity_type}/{entity_name}...[/yellow]")
    console.print(f"   [dim]Role: {role}[/dim]")

    # For teams, gather member information
    team_members = None
    if entity_type == "teams" and all_agents:
        team_members = []
        for agent_name in entity_data.get("agents", []):
            if agent_name in all_agents:
                agent_role = all_agents[agent_name].get("role", "Team member")
                team_members.append({"name": agent_name, "role": agent_role})
        console.print(f"   [dim]Team members: {', '.join([m['name'] for m in team_members])}[/dim]")

    # Generate a custom prompt using AI based on the role
    try:
        prompt = await generate_prompt(client, entity_type, entity_name, role, team_members)
    except Exception as e:
        console.print(f"[red]✗ Failed to generate prompt for {entity_type}/{entity_name}: {e}[/red]")
        return

    try:
        response = await client.images.generate(
            model="gpt-image-1",
            prompt=prompt,
            size="1024x1024",  # API minimum size
            quality="low",  # Use low quality for cheaper generation
            n=1,
        )
    except Exception as e:
        console.print(f"[red]✗ Failed to generate image for {entity_type}/{entity_name}: {e}[/red]")
        return

    if response.data and len(response.data) > 0:
        image_data = response.data[0]

        # Check if we have base64 data or URL
        if hasattr(image_data, "b64_json") and image_data.b64_json:
            # Decode base64 image
            image_bytes = base64.b64decode(image_data.b64_json)
            async with aiofiles.open(avatar_path, "wb") as f:
                await f.write(image_bytes)
            console.print(f"[green]✓ Generated avatar for {entity_type}/{entity_name}[/green]")
        elif hasattr(image_data, "url") and image_data.url:
            # Download from URL
            await download_image(image_data.url, avatar_path)
            console.print(f"[green]✓ Generated avatar for {entity_type}/{entity_name}[/green]")
        else:
            console.print(f"[red]✗ No image data found for {entity_type}/{entity_name}[/red]")


async def set_room_avatars_in_matrix() -> None:
    """Set avatars for all rooms in Matrix."""
    console.print("\n[bold cyan]Setting room avatars in Matrix...[/bold cyan]")

    # Get the router account from state (router has permission to modify rooms)
    state = MatrixState.load()
    router_account = state.get_account(f"agent_{ROUTER_AGENT_NAME}")
    if not router_account:
        console.print("[red]No router account found in Matrix state[/red]")
        console.print("[dim]Make sure mindroom has been started at least once[/dim]")
        return

    # Create router user object
    server_name = extract_server_name_from_homeserver(MATRIX_HOMESERVER)
    router_user = AgentMatrixUser(
        agent_name=ROUTER_AGENT_NAME,
        user_id=MatrixID.from_username(router_account.username, server_name).full_id,
        display_name="Router",
        password=router_account.password,
        access_token=None,
    )

    # Login as router
    client = await login_agent_user(MATRIX_HOMESERVER, router_user)
    console.print("[green]✓ Logged in to Matrix as router[/green]")

    # Get all rooms
    config = load_config()
    all_rooms = set()
    for agent_data in config.get("agents", {}).values():
        all_rooms.update(agent_data.get("rooms", []))

    avatars_dir = get_project_root() / "avatars" / "rooms"
    success_count = 0
    skip_count = 0

    for room_name in sorted(all_rooms):
        avatar_path = avatars_dir / f"{room_name}.png"

        if not avatar_path.exists():
            skip_count += 1
            continue

        # Get room ID
        room_id = get_room_id(room_name)
        if not room_id:
            console.print(f"[yellow]⚠ Room '{room_name}' not found in Matrix[/yellow]")
            continue

        # Set avatar
        if await check_and_set_room_avatar(client, room_id, avatar_path):
            console.print(f"[green]✓ Set avatar for room '{room_name}'[/green]")
            success_count += 1
        else:
            console.print(f"[yellow]⊘ Avatar already set or failed for room '{room_name}'[/yellow]")

    await client.close()

    if success_count > 0:
        console.print(f"\n[green]✓ Set {success_count} room avatars[/green]")
    if skip_count > 0:
        console.print(f"[dim]⊘ Skipped {skip_count} rooms (no avatar file)[/dim]")


async def main() -> None:  # noqa: C901
    """Main function to generate and set avatars."""
    # Check for --set-only flag
    set_only = "--set-only" in sys.argv

    if not set_only:
        # Check for OpenAI API key
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            console.print("[red]Error: OPENAI_API_KEY environment variable not set[/red]")
            console.print("Please set it in your .env file or environment")
            return

        client = AsyncOpenAI(api_key=api_key)

    config = load_config()

    if not set_only:
        # Collect all entities to generate
        tasks = []

        # Process agents
        agents = config.get("agents", {})
        for agent_name, agent_data in agents.items():
            tasks.append(generate_avatar(client, "agents", agent_name, agent_data))

        # Add router agent (special system agent)
        tasks.append(generate_avatar(client, "agents", "router", {"role": "Intelligent routing and agent selection"}))

        # Process teams (pass agents dict for team member info)
        teams = config.get("teams", {})
        for team_name, team_data in teams.items():
            tasks.append(generate_avatar(client, "teams", team_name, team_data, agents))

        # Process rooms
        # Get unique rooms from all agents
        all_rooms = set()
        for agent_data in agents.values():
            rooms = agent_data.get("rooms", [])
            all_rooms.update(rooms)

        # Define room purposes
        room_purposes = {
            "lobby": "Central meeting space for initial interactions and general discussions",
            "research": "Knowledge discovery, data analysis, and investigation space",
            "docs": "Documentation, writing, and knowledge management space",
            "ops": "Operations, DevOps, and system management space",
            "automation": "Workflow automation and process optimization space",
        }

        for room_name in all_rooms:
            room_data = {"role": room_purposes.get(room_name, f"Collaboration space for {room_name} activities")}
            tasks.append(generate_avatar(client, "rooms", room_name, room_data))

        # Get counts for display
        agents = config.get("agents", {})
        teams = config.get("teams", {})
        all_rooms_count = len(all_rooms)
    else:
        # For set-only mode, just get counts for display
        agents = config.get("agents", {})
        teams = config.get("teams", {})
        all_rooms = set()
        for agent_data in agents.values():
            all_rooms.update(agent_data.get("rooms", []))
        all_rooms_count = len(all_rooms)

    if not set_only:
        # Generate avatars
        console.print(
            f"\n[bold cyan]🚀 Generating avatars for {len(agents) + 1} agents, {len(teams)} teams, and {all_rooms_count} rooms...[/bold cyan]\n",
        )

        # Process all tasks (OpenAI handles rate limiting)
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task_id = progress.add_task("Processing avatars...", total=None)
            await asyncio.gather(*tasks)
            progress.update(task_id, completed=True)

        console.print("\n[bold green]✨ Avatar generation complete![/bold green]")

    # Set room avatars in Matrix (always try, even in set-only mode)
    try:
        await set_room_avatars_in_matrix()
    except Exception as e:
        console.print(f"\n[yellow]Warning: Could not set Matrix avatars: {e}[/yellow]")
        console.print("[dim]This is normal if Matrix server is not running[/dim]")


if __name__ == "__main__":
    asyncio.run(main())
