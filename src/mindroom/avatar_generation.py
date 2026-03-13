"""Generate and synchronize avatars for MindRoom entities."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Literal

from google import genai
from google.genai import types
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.text import Text

from mindroom.config.main import Config
from mindroom.constants import (
    CONFIG_PATH,
    MATRIX_HOMESERVER,
    ROUTER_AGENT_NAME,
    resolve_avatar_path,
    workspace_avatar_path,
)
from mindroom.credentials_sync import get_secret_from_env
from mindroom.error_handling import AvatarGenerationError, AvatarSyncError
from mindroom.logging_config import get_logger
from mindroom.matrix.avatar import set_room_avatar_from_file
from mindroom.matrix.identity import MatrixID, extract_server_name_from_homeserver
from mindroom.matrix.rooms import get_room_id
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import AgentMatrixUser, login_agent_user

if TYPE_CHECKING:
    from pathlib import Path

    import nio

    from mindroom.matrix.state import _MatrixAccount


logger = get_logger(__name__)

PROMPT_MODEL = "gemini-3.1-flash-lite-preview"
# Gemini 3.1 Flash Image Preview is the current Google image-generation model.
IMAGE_MODEL = "gemini-3.1-flash-image-preview"
ROOT_SPACE_AVATAR_NAME = "root_space"

CHARACTER_STYLE = "professional AI avatar portrait, abstract geometric silhouette, premium product-render aesthetic, refined materials, subtle depth, precise lighting, centered composition, restrained but distinctive color palette, modern enterprise technology brand language, calm intelligent presence, abstract interface motifs, no text, not cartoonish, not childish"

ROOM_STYLE = "minimalist wayfinding icon, precise geometry, strong silhouette, centered symbol, solid or restrained gradient background, contemporary enterprise technology design language, subtle depth, highly legible at small size, no text, not playful, not sticker-like"

TEAM_SYSTEM_PROMPT = """You are creating distinctive visual elements for a professional AI team avatar.
Given a team's name and purpose, suggest visual elements that feel advanced, credible, and memorable:
- A refined color system with one or two main colors
- A core geometric motif or silhouette
- A subtle interface, signal, or network detail
- A unifying emblem, structure, or arrangement that suggests collaboration
- Optional material or lighting cues
Output visual elements as a comma-separated list.
Example: "deep teal and graphite, interlocking geometric forms, thin orbital light rings, shared central core, brushed metal accents"
Avoid mascots, toy-like characters, exaggerated expressions, or whimsical accessories.
Make each team feel like part of one cohesive MindRoom identity system while remaining distinct."""

AGENT_SYSTEM_PROMPT = """You are creating distinctive visual elements for a professional AI agent avatar.
Given an agent's name and role, suggest visual elements that communicate expertise and personality through form, color, and motif:
- A distinctive but restrained color palette
- A signature geometric or architectural form
- A subtle interface, signal, or instrument detail related to the role
- A clear mood such as focused, analytical, decisive, calm, or exploratory
- Optional lighting or material cues
Output visual elements as a comma-separated list.
Examples:
- Researcher: "teal and graphite, precise radial scan motif, layered data planes, cool rim lighting, focused presence"
- Operations: "amber and charcoal, structured grid framework, status indicators, robust protective framing, steady presence"
Avoid mascots, toy-like characters, comic exaggeration, or whimsical accessories.
Keep it polished, modern, and credible."""

ROOM_SYSTEM_PROMPT = """You are creating a refined, minimalist icon design for a room avatar.
Given a room's purpose, suggest a simple icon and distinctive color system:
- ONE strong background color or restrained duotone
- ONE simple symbol that represents the room's purpose
- Clean geometry and a strong silhouette
Output as: "background color, icon description"

IMPORTANT:
- Keep every room clearly distinct in color and symbol.
- Prefer confident, professional colors rather than novelty shades.
- Think product icon, wayfinding symbol, or control-room tile.

