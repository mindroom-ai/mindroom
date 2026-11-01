"""Background auto-flush for file-backed memory: the new finished turns of conversations become daily notes.

Stored sessions are the source of truth; one progress file below `tracking/` records how far each conversation's log
has been flushed, so a restart resumes where the last pass stopped, and each note names its last turn's place, so a
retry never writes it twice.
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

from mindroom.agent_loop.helper import complete_prompt
from mindroom.background_tasks import SupervisedLoop
from mindroom.config.resolution import entity_model_name, memory_backend
from mindroom.constants import tracking_dir
from mindroom.contacts.turns import contact_session_check
from mindroom.durable_write import write_json_file_durable
from mindroom.helper_usage import HelperUsageOwner, record_helper_usage
from mindroom.history.conversation_text import one_line, utterances
from mindroom.history.session_runs import (
    DELEGATION_SESSION_PREFIX,
    Cursor,
    back_off,
    caught_up,
    due_conversations,
    start_seq,
    unredacted,
)
from mindroom.llm.errors import ModelSafeguardRefusal
from mindroom.logging_config import get_logger
from mindroom.memory._prompting import redact_memory_text
from mindroom.memory.functions import append_agent_daily_memory, list_all_agent_memories
from mindroom.memory.legacy_auto_flush import (
    released_flush_start,
    released_note_covers,
    released_note_turns,
    remove_legacy_flush_queue,
)
from mindroom.providers import registry as provider_registry
from mindroom.requester_identity import agent_instances
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_system.worker_routing import build_requester_execution_identity

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.config.memory import MemoryAutoFlushConfig
    from mindroom.constants import RuntimePaths
    from mindroom.history.session_runs import DueConversation, RedactedEventIds
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.memory._shared import MemoryResult
    from mindroom.session_storage.store import LoggedRun
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


class _ConversationState(Cursor):
    # The log place of the last turn flushed or given up; None until the conversation is first read, when its turns from
    # before auto-flush started are skipped.
    seq: int | None


class _FlushState(TypedDict):
    started_at: int
    # Each file-memory agent's start; in any of the agent's scopes, turns created before it that had finished when their
    # conversation was first read are never flushed.
    agents: dict[str, int]
    # Each scope's conversations by session ID.
    scopes: dict[str, dict[str, _ConversationState]]


def _auto_flush_enabled(config: Config) -> bool:
    """Return whether file-memory auto-flush is enabled."""
    return config.memory.auto_flush.enabled and config.uses_file_memory()


def _file_memory_agents(config: Config) -> list[str]:
    return [name for name in config.agents if memory_backend(config, name) == "file"]


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
    return {"updated_at": 0, "seq": None, "failures": 0, "failing_since": 0, "retry_at": 0}


def _progress(
    conversations: dict[str, _ConversationState],
    session_id: str,
    turns: list[LoggedRun],
    started_at: int,
) -> tuple[_ConversationState, int]:
    """Return the conversation's progress and the place its unflushed turns follow, fixed when it is first read."""
    entry = conversations.setdefault(session_id, _new_conversation())
    if (seq := entry.get("seq")) is not None:
        return entry, seq
    seq = released_flush_start(entry, turns)
    if seq is None:
        # A conversation first seen now starts with the turns after auto-flush started, so history is never flushed.
        seq = start_seq(turns, older=lambda run: run.created_at < started_at)
    # Rebuilt rather than updated, so a released entry's `marker` and `recheck_at` leave the file.
    current: _ConversationState = {
        "updated_at": entry["updated_at"],
        "seq": seq,
        "failures": entry["failures"],
        "failing_since": entry["failing_since"],
        "retry_at": entry["retry_at"],
    }
    conversations[session_id] = current
    return current, seq


def _run_lines(run: AnyRunRecord) -> list[str]:
    """Return what was said in one turn as the ``speaker: text`` lines auto-flush extracts memories from."""
    lines: list[str] = []
    for said in utterances(run):
        cleaned = one_line(redact_memory_text(said.unredacted))
        if not cleaned:
            continue
        if len(cleaned) > _MAX_LINE_CHARS:
            cleaned = f"{cleaned[: _MAX_LINE_CHARS - 3]}..."
        lines.append(f"{said.sender or 'assistant'}: {cleaned}")
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


def _next_batch(turns: list[LoggedRun], settings: MemoryAutoFlushConfig) -> tuple[list[LoggedRun], list[str]]:
    """Return the oldest turns whose lines fit one extraction, and those lines; a turn too long alone keeps its newest."""
    limits = {
        "max_messages": settings.extractor.max_messages_per_flush,
        "max_chars": settings.extractor.max_chars_per_flush,
    }
    batch: list[LoggedRun] = []
    lines: list[str] = []
    for turn in turns:
        candidate = [*lines, *_run_lines(turn.run)]
        # Turns with no lines at the start of a batch, such as an automation's, ride along with the turn after them.
        if lines and len(_newest_lines(candidate, **limits)) < len(candidate):
            break
        batch.append(turn)
        lines = candidate
    return batch, _newest_lines(lines, **limits)


