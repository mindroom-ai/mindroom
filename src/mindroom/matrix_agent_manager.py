"""Matrix agent user account management.

This module handles creation and management of individual Matrix user accounts
for each AI agent, allowing them to appear as separate users in chat rooms.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import nio
import yaml
from dotenv import load_dotenv
from loguru import logger

from .agent_loader import load_config

load_dotenv()

MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", "http://localhost:8008")
MATRIX_USERS_FILE = Path("matrix_users.yaml")


@dataclass
class AgentMatrixUser:
    """Represents a Matrix user account for an agent."""

    agent_name: str
    user_id: str
    display_name: str
    password: str
    access_token: str | None = None


async def register_matrix_user(
    username: str,
    password: str,
    display_name: str,
) -> str:
    """Register a new Matrix user account.

    Args:
        username: The username for the Matrix account (without domain)
        password: The password for the account
        display_name: The display name for the user

    Returns:
        The full Matrix user ID (e.g., @agent_calculator:localhost)

    Raises:
        ValueError: If registration fails
    """
    client = nio.AsyncClient(MATRIX_HOMESERVER)

    # Extract server name from homeserver URL
    server_name = MATRIX_HOMESERVER.split("://")[1].split(":")[0]
    user_id = f"@{username}:{server_name}"

    try:
        # Try to register the user
        response = await client.register(
            username=username,
            password=password,
            device_name="mindroom_agent",
        )

        if isinstance(response, nio.RegisterResponse):
            logger.info(f"Successfully registered user: {user_id}")
            # Set display name
            await client.login(password)
            await client.set_displayname(display_name)
            await client.close()
            return user_id
        elif (
            isinstance(response, nio.ErrorResponse)
            and hasattr(response, "status_code")
            and response.status_code == "M_USER_IN_USE"
        ):
            logger.info(f"User {user_id} already exists")
            await client.close()
            return user_id
        else:
            await client.close()
            raise ValueError(f"Failed to register user {username}: {response}")
    except Exception as e:
        await client.close()
        raise ValueError(f"Error registering user {username}: {e}") from e


async def create_agent_user(agent_name: str, agent_display_name: str) -> AgentMatrixUser:
    """Create or retrieve a Matrix user account for an agent.

    Args:
        agent_name: The internal agent name (e.g., 'calculator')
        agent_display_name: The display name for the agent (e.g., 'CalculatorAgent')

    Returns:
        AgentMatrixUser object with account details
    """
    # Check if credentials already exist in matrix_users.yaml
    existing_creds = get_agent_credentials(agent_name)

    if existing_creds:
        username = existing_creds["username"]
        password = existing_creds["password"]
        logger.info(f"Using existing credentials for agent {agent_name} from matrix_users.yaml")
    else:
        # Generate new credentials
        username = f"mindroom_{agent_name}"
        password = f"{agent_name}_secure_password_{os.urandom(8).hex()}"

        # Save to matrix_users.yaml
        save_agent_credentials(agent_name, username, password)
        logger.info(f"Generated new credentials for agent {agent_name}")

    # Extract server name from homeserver URL
    server_name = MATRIX_HOMESERVER.split("://")[1].split(":")[0]
    user_id = f"@{username}:{server_name}"

    # Try to register/verify the user
    try:
        await register_matrix_user(
            username=username,
            password=password,
            display_name=agent_display_name,
        )
    except ValueError as e:
        # If user already exists, that's fine
        if "already exists" not in str(e):
            raise

    return AgentMatrixUser(
        agent_name=agent_name,
        user_id=user_id,
        display_name=agent_display_name,
        password=password,
    )


async def login_agent_user(agent_user: AgentMatrixUser) -> nio.AsyncClient:
    """Login an agent user and return the authenticated client.

    Args:
        agent_user: The agent user to login

    Returns:
        Authenticated AsyncClient instance

    Raises:
        ValueError: If login fails
    """
    client = nio.AsyncClient(MATRIX_HOMESERVER, agent_user.user_id)

    response = await client.login(agent_user.password)
    if isinstance(response, nio.LoginResponse):
        agent_user.access_token = response.access_token
        logger.info(f"Successfully logged in agent: {agent_user.user_id}")
        return client
    else:
        await client.close()
        raise ValueError(f"Failed to login agent {agent_user.user_id}: {response}")


async def ensure_all_agent_users() -> dict[str, AgentMatrixUser]:
    """Ensure all configured agents have Matrix user accounts.

    Returns:
        Dictionary mapping agent names to AgentMatrixUser objects
    """
    config = load_config()
    agent_users = {}

    for agent_name, agent_config in config.agents.items():
        try:
            agent_user = await create_agent_user(agent_name, agent_config.display_name)
            agent_users[agent_name] = agent_user
            logger.info(f"Ensured Matrix user for agent: {agent_name} -> {agent_user.user_id}")
        except Exception as e:
            logger.error(f"Failed to create Matrix user for agent {agent_name}: {e}")

    return agent_users


def load_matrix_users() -> dict[str, dict[str, str]]:
    """Load existing matrix users from YAML file.

    Returns:
        Dictionary of existing users (excluding rooms section)
    """
    if not MATRIX_USERS_FILE.exists():
        return {}

    with open(MATRIX_USERS_FILE) as f:
        data = yaml.safe_load(f) or {}

    # Filter out non-user entries (like 'rooms')
    return {k: v for k, v in data.items() if isinstance(v, dict) and "username" in v}


def save_matrix_users(users: dict[str, dict[str, str]]) -> None:
    """Save matrix users to YAML file.

    Args:
        users: Dictionary of users to save
    """
    # Load existing data to preserve rooms section
    existing_data: dict[str, Any] = {}
    if MATRIX_USERS_FILE.exists():
        with open(MATRIX_USERS_FILE) as f:
            existing_data = yaml.safe_load(f) or {}

    # Remove the 'rooms' key from users if it exists (to avoid overwriting)
    users_only = {k: v for k, v in users.items() if k != "rooms"}

    # Merge user data with existing data, preserving rooms
    rooms_data = existing_data.get("rooms", {})
    merged_data = {**users_only}
    if rooms_data:
        merged_data["rooms"] = rooms_data

    with open(MATRIX_USERS_FILE, "w") as f:
        yaml.dump(merged_data, f, default_flow_style=False, sort_keys=False)
    logger.info(f"Saved matrix users to {MATRIX_USERS_FILE}")


def get_agent_credentials(agent_name: str) -> dict[str, str] | None:
    """Get credentials for a specific agent from matrix_users.yaml.

    Args:
        agent_name: The agent name

    Returns:
        Dictionary with username and password, or None if not found
    """
    users = load_matrix_users()
    agent_key = f"agent_{agent_name}"
    return users.get(agent_key)


def save_agent_credentials(agent_name: str, username: str, password: str) -> None:
    """Save credentials for a specific agent to matrix_users.yaml.

    Args:
        agent_name: The agent name
        username: The Matrix username
        password: The Matrix password
    """
    users = load_matrix_users()
    agent_key = f"agent_{agent_name}"
    users[agent_key] = {
        "username": username,
        "password": password,
    }
    save_matrix_users(users)