Examples:
- Lobby: "deep blue background, doorway outline with soft inner glow"
- Research: "slate teal background, layered lens or scan ring"
- Docs: "cool gray background, structured document sheet"
- Ops: "burnt orange background, segmented control dial"
- Communication: "indigo background, speech contour with signal lines"
- Finance: "forest green background, stacked bar glyph"
- Home: "warm graphite background, house outline with centered node"

Avoid childish, sticker-like, or overly decorative designs.
Make each room instantly recognizable at small sizes."""

ROOM_PURPOSES = {
    "lobby": "Central meeting space, entrance and welcome area",
    "research": "Scientific investigation and data analysis",
    "docs": "Documentation and writing center",
    "ops": "Operations and system management",
    "automation": "Workflow automation and bot control",
    "analysis": "Data analysis and insights",
    "business": "Business strategy and planning",
    "communication": "Messages and team communication",
    "dev": "Software development and coding",
    "finance": "Financial analysis and trading",
    "help": "Support and assistance center",
    "home": "Personal home base and dashboard",
    "news": "News updates and current events",
    "productivity": "Task management and efficiency",
    "science": "Scientific research and experiments",
}

AvatarEntityType = Literal["agents", "teams", "rooms", "spaces"]


@dataclass(frozen=True)
class AvatarTeamMember:
    """A typed team member descriptor used for prompt generation."""

    name: str
    role: str


@dataclass(frozen=True)
class AvatarTarget:
    """A typed avatar generation target."""

    entity_type: AvatarEntityType
    entity_name: str
    role: str
    team_members: tuple[AvatarTeamMember, ...] = ()


@cache
def get_console() -> Console:
    """Create the shared Rich console used by avatar generation output."""
    return Console()


def load_validated_config() -> Config:
    """Load and validate the active MindRoom configuration."""
    return Config.from_yaml(CONFIG_PATH.expanduser().resolve())


def get_avatar_path(entity_type: str, entity_name: str) -> Path:
    """Get the output path for an avatar file."""
    avatar_path = workspace_avatar_path(entity_type, entity_name)
    avatar_path.parent.mkdir(parents=True, exist_ok=True)
    return avatar_path


def _managed_room_avatar_keys(config: Config) -> set[str]:
    """Return room keys that participate in managed avatar generation and sync."""
    return {room_name for room_name in config.get_all_configured_rooms() if not room_name.startswith(("!", "#"))}


def _managed_avatar_targets(config: Config) -> list[tuple[str, str]]:
    """Return every managed avatar target for the active config."""
    targets = [("agents", agent_name) for agent_name in config.agents]
    targets.append(("agents", "router"))
    targets.extend(("teams", team_name) for team_name in config.teams)
    targets.extend(("rooms", room_name) for room_name in _managed_room_avatar_keys(config))
    if config.matrix_space.enabled:
        targets.append(("spaces", ROOT_SPACE_AVATAR_NAME))
    return targets


def _missing_avatar_targets(
    config: Config,
    *,
    config_path: Path | None = None,
) -> set[tuple[str, str]]:
    """Return the managed avatar targets with no bundled or workspace avatar yet."""
    return {
        (entity_type, entity_name)
        for entity_type, entity_name in _managed_avatar_targets(config)
        if not resolve_avatar_path(entity_type, entity_name, config_path=config_path).exists()
    }


def has_missing_managed_avatars(config: Config, *, config_path: Path | None = None) -> bool:
    """Return whether any managed avatar file is missing from the workspace."""
    return bool(_missing_avatar_targets(config, config_path=config_path))


async def generate_prompt(
    client: genai.Client,
    target: AvatarTarget,
) -> str:
    """Generate an image prompt based on the entity's role using AI."""
    if target.entity_type in {"rooms", "spaces"}:
        system_prompt = ROOM_SYSTEM_PROMPT
        user_prompt = f"Room name: {target.entity_name}\nPurpose: {target.role}"
    elif target.entity_type == "teams":
        system_prompt = TEAM_SYSTEM_PROMPT
        user_prompt = f"Team name: {target.entity_name}\nTeam role: {target.role}"
        if target.team_members:
            members_info = "\n".join(f"- {member.name}: {member.role}" for member in target.team_members)
            user_prompt = f"{user_prompt}\nTeam members:\n{members_info}"
    else:
        system_prompt = AGENT_SYSTEM_PROMPT
        user_prompt = f"Agent name: {target.entity_name}\nRole: {target.role}\nType: {target.entity_type}"

    response = await client.aio.models.generate_content(
        model=PROMPT_MODEL,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.7,
            max_output_tokens=150,
        ),
    )
    if not response.text:
        msg = f"Gemini returned no text prompt for {target.entity_type}/{target.entity_name}"
        raise ValueError(msg)

    visual_elements = response.text.strip()
    base_style = ROOM_STYLE if target.entity_type in {"rooms", "spaces"} else CHARACTER_STYLE
    final_prompt = f"{base_style}, {visual_elements}"

    get_console().print(
        Panel(
            Text(final_prompt, style="cyan"),
            title=f"[bold yellow]{target.entity_type}/{target.entity_name}[/bold yellow]",
            border_style="green",
        ),
    )
    return final_prompt