def _flush_tag(session_id: str, seq: int) -> str:
    return f"{_TAG_PREFIX}{session_id}@{seq}]"


def _unflushed_turns(
    pending: list[LoggedRun],
    turns: list[LoggedRun],
    session_id: str,
    memories: list[MemoryResult],
) -> list[LoggedRun]:
    """Return the pending turns no note written before its progress was saved already covers.

    A note covers the turns placed at or before the turn it names, its batch's last.
    """
    notes = [memory.get("memory", "") for memory in memories]
    prefix = f"{_TAG_PREFIX}{session_id}@"
    places = [note[len(prefix) :].partition("]")[0] for note in notes if note.startswith(prefix)]
    through = max((int(place) for place in places if place.isdigit()), default=0)
    named = released_note_turns(notes, session_id, turns)
    return [turn for turn in pending if turn.seq > through and not released_note_covers(named, turn)]


def _record_failure(entry: _ConversationState, settings: MemoryAutoFlushConfig, failed_at: float) -> bool:
    """Count one failed attempt, back off from when it failed, and return whether to give up on the failing turns."""
    back_off(
        entry,
        failed_at,
        first_seconds=settings.retry_cooldown_seconds,
        max_seconds=settings.max_retry_cooldown_seconds,
    )
    return failed_at - entry["failing_since"] >= settings.stale_ttl_seconds


def _record_unreadable(
    entry: _ConversationState,
    settings: MemoryAutoFlushConfig,
    updated_at: int,
    failed_at: float,
) -> None:
    """Back off a conversation that could not be read, and once that has failed for long enough, wait until it changes."""
    if _record_failure(entry, settings, failed_at):
        caught_up(entry, updated_at)


def _record_progress(
    entry: _ConversationState,
    pending: list[LoggedRun],
    fresh: list[LoggedRun],
    done: list[LoggedRun],
    updated_at: int,
) -> None:
    """Move past the turns flushed or given up, and mark the conversation caught up when none of ``fresh`` is left.

    ``fresh`` are the pending turns no note covers yet and ``done`` the first of them, flushed or given up.
    """
    if len(done) < len(fresh):
        # A long backlog spreads over passes; the conversation stays due because its recorded update time is unchanged.
        entry.update(seq=done[-1].seq, failures=0, failing_since=0, retry_at=0)
        return
    if pending:
        entry["seq"] = pending[-1].seq
    caught_up(entry, updated_at)


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
    unique: dict[str, str] = {}
    for line in raw_output.splitlines():
        if (normalized := _normalize_extractor_line(line, no_reply_token)) is not None:
            unique.setdefault(normalized.lower(), normalized)
    return " | ".join(list(unique.values())[:10]) or None


def _existing_memory_context(memories: list[MemoryResult], config: Config) -> str:
    """Return a few of the agent's memories for the extractor to avoid repeating."""
    context_config = config.memory.auto_flush.extractor.include_memory_context
    if context_config.memory_snippets <= 0:
        return ""

    snippets: list[str] = []
    for memory in reversed(memories[: context_config.memory_snippets * 8]):
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
    execution_identity: ToolExecutionIdentity | None,
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

    model_name = extractor.model or entity_model_name(config, agent_name)
    response = await complete_prompt(
        provider_registry.build_model_provider(config, runtime_paths, model_name),
        prompt,
        instructions=["Extract durable memory statements for long-term memory storage."],
        run_id=uuid4().hex,
    )
    await record_helper_usage(
        response.usage,
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
        kind="memory_auto_flush",
        requester_id=execution_identity.requester_id if execution_identity is not None else None,
    )
    return _sanitize_extractor_output(response.content, extractor.no_reply_token)


