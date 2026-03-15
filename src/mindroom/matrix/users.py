"""Matrix user account management for agents."""

import secrets
from dataclasses import dataclass
from functools import cached_property

import httpx
import nio

from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME, RuntimePaths, runtime_matrix_ssl_verify
from mindroom.logging_config import get_logger
from mindroom.matrix import provisioning
from mindroom.matrix.client import (
    login,
    matrix_client,
    matrix_startup_error,
)
from mindroom.matrix.identity import MatrixID, agent_username_localpart, extract_server_name_from_homeserver
from mindroom.matrix.state import MatrixState

logger = get_logger(__name__)

_INVALID_REGISTRATION_TOKEN_MESSAGE = (
    "Matrix registration failed: MATRIX_REGISTRATION_TOKEN is invalid. "  # noqa: S105
    "Generate/issue a valid token for bot provisioning and try again."
)


def _account_key_for_agent(agent_name: str) -> str:
    """Build the Matrix state account key for an agent-like entity."""
    return f"agent_{agent_name}"


INTERNAL_USER_AGENT_NAME = "user"
INTERNAL_USER_ACCOUNT_KEY = _account_key_for_agent(INTERNAL_USER_AGENT_NAME)


def _extract_domain_from_user_id(user_id: str) -> str:
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


def _get_agent_credentials(
    agent_name: str,
    runtime_paths: RuntimePaths,
) -> dict[str, str] | None:
    """Get credentials for a specific agent from matrix_state.yaml.

    Args:
        agent_name: The agent name
        runtime_paths: Explicit runtime context for matrix state lookup

    Returns:
        Dictionary with username and password, or None if not found

    """
    state = MatrixState.load(runtime_paths=runtime_paths)
    agent_key = _account_key_for_agent(agent_name)
    account = state.get_account(agent_key)
    if account:
        return {"username": account.username, "password": account.password}
    return None


def _save_agent_credentials(
    agent_name: str,
    username: str,
    password: str,
    runtime_paths: RuntimePaths,
) -> None:
    """Save credentials for a specific agent to matrix_state.yaml.

    Args:
        agent_name: The agent name
        username: The Matrix username
        password: The Matrix password
        runtime_paths: Explicit runtime context for matrix state persistence

    """
    state = MatrixState.load(runtime_paths=runtime_paths)
    agent_key = _account_key_for_agent(agent_name)
    state.add_account(agent_key, username, password)
    state.save(runtime_paths=runtime_paths)
    logger.info(f"Saved credentials for agent {agent_name}")


async def _homeserver_requires_registration_token(
    homeserver: str,
    runtime_paths: RuntimePaths,
) -> bool:
    """Check whether the homeserver advertises registration-token flow."""
    url = f"{homeserver.rstrip('/')}/_matrix/client/v3/register"
    try:
        async with httpx.AsyncClient(
            timeout=5,
            verify=runtime_matrix_ssl_verify(runtime_paths=runtime_paths),
        ) as client:
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
    runtime_paths: RuntimePaths,
) -> str | None:
    if (
        response.status_code == "M_FORBIDDEN"
        and registration_token
        and "Invalid registration token" in (response.message or "")
    ):
        return _INVALID_REGISTRATION_TOKEN_MESSAGE

    if (
        response.message == "unknown error"
        and not registration_token
        and await _homeserver_requires_registration_token(homeserver, runtime_paths)
    ):
        return (
            "Matrix homeserver requires registration tokens for account creation. "
            "Set MATRIX_REGISTRATION_TOKEN and retry."
        )

    return None