def extract_image_bytes(response: types.GenerateContentResponse) -> bytes | None:
    """Return the first generated image bytes from a Gemini response."""
    for part in response.parts or []:
        if part.inline_data and part.inline_data.data:
            return part.inline_data.data
    return None


async def generate_avatar(
    client: genai.Client,
    target: AvatarTarget,
) -> None:
    """Generate an avatar for a single entity if it does not exist."""
    avatar_path = get_avatar_path(target.entity_type, target.entity_name)
    if avatar_path.exists():
        get_console().print(
            f"[green]✓[/green] Avatar already exists for [bold]{target.entity_type}/{target.entity_name}[/bold]",
        )
        return

    console = get_console()
    console.print(f"\n[yellow]🎨 Generating avatar for {target.entity_type}/{target.entity_name}...[/yellow]")
    console.print(f"   [dim]Role: {target.role}[/dim]")
    if target.team_members:
        console.print(f"   [dim]Team members: {', '.join(member.name for member in target.team_members)}[/dim]")

    prompt = await generate_prompt(client, target)
    response = await client.aio.models.generate_content(
        model=IMAGE_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(
                aspect_ratio="1:1",
                image_size="1K",
            ),
        ),
    )

    image_bytes = extract_image_bytes(response)
    if not image_bytes:
        msg = f"No image data found for {target.entity_type}/{target.entity_name}"
        raise ValueError(msg)

    avatar_path.write_bytes(image_bytes)
    console.print(f"[green]✓ Generated avatar for {target.entity_type}/{target.entity_name}[/green]")


def _build_router_user(router_account: _MatrixAccount) -> AgentMatrixUser:
    """Create the router user object from persisted Matrix state."""
    server_name = extract_server_name_from_homeserver(MATRIX_HOMESERVER)
    return AgentMatrixUser(
        agent_name=ROUTER_AGENT_NAME,
        user_id=MatrixID.from_username(router_account.username, server_name).full_id,
        display_name="Router",
        password=router_account.password,
        access_token=None,
    )


async def _sync_avatar_target(
    client: nio.AsyncClient,
    *,
    avatar_path: Path,
    room_id: str,
    label: str,
) -> bool:
    """Apply one managed avatar target by replacing the current room avatar."""
    if await set_room_avatar_from_file(client, room_id, avatar_path):
        get_console().print(f"[green]✓ Set avatar for {label}[/green]")
        return True
    get_console().print(f"[red]✗ Failed to set avatar for {label}[/red]")
    return False


