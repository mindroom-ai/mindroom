"""Background auto-flush for file-backed memory: the new finished turns of conversations become daily notes.

Stored sessions are the source of truth; one progress file below `tracking/` records how far each conversation has been
flushed, so a restart resumes where the last pass stopped, and each note names its last turn, so a retry never writes it
twice.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, TypedDict
from uuid import uuid4

from agno.agent import Agent

from mindroom.automations.steps import is_automation_hook_source
from mindroom.constants import HOOK_SOURCE_RUN_METADATA_KEY, tracking_dir
from mindroom.durable_write import write_json_file_durable
from mindroom.helper_usage import HelperUsageOwner, record_helper_usage
from mindroom.history.session_runs import (
    DELEGATION_SESSION_PREFIX,
    finished_runs,
    run_key,
    session_runs,
    unredacted_runs,
    updated_sessions,
)
from mindroom.logging_config import get_logger
from mindroom.memory._prompting import redact_memory_text
from mindroom.memory.functions import append_agent_daily_memory, list_all_agent_memories
from mindroom.memory.legacy_auto_flush import remove_legacy_flush_queue
from mindroom.providers import model_loading
from mindroom.requester_identity import agent_instances
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_system.worker_routing import build_requester_execution_identity

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agno.run.agent import RunOutput

    from mindroom.config.main import Config
    from mindroom.config.memory import MemoryAutoFlushConfig
    from mindroom.constants import RuntimePaths
    from mindroom.history.session_runs import RedactedEventIds
    from mindroom.memory._shared import MemoryResult
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

_STATE_FILENAME = "memory_auto_flush.json"
_DISABLED_POLL_SECONDS = 30
_MAX_LINE_CHARS = 500
_TAG_PREFIX = "[auto_flush:"


@dataclass(frozen=True)
class _FlushScope:
    """One file-memory agent, or one requester's private instance of it, and its key in the progress file."""

    agent_name: str
    execution_identity: ToolExecutionIdentity | None
    key: str


class _ConversationState(TypedDict):
    # The conversation's update time once every new turn in it was flushed.
    updated_at: int
    # The run key of the last turn flushed or skipped; empty until then, when turns from before auto-flush started are
    # skipped.
    marker: list[int | str]
    # Consecutive failures, which set the backoff.
    failures: int
    # When the current failures began, so turns that never flush are given up after a while.
    failing_since: float
    retry_at: float
    # When a turn holding the conversation back stops waiting for approval, or 0.
    recheck_at: float


class _ScopeState(TypedDict):
    # Turns created before this second, its agent's start, are never flushed.
    started_at: int
    conversations: dict[str, _ConversationState]


class _FlushState(TypedDict):
    started_at: int
    # Each file-memory agent's start, which every scope of the agent shares.
    agents: dict[str, int]
    scopes: dict[str, _ScopeState]


@dataclass(frozen=True)
class _DueConversation:
    session_id: str
    updated_at: int
    runs: list[RunOutput]


def auto_flush_enabled(config: Config) -> bool:
    """Return whether file-memory auto-flush is enabled."""
    return config.memory.auto_flush.enabled and config.uses_file_memory()


def _file_memory_agents(config: Config) -> list[str]:
    return [name for name in config.agents if config.resolve_entity(name).memory_backend == "file"]


def _flush_scopes(config: Config, runtime_paths: RuntimePaths, agent_names: list[str]) -> list[_FlushScope]:
    """Return each shared agent and each private instance of a private one."""
    storage_root = runtime_paths.storage_root.expanduser().resolve()
    scopes: list[_FlushScope] = []
    for agent_name, requester_id in agent_instances(config, runtime_paths, agent_names):
        identity = (
            None
            if requester_id is None
            else build_requester_execution_identity(
                agent_name=agent_name,
                runtime_paths=runtime_paths,
                requester_id=requester_id,
            )
        )
        # The state root is unique per agent and per private instance, and stable across restarts.
        state_root = resolve_agent_storage(agent_name, config, runtime_paths, identity).state_root
        scopes.append(_FlushScope(agent_name, identity, state_root.relative_to(storage_root).as_posix()))
    return scopes


def _new_conversation() -> _ConversationState:
    return {"updated_at": 0, "marker": [], "failures": 0, "failing_since": 0, "retry_at": 0, "recheck_at": 0}


