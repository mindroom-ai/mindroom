"""Computer-specific auth bridge to shared Matrix OpenID verification."""

from mindroom.constants import RuntimePaths
from mindroom.matrix_openid import (
    MatrixOpenIDError,
    MatrixOpenIDToken,
    allowed_client_origins,
    verify_matrix_openid,
)
from mindroom.runtime_env_policy import COMPUTER_ALLOWED_ORIGINS_ENV
from mindroom.worker_computer.sessions import ComputerError


def computer_origins(paths: RuntimePaths) -> tuple[str, ...]:
    """Read exact web and bundled iOS app origins; invalid configuration fails closed."""
    return allowed_client_origins(paths, COMPUTER_ALLOWED_ORIGINS_ENV)


async def verify_openid(token: MatrixOpenIDToken, paths: RuntimePaths) -> str:
    """Verify only at the configured homeserver with no redirects or URL logging."""
    try:
        return await verify_matrix_openid(token, paths)
    except MatrixOpenIDError as error:
        raise ComputerError(error.status_code, error.detail) from error