async def _sync_configured_room_avatars(client: nio.AsyncClient, config: Config) -> tuple[int, int]:
    """Apply configured room avatars and return success/skip counts."""
    success_count = 0
    skip_count = 0
    for room_name in sorted(_managed_room_avatar_keys(config)):
        avatar_path = resolve_avatar_path("rooms", room_name)
        if not avatar_path.exists():
            skip_count += 1
            continue

        room_id = get_room_id(room_name)
        if not room_id:
            get_console().print(f"[yellow]⚠ Room '{room_name}' not found in Matrix[/yellow]")
            continue

        success_count += int(
            await _sync_avatar_target(
                client,
                avatar_path=avatar_path,
                room_id=room_id,
                label=f"room '{room_name}'",
            ),
        )
    return success_count, skip_count


async def _sync_root_space_avatar(
    client: nio.AsyncClient,
    config: Config,
    state: MatrixState,
) -> int:
    """Apply the managed root-space avatar when both the asset and room exist."""
    if not config.matrix_space.enabled or not state.space_room_id:
        return 0

    root_space_avatar_path = resolve_avatar_path("spaces", ROOT_SPACE_AVATAR_NAME)
    if not root_space_avatar_path.exists():
        return 0

    return int(
        await _sync_avatar_target(
            client,
            avatar_path=root_space_avatar_path,
            room_id=state.space_room_id,
            label="root space",
        ),
    )


async def set_room_avatars_in_matrix() -> None:
    """Set avatars for all rooms in Matrix."""
    console = get_console()
    console.print("\n[bold cyan]Setting room avatars in Matrix...[/bold cyan]")

    state = MatrixState.load()
    router_account = state.get_account(f"agent_{ROUTER_AGENT_NAME}")
    if not router_account:
        msg = "No router account found in Matrix state. Make sure mindroom has been started at least once."
        raise AvatarSyncError(msg)

    router_user = _build_router_user(router_account)
    try:
        client = await login_agent_user(MATRIX_HOMESERVER, router_user)
    except ValueError as exc:
        msg = f"Failed to log in as router for avatar sync: {exc}"
        raise AvatarSyncError(msg) from exc
    console.print("[green]✓ Logged in to Matrix as router[/green]")

    config = load_validated_config()
    try:
        success_count, skip_count = await _sync_configured_room_avatars(client, config)
        success_count += await _sync_root_space_avatar(client, config, state)
    finally:
        await client.close()

    if success_count > 0:
        console.print(f"\n[green]✓ Set {success_count} room avatars[/green]")
    if skip_count > 0:
        console.print(f"[dim]⊘ Skipped {skip_count} rooms (no avatar file)[/dim]")


def _build_avatar_generation_targets(
    config: Config,
    missing_targets: set[tuple[str, str]],
) -> list[AvatarTarget]:
    """Build typed avatar targets for every missing managed avatar."""
    targets: list[AvatarTarget] = []

    for agent_name, agent_config in config.agents.items():
        if ("agents", agent_name) in missing_targets:
            targets.append(
                AvatarTarget(
                    entity_type="agents",
                    entity_name=agent_name,
                    role=agent_config.role or "AI assistant",
                ),
            )

    if ("agents", "router") in missing_targets:
        targets.append(
            AvatarTarget(
                entity_type="agents",
                entity_name="router",
                role="Intelligent routing and agent selection",
            ),
        )

    for team_name, team_config in config.teams.items():
        if ("teams", team_name) in missing_targets:
            team_members = tuple(
                AvatarTeamMember(
                    name=agent_name,
                    role=config.agents[agent_name].role or "Team member",
                )
                for agent_name in team_config.agents
                if agent_name in config.agents
            )
            targets.append(
                AvatarTarget(
                    entity_type="teams",
                    entity_name=team_name,
                    role=team_config.role,
                    team_members=team_members,
                ),
            )

    targets.extend(
        AvatarTarget(
            entity_type="rooms",
            entity_name=room_name,
            role=ROOM_PURPOSES.get(room_name, f"Collaboration space for {room_name} activities"),
        )
        for room_name in _managed_room_avatar_keys(config)
        if ("rooms", room_name) in missing_targets
    )

    if ("spaces", ROOT_SPACE_AVATAR_NAME) in missing_targets:
        targets.append(
            AvatarTarget(
                entity_type="spaces",
                entity_name=ROOT_SPACE_AVATAR_NAME,
                role=f"Workspace space named {config.matrix_space.name} that organizes all managed rooms",
            ),
        )

    return targets


