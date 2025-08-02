"""Common utility functions used across the codebase."""


def extract_domain_from_user_id(user_id: str) -> str:
    """Extract domain from a Matrix user ID.

    Args:
        user_id: Matrix user ID like "@user:example.com"

    Returns:
        Domain part (e.g., "example.com") or "localhost" if not found
    """
    if ":" in user_id:
        return user_id.split(":", 1)[1]
    return "localhost"


def extract_username_from_user_id(user_id: str) -> str:
    """Extract username from a Matrix user ID.

    Args:
        user_id: Matrix user ID like "@mindroom_calculator:example.com"

    Returns:
        Username without @ and domain (e.g., "mindroom_calculator")
    """
    if user_id.startswith("@"):
        username = user_id[1:]  # Remove @
        if ":" in username:
            return username.split(":", 1)[0]
        return username
    return user_id


def extract_server_name_from_homeserver(homeserver: str) -> str:
    """Extract server name from a homeserver URL.

    Args:
        homeserver: Homeserver URL like "http://localhost:8008"

    Returns:
        Server name (e.g., "localhost")
    """
    # Remove protocol
    server_part = homeserver.split("://", 1)[1] if "://" in homeserver else homeserver

    # Remove port if present
    if ":" in server_part:
        return server_part.split(":", 1)[0]
    return server_part


def construct_agent_user_id(agent_name: str, domain: str) -> str:
    """Construct a Matrix user ID for an agent.

    Args:
        agent_name: Agent name (e.g., "calculator")
        domain: Domain part (e.g., "localhost")

    Returns:
        Full Matrix user ID (e.g., "@mindroom_calculator:localhost")
    """
    return f"@mindroom_{agent_name}:{domain}"