async def _register_user_with_token(
    *,
    homeserver: str,
    user_id: str,
    username: str,
    password: str,
    display_name: str,
    registration_token: str,
    runtime_paths: RuntimePaths,
) -> str:
    """Register a user with token auth, supporting both direct and UIAA flows."""
    register_url = f"{homeserver.rstrip('/')}/_matrix/client/v3/register"
    request_payload = {
        "username": username,
        "password": password,
        "device_name": "mindroom_agent",
        "auth": {
            "type": "m.login.registration_token",
            "token": registration_token,
        },
    }

    try:
        async with httpx.AsyncClient(
            timeout=10,
            verify=runtime_matrix_ssl_verify(runtime_paths=runtime_paths),
        ) as client:
            response = await client.post(register_url, json=request_payload)
    except httpx.HTTPError as exc:
        msg = f"Could not reach Matrix homeserver ({homeserver}) during registration: {exc}"
        raise matrix_startup_error(msg) from exc

    detail, errcode = _registration_http_error_details(response)
    if response.is_success:
        logger.info(f"✅ Successfully registered user with token: {user_id}")
    elif errcode == "M_USER_IN_USE":
        logger.info(f"User {user_id} already exists")
    else:
        permanent_error = _direct_token_registration_error(username=username, errcode=errcode, detail=detail)
        if permanent_error is not None:
            raise permanent_error
        logger.info(
            "Direct token registration failed; falling back to matrix-nio interactive registration",
            user_id=user_id,
            status_code=response.status_code,
            errcode=errcode,
            detail=detail,
        )
        return await _register_user_with_token_via_nio(
            homeserver=homeserver,
            user_id=user_id,
            username=username,
            password=password,
            display_name=display_name,
            registration_token=registration_token,
            runtime_paths=runtime_paths,
        )

    return await _login_existing_user_or_raise_collision(
        homeserver=homeserver,
        user_id=user_id,
        password=password,
        display_name=display_name,
        runtime_paths=runtime_paths,
    )


def _registration_http_error_details(response: httpx.Response) -> tuple[str, str | None]:
    """Extract a human-readable error detail and errcode from an HTTP response."""
    detail = response.text.strip() or "unknown error"
    errcode = None
    try:
        body = response.json()
    except ValueError:
        return detail, errcode

    if not isinstance(body, dict):
        return detail, errcode
    raw_errcode = body.get("errcode")
    if isinstance(raw_errcode, str) and raw_errcode:
        errcode = raw_errcode
    raw_error = body.get("error")
    if raw_error is not None:
        detail = str(raw_error)
    return detail, errcode


def _direct_token_registration_error(
    *,
    username: str,
    errcode: str | None,
    detail: str,
) -> ValueError | None:
    """Return a permanent startup error for terminal direct token registration failures."""
    if errcode == "M_FORBIDDEN" and "Invalid registration token" in detail:
        return matrix_startup_error(_INVALID_REGISTRATION_TOKEN_MESSAGE, permanent=True)
    if errcode == "M_INVALID_USERNAME":
        msg = f"Failed to register user {username}: {errcode}"
        return matrix_startup_error(msg, permanent=True)
    return None


async def _register_user_with_token_via_nio(
    *,
    homeserver: str,
    user_id: str,
    username: str,
    password: str,
    display_name: str,
    registration_token: str,
    runtime_paths: RuntimePaths,
) -> str:
    """Fallback to matrix-nio's interactive registration for spec-strict homeservers."""

    async def _register_with_client(client: nio.AsyncClient) -> str:
        response = await client.register_with_token(
            username=username,
            password=password,
            registration_token=registration_token,
            device_name="mindroom_agent",
        )
        return await _handle_register_response(
            response=response,
            client=client,
            homeserver=homeserver,
            user_id=user_id,
            username=username,
            password=password,
            display_name=display_name,
            registration_token=registration_token,
            runtime_paths=runtime_paths,
        )

    async with matrix_client(homeserver, user_id=user_id, runtime_paths=runtime_paths) as client:
        return await _register_with_client(client)


def _account_collision_error(user_id: str, login_response: object) -> ValueError:
    msg = (
        f"Matrix account collision for {user_id}: the user already exists but login with the configured password failed "
        f"({login_response}). Set a unique MINDROOM_NAMESPACE (or choose different names) and retry."
    )
    return matrix_startup_error(msg, permanent=True)


