"""Matrix user account management for agents."""

import os
from dataclasses import dataclass

import nio

from ..agent_config import load_config
from ..logging_config import get_logger
from .state import MatrixState

logger = get_logger(__name__)


def extract_domain_from_user_id(user_id: str) -> str:
    """Extract domain from a Matrix user ID like "@user:example.com"."""
    if ":" in user_id:
        return user_id.split(":", 1)[1]
    return "localhost"


def extract_username_from_user_id(user_id: str) -> str:
    """Extract username from a Matrix user ID like "@mindroom_calculator:example.com"."""
    if user_id.startswith("@"):
        username = user_id[1:]  # Remove @
        if ":" in username:
            return username.split(":", 1)[0]
        return username
    return user_id


def extract_server_name_from_homeserver(homeserver: str) -> str:
    """Extract server name from a homeserver URL like "http://localhost:8008"."""
    # Remove protocol
    server_part = homeserver.split("://", 1)[1] if "://" in homeserver else homeserver

    # Remove port if present
    if ":" in server_part:
        return server_part.split(":", 1)[0]
    return server_part


def construct_agent_user_id(agent_name: str, domain: str) -> str:
    """Construct a Matrix user ID for an agent like "@mindroom_calculator:localhost"."""
    return f"@mindroom_{agent_name}:{domain}"


def extract_agent_name(sender_id: str) -> str | None:
    """Extract agent name from Matrix user ID like @mindroom_calculator:localhost.

    Returns agent name (e.g., 'calculator') or None if not an agent.
    """
    if not sender_id.startswith("@mindroom_"):
        return None

    # Extract username part
    username = sender_id.split(":")[0][1:]  # Remove @ and domain

    # Skip regular users
    if username.startswith("mindroom_user"):
        return None

    # Extract potential agent name after mindroom_
    agent_name = username.replace("mindroom_", "")

    # Check if this is actually a configured agent
    from ..agent_config import load_config

    config = load_config()
    if agent_name in config.agents:
        return agent_name

    return None


@dataclass
class AgentMatrixUser:
    """Represents a Matrix user account for an agent."""

    agent_name: str
    user_id: str
    display_name: str
    password: str
    access_token: str | None = None


def get_agent_credentials(agent_name: str) -> dict[str, str] | None:
    """Get credentials for a specific agent from matrix_state.yaml.

    Args:
        agent_name: The agent name

    Returns:
        Dictionary with username and password, or None if not found
    """
    state = MatrixState.load()
    agent_key = f"agent_{agent_name}"
    account = state.get_account(agent_key)
    if account:
        return {"username": account.username, "password": account.password}
    return None


def save_agent_credentials(agent_name: str, username: str, password: str) -> None:
    """Save credentials for a specific agent to matrix_state.yaml.

    Args:
        agent_name: The agent name
        username: The Matrix username
        password: The Matrix password
    """
    state = MatrixState.load()
    agent_key = f"agent_{agent_name}"
    state.add_account(agent_key, username, password)
    state.save()
    logger.info(f"Saved credentials for agent {agent_name}")


async def create_agent_user(
    homeserver: str,
    agent_name: str,
    agent_display_name: str,
) -> AgentMatrixUser:
    """Create or retrieve a Matrix user account for an agent.

    Args:
        homeserver: The Matrix homeserver URL
        agent_name: The internal agent name (e.g., 'calculator')
        agent_display_name: The display name for the agent (e.g., 'CalculatorAgent')

    Returns:
        AgentMatrixUser object with account details
    """
    # Check if credentials already exist in matrix_state.yaml
    existing_creds = get_agent_credentials(agent_name)

    if existing_creds:
        username = existing_creds["username"]
        password = existing_creds["password"]
        logger.info(f"Using existing credentials for agent {agent_name} from matrix_state.yaml")
    else:
        # Generate new credentials
        username = f"mindroom_{agent_name}"
        password = f"{agent_name}_secure_password_{os.urandom(8).hex()}"

        # Save to matrix_state.yaml
        save_agent_credentials(agent_name, username, password)
        logger.info(f"Generated new credentials for agent {agent_name}")

    # Extract server name from homeserver URL
    server_name = extract_server_name_from_homeserver(homeserver)
    user_id = f"@{username}:{server_name}"

    # Try to register/verify the user
    try:
        from .client import register_user

        await register_user(
            homeserver=homeserver,
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


async def login_agent_user(homeserver: str, agent_user: AgentMatrixUser) -> nio.AsyncClient:
    """Login an agent user and return the authenticated client.

    Args:
        homeserver: The Matrix homeserver URL
        agent_user: The agent user to login

    Returns:
        Authenticated AsyncClient instance

    Raises:
        ValueError: If login fails
    """
    from .client import login

    client = await login(homeserver, agent_user.user_id, agent_user.password)
    agent_user.access_token = client.access_token
    return client


async def ensure_all_agent_users(homeserver: str) -> dict[str, AgentMatrixUser]:
    """Ensure all configured agents have Matrix user accounts.

    Args:
        homeserver: The Matrix homeserver URL

    Returns:
        Dictionary mapping agent names to AgentMatrixUser objects
    """
    config = load_config()
    agent_users = {}

    for agent_name, agent_config in config.agents.items():
        try:
            agent_user = await create_agent_user(
                homeserver,
                agent_name,
                agent_config.display_name,
            )
            agent_users[agent_name] = agent_user
            logger.info(f"Ensured Matrix user for agent: {agent_name} -> {agent_user.user_id}")
        except Exception as e:
            # Continue with other agents even if one fails
            logger.error(f"Failed to create Matrix user for agent {agent_name}: {e}")

    return agent_users
