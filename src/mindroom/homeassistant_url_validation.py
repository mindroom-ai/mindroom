"""Home Assistant URL validation helpers."""

from __future__ import annotations

from mindroom.logging_config import get_logger
from mindroom.server_fetch_url import ServerFetchUrlError, validate_server_fetch_url

logger = get_logger(__name__)

_HOMEASSISTANT_PRIVATE_URL_OPT_IN_DETAIL = (
    "Private Home Assistant URLs require explicit opt-in. "
    "Enable private URL access only for trusted self-hosted instances."
)
_HOMEASSISTANT_INVALID_URL_DETAIL = "Home Assistant instance URL is not allowed for server-side fetching."
_PRIVATE_URL_REASONS = frozenset({"private_address", "private_hostname"})


def homeassistant_url_error_detail(error: ServerFetchUrlError, *, allow_private_url: bool) -> str:
    """Return a user-facing Home Assistant URL validation error."""
    if not allow_private_url and error.reason in _PRIVATE_URL_REASONS:
        return _HOMEASSISTANT_PRIVATE_URL_OPT_IN_DETAIL
    return _HOMEASSISTANT_INVALID_URL_DETAIL


def validate_homeassistant_instance_url(instance_url: str, *, allow_private_url: bool) -> str:
    """Validate a Home Assistant URL and log only non-sensitive rejection metadata."""
    try:
        return validate_server_fetch_url(instance_url, allow_private_networks=allow_private_url)
    except ServerFetchUrlError as e:
        logger.warning(
            "Rejected Home Assistant server-side fetch URL",
            reason=e.reason,
            allow_private_url=allow_private_url,
        )
        raise
