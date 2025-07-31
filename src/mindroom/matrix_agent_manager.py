"""Matrix agent user account management.

This module handles creation and management of individual Matrix user accounts
for each AI agent, allowing them to appear as separate users in chat rooms.
"""

import os
from dataclasses import dataclass

import nio
from dotenv import load_dotenv
from loguru import logger

from .agent_loader import load_config
from .matrix_config import MatrixConfig
from .matrix_utils import matrix_client

load_dotenv()

MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", "http://localhost:8008")


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
    # Extract server name from homeserver URL
    server_name = MATRIX_HOMESERVER.split("://")[1].split(":")[0]
    user_id = f"@{username}:{server_name}"

    async with matrix_client(MATRIX_HOMESERVER) as client:
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
            return user_id
        elif (
            isinstance(response, nio.ErrorResponse)
            and hasattr(response, "status_code")
            and response.status_code == "M_USER_IN_USE"
        ):
            logger.info(f"User {user_id} already exists")
            return user_id
        else:
            raise ValueError(f"Failed to register user {username}: {response}")


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
    # Create a store path for this agent to persist sync tokens
    # This prevents the agent from processing old messages on restart
    store_path = os.path.join("tmp", "stores", agent_user.agent_name)
    os.makedirs(store_path, exist_ok=True)

    # Configure client to store sync tokens
    # This ensures the agent remembers which messages it has already seen
    config = nio.AsyncClientConfig(
        store_sync_tokens=True,
    )

    client = nio.AsyncClient(
        MATRIX_HOMESERVER,
        agent_user.user_id,
        store_path=store_path,
        config=config,
    )

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


def get_agent_credentials(agent_name: str) -> dict[str, str] | None:
    """Get credentials for a specific agent from matrix_users.yaml.

    Args:
        agent_name: The agent name

    Returns:
        Dictionary with username and password, or None if not found
    """
    config = MatrixConfig.load()
    agent_key = f"agent_{agent_name}"
    account = config.get_account(agent_key)
    if account:
        return {"username": account.username, "password": account.password}
    return None


def save_agent_credentials(agent_name: str, username: str, password: str) -> None:
    """Save credentials for a specific agent to matrix_users.yaml.

    Args:
        agent_name: The agent name
        username: The Matrix username
        password: The Matrix password
    """
    config = MatrixConfig.load()
    agent_key = f"agent_{agent_name}"
    config.add_account(agent_key, username, password)
    config.save()
    logger.info(f"Saved credentials for agent {agent_name}")
