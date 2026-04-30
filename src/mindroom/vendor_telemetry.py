"""Disable third-party telemetry and vendor phone-home defaults."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from mindroom.constants import VENDOR_TELEMETRY_ENV_VALUES

if TYPE_CHECKING:
    from collections.abc import MutableMapping


def disable_vendor_telemetry(environ: MutableMapping[str, str] | None = None) -> None:
    """Force known third-party telemetry and vendor update checks off."""
    target_env = os.environ if environ is None else environ
    target_env.update(VENDOR_TELEMETRY_ENV_VALUES)
    if target_env is os.environ:
        _disable_loaded_vendor_modules()


def vendor_telemetry_env_values() -> dict[str, str]:
    """Return a mutable copy of the vendor telemetry opt-out env."""
    return dict(VENDOR_TELEMETRY_ENV_VALUES)


def _disable_loaded_vendor_modules() -> None:
    """Apply best-effort guards for vendor modules imported before MindRoom."""
    if posthog_module := sys.modules.get("posthog"):
        posthog_vars = vars(posthog_module)
        posthog_vars["disabled"] = True
        posthog_vars["send"] = False
        if default_client := posthog_vars.get("default_client"):
            client_vars = vars(default_client)
            client_vars["disabled"] = True
            client_vars["send"] = False

    if mem0_telemetry_module := sys.modules.get("mem0.memory.telemetry"):
        mem0_telemetry_vars = vars(mem0_telemetry_module)
        mem0_telemetry_vars["MEM0_TELEMETRY"] = False

    if litellm_module := sys.modules.get("litellm"):
        vars(litellm_module)["telemetry"] = False

    if hf_constants_module := sys.modules.get("huggingface_hub.constants"):
        vars(hf_constants_module)["HF_HUB_DISABLE_TELEMETRY"] = True
