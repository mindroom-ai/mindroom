#!/usr/bin/env -S uv run
"""Generate avatars for all agents and teams using OpenAI GPT Image model.

This script:
1. Reads all agents and teams from config.yaml
2. Generates consistent-style avatars using GPT Image 1
3. Stores avatars in avatars/ directory with Git LFS tracking
4. Only regenerates missing avatars (idempotent)
5. Sets up Git LFS tracking automatically

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


def generate_prompt(entity_type: str, entity_name: str, role: str) -> str:
    """Generate a DALL-E prompt for consistent avatar style."""
    base_style = "minimalist modern avatar portrait, clean geometric shapes, vibrant gradient colors, professional, friendly, tech-inspired, flat design, no text, centered composition"

    if entity_type == "agents":
        # Customize based on agent role/name
        agent_themes = {
            "calculator": "mathematical symbols, numbers, geometric patterns",
            "code": "code brackets, terminal window, syntax highlighting colors",
            "research": "magnifying glass, book, knowledge symbols",
            "analyst": "charts, graphs, data visualization elements",
            "finance": "currency symbols, stock charts, financial graphs",
            "security": "shield, lock, protective elements",
            "general": "friendly robot, conversational bubbles",
            "home": "smart home, house, connected devices",
            "news": "newspaper, broadcast, information flow",
            "shell": "terminal, command line interface, console",
            "summary": "document, condensed text, bullet points",
            "email_assistant": "envelope, inbox, communication",
            "data_analyst": "data points, analytics dashboard, insights",
            "callagent": "phone, communication waves, calling",
            "router": "network hub, interconnected pathways, routing arrows, central node",
        }

        theme = agent_themes.get(entity_name.lower(), "abstract technological patterns")
        return f"{base_style}, {theme}, representing {role}"

    # teams
    return f"{base_style}, collaborative elements, team unity, interconnected nodes, representing a team that {role}"


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
) -> None:
    """Generate an avatar for a single entity if it doesn't exist."""
    avatar_path = get_avatar_path(entity_type, entity_name)

    if avatar_path.exists():
        print(f"✓ Avatar already exists for {entity_type}/{entity_name}")
        return

    role = entity_data.get("role", "AI assistant")
    prompt = generate_prompt(entity_type, entity_name, role)

    print(f"🎨 Generating avatar for {entity_type}/{entity_name}...")

    try:
        response = await client.images.generate(
            model="gpt-image-1",
            prompt=prompt,
            size="1024x1024",  # API minimum size
            quality="high",
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
                print(f"✓ Generated avatar for {entity_type}/{entity_name}")
            elif hasattr(image_data, "url") and image_data.url:
                # Download from URL
                await download_image(image_data.url, avatar_path)
                print(f"✓ Generated avatar for {entity_type}/{entity_name}")
            else:
                print(f"✗ No image data found for {entity_type}/{entity_name}")

    except Exception as e:
        print(f"✗ Error generating avatar for {entity_type}/{entity_name}: {e}")


async def main() -> None:
    """Main function to generate all avatars."""
    # Check for OpenAI API key
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("Error: OPENAI_API_KEY environment variable not set")
        print("Please set it in your .env file or environment")
        return

    client = AsyncOpenAI(api_key=api_key)
    config = load_config()

    # Collect all entities to generate
    tasks = []

    # Process agents
    agents = config.get("agents", {})
    for agent_name, agent_data in agents.items():
        tasks.append(generate_avatar(client, "agents", agent_name, agent_data))
        break

    # Add router agent (special system agent)
    tasks.append(generate_avatar(client, "agents", "router", {"role": "Intelligent routing and agent selection"}))

    # Process teams
    teams = config.get("teams", {})
    for team_name, team_data in teams.items():
        tasks.append(generate_avatar(client, "teams", team_name, team_data))

    # Generate avatars
    print(f"🚀 Generating avatars for {len(agents) + 1} agents and {len(teams)} teams...")

    # Process all tasks (OpenAI handles rate limiting)
    await asyncio.gather(*tasks)

    print("✨ Avatar generation complete!")

    # Initialize git LFS if needed
    gitattributes_path = get_project_root() / ".gitattributes"

    # Check if we need to add LFS tracking
    lfs_patterns = [
        "avatars/**/*.png filter=lfs diff=lfs merge=lfs -text",
        "avatars/**/*.jpg filter=lfs diff=lfs merge=lfs -text",
        "avatars/**/*.webp filter=lfs diff=lfs merge=lfs -text",
    ]

    existing_content = ""
    if gitattributes_path.exists():
        with gitattributes_path.open() as f:
            existing_content = f.read()

    patterns_to_add = [pattern for pattern in lfs_patterns if pattern not in existing_content]

    if patterns_to_add:
        print("\n📦 Setting up Git LFS tracking...")
        with gitattributes_path.open("a") as f:
            if existing_content and not existing_content.endswith("\n"):
                f.write("\n")
            for pattern in patterns_to_add:
                f.write(f"{pattern}\n")
        print("✓ Added Git LFS tracking for avatar images")
        print("  Run 'git lfs install' if you haven't already")


if __name__ == "__main__":
    asyncio.run(main())
