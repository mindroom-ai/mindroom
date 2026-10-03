"""One-time cleanups of tool settings the dashboard saved with former insecure defaults or placements."""

# LEGACY_COMPAT: Daytona settings saved with the former `verify_ssl: false` default.
# Legacy format: a `daytona` credential document whose `verify_ssl` is false; the dashboard pre-fills every boolean with its declared default and saves all fields, so every Daytona setup saved through it stored false.
# Last legacy release: v2026.9.370, whose Daytona field still defaulted to false, as it had since the tool shipped in v0.1.0; replacement: v2026.9.371 defaults `verify_ssl` to true.
# Handling: before serving, once per storage root and under a lock shared by every starting process, drop a false `verify_ssl` from the primary store and every existing worker store, then write a receipt so a false saved deliberately afterwards is kept.
# The receipt waits while any stored Daytona document is unreadable, so one readable again later, for example under the right encryption key, is still cleaned.
# Coverage: tests/test_legacy_tool_credentials.py::test_startup_drops_saved_daytona_verify_ssl_false_once,
# tests/test_legacy_tool_credentials.py::test_both_entry_points_clean_up_before_credentials_are_used,
# tests/test_legacy_tool_credentials.py::test_unreadable_documents_keep_the_cleanup_pending,
# tests/test_legacy_tool_credentials.py::test_a_concurrent_start_rechecks_the_receipt_under_the_lock.

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.credentials import (
    get_runtime_credentials_manager,
    remove_worker_service_credentials,
    update_stored_service_credentials,
)
from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.logging_config import get_logger
from mindroom.tool_system.catalog import TOOL_METADATA, ensure_tool_registry_loaded

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

_RECEIPT_NAME = ".daytona-verify-ssl-default-dropped.json"
_LOCK_NAME = ".daytona-verify-ssl-default.lock"
_WORKER_COPIES_RECEIPT_NAME = ".primary-tool-settings-removed-from-workers.json"
logger = get_logger(__name__)


async def migrate_tool_credential_defaults(runtime_paths: RuntimePaths) -> None:
    """Finish the one-time cleanups before any tool is built from stored settings."""
    # Deleting first spares the Daytona cleanup from rewriting, or waiting to read, worker copies that are about to go.
    await run_blocking_until_complete(_remove_worker_copies_of_primary_tool_settings, runtime_paths)
    await run_blocking_until_complete(_migrate_daytona_verify_ssl, runtime_paths)


# LEGACY_COMPAT: Worker-store copies of settings for tools that never run in a worker.
# Legacy format: `<tool>_credentials.json` in a worker's own store, `workers/<worker>/credentials/`, for a built-in tool that requires the primary runtime or room context; the dashboard saved a scoped agent's tool settings there, where worker code can read them.
# Last legacy release: v2026.9.399 for tools that require the primary runtime, except v2026.10.8 for `homeassistant`, whose dedicated dashboard route kept saving there, and v2026.9.404 for tools that only require room context; replacement: v2026.9.400, v2026.10.9, and v2026.9.405 respectively save them in the primary's agent- or requester-scoped stores and never read worker copies.
# Handling: before serving, once per storage root, delete those documents from every existing worker's own store without reading them; shared-credential mirrors are left to their per-call sync, and the receipt waits while any worker store cannot be inspected or cleaned.
# Concurrent starts may both delete; removal is idempotent, so no lock is needed.
# Coverage: tests/test_legacy_tool_credentials.py::test_startup_deletes_worker_copies_of_primary_only_tool_settings_once,
# tests/test_legacy_tool_credentials.py::test_a_worker_store_that_cannot_be_cleaned_keeps_the_cleanup_pending,
# tests/test_legacy_tool_credentials.py::test_a_worker_store_hidden_from_discovery_keeps_the_cleanup_pending,
# tests/test_legacy_tool_credentials.py::test_a_worker_copy_that_cannot_be_inspected_keeps_the_cleanup_pending,
# tests/test_legacy_tool_credentials.py::test_both_entry_points_clean_up_before_credentials_are_used.
def _remove_worker_copies_of_primary_tool_settings(runtime_paths: RuntimePaths) -> None:
    receipt = get_runtime_credentials_manager(runtime_paths).base_path / _WORKER_COPIES_RECEIPT_NAME
    if receipt.exists():
        return
    ensure_tool_registry_loaded(runtime_paths)
    services = frozenset(
        name
        for name, metadata in TOOL_METADATA.items()
        if metadata.requires_primary_runtime or metadata.requires_room_context
    )
    removal = remove_worker_service_credentials(runtime_paths, services)
    if removal.removed:
        logger.warning(
            "Deleted worker copies of settings for tools that only run in the primary",
            documents=removal.removed,
        )
    if removal.failed:
        logger.warning(
            "Keeping the worker copy cleanup pending until every worker credential store can be cleaned",
            stores=removal.failed,
        )
        return
    write_json_file_durable(receipt, {"version": 1})


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
