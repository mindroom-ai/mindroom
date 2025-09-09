"""Matrix user account management for agents."""

from dataclasses import dataclass
from functools import cached_property

import nio

from mindroom.config import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger

from .client import login, register_user
from .identity import MatrixID, extract_server_name_from_homeserver
from .state import MatrixState

logger = get_logger(__name__)


def extract_domain_from_user_id(user_id: str) -> str:
    """Extract domain from a Matrix user ID like "@user:example.com"."""
    if not user_id.startswith("@") or ":" not in user_id:
        return "localhost"
    return MatrixID.parse(user_id).domain


@dataclass
class AgentMatrixUser:
    """Represents a Matrix user account for an agent."""

    agent_name: str
    user_id: str
    display_name: str
    password: str
    access_token: str | None = None

    @cached_property
    def matrix_id(self) -> MatrixID:
        """MatrixID object from user_id."""
        return MatrixID.parse(self.user_id)


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
        registration_needed = False
    else:
        # Generate new credentials
        username = f"mindroom_{agent_name}"
        password = f"{agent_name}_secure_password"  # _{os.urandom(8).hex()}"
        logger.info(f"Generated new credentials for agent {agent_name}")
        registration_needed = True

    # Extract server name from homeserver URL
    server_name = extract_server_name_from_homeserver(homeserver)
    user_id = MatrixID.from_username(username, server_name).full_id

    # Try to register/verify the user
    try:
        await register_user(
            homeserver=homeserver,
            username=username,
            password=password,
            display_name=agent_display_name,
        )
        # Only save credentials after successful registration
        if registration_needed:
            save_agent_credentials(agent_name, username, password)
            logger.info(f"Saved credentials for agent {agent_name} after successful registration")
    except ValueError as e:
        # If user already exists, that's fine
        error_msg = str(e) if e else ""
        logger.debug(f"ValueError when registering {username}: {error_msg}")
        if "already exists" not in error_msg and "RegisterErrorResponse" not in error_msg:
            raise
        # Save credentials if the user already exists (registration succeeded in the past)
        if registration_needed and "already exists" in error_msg:
            save_agent_credentials(agent_name, username, password)
            logger.info(f"Saved credentials for agent {agent_name} (user already exists)")

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
    client = await login(homeserver, agent_user.user_id, agent_user.password)
    agent_user.access_token = client.access_token
    return client


# TODO: Check, this seems unused!
async def ensure_all_agent_users(homeserver: str, config: Config) -> dict[str, AgentMatrixUser]:
    """Ensure all configured agents and teams have Matrix user accounts.

    This includes user-configured agents, teams, and the built-in router agent.

    Args:
        homeserver: The Matrix homeserver URL
        config: Application configuration

    Returns:
        Dictionary mapping agent/team names to AgentMatrixUser objects

    """
    agent_users = {}

    # First, create the built-in router agent
    try:
        router_user = await create_agent_user(
            homeserver,
            ROUTER_AGENT_NAME,
            "RouterAgent",
        )
        agent_users[ROUTER_AGENT_NAME] = router_user
        logger.info(f"Ensured Matrix user for built-in router agent: {router_user.user_id}")
    except Exception:
        logger.exception("Failed to create Matrix user for built-in router agent")

    # Create user-configured agents
    for agent_name, agent_config in config.agents.items():
        try:
            agent_user = await create_agent_user(
                homeserver,
                agent_name,
                agent_config.display_name,
            )
            agent_users[agent_name] = agent_user
            logger.info(f"Ensured Matrix user for agent: {agent_name} -> {agent_user.user_id}")
        except Exception:
            # Continue with other agents even if one fails
            logger.exception("Failed to create Matrix user for agent", agent_name=agent_name)

    # Create team users
    for team_name, team_config in config.teams.items():
        try:
            team_user = await create_agent_user(
                homeserver,
                team_name,
                team_config.display_name,
            )
            agent_users[team_name] = team_user
            logger.info(f"Ensured Matrix user for team: {team_name} -> {team_user.user_id}")
        except Exception:
            # Continue with other teams even if one fails
            logger.exception("Failed to create Matrix user for team", team_name=team_name)

    return agent_users
