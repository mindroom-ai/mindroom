"""Test helpers for publishing and corrupting current OAuth credential state."""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.constants import resolve_runtime_paths
from mindroom.oauth.credential_store import oauth_credential_transaction

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


@dataclass(frozen=True, slots=True)
class _OAuthStoreTestContext:
    runtime_paths: RuntimePaths
    provider: OAuthProvider
    credentials_manager: CredentialsManager
    worker_target: ResolvedWorkerTarget | None


def publish_oauth_credentials(
    provider: OAuthProvider,
    credentials: Mapping[str, Any],
    *,
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget | None,
) -> None:
    """Publish credentials through the real SQLite transaction owner from any test context."""
    context = _OAuthStoreTestContext(
        resolve_runtime_paths(storage_path=credentials_manager.storage_root, process_env={}),
        provider,
        credentials_manager,
        worker_target,
    )

    async def publish() -> None:
        async with oauth_credential_transaction(context) as transaction:
            transaction.publish(credentials, advance_connection_generation=True)
            await transaction.commit()

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(asyncio.run, publish()).result()


def corrupt_oauth_credential_payload(database_path: Path, payload: bytes) -> None:
    """Replace a current credential payload with unreadable bytes for recovery tests."""
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            "UPDATE oauth_credential_state SET credential_payload = ? WHERE singleton = 1",
            (payload,),
        )
        connection.commit()