@dataclass
class _MemoryAutoFlushWorker:
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
            if config is not None and _auto_flush_enabled(config):
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
        budget = settings.batch.max_sessions_per_cycle
        # Each pass starts with the next scope, so one with a long backlog cannot take every pass's budget.
        start = self._passes % len(scopes) if scopes else 0
        self._passes += 1
        for scope in [*scopes[start:], *scopes[:start]]:
            if budget <= 0:
                break
            # One agent or private instance whose conversations cannot be listed must not stop the others.
            try:
                limit = min(budget, settings.batch.max_sessions_per_agent_per_cycle)
                due, changed = await asyncio.to_thread(self._due, config, scope, state, now, limit)
                if changed:
                    await self._persist(state)
                for conversation in due:
                    budget -= await self._flush(config, scope, state, conversation)
            except Exception:
                logger.exception("Memory auto-flush failed for an agent", agent=scope.agent_name)

    def _due(
        self,
        config: Config,
        scope: _FlushScope,
        state: _FlushState,
        now: float,
        limit: int,
    ) -> tuple[list[DueConversation], bool]:
        """Return up to ``limit`` quiet conversations with news, and whether listing them changed the progress."""
        settings = config.memory.auto_flush
        storage = create_session_storage(
            scope.agent_name,
            config,
            self.runtime_paths,
            execution_identity=scope.execution_identity,
        )
        conversations = state["scopes"].setdefault(scope.key, {})
        in_contact_room = contact_session_check(config, self.runtime_paths, scope.agent_name)
        scan = due_conversations(
            storage,
            conversations,
            agent_name=scope.agent_name,
            # Update times are whole seconds, so one in the start's own second is listed too.
            since=state["agents"][scope.agent_name] - 1,
            now=now,
            idle_seconds=settings.idle_seconds,
            limit=limit,
            # A delegated task's prompt comes from another agent, so it is not the delegate's conversation, and the
            # other side of a contact drove the conversations in its contact room.
            skip=lambda session_id: session_id.startswith(DELEGATION_SESSION_PREFIX) or in_contact_room(session_id),
        )
        for session_id, updated_at in scan.empty:
            caught_up(conversations.setdefault(session_id, _new_conversation()), updated_at)
        for unreadable in scan.unreadable:
            # One conversation that cannot be read backs off instead of blocking the agent's others.
            logger.error(
                "Memory auto-flush could not read a conversation",
                agent=scope.agent_name,
                session_id=unreadable.session_id,
                exc_info=unreadable.error,
            )
            entry = conversations.setdefault(unreadable.session_id, _new_conversation())
            _record_unreadable(entry, settings, unreadable.updated_at, self.clock())
        return scan.due, bool(scan.empty or scan.unreadable)

    async def _flush(
        self,
        config: Config,
        scope: _FlushScope,
        state: _FlushState,
        conversation: DueConversation,
    ) -> int:
        """Flush the oldest new turns of one conversation and return how much of the pass budget that spent."""
        settings = config.memory.auto_flush
        session_id = conversation.session_id
        conversations = state["scopes"][scope.key]
        try:
            kept = await unredacted(conversation, scope.agent_name, self.redacted_event_ids)
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
        if kept is None:
            # Its agent cannot say yet what was redacted, so the conversation waits for a later pass.
            return 0
        entry, start = _progress(conversations, session_id, conversation.turns, state["agents"][scope.agent_name])
        pending = fresh = [turn for turn in kept if turn.seq > start]
        batch: list[LoggedRun] = []
        try:
            if pending:
                memories = await list_all_agent_memories(
                    scope.agent_name,
                    config,
                    self.runtime_paths,
                    limit=sys.maxsize,
                    execution_identity=scope.execution_identity,
                )
                # A note written by an attempt whose progress was not saved already covers its turns.
                fresh = _unflushed_turns(pending, conversation.turns, session_id, memories)
                batch, lines = _next_batch(fresh, settings)
                if lines:
                    await self._write_note(config, scope, session_id, batch[-1], lines, memories)
        except ModelSafeguardRefusal:
            # The model refuses the same turns every time, so they are given up at once instead of retried.
            logger.warning(
                "Memory auto-flush gave up on turns the model's safeguards refused",
                agent=scope.agent_name,
                session_id=session_id,
            )
            batch = _next_batch(fresh, settings)[0]
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
            batch = _next_batch(fresh, settings)[0]
        _record_progress(entry, pending, fresh, batch, conversation.updated_at)
        await self._persist(state)
        return 1

    async def _write_note(
        self,
        config: Config,
        scope: _FlushScope,
        session_id: str,
        last_turn: LoggedRun,
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
                f"{_flush_tag(session_id, last_turn.seq)} {summary}",
                agent_name=scope.agent_name,
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


@dataclass
class MemoryAutoFlushService:
    """Run the auto-flush worker while the config enables auto-flush."""

    name = "memory_auto_flush"
    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config | None]
    redacted_event_ids: RedactedEventIds
    _loop: SupervisedLoop = field(init=False)

    def __post_init__(self) -> None:
        """Create the supervised worker loop."""
        self._loop = SupervisedLoop("memory_auto_flush_worker", self._run_worker)

    async def sync(self, config: Config) -> None:
        """Start the worker when ``config`` enables auto-flush, and stop it otherwise."""
        if _auto_flush_enabled(config):
            self._loop.start()
        else:
            await self.stop()

    async def stop(self) -> None:
        """Stop the worker without waiting for its pass."""
        # A pass saves each conversation's progress and a retried note is never written twice, so cancelling loses
        # nothing.
        await self._loop.stop()

    async def _run_worker(self) -> None:
        worker = _MemoryAutoFlushWorker(
            runtime_paths=self.runtime_paths,
            config_provider=self.config_provider,
            redacted_event_ids=self.redacted_event_ids,
        )
        await worker.run()