def _print_avatar_generation_plan(missing_targets: set[tuple[str, str]]) -> None:
    """Print the number of missing avatars that will be generated."""
    space_count = int(("spaces", ROOT_SPACE_AVATAR_NAME) in missing_targets)
    room_count = sum(1 for entity_type, _ in missing_targets if entity_type == "rooms")
    team_count = sum(1 for entity_type, _ in missing_targets if entity_type == "teams")
    agent_count = sum(1 for entity_type, _ in missing_targets if entity_type == "agents")
    get_console().print(
        f"\n[bold cyan]🚀 Generating {agent_count} agents, {team_count} teams, {room_count} rooms, and {space_count} spaces...[/bold cyan]\n",
    )


def _remaining_missing_avatar_targets(missing_targets: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """Return targets that are still missing after a generation attempt."""
    return {
        (entity_type, entity_name)
        for entity_type, entity_name in missing_targets
        if not workspace_avatar_path(entity_type, entity_name).exists()
    }


async def _generate_missing_avatars(
    config: Config,
    missing_targets: set[tuple[str, str]],
) -> bool:
    """Generate every missing managed avatar and report whether startup may continue."""
    console = get_console()
    if not missing_targets:
        console.print("\n[dim]⊘ All managed avatars already exist; skipping generation[/dim]")
        return True

    api_key = get_secret_from_env("GOOGLE_API_KEY")
    if not api_key:
        console.print("[red]Error: GOOGLE_API_KEY or GOOGLE_API_KEY_FILE environment variable not set[/red]")
        console.print("Please set it in your .env file, secrets mount, or environment")
        return False

    client = genai.Client(api_key=api_key)
    targets = _build_avatar_generation_targets(config, missing_targets)
    _print_avatar_generation_plan(missing_targets)

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task_id = progress.add_task("Processing avatars...", total=None)
            results = await asyncio.gather(
                *(generate_avatar(client, target) for target in targets),
                return_exceptions=True,
            )
            progress.update(task_id, completed=True)
    finally:
        await client.aio.aclose()

    failed_targets: list[tuple[AvatarTarget, Exception]] = []
    for target, result in zip(targets, results, strict=True):
        if isinstance(result, Exception):
            failed_targets.append((target, result))
            logger.error(
                "Avatar generation failed",
                entity_type=target.entity_type,
                entity_name=target.entity_name,
                error=repr(result),
                exc_info=(type(result), result, result.__traceback__),
            )
            console.print(f"[red]✗ Failed to generate {target.entity_type}/{target.entity_name}: {result}[/red]")

    remaining_targets = _remaining_missing_avatar_targets(missing_targets)
    if failed_targets or remaining_targets:
        failed_target_keys = {(target.entity_type, target.entity_name) for target, _error in failed_targets}
        formatted_targets = ", ".join(
            f"{entity_type}/{entity_name}"
            for entity_type, entity_name in sorted(remaining_targets | failed_target_keys)
        )
        console.print(f"\n[red]✗ Avatar generation failed for: {formatted_targets}[/red]")
        return False

    console.print("\n[bold green]✨ Avatar generation complete![/bold green]")
    return True


async def run_avatar_generation() -> None:
    """Generate missing managed avatars in the workspace."""
    config = load_validated_config()
    missing_targets = _missing_avatar_targets(config)

    if not await _generate_missing_avatars(config, missing_targets):
        msg = "Avatar generation failed. See errors above."
        raise AvatarGenerationError(msg)
