"""Test helpers for the explicit authored-to-runtime config boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.config.main import Config, RuntimeConfig

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths


def runtime_config_from_data(
    data: object,
    runtime_paths: RuntimePaths,
    *,
    tolerate_plugin_load_errors: bool = False,
) -> RuntimeConfig:
    """Parse authored data, then run the production runtime validation stage."""
    return RuntimeConfig.from_authored(
        Config.model_validate(data),
        runtime_paths,
        tolerate_plugin_load_errors=tolerate_plugin_load_errors,
    )