def _start(entry: _ConversationState, runs: list[RunOutput], started_at: int) -> tuple[int, str] | None:
    """Return the run key the conversation's unflushed turns follow, or None when all of them are unflushed."""
    if entry["marker"]:
        return (int(entry["marker"][0]), str(entry["marker"][1]))
    # A conversation first seen now starts with the turns after auto-flush started, so history is never flushed.
    older = [run for run in runs if (run.created_at or 0) < started_at]
    return run_key(older[-1]) if older else None


def _is_automation_turn(run: RunOutput) -> bool:
    """Return whether an automation started the turn; its maintenance work, such as a memory proposal, is not memory."""
    hook_source = (run.metadata or {}).get(HOOK_SOURCE_RUN_METADATA_KEY)
    return isinstance(hook_source, str) and is_automation_hook_source(hook_source)


def _run_lines(run: RunOutput) -> list[str]:
    """Return one turn as the ``role: text`` lines auto-flush extracts memories from."""
    lines: list[str] = []
    for message in run.messages or []:
        if message.from_history or message.role not in {"user", "assistant"} or not isinstance(message.content, str):
            continue
        cleaned = " ".join(redact_memory_text(message.content).split())
        if not cleaned:
            continue
        if len(cleaned) > _MAX_LINE_CHARS:
            cleaned = f"{cleaned[: _MAX_LINE_CHARS - 3]}..."
        lines.append(f"{message.role}: {cleaned}")
    return lines


def _newest_lines(lines: list[str], *, max_messages: int, max_chars: int) -> list[str]:
    """Return the newest lines within one extraction's limits, oldest first."""
    selected: list[str] = []
    char_count = 0
    for line in reversed(lines):
        next_count = char_count + len(line) + 1
        if selected and next_count > max_chars:
            break
        selected.append(line)
        char_count = next_count
        if len(selected) >= max_messages:
            break
    selected.reverse()
    return selected


def _next_batch(runs: list[RunOutput], settings: MemoryAutoFlushConfig) -> tuple[list[RunOutput], list[str]]:
    """Return the oldest turns whose lines fit one extraction, and those lines; a turn too long alone keeps its newest."""
    limits = {
        "max_messages": settings.extractor.max_messages_per_flush,
        "max_chars": settings.extractor.max_chars_per_flush,
    }
    batch: list[RunOutput] = []
    lines: list[str] = []
    for run in runs:
        candidate = [*lines, *_run_lines(run)]
        if batch and len(_newest_lines(candidate, **limits)) < len(candidate):
            break
        batch.append(run)
        lines = candidate
    return batch, _newest_lines(lines, **limits)


def _flush_tag(session_id: str, run: RunOutput) -> str:
    return f"{_TAG_PREFIX}{session_id}:{run.run_id}]"


def _already_flushed(runs: list[RunOutput], session_id: str, memories: list[MemoryResult]) -> int:
    """Return how many of the runs a note written before its progress was saved already covers."""
    tags = {
        text.partition("]")[0] + "]"
        for memory in memories
        if (text := memory.get("memory", "")).startswith(_TAG_PREFIX)
    }
    covered = [index for index, run in enumerate(runs, 1) if _flush_tag(session_id, run) in tags]
    return covered[-1] if covered else 0


def _record_failure(entry: _ConversationState, settings: MemoryAutoFlushConfig, failed_at: float) -> bool:
    """Count one failed attempt, back off from when it failed, and return whether to give up on the failing turns."""
    entry["failing_since"] = entry["failing_since"] or failed_at
    entry["failures"] += 1
    entry["retry_at"] = failed_at + _retry_cooldown_seconds(settings, entry["failures"])
    return failed_at - entry["failing_since"] >= settings.stale_ttl_seconds


def _record_unreadable(
    entry: _ConversationState,
    settings: MemoryAutoFlushConfig,
    updated_at: int,
    failed_at: float,
) -> None:
    """Back off a conversation that could not be read, and once that has failed for long enough, wait until it changes."""
    if _record_failure(entry, settings, failed_at):
        entry.update(updated_at=updated_at, failures=0, failing_since=0, retry_at=0)


def _record_progress(
    entry: _ConversationState,
    pending: list[RunOutput],
    flushable: list[RunOutput],
    covered: int,
    updated_at: int,
    blocked_until: float | None,
) -> None:
    """Move the marker past the turns flushed or given up, and mark the conversation done when none is left."""
    entry.update(failures=0, failing_since=0, retry_at=0)
    if covered < len(flushable):
        # A long backlog spreads over passes; the conversation stays due because its recorded update time is unchanged.
        entry["marker"] = list(run_key(flushable[covered - 1]))
        return
    if pending:
        entry["marker"] = list(run_key(pending[-1]))
    # Held back by a turn waiting for approval, it is looked at again when those approvals expire, even without news.
    entry.update(updated_at=updated_at, recheck_at=blocked_until or 0)