async def _login_and_sync_display_name(
    *,
    client: nio.AsyncClient,
    password: str,
    display_name: str,
) -> nio.LoginResponse | nio.LoginError:
    """Login with known password and keep display name synchronized."""
    login_response = await client.login(password)
    if isinstance(login_response, nio.LoginResponse):
        display_response = await client.set_displayname(display_name)
        if isinstance(display_response, nio.ErrorResponse):
            logger.warning(f"Failed to set display name for existing user: {display_response}")
    return login_response


async def _login_existing_user(
    *,
    homeserver: str,
    user_id: str,
    password: str,
    display_name: str,
    runtime_paths: RuntimePaths,
) -> nio.LoginResponse | nio.LoginError:
    """Login an existing user with a fresh client and keep the display name synchronized."""

    async def _login_with_client(client: nio.AsyncClient) -> nio.LoginResponse | nio.LoginError:
        return await _login_and_sync_display_name(
            client=client,
            password=password,
            display_name=display_name,
        )

    async with matrix_client(homeserver, user_id=user_id, runtime_paths=runtime_paths) as client:
        return await _login_with_client(client)


async def _login_existing_user_or_raise_collision(
    *,
    homeserver: str,
    user_id: str,
    password: str,
    display_name: str,
    runtime_paths: RuntimePaths,
) -> str:
    """Login an existing user, sync display name, and fail permanently on collisions."""
    login_response = await _login_existing_user(
        homeserver=homeserver,
        user_id=user_id,
        password=password,
        display_name=display_name,
        runtime_paths=runtime_paths,
    )
    if not isinstance(login_response, nio.LoginResponse):
        raise _account_collision_error(user_id, login_response)
    return user_id


async def _login_existing_user_with_client_or_raise_collision(
    *,
    client: nio.AsyncClient,
    user_id: str,
    password: str,
    display_name: str,
) -> str:
    """Login an existing user with a provided client, sync display name, and fail on collisions."""
    login_response = await _login_and_sync_display_name(
        client=client,
        password=password,
        display_name=display_name,
    )
    if not isinstance(login_response, nio.LoginResponse):
        raise _account_collision_error(user_id, login_response)
    return user_id


async def _handle_register_response(
    *,
    response: nio.RegisterResponse | nio.ErrorResponse,
    client: nio.AsyncClient,
    homeserver: str,
    user_id: str,
    username: str,
    password: str,
    display_name: str,
    registration_token: str | None,
    runtime_paths: RuntimePaths,
) -> str:
    """Handle a matrix-nio register response and finalize account setup."""
    if isinstance(response, nio.RegisterResponse):
        logger.info(f"✅ Successfully registered user: {user_id}")
        client.user_id = response.user_id
        client.access_token = response.access_token
        client.device_id = response.device_id

        display_response = await client.set_displayname(display_name)
        if isinstance(display_response, nio.ErrorResponse):
            logger.warning(f"Failed to set display name: {display_response}")

        return user_id
    if isinstance(response, nio.ErrorResponse) and response.status_code == "M_USER_IN_USE":
        logger.info(f"User {user_id} already exists")
        return await _login_existing_user_with_client_or_raise_collision(
            client=client,
            user_id=user_id,
            password=password,
            display_name=display_name,
        )

    if not isinstance(response, nio.ErrorResponse):
        msg = f"Failed to register user {username}: {response}"
        raise matrix_startup_error(msg)
    failure_message = await _registration_failure_message(
        response,
        homeserver,
        registration_token,
        runtime_paths,
    )
    if failure_message:
        raise matrix_startup_error(failure_message, permanent=True)
    msg = f"Failed to register user {username}: {response}"
    raise matrix_startup_error(msg, response=response)


