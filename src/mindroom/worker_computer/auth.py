"""Computer-specific Matrix OpenID verification and exact browser origins."""

import json
from typing import Literal

import aiohttp
from pydantic import BaseModel, ConfigDict, Field

from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes
from mindroom.constants import RuntimePaths, runtime_matrix_homeserver
from mindroom.origins import parse_browser_origin
from mindroom.requester_identity import runtime_matrix_domain
from mindroom.runtime_env_policy import COMPUTER_ALLOWED_ORIGINS_ENV
from mindroom.worker_computer.sessions import ComputerError

_CAPACITOR_ORIGIN = "capacitor://localhost"


class MatrixOpenIDToken(BaseModel):
    """SDK-issued short-lived OpenID payload, never a verifier URL."""

    model_config = ConfigDict(extra="forbid")
    access_token: str = Field(min_length=1, max_length=4096, repr=False)
    token_type: Literal["Bearer"]
    matrix_server_name: str = Field(min_length=1, max_length=255)
    expires_in: int = Field(gt=0)


def computer_origins(paths: RuntimePaths) -> tuple[str, ...]:
    """Read exact web and bundled iOS app origins; invalid configuration fails closed."""
    try:
        origins = json.loads(paths.env_value(COMPUTER_ALLOWED_ORIGINS_ENV, default="[]") or "[]")
        if not isinstance(origins, list):
            return ()
        if not all(isinstance(origin, str) for origin in origins):
            return ()
        # The bundled iOS app has a fixed custom-scheme origin. Accept only
        # this literal, never arbitrary capacitor origins or opaque "null".
        return tuple(origin if origin == _CAPACITOR_ORIGIN else parse_browser_origin(origin) for origin in origins)
    except ValueError:
        return ()


async def verify_openid(token: MatrixOpenIDToken, paths: RuntimePaths) -> str:
    """Verify only at the configured homeserver with no redirects or URL logging."""
    domain = runtime_matrix_domain(paths)
    if token.matrix_server_name != domain:
        raise ComputerError(401, "OpenID server does not match the configured Matrix server.")
    url = runtime_matrix_homeserver(paths).rstrip("/") + "/_matrix/federation/v1/openid/userinfo"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as client:  # noqa: SIM117 - own the client until response cleanup completes
            async with client.get(url, params={"access_token": token.access_token}, allow_redirects=False) as response:
                if response.status >= 500 or response.status == 429:
                    raise ComputerError(503, "Matrix OpenID verifier is unavailable.")
                if response.status != 200:
                    raise ComputerError(401, "Matrix OpenID verification failed.")
                # StreamReader.read(n) may return a partial response; read to EOF with a hard cap.
                body = await collect_bounded_bytes(response.content.iter_chunked(4096), max_bytes=16384)
                payload = json.loads(body)
    except ByteLimitExceededError:
        raise ComputerError(401, "Invalid Matrix OpenID response.") from None
    except (aiohttp.ClientError, TimeoutError):
        # Do not chain upstream exceptions: their URLs contain the OpenID token.
        raise ComputerError(503, "Matrix OpenID verifier is unavailable.") from None
    except (ValueError, UnicodeError):
        raise ComputerError(401, "Invalid Matrix OpenID response.") from None
    subject = payload.get("sub") if isinstance(payload, dict) else None
    if not isinstance(subject, str) or not subject.startswith("@") or ":" not in subject:
        raise ComputerError(401, "Invalid Matrix OpenID subject.")
    localpart, subject_domain = subject[1:].split(":", 1)
    if not localpart or subject_domain != domain or len(subject) > 255:
        raise ComputerError(401, "Invalid Matrix OpenID subject.")
    return subject