def _normalize_extractor_line(line: str, no_reply_token: str) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    if stripped.upper() == no_reply_token.upper():
        return None
    if stripped.startswith(("- ", "* ")):
        stripped = stripped[2:].strip()
    stripped = re.sub(r"^\d+\.\s+", "", stripped)
    return stripped or None


def _sanitize_extractor_output(raw_output: str, no_reply_token: str) -> str | None:
    cleaned = raw_output.strip()
    if not cleaned:
        return None
    if cleaned.upper() == no_reply_token.upper():
        return None

    normalized_lines = [
        normalized
        for line in cleaned.splitlines()
        if (normalized := _normalize_extractor_line(line, no_reply_token)) is not None
    ]

    if not normalized_lines:
        return None

    deduped: list[str] = []
    seen: set[str] = set()
    for line in normalized_lines:
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(line)
    return " | ".join(deduped[:10])


def _existing_memory_context(memories: list[MemoryResult], config: Config) -> str:
    """Return a few of the agent's memories for the extractor to avoid repeating."""
    context_config = config.memory.auto_flush.extractor.include_memory_context
    if context_config.memory_snippets <= 0:
        return ""

    max_memories = max(context_config.memory_snippets * 8, context_config.memory_snippets)
    snippets: list[str] = []
    for memory in reversed(memories[:max_memories]):
        text = memory.get("memory", "").strip()
        if not text:
            continue
        if len(text) > context_config.snippet_max_chars:
            text = f"{text[: context_config.snippet_max_chars - 3]}..."
        snippets.append(f"- {text}")
        if len(snippets) >= context_config.memory_snippets:
            break

    snippets.reverse()
    return "\n".join(snippets)


async def _extract_memory_summary(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    session_id: str,
    lines: list[str],
    existing_context: str,
    execution_identity: ToolExecutionIdentity | None = None,
) -> str | None:
    extractor = config.memory.auto_flush.extractor
    existing_block = (
        config.render_prompt("MEMORY_EXISTING_SNIPPETS_TEMPLATE", existing_context=existing_context)
        if existing_context
        else config.get_prompt("MEMORY_NO_EXISTING_SNIPPETS")
    )
    excerpt = "\n".join(lines)
    prompt = config.render_prompt(
        "MEMORY_AUTO_FLUSH_EXTRACT_PROMPT_TEMPLATE",
        no_reply_token=extractor.no_reply_token,
        existing_block=existing_block,
        excerpt=excerpt,
    )

    model_name = config.resolve_entity(agent_name).model_name
    model = model_loading.get_model_instance(config, runtime_paths, model_name)
    extractor_agent = Agent(
        name="MemoryAutoFlushExtractor",
        role="Extract durable memory statements for long-term memory storage.",
        model=model,
        telemetry=False,
    )
    invocation_id = uuid4().hex
    response = await extractor_agent.arun(
        prompt,
        run_id=invocation_id,
        session_id=f"memory_auto_flush_extract:{agent_name}:{session_id}",
    )
    await record_helper_usage(
        response,
        owner=HelperUsageOwner(
            storage_factory=partial(
                create_session_storage,
                agent_name,
                config,
                runtime_paths,
                execution_identity=execution_identity,
            ),
            session_id=session_id,
        ),
        invocation_id=invocation_id,
        kind="memory_auto_flush",
        requester_id=execution_identity.requester_id if execution_identity is not None else None,
    )
    content = response.content
    raw_output = content if isinstance(content, str) else str(content or "")
    return _sanitize_extractor_output(raw_output, extractor.no_reply_token)


def _retry_cooldown_seconds(settings: MemoryAutoFlushConfig, failures: int) -> int:
    if failures <= 0:
        return settings.retry_cooldown_seconds
    multiplier = 2 ** max(0, failures - 1)
    return min(
        settings.max_retry_cooldown_seconds,
        settings.retry_cooldown_seconds * multiplier,
    )


