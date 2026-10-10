"""Shared Matrix OpenID verification and exact browser origins."""

import ipaddress
import json
from contextlib import suppress
from time import monotonic
from typing import Literal
from urllib.parse import urlsplit

import aiohttp
from pydantic import BaseModel, ConfigDict, Field

from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes
from mindroom.constants import RuntimePaths, runtime_matrix_homeserver
from mindroom.logging_config import get_logger
from mindroom.requester_identity import runtime_matrix_domain

logger = get_logger(__name__)

_AUDIENCE_FEATURE = "io.mindroom.openid_audience"
_AUDIENCE_PARAM = "io.mindroom.audience"
_CAPABILITY_TTL_SECONDS = 600.0
_VERSIONS_TIMEOUT = aiohttp.ClientTimeout(total=5)
_DEFAULT_PORTS = {"http": 80, "https": 443}
# Homeserver URL to (expiry on the monotonic clock, whether it binds OpenID tokens to an audience).
_audience_support: dict[str, tuple[float, bool]] = {}


class MatrixOpenIDToken(BaseModel):
    """SDK-issued short-lived OpenID payload, never a verifier URL."""

    model_config = ConfigDict(extra="forbid")
    access_token: str = Field(min_length=1, max_length=4096, repr=False)
    token_type: Literal["Bearer"]
    matrix_server_name: str = Field(min_length=1, max_length=255)
    expires_in: int = Field(gt=0)


class MatrixOpenIDError(Exception):
    """Matrix OpenID verification error with HTTP status and detail."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def allowed_client_origins(paths: RuntimePaths, env_name: str) -> tuple[str, ...]:
    """Read exact web and bundled iOS app origins; invalid configuration fails closed."""
    try:
        origins = json.loads(paths.env_value(env_name, default="[]") or "[]")
        if not isinstance(origins, list):
            return ()
        for origin in origins:
            if not isinstance(origin, str) or any(character.isspace() or ord(character) < 32 for character in origin):
                return ()
            # The bundled iOS app has a fixed custom-scheme origin. Accept only
            # this literal, never arbitrary capacitor origins or opaque "null".
            if origin == "capacitor://localhost":
                continue
            parsed = urlsplit(origin)
            hostname = parsed.hostname
            port = parsed.port  # Validate malformed and out-of-range ports.
            loopback = hostname == "localhost"
            if hostname and not loopback:
                with suppress(ValueError):
                    loopback = ipaddress.ip_address(hostname).is_loopback
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or not hostname
                or (parsed.scheme == "http" and not loopback)
                or (port is None and parsed.netloc.endswith(":"))
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


def _bound_audience(paths: RuntimePaths) -> str | None:
    """Return `MINDROOM_PUBLIC_URL` as a browser `URL.origin` (lowercase, default port dropped, no path), or None.

    urlsplit already lowercases the scheme and host.
    """
    parsed = urlsplit((paths.env_value("MINDROOM_PUBLIC_URL") or "").strip())
    scheme = parsed.scheme
    host = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        return None
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    host = f"[{host}]" if ":" in host else host
    return f"{scheme}://{host}" + (f":{port}" if port not in {None, _DEFAULT_PORTS[scheme]} else "")


async def _homeserver_binds_audience(client: aiohttp.ClientSession, homeserver: str) -> bool:
    """Read whether the homeserver advertises audience-bound OpenID tokens, caching each answer for 10 minutes.

    A failed or malformed answer is not cached, so the next verification retries.
    It keeps the last known answer, or counts as unsupported when there is none.
    """
    cached = _audience_support.get(homeserver)
    if cached is not None and monotonic() < cached[0]:
        return cached[1]
    last_known = cached is not None and cached[1]
    try:
        async with client.get(
            homeserver + "/_matrix/client/versions",
            allow_redirects=False,
            timeout=_VERSIONS_TIMEOUT,
        ) as response:
            if response.status != 200:
                return last_known
            body = await collect_bounded_bytes(response.content.iter_chunked(4096), max_bytes=65536)
            payload = json.loads(body)
    except (aiohttp.ClientError, TimeoutError, ByteLimitExceededError, ValueError, UnicodeError):
        return last_known
    features = payload.get("unstable_features") if isinstance(payload, dict) else None
    if not isinstance(features, dict):
        return last_known
    supported = features.get(_AUDIENCE_FEATURE) is True
    _audience_support[homeserver] = (monotonic() + _CAPABILITY_TTL_SECONDS, supported)
    return supported


async def _userinfo_params(
    client: aiohttp.ClientSession,
    paths: RuntimePaths,
    homeserver: str,
    token: MatrixOpenIDToken,
) -> dict[str, str]:
    params = {"access_token": token.access_token}
    if await _homeserver_binds_audience(client, homeserver):
        audience = _bound_audience(paths)
        if audience is None:
            raise MatrixOpenIDError(503, "Set MINDROOM_PUBLIC_URL to verify bound Matrix OpenID tokens.")
        params[_AUDIENCE_PARAM] = audience
    return params


async def verify_matrix_openid(token: MatrixOpenIDToken, paths: RuntimePaths) -> str:
    """Verify only at the configured homeserver with no redirects or URL logging.

    When the homeserver binds tokens to an audience, the token must have been requested for the origin of `MINDROOM_PUBLIC_URL`.
    That audience never comes from the request, whose `Host` header a replaying backend controls, so a missing or invalid value is a 503.
    Other homeservers cannot bind tokens, so no audience is sent and the token is accepted as before.
    """
    domain = runtime_matrix_domain(paths)
    if token.matrix_server_name != domain:
        raise MatrixOpenIDError(401, "OpenID server does not match the configured Matrix server.")
    homeserver = runtime_matrix_homeserver(paths).rstrip("/")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as client:
            params = await _userinfo_params(client, paths, homeserver, token)
            async with client.get(
                homeserver + "/_matrix/federation/v1/openid/userinfo",
                params=params,
                allow_redirects=False,
            ) as response:
                if response.status >= 500 or response.status == 429:
                    raise MatrixOpenIDError(503, "Matrix OpenID verifier is unavailable.")
                if response.status != 200:
                    if _AUDIENCE_PARAM in params and response.status == 401:
                        logger.warning(
                            "Bound Matrix OpenID token refused by the homeserver",
                            audience=params[_AUDIENCE_PARAM],
                        )
                    raise MatrixOpenIDError(401, "Matrix OpenID verification failed.")
                # StreamReader.read(n) may return a partial response; read to EOF with a hard cap.
                body = await collect_bounded_bytes(response.content.iter_chunked(4096), max_bytes=16384)
                payload = json.loads(body)
    except ByteLimitExceededError:
        raise MatrixOpenIDError(401, "Invalid Matrix OpenID response.") from None
    except (aiohttp.ClientError, TimeoutError):
        # Do not chain upstream exceptions: their URLs contain the OpenID token.
        raise MatrixOpenIDError(503, "Matrix OpenID verifier is unavailable.") from None
    except (ValueError, UnicodeError):
        raise MatrixOpenIDError(401, "Invalid Matrix OpenID response.") from None
    subject = payload.get("sub") if isinstance(payload, dict) else None
    if not isinstance(subject, str) or not subject.startswith("@") or ":" not in subject:
        raise MatrixOpenIDError(401, "Invalid Matrix OpenID subject.")
    localpart, subject_domain = subject[1:].split(":", 1)
    if not localpart or subject_domain != domain or len(subject) > 255:
        raise MatrixOpenIDError(401, "Invalid Matrix OpenID subject.")
    return subject
