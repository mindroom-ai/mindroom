"""Matrix user account management for agents."""

import secrets
from dataclasses import dataclass
from functools import cached_property

import httpx
import nio

from mindroom.config.main import Config
from mindroom.constants import MATRIX_SSL_VERIFY, ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger

from . import provisioning
from .client import login, matrix_client
from .identity import MatrixID, agent_username_localpart, extract_server_name_from_homeserver
from .state import MatrixState

logger = get_logger(__name__)


def account_key_for_agent(agent_name: str) -> str:
    """Build the Matrix state account key for an agent-like entity."""
    return f"agent_{agent_name}"


INTERNAL_USER_AGENT_NAME = "user"
INTERNAL_USER_ACCOUNT_KEY = account_key_for_agent(INTERNAL_USER_AGENT_NAME)


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
    agent_key = account_key_for_agent(agent_name)
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
    agent_key = account_key_for_agent(agent_name)
    state.add_account(agent_key, username, password)
    state.save()
    logger.info(f"Saved credentials for agent {agent_name}")


async def _homeserver_requires_registration_token(homeserver: str) -> bool:
    """Check whether the homeserver advertises registration-token flow."""
    url = f"{homeserver.rstrip('/')}/_matrix/client/v3/register"
    try:
        async with httpx.AsyncClient(timeout=5, verify=MATRIX_SSL_VERIFY) as client:
            response = await client.post(url, json={})
            data = response.json()
    except (httpx.HTTPError, ValueError):
        return False

    flows = data.get("flows")
    if not isinstance(flows, list):
        return False
    for flow in flows:
        if not isinstance(flow, dict):
            continue
        stages = flow.get("stages")
        if isinstance(stages, list) and "m.login.registration_token" in stages:
            return True
    return False


async def _registration_failure_message(
    response: nio.ErrorResponse,
    homeserver: str,
    registration_token: str | None,
) -> str | None:
    if (
        response.status_code == "M_FORBIDDEN"
        and registration_token
        and "Invalid registration token" in (response.message or "")
    ):
        return (
            "Matrix registration failed: MATRIX_REGISTRATION_TOKEN is invalid. "
            "Generate/issue a valid token for bot provisioning and try again."
        )

    if (
        response.message == "unknown error"
        and not registration_token
        and await _homeserver_requires_registration_token(homeserver)
    ):
        return (
            "Matrix homeserver requires registration tokens for account creation. "
            "Set MATRIX_REGISTRATION_TOKEN and retry."
        )

    return None


async def _login_and_sync_display_name(
    *,
    client: nio.AsyncClient,
    user_id: str,
    password: str,
    display_name: str,
) -> None:
    """Login with known password and keep display name synchronized."""
    login_response = await client.login(password)
    if isinstance(login_response, nio.LoginResponse):
        display_response = await client.set_displayname(display_name)
        if isinstance(display_response, nio.ErrorResponse):
            logger.warning(f"Failed to set display name for existing user: {display_response}")
        return

    msg = (
        f"Matrix account collision for {user_id}: the user already exists but login with the configured password failed "
        f"({login_response}). Set a unique MINDROOM_NAMESPACE (or choose different names) and retry."
    )
    raise ValueError(msg)


