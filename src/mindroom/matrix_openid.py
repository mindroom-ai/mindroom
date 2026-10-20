"""Matrix OpenID verification and token-binding detection at the configured homeserver."""

import json
from typing import Literal

import aiohttp
from pydantic import BaseModel, ConfigDict, Field

from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes
from mindroom.constants import RuntimePaths, runtime_matrix_homeserver
from mindroom.logging_config import get_logger
from mindroom.origins import local_api_origin, normalized_origin
from mindroom.requester_identity import runtime_matrix_domain

# MindRoom's Tuwunel fork binds tokens to this audience (`io.mindroom.openid_audience`); other homeservers ignore it.
_AUDIENCE_PARAM = "io.mindroom.audience"
# The fork lists this in `/_matrix/client/versions` `unstable_features` while it binds tokens.
_BINDING_FEATURE = "io.mindroom.openid_audience"
_BINDING_CHECK_FAILED = "Could not check whether the homeserver binds OpenID tokens"

logger = get_logger(__name__)


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


def _openid_audience(paths: RuntimePaths) -> str | None:
    """Return the audience MindRoom asks the homeserver to enforce, never taken from a request.

    It is the normalized origin of `MINDROOM_PUBLIC_URL`, or MindRoom's own local origin when that is unset, which is
    the address MindRoom Chat opens for a backend on the same machine.
    """
    public_url = (paths.env_value("MINDROOM_PUBLIC_URL") or "").strip()
    return normalized_origin(public_url or local_api_origin(paths))


async def verify_matrix_openid(token: MatrixOpenIDToken, paths: RuntimePaths) -> str:
    """Verify only at the configured homeserver with no redirects or URL logging.

    The origin of `MINDROOM_PUBLIC_URL` goes along as the audience, so a homeserver that binds tokens accepts only a
    token Chat requested for this deployment. The audience never comes from the request, whose `Host` header a
    replaying backend controls.
    """
    domain = runtime_matrix_domain(paths)
    if token.matrix_server_name != domain:
        raise MatrixOpenIDError(401, "OpenID server does not match the configured Matrix server.")
    url = runtime_matrix_homeserver(paths).rstrip("/") + "/_matrix/federation/v1/openid/userinfo"
    params = {"access_token": token.access_token}
    audience = _openid_audience(paths)
    if audience is not None:
        params[_AUDIENCE_PARAM] = audience
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as client:  # noqa: SIM117 - own the client until response cleanup completes
            async with client.get(url, params=params, allow_redirects=False) as response:
                if response.status >= 500 or response.status == 429:
                    raise MatrixOpenIDError(503, "Matrix OpenID verifier is unavailable.")
                if response.status != 200:
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


async def homeserver_binds_openid_audience(paths: RuntimePaths) -> bool:
    """Return whether the configured homeserver binds OpenID tokens to the audience they were requested for.

    While it does, a token that `verify_matrix_openid` accepts was requested for this deployment's origin. Anything
    other than a small, direct JSON answer listing the feature as `true` counts as no binding.
    """
    url = runtime_matrix_homeserver(paths).rstrip("/") + "/_matrix/client/versions"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as client:  # noqa: SIM117 - own the client until response cleanup completes
            async with client.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    logger.warning(_BINDING_CHECK_FAILED, status=response.status)
                    return False
                body = await collect_bounded_bytes(response.content.iter_chunked(4096), max_bytes=16384)
        payload = json.loads(body)
    except (aiohttp.ClientError, TimeoutError, ByteLimitExceededError, ValueError, UnicodeError) as exc:
        logger.warning(_BINDING_CHECK_FAILED, error=type(exc).__name__)
        return False
    features = payload.get("unstable_features") if isinstance(payload, dict) else None
    return isinstance(features, dict) and features.get(_BINDING_FEATURE) is True
