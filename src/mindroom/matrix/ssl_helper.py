"""SSL helper for development environments with self-signed certificates."""


def get_ssl_context(homeserver: str | None = None) -> None:
    """Get SSL context based on homeserver URL.

    Args:
        homeserver: The homeserver URL (optional).
                   Returns None for all cases to use default SSL handling.

    Returns:
        None to use nio's default SSL handling (proper verification for HTTPS,
        no SSL for HTTP)
    """
    # Always return None to let nio handle SSL properly
    # - For HTTP: nio won't use SSL
    # - For HTTPS: nio will use proper SSL verification
    return None
