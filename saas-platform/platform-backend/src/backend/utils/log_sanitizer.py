"""
Simple log sanitizer for sensitive data in production.
KISS principle - no overengineering.
"""

import re
import os
from typing import Any

# Simple patterns for sensitive data
PATTERNS = {
    # UUIDs (account IDs, user IDs, etc.)
    "uuid": re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE),
    # Email addresses
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    # Bearer tokens
    "bearer": re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+", re.IGNORECASE),
    # API keys (simple pattern for keys that look like secrets)
    "api_key": re.compile(
        r'\b(api[_-]?key|secret|token|password)["\']?\s*[:=]\s*["\']?[A-Za-z0-9\-._~+/]{20,}', re.IGNORECASE
    ),
}


def sanitize_string(text: str) -> str:
    """
    Sanitize sensitive data from a string in production.
    In development, return as-is for debugging.
    """
    # Check environment at runtime, not import time
    is_production = os.getenv("ENVIRONMENT", "development").lower() in ["production", "prod"]
    if not is_production or not text:
        return text

    # Apply each pattern
    result = text
    result = PATTERNS["uuid"].sub("[UUID]", result)
    result = PATTERNS["email"].sub("[EMAIL]", result)
    result = PATTERNS["bearer"].sub("Bearer [TOKEN]", result)
    result = PATTERNS["api_key"].sub("[REDACTED]", result)

    return result


def sanitize_args(*args: Any) -> tuple:
    """
    Sanitize all arguments that are strings.
    """
    is_production = os.getenv("ENVIRONMENT", "development").lower() in ["production", "prod"]
    if not is_production:
        return args

    return tuple(sanitize_string(arg) if isinstance(arg, str) else arg for arg in args)


def sanitize_kwargs(**kwargs: dict) -> dict:
    """
    Sanitize all keyword arguments that are strings.
    """
    is_production = os.getenv("ENVIRONMENT", "development").lower() in ["production", "prod"]
    if not is_production:
        return kwargs

    return {key: sanitize_string(value) if isinstance(value, str) else value for key, value in kwargs.items()}