async def _register_user(
    homeserver: str,
    username: str,
    password: str,
    display_name: str,
    runtime_paths: RuntimePaths,
) -> str:
    """Register a new Matrix user account.

    Args:
        homeserver: The Matrix homeserver URL
        username: The username for the Matrix account (without domain)
        password: The password for the account
        display_name: The display name for the user
        runtime_paths: Optional explicit runtime context for env and SSL resolution

    Returns:
        The full Matrix user ID (e.g., @user:localhost)

    Raises:
        ValueError: If registration fails

    """
    server_name = extract_server_name_from_homeserver(homeserver, runtime_paths=runtime_paths)
    user_id = MatrixID.from_username(username, server_name).full_id
    registration_token = provisioning.registration_token_from_env(runtime_paths=runtime_paths)

    provisioning_result = await _register_user_via_provisioning_if_configured(
        homeserver=homeserver,
        user_id=user_id,
        username=username,
        password=password,
        display_name=display_name,
        registration_token=registration_token,
        runtime_paths=runtime_paths,
    )
    if provisioning_result is not None:
        return provisioning_result
    if registration_token:
        return await _register_user_with_token(
            homeserver=homeserver,
            user_id=user_id,
            username=username,
            password=password,
            display_name=display_name,
            registration_token=registration_token,
            runtime_paths=runtime_paths,
        )
    return await _register_user_without_token(
        homeserver=homeserver,
        user_id=user_id,
        username=username,
        password=password,
        display_name=display_name,
        runtime_paths=runtime_paths,
    )


