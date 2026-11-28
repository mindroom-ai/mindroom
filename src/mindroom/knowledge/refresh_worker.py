"""Execute a refresh in the child process or publish file-only metadata after a dashboard mutation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, cast

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.github_app_auth import GitHubAppTokenBinding, get_runtime_github_app_token_provider
from mindroom.knowledge.file_listing import collected_oversized_files
from mindroom.knowledge.manager import KnowledgeManager
from mindroom.knowledge.redaction import redact_credentials_in_text
from mindroom.knowledge.refresh_locks import refresh_source_root_lock
from mindroom.knowledge.refresh_runner import (
    RefreshChildResult,
    SubprocessGitHubAppToken,
    SubprocessRefreshRequest,
    child_result_adapter,
    github_app_refresh_binding,
)
from mindroom.knowledge.registry import (
    record_refresh_succeeded,
    refresh_reason_generation,
    resolve_published_index_key,
    source_root_for_published_index_key,
)
from mindroom.logging_config import get_logger, setup_subprocess_logging
from mindroom.tool_system.worker_routing import parse_tool_execution_identity_payload

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.registry import PublishedIndexKey
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)


def _knowledge_manager(
    key: PublishedIndexKey,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> KnowledgeManager:
    return KnowledgeManager(
        base_id=key.base_id,
        config=config,
        runtime_paths=runtime_paths,
        storage_path=Path(key.storage_root),
        knowledge_path=Path(key.knowledge_path),
    )


async def _refresh_knowledge_binding(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    force_reindex: bool = False,
) -> RefreshChildResult:
    """Refresh one resolved knowledge binding under its source-root lock: the refresh child's whole job."""
    key = resolve_published_index_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=True,
    )
    async with refresh_source_root_lock(source_root_for_published_index_key(key)):
        manager = _knowledge_manager(key, config=config, runtime_paths=runtime_paths)
        outcome = await manager.refresh(force_reindex=force_reindex)
    return RefreshChildResult(
        indexed_count=outcome.indexed_count,
        published=outcome.published,
        error=outcome.error,
        deferred=outcome.deferred,
        source_changed=outcome.source_changed,
    )


async def publish_file_mode_source_metadata_for_base(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> None:
    """Publish a local file-only base's current source listing; the caller holds its source-root lock.

    This runs in the runtime, after a dashboard upload or delete, so it records its success itself.
    """
    key = resolve_published_index_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=True,
    )
    manager = _knowledge_manager(key, config=config, runtime_paths=runtime_paths)
    reason_generation = await asyncio.to_thread(refresh_reason_generation, key)
    await manager.publish_files_mode()
    await run_blocking_until_complete(partial(record_refresh_succeeded, key, reason_generation=reason_generation))


def _load_subprocess_github_app_binding(raw_binding_payload: object) -> GitHubAppTokenBinding:
    if not isinstance(raw_binding_payload, dict):
        msg = "Knowledge refresh subprocess request github_app_token.binding must be an object"
        raise TypeError(msg)
    binding_payload = cast("dict[str, object]", raw_binding_payload)
    raw_app_id = binding_payload.get("app_id")
    raw_installation_id = binding_payload.get("installation_id")
    raw_owner = binding_payload.get("owner")
    raw_repository = binding_payload.get("repository")
    raw_private_key_file = binding_payload.get("private_key_file")
    if not isinstance(raw_app_id, int) or isinstance(raw_app_id, bool) or raw_app_id <= 0:
        msg = "Knowledge refresh subprocess request github_app_token.binding.app_id must be positive"
        raise TypeError(msg)
    if not isinstance(raw_installation_id, int) or isinstance(raw_installation_id, bool) or raw_installation_id <= 0:
        msg = "Knowledge refresh subprocess request github_app_token.binding.installation_id must be positive"
        raise TypeError(msg)
    if not isinstance(raw_owner, str) or not raw_owner:
        msg = "Knowledge refresh subprocess request github_app_token.binding.owner must be non-empty"
        raise TypeError(msg)
    if not isinstance(raw_repository, str) or not raw_repository:
        msg = "Knowledge refresh subprocess request github_app_token.binding.repository must be non-empty"
        raise TypeError(msg)
    if not isinstance(raw_private_key_file, str) or not raw_private_key_file:
        msg = "Knowledge refresh subprocess request github_app_token.binding.private_key_file must be non-empty"
        raise TypeError(msg)
    return GitHubAppTokenBinding(
        app_id=raw_app_id,
        installation_id=raw_installation_id,
        owner=raw_owner,
        repository=raw_repository,
        private_key_file=raw_private_key_file,
    )


def _load_subprocess_github_app_token(raw_token_payload: object) -> SubprocessGitHubAppToken | None:
    if raw_token_payload is None:
        return None
    if not isinstance(raw_token_payload, dict):
        msg = "Knowledge refresh subprocess request github_app_token must be an object when present"
        raise TypeError(msg)
    token_payload = cast("dict[str, object]", raw_token_payload)
    raw_token = token_payload.get("token")
    raw_expires_at_epoch = token_payload.get("expires_at_epoch")
    if not isinstance(raw_token, str) or not raw_token:
        msg = "Knowledge refresh subprocess request github_app_token.token must be non-empty"
        raise TypeError(msg)
    if not isinstance(raw_expires_at_epoch, int) or isinstance(raw_expires_at_epoch, bool) or raw_expires_at_epoch <= 0:
        msg = "Knowledge refresh subprocess request github_app_token.expires_at_epoch must be positive"
        raise TypeError(msg)
    return SubprocessGitHubAppToken(
        token=raw_token,
        expires_at_epoch=raw_expires_at_epoch,
        binding=_load_subprocess_github_app_binding(token_payload.get("binding")),
    )


