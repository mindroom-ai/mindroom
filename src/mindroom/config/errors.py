"""Configuration errors shared by authored and runtime stages."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class ConfigRuntimeValidationError(ValueError):
    """Runtime-aware config validation failed after authored schema validation."""


class ConfigSourceChangedError(RuntimeError):
    """Config source files changed while one runtime snapshot was being prepared."""

    def __init__(self, source_files: frozenset[Path]) -> None:
        super().__init__("Configuration source changed during runtime publication")
        self.source_files = source_files
