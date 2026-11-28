"""Naming rules for credential services and config names used in those services."""

from __future__ import annotations

import re

# The characters a credential service name may contain. A config name that becomes part of one, such as a model
# name in its dashboard key ``model:<name>``, follows the same rule.
SERVICE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9:_-]+$")
SERVICE_NAME_CHARACTERS = "letters, numbers, colon, underscore, and hyphen"


def validate_service_name(service: str) -> str:
    """Validate and normalize credential service names."""
    normalized = service.strip()
    if not normalized:
        msg = "Service name is required"
        raise ValueError(msg)
    if not SERVICE_NAME_PATTERN.fullmatch(normalized):
        msg = f"Service name can only include {SERVICE_NAME_CHARACTERS}"
        raise ValueError(msg)
    return normalized
