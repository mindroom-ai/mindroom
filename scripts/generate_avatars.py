#!/usr/bin/env -S uv run
"""Generate avatars for all agents and teams using OpenAI GPT Image model.

This script:
1. Reads all agents and teams from config.yaml
2. Uses AI to generate custom prompts based on agent roles
3. Generates consistent-style avatars using GPT Image 1
4. Stores avatars in avatars/ directory
5. Only regenerates missing avatars (idempotent)

Usage:
    uv run scripts/generate_avatars.py

Requires:
    OPENAI_API_KEY environment variable to be set (or in .env file)
"""
# /// script
# dependencies = [
#   "openai",
#   "pyyaml",
#   "httpx",
#   "aiofiles",
#   "python-dotenv",
#   "rich",
# ]
# ///

import asyncio
import base64
import os
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
    base_style = "minimalist modern avatar portrait, clean geometric shapes, vibrant gradient colors, professional, friendly, tech-inspired, flat design, no text, centered composition"

    # Use a simple AI model to generate visual themes based on the role
    if entity_type == "teams" and team_members:
        # For teams, create a prompt that combines the team members' roles
        system_prompt = """You are an expert at creating visual descriptions for team avatars.
Given a team's name, description, and the roles of its members, suggest 5-7 visual elements that would represent this collaborative team well.
The avatar should represent the combination and synergy of the team members.
Output ONLY the visual elements as a comma-separated list, no other text.
Example: "collaborative nodes, interconnected gears, diverse symbols merging, team synergy, unified elements"
"""

        members_info = "\n".join([f"- {m['name']}: {m['role']}" for m in team_members])
        user_prompt = f"Team name: {entity_name}\nTeam role: {role}\nTeam members:\n{members_info}"
    else:
        # For individual agents
        system_prompt = """You are an expert at creating visual descriptions for avatars.
Given an agent's name and role description, suggest 3-5 visual elements that would represent them well in an avatar.
Output ONLY the visual elements as a comma-separated list, no other text.
Examples:
- For a calculator agent: "mathematical symbols, numbers, geometric patterns"
- For a research agent: "magnifying glass, book, knowledge symbols"
"""
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
    final_prompt = f"{base_style}, {visual_elements}"

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
    prompt = await generate_prompt(client, entity_type, entity_name, role, team_members)

    response = await client.images.generate(
        model="gpt-image-1",
        prompt=prompt,
        size="1024x1024",  # API minimum size
        quality="low",  # Use low quality for cheaper generation
        n=1,
    )

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


async def main() -> None:
    """Main function to generate all avatars."""
    # Check for OpenAI API key
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        console.print("[red]Error: OPENAI_API_KEY environment variable not set[/red]")
        console.print("Please set it in your .env file or environment")
        return

    client = AsyncOpenAI(api_key=api_key)
    config = load_config()

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

    # Generate avatars
    console.print(
        f"\n[bold cyan]🚀 Generating avatars for {len(agents) + 1} agents and {len(teams)} teams...[/bold cyan]\n",
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


if __name__ == "__main__":
    asyncio.run(main())
