"""Computer-specific Matrix OpenID verification and exact browser origins."""

import json
from typing import Literal
from urllib.parse import urlsplit

import aiohttp
from pydantic import BaseModel, ConfigDict, Field

from mindroom.constants import RuntimePaths, runtime_matrix_homeserver
from mindroom.requester_identity import runtime_matrix_domain
from mindroom.runtime_env_policy import COMPUTER_ALLOWED_ORIGINS_ENV
from mindroom.worker_computer.sessions import ComputerError


class MatrixOpenIDToken(BaseModel):
    """SDK-issued short-lived OpenID payload, never a verifier URL."""

    model_config = ConfigDict(extra="forbid")
    access_token: str = Field(min_length=1, max_length=4096, repr=False)
    token_type: Literal["Bearer"]
    matrix_server_name: str = Field(min_length=1, max_length=255)
    expires_in: int = Field(gt=0)


def computer_origins(paths: RuntimePaths) -> tuple[str, ...]:
    """Read only exact HTTP origins; invalid configuration fails closed."""
    try:
        origins = json.loads(paths.env_value(COMPUTER_ALLOWED_ORIGINS_ENV, default="[]") or "[]")
        if not isinstance(origins, list):
            return ()
        for origin in origins:
            if not isinstance(origin, str):
                return ()
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
                or "*" in origin
            ):
                return ()
    except (ValueError, TypeError):
        return ()
    return tuple(origins)


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
                body = bytearray()
                async for chunk in response.content.iter_chunked(4096):
                    body.extend(chunk)
                    if len(body) > 16384:
                        raise ComputerError(401, "Invalid Matrix OpenID response.")
                payload = json.loads(body)
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
