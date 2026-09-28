"""One-time cleanup of tool settings the dashboard saved with a former insecure default."""

# LEGACY_COMPAT: Daytona settings saved with the former `verify_ssl: false` default.
# Legacy format: a `daytona` credential document whose `verify_ssl` is false; the dashboard pre-fills every boolean with its declared default and saves all fields, so every Daytona setup saved through it stored false.
# Last legacy release: v2026.9.362, whose Daytona field still defaulted to false, as it had since the tool shipped in v0.1.0; replacement: the next release defaults `verify_ssl` to true.
# Handling: before serving, once per storage root, drop a false `verify_ssl` from the primary store and every existing worker store, then write a receipt so a false saved deliberately afterwards is kept.
# Coverage: tests/test_legacy_tool_credentials.py::test_startup_drops_saved_daytona_verify_ssl_false_once,
# tests/test_legacy_tool_credentials.py::test_both_entry_points_clean_up_before_credentials_are_used.

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.credentials import get_runtime_credentials_manager, update_stored_service_credentials
from mindroom.durable_write import write_json_file_durable
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

_RECEIPT_NAME = ".daytona-verify-ssl-default-dropped.json"
logger = get_logger(__name__)


async def migrate_tool_credential_defaults(runtime_paths: RuntimePaths) -> None:
    """Finish the one-time cleanup before any tool is built from stored settings."""
    await run_blocking_until_complete(_migrate_daytona_verify_ssl, runtime_paths)


def _migrate_daytona_verify_ssl(runtime_paths: RuntimePaths) -> None:
    receipt = get_runtime_credentials_manager(runtime_paths).base_path / _RECEIPT_NAME
    if receipt.exists():
        return
    rewritten = update_stored_service_credentials(runtime_paths, "daytona", _without_false_verify_ssl)
    if rewritten:
        logger.warning(
            "Re-enabled TLS certificate verification for Daytona settings saved with the former default",
            stores=rewritten,
        )
    write_json_file_durable(receipt, {"version": 1})


def _without_false_verify_ssl(credentials: dict[str, Any]) -> dict[str, Any] | None:
    if credentials.get("verify_ssl") is not False:
        return None
    return {key: value for key, value in credentials.items() if key != "verify_ssl"}