async def register_user(
    homeserver: str,
    username: str,
    password: str,
    display_name: str,
) -> str:
    """Register a new Matrix user account.

    Args:
        homeserver: The Matrix homeserver URL
        username: The username for the Matrix account (without domain)
        password: The password for the account
        display_name: The display name for the user

    Returns:
        The full Matrix user ID (e.g., @user:localhost)

    Raises:
        ValueError: If registration fails

    """
    # Extract server name from homeserver URL
    server_name = extract_server_name_from_homeserver(homeserver)
    user_id = MatrixID.from_username(username, server_name).full_id

    registration_token = provisioning.registration_token_from_env()
    provisioning_url = provisioning.provisioning_url_from_env()
    creds = provisioning.required_local_provisioning_client_credentials_for_registration(
        provisioning_url=provisioning_url,
        registration_token=registration_token,
    )
    if creds and provisioning_url:
        client_id, client_secret = creds
        provisioning_result = await provisioning.register_user_via_provisioning_service(
            provisioning_url=provisioning_url,
            client_id=client_id,
            client_secret=client_secret,
            homeserver=homeserver,
            username=username,
            password=password,
            display_name=display_name,
        )
        if provisioning_result.status == "created":
            logger.info(f"✅ Successfully registered user via provisioning service: {provisioning_result.user_id}")
            return provisioning_result.user_id

        login_user_id = user_id
        if provisioning_result.user_id != user_id:
            logger.warning(
                "Provisioning service returned mismatched user_id for user_in_use; using local server_name-derived ID",
                provisioning_user_id=provisioning_result.user_id,
                expected_user_id=user_id,
            )

        logger.info(f"User {login_user_id} already exists (provisioning service)")
        async with matrix_client(homeserver, user_id=login_user_id) as client:
            await _login_and_sync_display_name(
                client=client,
                user_id=login_user_id,
                password=password,
                display_name=display_name,
            )
        return login_user_id

    async with matrix_client(homeserver, user_id=user_id) as client:
        # Try to register the user
        if registration_token:
            response = await client.register_with_token(
                username=username,
                password=password,
                registration_token=registration_token,
                device_name="mindroom_agent",
            )
        else:
            response = await client.register(
                username=username,
                password=password,
                device_name="mindroom_agent",
            )

        if isinstance(response, nio.RegisterResponse):
            logger.info(f"✅ Successfully registered user: {user_id}")
            # After registration, we already have an access token
            client.user_id = response.user_id
            client.access_token = response.access_token
            client.device_id = response.device_id

            # Set display name using the existing session
            display_response = await client.set_displayname(display_name)
            if isinstance(display_response, nio.ErrorResponse):
                logger.warning(f"Failed to set display name: {display_response}")

            return user_id
        if isinstance(response, nio.ErrorResponse) and response.status_code == "M_USER_IN_USE":
            logger.info(f"User {user_id} already exists")
            await _login_and_sync_display_name(
                client=client,
                user_id=user_id,
                password=password,
                display_name=display_name,
            )
            return user_id

        if isinstance(response, nio.ErrorResponse):
            failure_message = await _registration_failure_message(response, homeserver, registration_token)
            if failure_message:
                raise ValueError(failure_message)
        msg = f"Failed to register user {username}: {response}"
        raise ValueError(msg)


async def create_agent_user(
    homeserver: str,
    agent_name: str,
    agent_display_name: str,
    username: str | None = None,
) -> AgentMatrixUser:
    """Create or retrieve a Matrix user account for an agent.

    Args:
        homeserver: The Matrix homeserver URL
        agent_name: The internal agent name (e.g., 'calculator')
        agent_display_name: The display name for the agent (e.g., 'CalculatorAgent')
        username: Optional explicit Matrix username localpart to use

    Returns:
        AgentMatrixUser object with account details

    """
    # Check if credentials already exist in matrix_state.yaml
    existing_creds = get_agent_credentials(agent_name)
    preferred_username = username

    if (
        agent_name == INTERNAL_USER_AGENT_NAME
        and preferred_username is not None
        and existing_creds
        and existing_creds["username"] != preferred_username
    ):
        msg = (
            "mindroom_user.username cannot be changed after first startup "
            f"(existing: '{existing_creds['username']}', configured: '{preferred_username}'). "
            "Keep the existing username and change mindroom_user.display_name instead."
        )
        raise ValueError(msg)

    if existing_creds and (preferred_username is None or existing_creds["username"] == preferred_username):
        matrix_username = existing_creds["username"]
        password = existing_creds["password"]
        logger.info(f"Using existing credentials for agent {agent_name} from matrix_state.yaml")
        registration_needed = False
    else:
        # Generate new credentials
        matrix_username = preferred_username or agent_username_localpart(agent_name)
        password = secrets.token_urlsafe(24)
        logger.info(f"Generated new credentials for agent {agent_name}")
        registration_needed = True

    # Extract server name from homeserver URL
    server_name = extract_server_name_from_homeserver(homeserver)
    user_id = MatrixID.from_username(matrix_username, server_name).full_id

    await register_user(
        homeserver=homeserver,
        username=matrix_username,
        password=password,
        display_name=agent_display_name,
    )

    # Save credentials only after registration/verification succeeds.
    if registration_needed:
        save_agent_credentials(agent_name, matrix_username, password)
        logger.info(f"Saved credentials for agent {agent_name} after successful registration")

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
