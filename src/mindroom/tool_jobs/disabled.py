"""Passive startup ownership for tool jobs parked until an enabled restart."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

from agno.run.team import TeamRunOutput

from mindroom.delegation.state import DelegationState
from mindroom.delegation.storage import delegation_storage_config
from mindroom.event_journal import EventKind
from mindroom.handled_turns import TurnRecordCodec
from mindroom.history.session_context import read_scope_session_run
from mindroom.history.types import HistoryScope
from mindroom.tool_jobs.runtime import read_job_snapshot
from mindroom.tool_jobs.settings import background_tool_jobs_enabled
from mindroom.tool_system.worker_routing import parse_tool_execution_identity_payload

if TYPE_CHECKING:
    from pathlib import Path

    from agno.run.agent import RunOutput

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalContinuation, EventJournalStore, JournalEvent


@dataclass
class _ParkedWork:
    sources: set[tuple[str, str]] = field(default_factory=set)
    approvals: set[str] = field(default_factory=set)


_PARKED: dict[Path, _ParkedWork] = {}


def clear_parked_work(runtime_paths: RuntimePaths) -> None:
    """Release only the stopped process's passive index."""
    _PARKED.pop(runtime_paths.storage_root.resolve(), None)


def event_is_parked(config: Config, runtime_paths: RuntimePaths, entity_name: str, event: JournalEvent) -> bool:
    """Fence saved sources and every internal completion before any handoff."""
    if background_tool_jobs_enabled(config, runtime_paths):
        return False
    parked = _PARKED.get(runtime_paths.storage_root.resolve())
    return event.kind is EventKind.TOOL_JOB_COMPLETION or (
        parked is not None and (entity_name, event.event_id) in parked.sources
    )


def approval_is_parked(runtime_paths: RuntimePaths, approval_id: str) -> bool:
    """Keep parked approval owners out of startup expiry and failure cleanup."""
    parked = _PARKED.get(runtime_paths.storage_root.resolve())
    return parked is not None and approval_id in parked.approvals


def _saved_sources(runtime_paths: RuntimePaths) -> _ParkedWork:
    root = runtime_paths.storage_root / "tool_jobs"
    if root.is_symlink():
        msg = "Tool job storage must not use symlinks."
        raise ValueError(msg)
    parked = _ParkedWork()
    for path in sorted(root.glob("*.json")):
        job = read_job_snapshot(path)
        source = job.adapter.get("source_event_id")
        if isinstance(source, str):
            parked.sources.add((job.owner.transport_agent_name or job.owner.agent_name, source))
    return parked


def _run_uses_jobs(run: RunOutput | TeamRunOutput) -> bool:
    tools = [*(run.tools or ()), *(r.tool_execution for r in run.requirements or () if r.tool_execution)]
    if any("wait_timeout" in (tool.tool_args or {}) for tool in tools):
        return True
    delegation = DelegationState.from_metadata(run.metadata)
    if any("wait_timeout" in hook.arguments for hook in delegation.hooks.values()):
        return True
    if any("wait_timeout" in (tool.get("tool_args") or {}) for tool in delegation.pending_tools):
        return True
    return isinstance(run, TeamRunOutput) and any(_run_uses_jobs(member) for member in run.member_responses)


def _approval_uses_jobs(continuation: ApprovalContinuation, config: Config, runtime_paths: RuntimePaths) -> bool:
    if continuation.source_kind == "tool_job_completion" or continuation.hook_source == "tool_job_completion":
        return True
    if any(call.toolkit_name == "job" for call in continuation.calls):
        return True
    config = delegation_storage_config(config, continuation.delegation_storage_bindings)
    scope = continuation.history_scope
    if scope is None:
        scope = HistoryScope(kind=continuation.entity_kind, scope_id=continuation.entity_name)
    identity = parse_tool_execution_identity_payload(continuation.execution_identity, strict=True)
    read_run = partial(
        read_scope_session_run,
        agent_name=continuation.entity_name,
        scope=scope,
        runtime_paths=runtime_paths,
        execution_identity=identity,
        session_id=continuation.session_id,
        run_id=continuation.run_id,
        requester_id=continuation.requester_id,
    )
    run = read_run(config=config) if scope.kind != "agent" or continuation.entity_name in config.agents else None
    if run is None and scope.kind == "agent":
        # Root approvals may predate cards and retain no storage binding.
        # Only the three canonical layouts can own this exact saved run.
        for private_scope in (None, "user", "user_agent"):
            if private_scope is not None and (identity is None or not identity.requester_id):
                break
            storage_config = delegation_storage_config(
                config,
                {
                    continuation.entity_name: {
                        "display_name": continuation.entity_name,
                        "private": None if private_scope is None else {"per": private_scope},
                        "worker_scope": None,
                    },
                },
            )
            run = read_run(config=storage_config)
            if run is not None:
                break
    return run is not None and _run_uses_jobs(run)


async def index_parked_work(
    config: Config,
    runtime_paths: RuntimePaths,
    journal: EventJournalStore | None = None,
) -> None:
    """Inspect saved ownership once; never recover, acknowledge, or execute it."""
    parked = await asyncio.to_thread(_saved_sources, runtime_paths)
    if journal is not None:
        for entity_name in {entity for entity, _source in parked.sources}:
            for event_id, _anchor, payload in await journal.turn_records(entity_name).load_all():
                if (entity_name, event_id) in parked.sources:
                    record = TurnRecordCodec._from_ledger_record(event_id, json.loads(payload))
                    if record is not None:
                        parked.sources.update((entity_name, source) for source in record.source_event_ids)
        cursor: tuple[str, str] | None = None
        while owners := await journal.approval_continuations(limit=100, after=cursor):
            for _principal_id, continuation in owners:
                owns_source = any(
                    (continuation.entity_name, event_id) in parked.sources for event_id in continuation.source_event_ids
                )
                if owns_source or await asyncio.to_thread(_approval_uses_jobs, continuation, config, runtime_paths):
                    parked.approvals.add(continuation.approval_id)
                    parked.sources.update(
                        (continuation.entity_name, event_id) for event_id in continuation.source_event_ids
                    )
            cursor = (owners[-1][1].entity_name, owners[-1][1].approval_id)
    _PARKED[runtime_paths.storage_root.resolve()] = parked
