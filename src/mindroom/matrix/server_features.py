"""The unstable features a homeserver lists in ``/_matrix/client/versions``."""

import json

import aiohttp

from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes
from mindroom.logging_config import get_logger

logger = get_logger(__name__)


async def homeserver_unstable_features(homeserver_url: str, *, failure_message: str) -> dict[str, object]:
    """Return the homeserver's ``unstable_features``; empty when it lists none.

    Anything other than a small, direct JSON answer counts as listing none, with ``failure_message`` logged as a
    warning that says why.
    """
    url = homeserver_url.rstrip("/") + "/_matrix/client/versions"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as client:  # noqa: SIM117 - own the client until response cleanup completes
            async with client.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    logger.warning(failure_message, status=response.status)
                    return {}
                body = await collect_bounded_bytes(response.content.iter_chunked(4096), max_bytes=16384)
        payload = json.loads(body)
    except (aiohttp.ClientError, TimeoutError, ByteLimitExceededError, ValueError, UnicodeError) as exc:
        logger.warning(failure_message, error=type(exc).__name__)
        return {}
    features = payload.get("unstable_features") if isinstance(payload, dict) else None
    return features if isinstance(features, dict) else {}
