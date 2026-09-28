"""One-time cleanup of tool settings the dashboard saved with a former insecure default."""

# LEGACY_COMPAT: Daytona settings saved with the former `verify_ssl: false` default.
# Legacy format: a `daytona` credential document whose `verify_ssl` is false; the dashboard pre-fills every boolean with its declared default and saves all fields, so every Daytona setup saved through it stored false.
# Last legacy release: v2026.9.363, whose Daytona field still defaulted to false, as it had since the tool shipped in v0.1.0; replacement: the next release defaults `verify_ssl` to true.
# Handling: before serving, once per storage root and under a lock shared by every starting process, drop a false `verify_ssl` from the primary store and every existing worker store, then write a receipt so a false saved deliberately afterwards is kept.
# The receipt waits while any stored Daytona document is unreadable, so one readable again later, for example under the right encryption key, is still cleaned.
# Coverage: tests/test_legacy_tool_credentials.py::test_startup_drops_saved_daytona_verify_ssl_false_once,
# tests/test_legacy_tool_credentials.py::test_both_entry_points_clean_up_before_credentials_are_used,
# tests/test_legacy_tool_credentials.py::test_unreadable_documents_keep_the_cleanup_pending,
# tests/test_legacy_tool_credentials.py::test_a_concurrent_start_rechecks_the_receipt_under_the_lock.

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.credentials import get_runtime_credentials_manager, update_stored_service_credentials
from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

_RECEIPT_NAME = ".daytona-verify-ssl-default-dropped.json"
_LOCK_NAME = ".daytona-verify-ssl-default.lock"
logger = get_logger(__name__)


async def migrate_tool_credential_defaults(runtime_paths: RuntimePaths) -> None:
    """Finish the one-time cleanup before any tool is built from stored settings."""
    await run_blocking_until_complete(_migrate_daytona_verify_ssl, runtime_paths)


def _migrate_daytona_verify_ssl(runtime_paths: RuntimePaths) -> None:
    credentials_dir = get_runtime_credentials_manager(runtime_paths).base_path
    receipt = credentials_dir / _RECEIPT_NAME
    # The orchestrator and a standalone API can start together; the receipt is only trusted under the lock.
    with advisory_file_lock(credentials_dir / _LOCK_NAME):
        if receipt.exists():
            return
        update = update_stored_service_credentials(runtime_paths, "daytona", _without_false_verify_ssl)
        if update.rewritten:
            logger.warning(
                "Re-enabled TLS certificate verification for Daytona settings saved with the former default",
                stores=update.rewritten,
            )
        if update.unreadable:
            logger.warning(
                "Keeping the Daytona TLS cleanup pending until every stored Daytona document can be read",
                unreadable=update.unreadable,
            )
            return
        write_json_file_durable(receipt, {"version": 1})


def _without_false_verify_ssl(credentials: dict[str, Any]) -> dict[str, Any] | None:
    if credentials.get("verify_ssl") is not False:
        return None
    return {key: value for key, value in credentials.items() if key != "verify_ssl"}