async def _register_user_via_provisioning_if_configured(
    *,
    homeserver: str,
    user_id: str,
    username: str,
    password: str,
    display_name: str,
    registration_token: str | None,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Register through the provisioning service when local client creds are configured."""
    provisioning_url = provisioning.provisioning_url_from_env(runtime_paths=runtime_paths)
    creds = provisioning.required_local_provisioning_client_credentials_for_registration(
        provisioning_url=provisioning_url,
        registration_token=registration_token,
        runtime_paths=runtime_paths,
    )
    if not (creds and provisioning_url):
        return None

    client_id, client_secret = creds
    provisioning_result = await provisioning.register_user_via_provisioning_service(
        provisioning_url=provisioning_url,
        client_id=client_id,
        client_secret=client_secret,
        homeserver=homeserver,
        username=username,
        password=password,
        display_name=display_name,
        runtime_paths=runtime_paths,
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
    return await _login_existing_user_or_raise_collision(
        homeserver=homeserver,
        user_id=login_user_id,
        password=password,
        display_name=display_name,
        runtime_paths=runtime_paths,
    )


async def _register_user_without_token(
    *,
    homeserver: str,
    user_id: str,
    username: str,
    password: str,
    display_name: str,
    runtime_paths: RuntimePaths,
) -> str:
    """Register directly against the Matrix homeserver without token auth."""

    async def _register_with_client(client: nio.AsyncClient) -> str:
        response = await client.register(
            username=username,
            password=password,
            device_name="mindroom_agent",
        )
        return await _handle_register_response(
            response=response,
            client=client,
            homeserver=homeserver,
            user_id=user_id,
            username=username,
            password=password,
            display_name=display_name,
            registration_token=None,
            runtime_paths=runtime_paths,
        )

    async with matrix_client(homeserver, user_id=user_id, runtime_paths=runtime_paths) as client:
        return await _register_with_client(client)


async def _register_user_for_runtime(
    *,
    homeserver: str,
    username: str,
    password: str,
    display_name: str,
    runtime_paths: RuntimePaths,
) -> None:
    """Register one Matrix user using the optional explicit runtime context."""
    await _register_user(
        homeserver=homeserver,
        username=username,
        password=password,
        display_name=display_name,
        runtime_paths=runtime_paths,
    )


async def _login_existing_user_for_runtime(
    *,
    homeserver: str,
    user_id: str,
    password: str,
    display_name: str,
    runtime_paths: RuntimePaths,
) -> nio.LoginResponse | nio.LoginError:
    """Login one Matrix user using the optional explicit runtime context."""
    return await _login_existing_user(
        homeserver=homeserver,
        user_id=user_id,
        password=password,
        display_name=display_name,
        runtime_paths=runtime_paths,
    )


async def create_agent_user(
    homeserver: str,
    agent_name: str,
    agent_display_name: str,
    runtime_paths: RuntimePaths,
    username: str | None = None,
) -> AgentMatrixUser:
    """Create or retrieve a Matrix user account for an agent.

    Args:
        homeserver: The Matrix homeserver URL
        agent_name: The internal agent name (e.g., 'calculator')
        agent_display_name: The display name for the agent (e.g., 'CalculatorAgent')
        username: Optional explicit Matrix username localpart to use
        runtime_paths: Optional explicit runtime context for env and SSL resolution

    Returns:
        AgentMatrixUser object with account details

    """
    # Check if credentials already exist in matrix_state.yaml
    existing_creds = _get_agent_credentials(agent_name, runtime_paths)
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
        raise matrix_startup_error(msg, permanent=True)

    if existing_creds and (preferred_username is None or existing_creds["username"] == preferred_username):
        matrix_username = existing_creds["username"]
        password = existing_creds["password"]
        logger.info(f"Using existing credentials for agent {agent_name} from matrix_state.yaml")
        registration_needed = False
    else:
        # Generate new credentials
        matrix_username = preferred_username or agent_username_localpart(agent_name, runtime_paths=runtime_paths)
        password = secrets.token_urlsafe(24)
        logger.info(f"Generated new credentials for agent {agent_name}")
        registration_needed = True

    # Extract server name from homeserver URL
    server_name = extract_server_name_from_homeserver(homeserver, runtime_paths=runtime_paths)
    user_id = MatrixID.from_username(matrix_username, server_name).full_id

    if registration_needed:
        await _register_user_for_runtime(
            homeserver=homeserver,
            username=matrix_username,
            password=password,
            display_name=agent_display_name,
            runtime_paths=runtime_paths,
        )
    else:
        login_response = await _login_existing_user_for_runtime(
            homeserver=homeserver,
            user_id=user_id,
            password=password,
            display_name=agent_display_name,
            runtime_paths=runtime_paths,
        )
        if not isinstance(login_response, nio.LoginResponse):
            logger.info(
                "Existing Matrix credentials failed login; attempting registration to recover account",
                agent_name=agent_name,
                user_id=user_id,
            )
            await _register_user_for_runtime(
                homeserver=homeserver,
                username=matrix_username,
                password=password,
                display_name=agent_display_name,
                runtime_paths=runtime_paths,
            )

    # Save credentials only after registration/verification succeeds.
    if registration_needed:
        _save_agent_credentials(agent_name, matrix_username, password, runtime_paths)
        logger.info(f"Saved credentials for agent {agent_name} after successful registration")

    return AgentMatrixUser(
        agent_name=agent_name,
        user_id=user_id,
        display_name=agent_display_name,
        password=password,
    )


async def login_agent_user(
    homeserver: str,
    agent_user: AgentMatrixUser,
    runtime_paths: RuntimePaths,
) -> nio.AsyncClient:
    """Login an agent user and return the authenticated client.

    Args:
        homeserver: The Matrix homeserver URL
        agent_user: The agent user to login
        runtime_paths: Optional explicit runtime context for env and SSL resolution

    Returns:
        Authenticated AsyncClient instance

    Raises:
        ValueError: If login fails

    """
    client = await login(
        homeserver,
        agent_user.user_id,
        agent_user.password,
        runtime_paths=runtime_paths,
    )
    agent_user.access_token = client.access_token
    return client


# TODO: Check, this seems unused!
async def _ensure_all_agent_users(
    homeserver: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> dict[str, AgentMatrixUser]:
    """Ensure all configured agents and teams have Matrix user accounts.

    This includes user-configured agents, teams, and the built-in router agent.

    Args:
        homeserver: The Matrix homeserver URL
        config: Application configuration
        runtime_paths: Explicit runtime context for Matrix IDs and credential storage

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
            runtime_paths=runtime_paths,
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
                runtime_paths=runtime_paths,
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
                runtime_paths=runtime_paths,
            )
            agent_users[team_name] = team_user
            logger.info(f"Ensured Matrix user for team: {team_name} -> {team_user.user_id}")
        except Exception:
            # Continue with other teams even if one fails
            logger.exception("Failed to create Matrix user for team", team_name=team_name)

    return agent_users
