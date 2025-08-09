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
    from mindroom.bot import main

    console.print(f"🚀 Starting Mindroom multi-agent system (log level: {log_level})...\n")

    # Ensure we have a user account
    await _ensure_user_account()

    # Agent accounts and rooms are created automatically by the bot system
    console.print("\n🤖 Starting agents...")
    console.print("Press Ctrl+C to stop\n")

    try:
        await main(log_level=log_level, storage_path=storage_path)
    except KeyboardInterrupt:
        console.print("\n✋ Stopped")


def main():
    """Main entry point that shows help by default."""

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