def _load_subprocess_refresh_request(payload: bytes) -> SubprocessRefreshRequest:
    raw_payload = json.loads(payload.decode())
    if not isinstance(raw_payload, dict):
        msg = "Knowledge refresh subprocess request must be a JSON object"
        raise TypeError(msg)
    raw_base_id = raw_payload.get("base_id")
    raw_config_data = raw_payload.get("config_data")
    raw_config_path = raw_payload.get("config_path")
    raw_storage_root = raw_payload.get("storage_root")
    raw_runtime_knowledge_base = raw_payload.get("runtime_knowledge_base")
    raw_execution_identity = raw_payload.get("execution_identity")
    raw_force_reindex = raw_payload.get("force_reindex", False)
    if not isinstance(raw_base_id, str) or not raw_base_id.strip():
        msg = "Knowledge refresh subprocess request is missing base_id"
        raise TypeError(msg)
    if not isinstance(raw_config_data, dict):
        msg = "Knowledge refresh subprocess request is missing config_data"
        raise TypeError(msg)
    if not isinstance(raw_config_path, str) or not raw_config_path.strip():
        msg = "Knowledge refresh subprocess request is missing config_path"
        raise TypeError(msg)
    if not isinstance(raw_storage_root, str) or not raw_storage_root.strip():
        msg = "Knowledge refresh subprocess request is missing storage_root"
        raise TypeError(msg)
    if raw_runtime_knowledge_base is not None and not isinstance(raw_runtime_knowledge_base, dict):
        msg = "Knowledge refresh subprocess request runtime_knowledge_base must be an object when present"
        raise TypeError(msg)
    if raw_execution_identity is not None and not isinstance(raw_execution_identity, dict):
        msg = "Knowledge refresh subprocess request execution_identity must be an object when present"
        raise TypeError(msg)
    return SubprocessRefreshRequest(
        base_id=raw_base_id,
        config_data=raw_config_data,
        config_path=raw_config_path,
        storage_root=raw_storage_root,
        runtime_knowledge_base=cast("dict[str, object] | None", raw_runtime_knowledge_base),
        execution_identity=raw_execution_identity,
        github_app_token=_load_subprocess_github_app_token(raw_payload.get("github_app_token")),
        force_reindex=bool(raw_force_reindex),
    )


async def _prime_subprocess_github_app_token(
    request: SubprocessRefreshRequest,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> None:
    if request.github_app_token is None:
        return
    binding = await github_app_refresh_binding(
        request.base_id,
        config=config,
        runtime_paths=runtime_paths,
    )
    if binding is None:
        return
    try:
        expires_at = datetime.fromtimestamp(request.github_app_token.expires_at_epoch, tz=UTC)
    except (OSError, OverflowError, ValueError) as exc:
        msg = "Knowledge refresh subprocess GitHub App token expiry is invalid"
        raise TypeError(msg) from exc
    repo_url, credentials = binding
    provider = get_runtime_github_app_token_provider()
    if provider.binding_for(repo_url, credentials) != request.github_app_token.binding:
        return
    provider.prime(
        repo_url,
        credentials,
        token=request.github_app_token.token,
        expires_at=expires_at,
    )


async def _run_subprocess_refresh_request(payload: bytes) -> RefreshChildResult:
    request = _load_subprocess_refresh_request(payload)
    runtime_paths = resolve_runtime_paths(
        config_path=Path(request.config_path),
        storage_path=Path(request.storage_root),
        process_env=dict(os.environ),
    )
    config = Config.validate_with_runtime(request.config_data, runtime_paths, tolerate_plugin_load_errors=True)
    if request.runtime_knowledge_base is not None:
        base_config = KnowledgeBaseConfig.model_validate(request.runtime_knowledge_base)
        config = config.with_runtime_knowledge_base_overlay(request.base_id, base_config)
    await _prime_subprocess_github_app_token(
        request,
        config=config,
        runtime_paths=runtime_paths,
    )
    execution_identity = (
        None
        if request.execution_identity is None
        else parse_tool_execution_identity_payload(
            request.execution_identity,
            error_prefix="Knowledge refresh execution_identity",
        )
    )
    return await _refresh_knowledge_binding(
        request.base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        force_reindex=request.force_reindex,
    )


def _failed_child_result(error: Exception) -> RefreshChildResult:
    """Return what a refresh child reports for an error it did not handle, credential-redacted."""
    return RefreshChildResult(error=redact_credentials_in_text(str(error)) or type(error).__name__)


def _run_child_request(payload: bytes) -> RefreshChildResult:
    """Run one refresh request and report its result, or its error, with the oversized files it skipped."""
    # `asyncio.run` copies this context, so the refresh adds to this same collection.
    with collected_oversized_files() as oversized_files:
        try:
            result = asyncio.run(_run_subprocess_refresh_request(payload))
        except Exception as exc:
            logger.exception("Knowledge refresh subprocess failed")
            result = _failed_child_result(exc)
    return replace(result, oversized_files=tuple(oversized_files.values()))


def main(argv: list[str] | None = None) -> int:
    """Internal CLI used by scheduled knowledge refresh subprocesses."""
    argparse.ArgumentParser(description="Run one internal MindRoom knowledge refresh request.").parse_args(argv)
    setup_subprocess_logging()
    payload = sys.stdin.buffer.read()
    # Native libraries and configured handlers can retain stdout's descriptor.
    with os.fdopen(os.dup(sys.stdout.fileno()), "wb") as output:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        result = _run_child_request(payload)
        logger.info(
            "Knowledge refresh subprocess completed",
            indexed_count=result.indexed_count,
            published=result.published,
            error=result.error is not None,
        )
        output.write(child_result_adapter.dump_json(result))
    return 0
