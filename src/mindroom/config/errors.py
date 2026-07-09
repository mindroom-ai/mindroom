"""Configuration validation errors shared by authored and runtime stages."""

from __future__ import annotations


class ConfigRuntimeValidationError(ValueError):
    """Runtime-aware config validation failed after authored schema validation."""