@dataclass
class MemoryAutoFlushWorker:
    """Periodically flush the new finished turns of every file-memory agent and private instance into daily notes."""

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config | None]
    redacted_event_ids: RedactedEventIds
    clock: Callable[[], float] = time.time
    _passes: int = field(default=0, init=False)

    async def run(self) -> None:
        """Run flush passes until cancelled."""
        while True:
            config = self.config_provider()
            interval = _DISABLED_POLL_SECONDS
            if config is not None and auto_flush_enabled(config):
                try:
                    await self._run_cycle(config)
                except Exception:
                    # A failed pass, such as an unwritable progress file, must not end flushing until a restart.
                    logger.exception("Memory auto-flush pass failed")
                interval = config.memory.auto_flush.flush_interval_seconds
            await asyncio.sleep(interval)

    async def _run_cycle(self, config: Config) -> None:
        """Flush up to ``max_sessions_per_cycle`` quiet conversations with new turns."""
        settings = config.memory.auto_flush
        now = self.clock()
        state = await asyncio.to_thread(self._load_state, now)
        agent_names = _file_memory_agents(config)
        # An agent first seen after the start, such as one switched to file memory, starts one pass interval before it
        # was seen, so its older history is never flushed. Its private instances share that start, so an instance keeps
        # all its turns however late it is first listed.
        new_agents = [name for name in agent_names if name not in state["agents"]]
        for name in new_agents:
            state["agents"][name] = max(state["started_at"], int(now) - settings.flush_interval_seconds)
        if new_agents:
            await self._persist(state)
        scopes = await asyncio.to_thread(_flush_scopes, config, self.runtime_paths, agent_names)
        for scope in scopes:
            state["scopes"].setdefault(
                scope.key,
                {"started_at": state["agents"][scope.agent_name], "conversations": {}},
            )
        budget = settings.batch.max_sessions_per_cycle
        # Each pass starts with the next scope, so one with a long backlog cannot take every pass's budget.
        start = self._passes % len(scopes) if scopes else 0
        self._passes += 1
        for scope in [*scopes[start:], *scopes[:start]]:
            if budget <= 0:
                break
            # One agent or private instance whose conversations cannot be listed must not stop the others.
            try:
                scope_state = state["scopes"][scope.key]
                limit = min(budget, settings.batch.max_sessions_per_agent_per_cycle)
                due, changed = await asyncio.to_thread(self._due, config, scope, scope_state, now, limit)
                if changed:
                    await self._persist(state)
                for conversation in due:
                    budget -= await self._flush(config, scope, state, scope_state, conversation, now)
            except Exception:
                logger.exception("Memory auto-flush failed for an agent", agent=scope.agent_name)

    def _due(
        self,
        config: Config,
        scope: _FlushScope,
        scope_state: _ScopeState,
        now: float,
        limit: int,
    ) -> tuple[list[_DueConversation], bool]:
        """Return up to ``limit`` quiet conversations with new turns, and whether listing them changed the progress."""
        settings = config.memory.auto_flush
        # At least a second, so a conversation updated after a pass always carries a later update time than it had then.
        idle_seconds = max(settings.idle_seconds, 1)
        storage = create_session_storage(
            scope.agent_name,
            config,
            self.runtime_paths,
            execution_identity=scope.execution_identity,
        )
        conversations = scope_state["conversations"]
        try:
            due: list[_DueConversation] = []
            changed = False
            for session_id, updated_at in updated_sessions(
                storage,
                scope.agent_name,
                # Update times are whole seconds, so one in the start's own second is listed too.
                since=scope_state["started_at"] - 1,
            ):
                if len(due) >= limit:
                    break
                # A delegated task's prompt comes from another agent, so it is not the delegate's conversation.
                if session_id.startswith(DELEGATION_SESSION_PREFIX) or now - updated_at < idle_seconds:
                    continue
                entry = conversations.get(session_id)
                if entry is not None and (
                    entry["retry_at"] > now
                    or (updated_at <= entry["updated_at"] and not 0 < entry["recheck_at"] <= now)
                ):
                    continue
                try:
                    runs = session_runs(storage, session_id)
                except Exception:
                    # One conversation that cannot be read backs off instead of blocking the agent's others.
                    logger.exception(
                        "Memory auto-flush could not read a conversation",
                        agent=scope.agent_name,
                        session_id=session_id,
                    )
                    entry = conversations.setdefault(session_id, _new_conversation())
                    _record_unreadable(entry, settings, updated_at, self.clock())
                    changed = True
                    continue
                if not runs:
                    # A conversation with no Matrix turns, such as one through the OpenAI-compatible API, has nothing to
                    # flush, so it does not take a place in the pass.
                    conversations.setdefault(session_id, _new_conversation())["updated_at"] = updated_at
                    changed = True
                    continue
                due.append(_DueConversation(session_id, updated_at, runs))
            return due, changed
        finally:
            storage.close()

    async def _flush(
        self,
        config: Config,
        scope: _FlushScope,
        state: _FlushState,
        scope_state: _ScopeState,
        conversation: _DueConversation,
        now: float,
    ) -> int:
        """Flush the oldest new turns of one conversation and return how much of the pass budget that spent."""
        settings = config.memory.auto_flush
        session_id = conversation.session_id
        conversations = scope_state["conversations"]
        finished, blocked_until = finished_runs(conversation.runs, config, now=now)
        try:
            runs = await unredacted_runs(finished, scope.agent_name, self.redacted_event_ids)
        except Exception:
            # A conversation whose redactions cannot be checked backs off like one that cannot be read.
            logger.exception(
                "Memory auto-flush could not check a conversation for redactions",
                agent=scope.agent_name,
                session_id=session_id,
            )
            _record_unreadable(
                conversations.setdefault(session_id, _new_conversation()),
                settings,
                conversation.updated_at,
                self.clock(),
            )
            await self._persist(state)
            return 0
        if runs is None:
            # Its agent cannot say yet what was redacted, so the conversation waits for a later pass.
            return 0
        entry = conversations.setdefault(session_id, _new_conversation())
        start = _start(entry, runs, scope_state["started_at"])
        pending = [run for run in runs if start is None or run_key(run) > start]
        flushable = [run for run in pending if not _is_automation_turn(run)]
        skipped = 0
        batch: list[RunOutput] = []
        try:
            if flushable:
                memories = await list_all_agent_memories(
                    scope.agent_name,
                    self.runtime_paths.storage_root,
                    config,
                    self.runtime_paths,
                    limit=sys.maxsize,
                    execution_identity=scope.execution_identity,
                )
                # A note written by an attempt whose progress was not saved already covers its turns.
                skipped = _already_flushed(flushable, session_id, memories)
                batch, lines = _next_batch(flushable[skipped:], settings)
                if lines:
                    await self._write_note(config, scope, session_id, batch[-1], lines, memories)
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                logger.warning(
                    "Memory auto-flush timed out",
                    agent=scope.agent_name,
                    session_id=session_id,
                    timeout_seconds=settings.extractor.max_extraction_seconds,
                )
            else:
                logger.exception("Memory auto-flush failed", agent=scope.agent_name, session_id=session_id)
            if not _record_failure(entry, settings, self.clock()):
                await self._persist(state)
                return 1
            logger.warning(
                "Memory auto-flush gave up on turns it kept failing to flush",
                agent=scope.agent_name,
                session_id=session_id,
            )
            batch = _next_batch(flushable[skipped:], settings)[0]
        _record_progress(entry, pending, flushable, skipped + len(batch), conversation.updated_at, blocked_until)
        await self._persist(state)
        return 1

    async def _write_note(
        self,
        config: Config,
        scope: _FlushScope,
        session_id: str,
        last_run: RunOutput,
        lines: list[str],
        memories: list[MemoryResult],
    ) -> None:
        """Extract durable memories from the lines and append them to today's daily note, tagged with the last turn."""
        summary = await asyncio.wait_for(
            _extract_memory_summary(
                config=config,
                runtime_paths=self.runtime_paths,
                agent_name=scope.agent_name,
                session_id=session_id,
                lines=lines,
                existing_context=_existing_memory_context(memories, config),
                execution_identity=scope.execution_identity,
            ),
            timeout=config.memory.auto_flush.extractor.max_extraction_seconds,
        )
        if summary is not None:
            append_agent_daily_memory(
                f"{_flush_tag(session_id, last_run)} {summary}",
                agent_name=scope.agent_name,
                storage_path=self.runtime_paths.storage_root,
                config=config,
                runtime_paths=self.runtime_paths,
                execution_identity=scope.execution_identity,
            )

    def _state_path(self) -> Path:
        return tracking_dir(self.runtime_paths) / _STATE_FILENAME

    def _load_state(self, now: float) -> _FlushState:
        try:
            return json.loads(self._state_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            remove_legacy_flush_queue(self.runtime_paths.storage_root)
        except ValueError:
            # Starting over skips turns not flushed yet but never flushes earlier conversation again.
            logger.warning("Memory auto-flush progress was unreadable; starting over")
        # Saved at once, so turns from before this start are never flushed, even for conversations first seen later.
        state: _FlushState = {"started_at": int(now), "agents": {}, "scopes": {}}
        write_json_file_durable(self._state_path(), state)
        return state

    async def _persist(self, state: _FlushState) -> None:
        await asyncio.to_thread(write_json_file_durable, self._state_path(), state)
