"""Bring a storage root to the current layout before anything in the process reads it."""

from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.credentials.legacy_tool_credentials import migrate_tool_credential_defaults
from mindroom.credentials.sync import sync_env_to_credentials
from mindroom.delegation.legacy_snapshots import migrate_subagent_sessions
from mindroom.legacy_state_root_records import migrate_state_root_records
from mindroom.logging_config import get_logger
from mindroom.private_storage.legacy_private_storage import migrate_private_storage
from mindroom.session_storage.legacy_agno3_conversion import migrate_conversation_storage
from mindroom.workers.backends.legacy_state_root_mounts import retire_state_root_worker_mounts

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)


def _sync_credentials_and_create_storage_root(runtime_paths: RuntimePaths) -> None:
    logger.info("Syncing API keys from environment to CredentialsManager...")
    sync_env_to_credentials(runtime_paths=runtime_paths)
    runtime_paths.storage_root.mkdir(parents=True, exist_ok=True)


async def prepare_runtime_storage(
    runtime_paths: RuntimePaths,
    *,
    converting: AbstractContextManager[object] | None = None,
) -> None:
    """Run every startup migration and the credential sync, in the one order they depend on.

    Old worker mounts of whole state roots retire before conversation storage converts, so no old worker can swap a
    sessions directory as it converts. ``converting`` is entered around the conversion only, which can take minutes.
    """
    await migrate_private_storage(runtime_paths)
    await migrate_state_root_records(runtime_paths)
    await migrate_subagent_sessions(runtime_paths)
    await migrate_tool_credential_defaults(runtime_paths)
    # Synchronous, so it runs off the loop while still failing startup when it raises.
    await run_blocking_until_complete(_sync_credentials_and_create_storage_root, runtime_paths)
    await run_blocking_until_complete(retire_state_root_worker_mounts, runtime_paths)
    with converting or nullcontext():
        await migrate_conversation_storage(runtime_paths)
