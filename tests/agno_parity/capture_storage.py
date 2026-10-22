"""Capture the conversation stores, run dicts, history selections, and run events Agno 3.0.9 produces in MindRoom.

Every scenario runs MindRoom's real code path (``ai_response``, ``stream_agent_response``, ``team_response``,
``team_response_stream``, the OpenAI-compatible API, and MindRoom's storage, compaction, and reply-linkage functions)
with real Agno agents and teams; only the provider is replaced by a scripted model.
``python -m tests.agno_parity.capture_storage`` rewrites the fixtures, and
``tests/fixtures/agno_parity/README.md`` describes them.
Task B8 deletes this module together with Agno.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as datetime_module
import json
import os
import re
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import types
import zlib
from collections import deque
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import agno.utils.timer
from agno.exceptions import ModelProviderError
from agno.media import Audio, File, Image, Video
from agno.metrics import MessageMetrics
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from agno.run.cancel import acancel_run
from agno.session.summary import SessionSummary
from agno.utils.message import copy_history_message, filter_tool_calls
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mindroom.providers.synthetic_model import SyntheticModel
from mindroom.session_storage.agno_compat_session_persistence import drain_agent_cancellation

from mindroom.agents import create_agent
from mindroom.ai import ai_response, stream_agent_response
from mindroom.api import openai_compat
from mindroom.api.main import initialize_api_app
from mindroom.approval.execution import AgentApprovalExecution
from mindroom.approval.receipt import approval_receipt_context, build_approval_receipt
from mindroom.approval.response import identify_approval_tools, plan_approval_calls
from mindroom.config.main import Config
from mindroom.constants import prompt_roles_for_history_storage, resolve_runtime_paths
from mindroom.conversation_state_writer import ConversationStateWriter, ConversationStateWriterDeps
from mindroom.delegation.state import DelegationChild, DelegationState
from mindroom.event_journal import ApprovalDecision
from mindroom.event_journal.approval_continuations import ApprovalContinuation
from mindroom.history import summary_rows
from mindroom.history.session_context import _scope_session_storage_name
from mindroom.history.storage import (
    append_compaction_chunk,
    append_fold,
    read_chunk_basis,
    read_fold_basis,
    remove_runs_answering,
)
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import HistoryScope
from mindroom.knowledge.utils import KnowledgeAccessSupport
from mindroom.logging_config import get_logger
from mindroom.media_inputs import MediaInputs
from mindroom.message_target import MessageTarget
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import (
    CompletedApprovalRun,
    PausedAnswer,
    ResponsePausedForApproval,
    build_matrix_run_metadata,
)
from mindroom.session_ids import create_session_id
from mindroom.session_storage.agent_storage import (
    create_session_storage,
    create_state_storage,
    get_agent_session,
    get_team_session,
    save_compaction_usage,
)
from mindroom.teams.envelope import team_response, team_response_stream
from mindroom.teams.members import select_model_for_team
from mindroom.teams.resume import continue_paused_team_run
from mindroom.teams.spec import TeamMode
from mindroom.tool_system.runtime_context import LiveToolDispatchContext, tool_runtime_context
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    run_with_tool_execution_identity,
    serialize_tool_execution_identity,
)
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    create_agno_2_sessions_db,
    make_conversation_reader_mock,
    make_relation_lookup,
    make_turn_context,
    message_origin,
)
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping

    from agno.db.base import BaseDb
    from agno.session.agent import AgentSession
    from agno.session.team import TeamSession

    from mindroom.ai import ResponseTurnContext
    from mindroom.constants import RuntimePaths
    from mindroom.response_turn import PausedAttempt
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "agno_parity"
OWNED_DIRS = ("storage", "runs", "history", "events")

# 2027-01-15T08:00:00Z; far from any real capture date, so a timestamp that escaped the frozen clock stands out.
BASE_EPOCH = 1_800_000_000
TIMED_SPAN = 0.5
FIRST_TOKEN_AFTER = 0.25
CAPTURE_ROOT = "/mindroom-capture"

ROOM = "!room:example.org"
REQUESTER = "@alice:example.org"
_HANG = "hang"

_CONFIG: dict[str, Any] = {
    "timezone": "UTC",
    "defaults": {"tools": [], "learning": False},
    "memory": {"backend": "none"},
    "agents": {
        "code": {
            "display_name": "Code",
            "role": "Writes and checks code.",
            "model": "code",
            "tools": ["calculator", "attachments"],
            "delegate_to": ["helper"],
            "memory_backend": "file",
        },
        "helper": {
            "display_name": "Helper",
            "role": "Runs calculations for other agents.",
            "model": "helper",
            "tools": ["calculator"],
            "access": {"users": [REQUESTER]},
        },
        "writer": {"display_name": "Writer", "role": "Writes answers.", "model": "writer", "tools": ["calculator"]},
        "checker": {"display_name": "Checker", "role": "Checks answers.", "model": "checker", "tools": ["calculator"]},
    },
    "teams": {
        "squad": {
            "display_name": "Squad",
            "role": "Answers with a writer and a checker.",
            "agents": ["writer", "checker"],
            "mode": "coordinate",
            "model": "leader",
        },
        "panel": {
            "display_name": "Panel",
            "role": "Collects one fact from every member.",
            "agents": ["writer", "checker"],
            "mode": "collaborate",
            "model": "leader",
        },
    },
    "models": {
        "default": {"provider": "synthetic", "id": "synthetic"},
        "code": {"provider": "synthetic", "id": "code-model", "display_name": "Code Model", "context_window": 200000},
        "helper": {"provider": "synthetic", "id": "helper-model"},
        "leader": {"provider": "synthetic", "id": "leader-model", "context_window": 100000},
        "writer": {"provider": "synthetic", "id": "writer-model"},
        "checker": {"provider": "synthetic", "id": "checker-model"},
    },
    "tool_approval": {"default": "auto_approve", "rules": [{"match": "multiply", "action": "require_approval"}]},
}


@dataclass(frozen=True)
class StoreSpec:
    """One conversation database, its layout under the storage root, and the history scope its runs belong to."""

    label: str
    state_dir: str
    name: str
    scope: HistoryScope

    @property
    def session_table(self) -> str:
        """Return the store's Agno sessions table name."""
        return f"{self.name}_sessions"

    @property
    def db_path(self) -> str:
        """Return the database path relative to the storage root."""
        return f"{self.state_dir}/sessions/{self.name}.db"


def _session(thread: str) -> str:
    return create_session_id(ROOM, thread)


def _agent_store(label: str, state_dir: str, agent_name: str) -> StoreSpec:
    return StoreSpec(label, state_dir, agent_name, HistoryScope(kind="agent", scope_id=agent_name))


def _team_store(label: str, team_name: str) -> StoreSpec:
    scope = HistoryScope(kind="team", scope_id=team_name)
    name = _scope_session_storage_name(scope)
    return StoreSpec(label, f"teams/{name}", name, scope)


CONVERSATION = _session("$conversation")
DELEGATION = _session("$delegation")
NATIVE = _session("$native")
COMPACTED = _session("$compacted")
SQUAD = _session("$squad")
PANEL = _session("$panel")
APPROVED = _session("$approved")
REJECTED = _session("$rejected")
DELEGATION_APPROVED = _session("$delegation-approved")
SQUAD_APPROVED = _session("$squad-approved")

AGNO_CANCELLED = _session("$agno-cancelled")
AGNO_CANCELLED_BLOCKING = _session("$agno-cancelled-blocking")

STORES = (
    _agent_store("agent_code", "agents/code", "code"),
    _agent_store("agent_helper", "agents/helper", "helper"),
    _team_store("team_squad", "squad"),
    _team_store("team_panel", "panel"),
    _agent_store("agno2_migrated", "agno2/agents/code", "code"),
)


def _live_store(entity: str) -> StoreSpec:
    """Return the store an agent or team writes during the capture; agno2_migrated is a migrated copy."""
    return next(spec for spec in STORES if spec.scope.scope_id == entity and not spec.state_dir.startswith("agno2/"))


HISTORY_SELECTIONS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("all", {}),
    ("last_run", {"last_n_runs": 1}),
    ("last_two_runs", {"last_n_runs": 2}),
    ("no_runs", {"last_n_runs": 0}),
    ("limit_1", {"limit": 1}),
    ("limit_3", {"limit": 3}),
    ("limit_4", {"limit": 4}),
    ("last_three_runs_limit_5", {"last_n_runs": 3, "limit": 5}),
    ("skip_prompt_roles", {"skip_roles": ["system", "developer"]}),
    ("every_status_limit_6", {"skip_statuses": [], "limit": 6}),
    ("every_status", {"skip_statuses": []}),
)
TOOL_CALL_FILTERS: tuple[tuple[str, int], ...] = (("all", 0), ("all", 1), ("all", 2), ("every_status", 1))
# History replay selects a scope's runs with its agent_id or team_id, the replay policy's run and message limits,
# and the prompt roles stored history never holds (history/replay.py, _session_history_messages).
# Each selection names the store's own scope or another scope of the same kind, which owns no run in the store,
# so the goldens show what the filter excludes.
_REPLAY_SKIP_ROLES = sorted(prompt_roles_for_history_storage())
SCOPED_SELECTIONS: tuple[tuple[str, bool, dict[str, Any]], ...] = (
    ("scope", True, {"last_n_runs": None, "limit": None, "skip_roles": _REPLAY_SKIP_ROLES}),
    ("scope_last_two_runs", True, {"last_n_runs": 2, "limit": None, "skip_roles": _REPLAY_SKIP_ROLES}),
    ("scope_limit_4", True, {"last_n_runs": None, "limit": 4, "skip_roles": _REPLAY_SKIP_ROLES}),
    ("other_scope", False, {"last_n_runs": None, "limit": None, "skip_roles": _REPLAY_SKIP_ROLES}),
)
_OTHER_SCOPE_IDS = {"code": "helper", "helper": "code", "squad": "panel", "panel": "squad"}


# ---------------------------------------------------------------------------
# Frozen clock
# ---------------------------------------------------------------------------

_REAL_TIME = time.time
_REAL_DATETIME = datetime_module.datetime
_REAL_TIMER = agno.utils.timer.Timer


@dataclass
class _Clock:
    """Wall time the scenarios set explicitly."""

    now: float = BASE_EPOCH

    def time(self) -> float:
        return self.now


class _FixedTimer(agno.utils.timer.Timer):
    """Agno's timer, with every span lasting ``TIMED_SPAN`` and every first token arriving after ``FIRST_TOKEN_AFTER``.

    Agno runs parallel tool calls concurrently, so timers reading a clock would time them in whatever order they ran.
    """

    @property
    def elapsed(self) -> float:
        if self.elapsed_time is not None:
            return self.elapsed_time
        return FIRST_TOKEN_AFTER if self.start_time is not None else 0.0

    def start(self) -> float:
        self.start_time = 0.0
        return self.start_time

    def stop(self) -> float:
        self.end_time = TIMED_SPAN
        if self.start_time is not None:
            self.elapsed_time = TIMED_SPAN
        return self.end_time

    def __enter__(self) -> _FixedTimer:
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop()


class _FrozenDatetimeType(type):
    """Keep ``isinstance(value, datetime)`` true for real datetimes in modules that see the frozen class."""

    def __instancecheck__(cls, instance: object) -> bool:
        return isinstance(instance, _REAL_DATETIME)

    def __subclasscheck__(cls, subclass: type) -> bool:
        return issubclass(subclass, _REAL_DATETIME)


def _frozen_datetime(clock: _Clock) -> type[datetime_module.datetime]:
    class FrozenDatetime(_REAL_DATETIME, metaclass=_FrozenDatetimeType):
        @classmethod
        def now(cls, tz: datetime_module.tzinfo | None = None) -> datetime_module.datetime:
            moment = _REAL_DATETIME.fromtimestamp(clock.now, tz or datetime_module.UTC)
            return moment if tz is not None else moment.replace(tzinfo=None)

        @classmethod
        def utcnow(cls) -> datetime_module.datetime:
            return cls.now()

        @classmethod
        def today(cls) -> datetime_module.datetime:
            return cls.now()

    return FrozenDatetime


@contextlib.contextmanager
def _frozen_clock(clock: _Clock) -> Iterator[None]:
    """Freeze wall time, ``datetime.now``, and Agno's timers in every loaded ``agno`` and ``mindroom`` module.

    Local time is UTC for the duration, so naive timestamps do not depend on the machine.
    """
    frozen_datetime = _frozen_datetime(clock)
    replacements: list[tuple[object, str, object, object]] = [(time, "time", _REAL_TIME, clock.time)]
    for module in list(sys.modules.values()):
        if not isinstance(module, types.ModuleType) or not module.__name__.startswith(("agno", "mindroom")):
            continue
        for attribute, value in list(vars(module).items()):
            if value is _REAL_DATETIME:
                replacements.append((module, attribute, value, frozen_datetime))
            elif value is _REAL_TIME:
                replacements.append((module, attribute, value, clock.time))
            elif value is _REAL_TIMER:
                replacements.append((module, attribute, value, _FixedTimer))
    previous_tz = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    for target, attribute, _original, replacement in replacements:
        setattr(target, attribute, replacement)
    try:
        yield
    finally:
        for target, attribute, original, _replacement in reversed(replacements):
            setattr(target, attribute, original)
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        time.tzset()


# ---------------------------------------------------------------------------
# Scripted provider
# ---------------------------------------------------------------------------

# A step inside a scripted turn that requests Agno's run cancellation, as a Stop's graceful cancel does.
_REQUEST_CANCEL = "request-cancel"

type _Turn = tuple[ModelResponse | str, ...] | Exception | str


@dataclass
class _Script:
    """Provider turns one configured model answers, in order, shared by every instance MindRoom builds."""

    turns: deque[_Turn] = field(default_factory=deque)
    hanging: asyncio.Event = field(default_factory=asyncio.Event)

    def __deepcopy__(self, _memo: dict[int, object]) -> _Script:
        """Stay shared when Agno copies a model, so every copy answers from the same script."""
        return self

    def load(self, *turns: _Turn) -> None:
        assert not self.turns, f"unconsumed turns: {self.turns}"
        self.turns.extend(turns)

    async def next(self) -> tuple[ModelResponse | str, ...]:
        turn = self.turns.popleft()
        if turn == _HANG:
            self.hanging.set()
            await asyncio.Event().wait()
        if isinstance(turn, Exception):
            raise turn
        assert isinstance(turn, tuple)
        return turn


class _ScriptedModel(SyntheticModel):
    """The synthetic provider, answering from a script instead of generating text."""

    def __init__(self, model_id: str, script: _Script) -> None:
        super().__init__(id=model_id)
        self._script = script

    async def ainvoke(self, *_args: object, **kwargs: object) -> ModelResponse:
        """Return the next scripted turn, which must be a single response."""
        (response,) = [step async for step in self._steps(kwargs)]
        return response

    async def ainvoke_stream(self, *_args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        """Yield the next scripted turn's deltas."""
        async for delta in self._steps(kwargs):
            yield delta

    async def _steps(self, kwargs: Mapping[str, object]) -> AsyncIterator[ModelResponse]:
        """Yield the next scripted turn's responses, requesting Agno's run cancellation where the script says."""
        for step in await self._script.next():
            if step == _REQUEST_CANCEL:
                run_response = kwargs["run_response"]
                assert isinstance(run_response, RunOutput)
                assert run_response.run_id is not None
                assert await acancel_run(run_response.run_id)
                continue
            assert isinstance(step, ModelResponse)
            yield step


def _usage(input_tokens: int, output_tokens: int, **counters: int) -> MessageMetrics:
    return MessageMetrics(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        **counters,
    )


def _call(name: str, call_id: str, **arguments: object) -> dict[str, object]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _answer(content: str, usage: MessageMetrics) -> tuple[ModelResponse, ...]:
    return (ModelResponse(content=content, response_usage=usage),)


def _calls(usage: MessageMetrics, *calls: dict[str, object], content: str | None = None) -> tuple[ModelResponse, ...]:
    return (ModelResponse(content=content, tool_calls=list(calls), response_usage=usage),)


def _png_pixel() -> bytes:
    """Return a 1x1 green PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\xff\x00"))
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def _until_pause(run_id: str, response: Awaitable[object], *, pauses: bool) -> PausedAttempt | None:
    """Await a turn, letting it end waiting for approval only when the scenario expects it to; return its pause."""
    try:
        await response
    except ResponsePausedForApproval as suspension:
        assert pauses, f"{run_id} paused for approval unexpectedly"
        return suspension.paused
    assert not pauses, f"{run_id} did not pause for approval"
    return None


async def _drain(stream: AsyncIterator[object]) -> None:
    async for _chunk in stream:
        pass


async def _run_in_tool_context[T](
    *,
    tool_context: ToolRuntimeContext | None,
    operation: Callable[[], Awaitable[T]],
) -> T:
    """Run ``operation`` with ``tool_context`` bound, as the bot's tool runtime does."""
    with tool_runtime_context(tool_context):
        return await operation()


@dataclass
class _Capture:
    """One capture run: its runtime, scripts, clock, and the run events it collected."""

    root: Path
    paths: RuntimePaths
    config: Config
    clock: _Clock
    scripts: dict[str, _Script]
    ai_run: dict[str, dict[str, Any]] = field(default_factory=dict)
    openai_compat: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Run events of turns Agno's run cancellation ended, captured after the first goldens.
    ai_run_cancelled: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Fixture path to the raw dump of each approval resume's sessions, taken just before the resume.
    pre_resume: dict[str, str] = field(default_factory=dict)
    minute: int = 0

    def tick(self) -> None:
        """Move the wall clock to the next minute, so each turn has its own timestamps."""
        self.minute += 1
        self.clock.now = BASE_EPOCH + 60 * self.minute

    def identity(self, agent_name: str, session_id: str) -> ToolExecutionIdentity:
        thread = session_id.removeprefix(f"{ROOM}:")
        return ToolExecutionIdentity(
            channel="matrix",
            agent_name=agent_name,
            requester_id=REQUESTER,
            room_id=ROOM,
            thread_id=thread,
            resolved_thread_id=thread,
            session_id=session_id,
        )

    def runtime_context(self, agent_name: str, session_id: str) -> ToolRuntimeContext:
        thread = session_id.removeprefix(f"{ROOM}:")
        return make_test_tool_runtime_context(
            agent_name=agent_name,
            target=MessageTarget(
                room_id=ROOM,
                source_thread_id=thread,
                resolved_thread_id=thread,
                reply_to_event_id=None,
                session_id=session_id,
            ),
            requester_id=REQUESTER,
            client=MagicMock(),
            config=self.config,
            runtime_paths=self.paths,
            relations=make_relation_lookup(),
            conversation_reader=make_conversation_reader_mock(),
        )

    @contextlib.contextmanager
    def tool_context(self, agent_name: str, session_id: str) -> Iterator[None]:
        with tool_runtime_context(self.runtime_context(agent_name, session_id)):
            yield

    def turn_context(self, entity: str, session_id: str, run_id: str, event_id: str) -> ResponseTurnContext:
        return make_turn_context(
            entity,
            session_id=session_id,
            run_id=run_id,
            correlation_id=f"corr-{run_id}",
            room_id=ROOM,
            thread_id=session_id.removeprefix(f"{ROOM}:"),
            reply_to_event_id=event_id,
            requester_id=REQUESTER,
        )

    async def agent_turn(
        self,
        session_id: str,
        run_id: str,
        prompt: str,
        *,
        stream: bool = False,
        stop: bool = False,
        pauses: bool = False,
        cancels: bool = False,
        media: MediaInputs | None = None,
    ) -> PausedAttempt | None:
        """Run one turn of the ``code`` agent through ``ai_response`` or ``stream_agent_response``.

        With ``stop``, the turn streams and is cancelled while its provider request hangs, as a Stop does.
        ``pauses`` says whether the turn must end waiting for an approval, whose pause is returned.
        ``cancels`` says the script requests Agno's run cancellation, so the turn must end cancelled.
        """
        self.tick()
        collector: dict[str, Any] = {}
        options: dict[str, Any] = {
            "prompt": prompt,
            "runtime_paths": self.paths,
            "config": self.config,
            "media": media,
            "run_metadata_collector": collector,
            "execution_identity": self.identity("code", session_id),
            "supports_native_tool_approval": True,
        }
        ctx = self.turn_context("code", session_id, run_id, f"${run_id}")
        response = _drain(stream_agent_response(ctx, **options)) if stream or stop else ai_response(ctx, **options)
        paused = None
        with self.tool_context("code", session_id):
            if stop:
                script = self.scripts["code"]
                script.hanging.clear()
                task = asyncio.create_task(_until_pause(run_id, response, pauses=pauses))
                await script.hanging.wait()
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            elif cancels:
                with contextlib.suppress(asyncio.CancelledError):
                    await response
                    msg = f"{run_id} was not cancelled"
                    raise AssertionError(msg)
            else:
                paused = await _until_pause(run_id, response, pauses=pauses)
        if collector:
            (self.ai_run_cancelled if cancels else self.ai_run)[run_id] = collector
        return paused

    async def team_turn(
        self,
        team_name: str,
        session_id: str,
        run_id: str,
        prompt: str,
        *,
        stream: bool = False,
        pauses: bool = False,
    ) -> PausedAttempt | None:
        """Run one turn of a configured team through ``team_response`` or ``team_response_stream``."""
        self.tick()
        collector: dict[str, Any] = {}
        team_config = self.config.teams[team_name]
        mode = TeamMode(team_config.mode)
        orchestrator = MagicMock()
        orchestrator.config = self.config
        orchestrator.runtime_paths = self.paths
        orchestrator.knowledge_refresh_scheduler = None
        options: dict[str, Any] = {
            "message": prompt,
            "orchestrator": orchestrator,
            "execution_identity": self.identity(team_name, session_id),
            "ctx": self.turn_context(team_name, session_id, run_id, f"${run_id}"),
            "user_id": REQUESTER,
            "configured_team_name": team_name,
            "run_metadata_collector": collector,
            "turn_recorder": TurnRecorder(user_message=prompt),
        }
        if stream:
            ids = entity_ids(self.config, self.paths)
            agent_ids = [ids[name] for name in team_config.agents]
            response = _drain(team_response_stream(agent_ids=agent_ids, mode=mode, **options))
        else:
            response = team_response(agent_names=list(team_config.agents), mode=mode, **options)
        with self.tool_context(team_name, session_id):
            paused = await _until_pause(run_id, response, pauses=pauses)
        if collector:
            self.ai_run[run_id] = collector
        return paused

    async def resume(self, entity: str, paused: PausedAttempt | None, prompt: str, *, approve: bool) -> None:
        """Decide every call ``paused`` waits on and continue its run, as an approval resume does.

        The calls are planned as a pause plans them, and the run continues through the collaborators
        ``ResponseRunner._continue_entity_call`` calls, with the continuation, decisions, and receipt it passes them.
        """
        assert paused is not None
        self.dump_before_resume(entity, paused)
        self.tick()
        thread = paused.session_id.removeprefix(f"{ROOM}:")
        team_config = self.config.teams.get(entity)
        identity = self.identity(entity, paused.session_id)
        plan = await plan_approval_calls(
            identify_approval_tools(paused, default_agent_name=entity),
            config=self.config,
            runtime_paths=self.paths,
            requester_id=REQUESTER,
            toolkit_owners=paused.toolkit_owners,
        )
        assert all(call.decision is None for call in plan.calls), plan.calls
        decision = ApprovalDecision.APPROVED if approve else ApprovalDecision.DENIED
        reason = None if approve else "Alice declined the multiplication."
        continuation = ApprovalContinuation(
            approval_id=f"approval-{paused.run_id}",
            run_id=paused.run_id,
            session_id=paused.session_id,
            entity_kind="agent" if team_config is None else "team",
            entity_name=entity,
            room_id=ROOM,
            thread_id=thread,
            requester_id=REQUESTER,
            response_event_id=f"$reply-{paused.run_id}",
            sources=ResponseSources((f"${paused.run_id}",), (f"${paused.run_id}",)),
            calls=tuple(replace(call, decision=decision, reason=reason) for call in plan.calls),
            state="claimed",
            delegation_storage_bindings=paused.delegation_storage_bindings,
            execution_identity=serialize_tool_execution_identity(identity),
            runtime_model_name=paused.runtime_model_name,
            continuation_count=paused.continuation_count,
            team_member_names=() if team_config is None else tuple(team_config.agents),
            team_member_model_names=paused.team_member_model_names,
            team_mode=None if team_config is None else team_config.mode,
            request_body=prompt,
            correlation_id=f"corr-{paused.run_id}",
            history_scope=HistoryScope(kind="agent" if team_config is None else "team", scope_id=entity),
            origin=message_origin(sender_id=REQUESTER),
        )
        decisions = {call.tool_call_id: call.decision is ApprovalDecision.APPROVED for call in continuation.calls}
        denial_reasons = {call.tool_call_id: call.reason for call in continuation.calls}
        paused_answer = PausedAnswer(
            text=paused.response_text,
            tool_trace=paused.tool_trace,
            team_state=paused.response_presentation_state or None,
        )
        runtime_context = self.runtime_context(entity, paused.session_id)
        dispatch = LiveToolDispatchContext(execution_identity=identity, runtime_context=runtime_context)
        with approval_receipt_context(build_approval_receipt(continuation.calls)):
            if team_config is None:
                result = await self.approval_execution().continue_run(
                    continuation,
                    paused_answer=paused_answer,
                    execution_identity=identity,
                    tool_dispatch=dispatch,
                    decisions=decisions,
                    denial_reasons=denial_reasons,
                    tool_trace_collector=[],
                    typing_log_context={},
                    progress=None,
                )
            else:

                async def continue_team() -> CompletedApprovalRun | PausedAttempt:
                    return await continue_paused_team_run(
                        member_names=continuation.team_member_names,
                        mode=TeamMode(continuation.team_mode or "coordinate"),
                        config=self.config,
                        runtime_paths=self.paths,
                        execution_identity=identity,
                        session_id=continuation.session_id,
                        run_id=continuation.run_id,
                        user_id=continuation.requester_id,
                        configured_team_name=entity,
                        model_name=continuation.runtime_model_name
                        or select_model_for_team(entity, ROOM, self.config, self.paths, thread_id=thread),
                        decisions=decisions,
                        denial_reasons=denial_reasons,
                        refresh_scheduler=None,
                        member_model_names=dict(continuation.team_member_model_names) or None,
                        approval_calls=continuation.calls,
                        history_scope=continuation.history_scope,
                        prior_tool_trace=paused_answer.tool_trace,
                        prior_presentation_state=paused_answer.team_state,
                        show_tool_calls=continuation.show_tool_calls,
                        tool_trace_collector=[],
                        progress=None,
                    )

                result = await _run_in_tool_context(
                    tool_context=runtime_context,
                    operation=lambda: run_with_tool_execution_identity(identity, operation=continue_team),
                )
        assert isinstance(result, CompletedApprovalRun), f"{paused.run_id} did not complete after its approval"
        self.ai_run[paused.run_id] = result.metadata_content

    def dump_before_resume(self, entity: str, paused: PausedAttempt) -> None:
        """Dump the sessions an approval resume reads: the paused run's session and its delegated children's."""
        spec = _live_store(entity)
        db_file = self.paths.storage_root / spec.db_path
        self.pre_resume[_pre_resume_path(spec, paused.run_id)] = _sessions_dump(db_file, {paused.session_id})
        children: dict[str, set[str]] = {}
        for child in _delegation_children(db_file, spec, paused.run_id):
            children.setdefault(child.child_agent_name, set()).add(child.session_id)
        for child_name, session_ids in sorted(children.items()):
            child_spec = _live_store(child_name)
            child_db = self.paths.storage_root / child_spec.db_path
            self.pre_resume[_pre_resume_path(child_spec, paused.run_id)] = _sessions_dump(child_db, session_ids)

    def approval_execution(self) -> AgentApprovalExecution:
        """Return the agent approval executor a response runner builds, around this capture's runtime."""
        return AgentApprovalExecution(
            config=lambda: self.config,
            runtime_paths=self.paths,
            client=AsyncMock,
            tool_runtime=MagicMock(run_in_context=_run_in_tool_context),
            knowledge_access=KnowledgeAccessSupport(
                runtime=MagicMock(config=self.config, orchestrator=None),
                runtime_paths=self.paths,
            ),
            refresh_scheduler=lambda: None,
        )


async def _conversation(capture: _Capture, attachment: Path) -> None:
    """Write completed, media, viewed-image, interrupted, and paused-approval turns into one agent session."""
    code = capture.scripts["code"]
    code.load(
        _answer("Hello, Alice.", _usage(120, 8, cache_read_tokens=100, cache_write_tokens=20, reasoning_tokens=3)),
    )
    await capture.agent_turn(CONVERSATION, "run-text", "Say hello.")

    code.load(
        (
            ModelResponse(content="Checking. "),
            ModelResponse(tool_calls=[_call("add", "call-add", a=2, b=3)], response_usage=_usage(150, 12)),
        ),
        (
            ModelResponse(
                tool_calls=[_call("subtract", "call-subtract", a=9, b=4), _call("add", "call-add-2", a=1, b=1)],
                response_usage=_usage(170, 14),
            ),
        ),
        (
            ModelResponse(reasoning_content="Every result is known."),
            ModelResponse(content="2 + 3 = 5, "),
            ModelResponse(
                content="9 - 4 = 5, and 1 + 1 = 2.",
                provider_data={"signature": "sig-tools"},
                redacted_reasoning_content="redacted-thinking",
                response_usage=_usage(190, 20, reasoning_tokens=6, cache_read_tokens=150),
            ),
        ),
    )
    await capture.agent_turn(CONVERSATION, "run-tools", "Compute 2 + 3, 9 - 4, and 1 + 1.", stream=True)

    attachment.write_text("a,b\n1,2\n", encoding="utf-8")
    code.load(_answer("I received an image, audio, a video, and three files.", _usage(300, 15)))
    media = MediaInputs(
        images=[Image(content=_png_pixel(), mime_type="image/png", format="png")],
        audio=[Audio(content=b"RIFF\x24\x00\x00\x00WAVEfmt ", mime_type="audio/wav", format="wav")],
        videos=[Video(content=b"\x00\x00\x00\x18ftypmp42", mime_type="video/mp4", format="mp4")],
        files=[
            File(content=b"%PDF-1.4\n%\xe2\xe3\xcf\xd3", mime_type="application/pdf", filename="report.pdf"),
            File(content=b"Plain text notes.", mime_type="text/plain", filename="notes.txt"),
            File(filepath=str(attachment), mime_type="text/csv", filename="data.csv"),
        ],
    )
    await capture.agent_turn(CONVERSATION, "run-media", "Describe these attachments.", media=media)

    workspace = capture.paths.storage_root / "agents" / "code" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "pixel.png").write_bytes(_png_pixel())
    code.load(
        _calls(_usage(210, 9), _call("view_file", "call-view", path="pixel.png")),
        _answer("The image is one green pixel.", _usage(260, 10)),
    )
    await capture.agent_turn(CONVERSATION, "run-view", "View pixel.png.")

    code.load(
        ModelProviderError("Upstream overloaded", status_code=529, model_name="code-model", model_id="code-model"),
    )
    await capture.agent_turn(CONVERSATION, "run-error", "This request fails.")

    code.load(_calls(_usage(230, 6), _call("add", "call-add-3", a=4, b=4)), _HANG)
    await capture.agent_turn(CONVERSATION, "run-cancelled", "Add 4 and 4, then stop.", stop=True)

    code.load(_calls(_usage(240, 7), _call("multiply", "call-multiply", a=6, b=7), content="I need approval first."))
    await capture.agent_turn(CONVERSATION, "run-paused-approval", "Multiply 6 by 7.", pauses=True)


async def _delegation(capture: _Capture) -> None:
    """Pause the ``code`` agent on a delegation whose child waits for approval."""
    capture.scripts["code"].load(
        _calls(_usage(310, 11), _call("run_subagent", "call-delegate", agent_name="helper", task="Multiply 6 by 7")),
    )
    capture.scripts["helper"].load(_calls(_usage(90, 5), _call("multiply", "call-helper-multiply", a=6, b=7)))
    await capture.agent_turn(DELEGATION, "run-paused-delegation", "Ask the helper to multiply 6 by 7.", pauses=True)


async def _native_statuses(capture: _Capture) -> None:
    """Store the error and cancelled runs Agno itself saves, before any MindRoom turn record replaces them."""
    code = capture.scripts["code"]
    identity = capture.identity("code", NATIVE)
    storage = create_session_storage("code", capture.config, capture.paths, identity)
    with capture.tool_context("code", NATIVE):
        agent = create_agent(
            "code",
            capture.config,
            capture.paths,
            identity,
            history_storage=storage,
            supports_native_tool_approval=True,
        )

        def run_options(run_id: str) -> dict[str, Any]:
            return {
                "session_id": NATIVE,
                "user_id": REQUESTER,
                "run_id": run_id,
                "metadata": build_matrix_run_metadata(
                    f"${run_id}",
                    [],
                    room_id=ROOM,
                    thread_id="$native",
                    requester_id=REQUESTER,
                    correlation_id=f"corr-{run_id}",
                ),
            }

        capture.tick()
        code.load(ModelProviderError("Rate limited", status_code=429, model_name="code-model", model_id="code-model"))
        await agent.arun([Message(role="user", content="This request fails.")], **run_options("run-native-error"))

        capture.tick()
        code.hanging.clear()
        code.load(_calls(_usage(140, 4), _call("add", "call-native-add", a=5, b=5)), _HANG)

        async def stream() -> None:
            async with drain_agent_cancellation(agent, "run-native-cancelled") as bind_owner:
                with bind_owner():
                    async for _event in agent.arun(
                        [Message(role="user", content="Add 5 and 5, then stop.")],
                        stream=True,
                        stream_events=True,
                        yield_run_output=True,
                        **run_options("run-native-cancelled"),
                    ):
                        pass

        task = asyncio.create_task(stream())
        await code.hanging.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    storage.close()


async def _compacted(capture: _Capture) -> None:
    """Write a compaction chunk, an edit cut, and a mid-turn fold into one agent session."""
    code = capture.scripts["code"]
    for index in range(1, 5):
        code.load(_answer(f"Answer {index}.", _usage(100 + index, index)))
        await capture.agent_turn(COMPACTED, f"run-c{index}", f"Question {index}.")
    scope = HistoryScope(kind="agent", scope_id="code")
    identity = capture.identity("code", COMPACTED)

    capture.tick()
    storage = create_session_storage("code", capture.config, capture.paths, identity)
    session = _load_session(storage, COMPACTED, "agent")
    basis = read_chunk_basis(storage, session, scope, ["run-c1", "run-c2"], previous_summary=None)
    assert basis is not None
    assert append_compaction_chunk(
        storage=storage,
        session=session,
        scope=scope,
        basis=basis,
        included_run_ids=["run-c1", "run-c2"],
        summary=SessionSummary(summary="Alice asked questions 1 and 2; Code answered both."),
        summary_model="code",
    )
    save_compaction_usage(
        storage,
        session_id=COMPACTED,
        requester_id=REQUESTER,
        model=_ScriptedModel("code-model", capture.scripts["code"]),
        usage=_usage(520, 40),
        initial_session=None,
    )
    assert remove_runs_answering(storage, COMPACTED, scope, "$run-c4")
    storage.close()

    code.load(
        _calls(_usage(180, 8), _call("add", "call-fold-add", a=2, b=3)),
        _answer("2 + 3 = 5.", _usage(200, 6)),
    )
    await capture.agent_turn(COMPACTED, "run-c5", "Add 2 and 3.")

    capture.tick()
    storage = create_session_storage("code", capture.config, capture.paths, identity)
    session = _load_session(storage, COMPACTED, "agent")
    folded = next(run for run in session.runs or [] if run.run_id == "run-c5")
    fold_basis = read_fold_basis(storage, session, scope)
    assert fold_basis is not None
    assert append_fold(
        storage,
        session=session,
        scope=scope,
        basis=fold_basis,
        summary="Alice asked for 2 + 3, and the add tool returned 5.",
        summary_model="code",
        segment=summary_rows.Segment(
            run_id="run-c5",
            metadata=folded.metadata or {},
            messages=[message.to_dict() for message in (folded.messages or [])[1:3]],
            tools=[tool.to_dict() for tool in folded.tools or []],
            member_responses=[],
        ),
    )
    storage.close()


async def _teams(capture: _Capture) -> None:
    """Write coordinate turns (completed, member failure, member approval) and a streamed collaborate turn."""
    leader, writer, checker = (capture.scripts[name] for name in ("leader", "writer", "checker"))
    leader.load(
        _calls(
            _usage(400, 12),
            _call("delegate_task_to_member", "call-team-writer", member_id="writer", task="Add 2 and 3"),
        ),
        _answer("The writer found 5.", _usage(450, 9)),
    )
    writer.load(_calls(_usage(80, 4), _call("add", "call-writer-add", a=2, b=3)), _answer("5", _usage(95, 1)))
    await capture.team_turn("squad", SQUAD, "team-completed", "What is 2 + 3?")

    leader.load(
        _calls(
            _usage(420, 12),
            _call("delegate_task_to_member", "call-team-checker", member_id="checker", task="Check that 2 + 3 = 5"),
        ),
        _answer("The checker could not answer.", _usage(470, 8)),
    )
    checker.load(
        ModelProviderError("Checker overloaded", status_code=503, model_name="checker-model", model_id="checker-model"),
    )
    await capture.team_turn("squad", SQUAD, "team-member-error", "Check the answer.")

    leader.load(
        _calls(
            _usage(430, 12),
            _call("delegate_task_to_member", "call-team-multiply", member_id="checker", task="Multiply 6 by 7"),
        ),
    )
    checker.load(_calls(_usage(85, 5), _call("multiply", "call-checker-multiply", a=6, b=7)))
    await capture.team_turn("squad", SQUAD, "team-paused", "Multiply 6 by 7.", pauses=True)

    leader.load(
        (
            ModelResponse(
                tool_calls=[_call("delegate_task_to_members", "call-panel", task="Give one fact about the number 5.")],
                response_usage=_usage(380, 10),
            ),
        ),
        (ModelResponse(content="Five is "), ModelResponse(content="prime and odd.", response_usage=_usage(410, 7))),
    )
    writer.load((ModelResponse(content="Five is "), ModelResponse(content="prime.", response_usage=_usage(60, 3))))
    checker.load((ModelResponse(content="Five is "), ModelResponse(content="odd.", response_usage=_usage(61, 3))))
    await capture.team_turn("panel", PANEL, "panel-completed", "Tell me about 5.", stream=True)


async def _resumed_approvals(capture: _Capture) -> None:
    """Approve or deny paused calls and continue each run, in sessions of their own beside the paused ones.

    An agent's approved and denied calls, a delegation whose child waits for approval, and a team member's call.
    """
    code, helper, leader, checker = (capture.scripts[name] for name in ("code", "helper", "leader", "checker"))
    for session_id, run_id, approve, answer in (
        (APPROVED, "run-approved", True, "6 x 7 = 42."),
        (REJECTED, "run-rejected", False, "I did not multiply, because the call was declined."),
    ):
        prompt = "Multiply 6 by 7."
        code.load(_calls(_usage(240, 7), _call("multiply", f"call-{run_id}", a=6, b=7), content="Checking first."))
        paused = await capture.agent_turn(session_id, run_id, prompt, pauses=True)
        code.load(_answer(answer, _usage(260, 9)))
        await capture.resume("code", paused, prompt, approve=approve)

    prompt = "Ask the helper to multiply 6 by 7."
    code.load(
        _calls(
            _usage(310, 11),
            _call("run_subagent", "call-delegate-approved", agent_name="helper", task="Multiply 6 by 7"),
        ),
    )
    helper.load(_calls(_usage(90, 5), _call("multiply", "call-helper-multiply-approved", a=6, b=7)))
    paused = await capture.agent_turn(DELEGATION_APPROVED, "run-delegation-approved", prompt, pauses=True)
    helper.load(_answer("42", _usage(100, 1)))
    code.load(_answer("The helper found 42.", _usage(330, 6)))
    await capture.resume("code", paused, prompt, approve=True)

    prompt = "Multiply 6 by 7."
    leader.load(
        _calls(
            _usage(430, 12),
            _call("delegate_task_to_member", "call-team-multiply-approved", member_id="checker", task=prompt),
        ),
    )
    checker.load(_calls(_usage(85, 5), _call("multiply", "call-checker-multiply-approved", a=6, b=7)))
    paused = await capture.team_turn("squad", SQUAD_APPROVED, "team-approved", prompt, pauses=True)
    checker.load(_answer("42", _usage(95, 1)))
    leader.load(_answer("The checker found 42.", _usage(470, 7)))
    await capture.resume("squad", paused, prompt, approve=True)


def _openai_compat(capture: _Capture) -> None:
    """Record the OpenAI-compatible API's blocking and streamed bodies for an agent and a team."""
    root = capture.root / "openai"
    paths = resolve_runtime_paths(
        config_path=root / "config.yaml",
        storage_path=root / "storage",
        process_env={"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"},
    )
    config = bind_runtime_paths(Config.model_validate(_CONFIG), paths)
    app = FastAPI()
    app.include_router(openai_compat.router)
    initialize_api_app(app, paths)
    requests: tuple[tuple[str, dict[str, Any], dict[str, tuple[_Turn, ...]]], ...] = (
        (
            "agent_blocking",
            {"model": "writer", "messages": [{"role": "user", "content": "Say hello."}]},
            {"writer": (_answer("Hello from the writer.", _usage(70, 5)),)},
        ),
        (
            "agent_stream_with_tool",
            {"model": "writer", "stream": True, "messages": [{"role": "user", "content": "Add 2 and 3."}]},
            {
                "writer": (
                    _calls(_usage(72, 3), _call("add", "call-api-add", a=2, b=3)),
                    (ModelResponse(content="The sum "), ModelResponse(content="is 5.", response_usage=_usage(78, 4))),
                ),
            },
        ),
        (
            "team_blocking",
            {"model": "team/squad", "messages": [{"role": "user", "content": "What is 2 + 3?"}]},
            {
                "leader": (
                    _calls(
                        _usage(400, 12),
                        _call("delegate_task_to_member", "call-api-team", member_id="writer", task="Add 2 and 3"),
                    ),
                    _answer("The writer found 5.", _usage(450, 9)),
                ),
                "writer": (
                    _calls(_usage(80, 4), _call("add", "call-api-writer-add", a=2, b=3)),
                    _answer("5", _usage(95, 1)),
                ),
            },
        ),
        (
            "team_stream",
            {"model": "team/squad", "stream": True, "messages": [{"role": "user", "content": "Check 5."}]},
            {
                "leader": (
                    _calls(
                        _usage(401, 12),
                        _call("delegate_task_to_member", "call-api-check", member_id="checker", task="Check 5"),
                    ),
                    (
                        ModelResponse(content="Checked: "),
                        ModelResponse(content="5 is right.", response_usage=_usage(451, 9)),
                    ),
                ),
                "checker": (
                    (ModelResponse(content="Looks "), ModelResponse(content="right.", response_usage=_usage(66, 2))),
                ),
            },
        ),
    )
    with (
        patch("mindroom.api.openai_compat._load_config", return_value=(config, paths)),
        TestClient(app, base_url="http://localhost") as client,
    ):
        for name, body, turns in requests:
            capture.tick()
            for model_name, model_turns in turns.items():
                capture.scripts[model_name].load(*model_turns)
            headers = {"X-Session-Id": f"api-{name}"}
            response = client.post("/v1/chat/completions", headers=headers, json=body)
            text = response.text
            capture.openai_compat[name] = {
                "request": {"headers": headers, "json": body},
                "status_code": response.status_code,
                "body": text.split("\n") if body.get("stream") else json.loads(text),
            }


async def _response_linkage(capture: _Capture) -> dict[str, Any]:
    """Re-save delivered runs with their Matrix reply's event id, as the state writer does after delivery.

    Runs after the stores are dumped, so the store goldens keep the rows as the turns saved them.
    """
    cases: dict[str, Any] = {}
    for entity, session_id, run_id in (("code", CONVERSATION, "run-media"), ("squad", SQUAD, "team-completed")):
        capture.tick()
        spec = _live_store(entity)
        db_file = capture.paths.storage_root / spec.db_path
        before = run_row(spec, db_file, run_id)
        writer = ConversationStateWriter(
            ConversationStateWriterDeps(
                runtime=MagicMock(config=capture.config),
                logger=get_logger(__name__),
                runtime_paths=capture.paths,
                agent_name=entity,
            ),
        )
        scope = writer.history_scope()
        response_event_id = f"$reply-{run_id}"
        await writer.apersist_response_event_id_in_session_run(
            create_storage=partial(writer.create_storage, capture.identity(entity, session_id), scope=scope),
            session_id=session_id,
            session_type=writer.session_type_for_scope(scope),
            run_id=run_id,
            response_event_id=response_event_id,
        )
        cases[f"{spec.label}:{run_id}"] = {
            "session_id": session_id,
            "response_event_id": response_event_id,
            "before": before,
            "after": run_row(spec, db_file, run_id),
        }
    return cases


async def _agno_cancelled_turns(capture: _Capture) -> None:
    """End a streamed and a blocking turn through Agno's run cancellation after a completed tool call.

    MindRoom's Stop cancels the reply task and then requests this cancellation; here the request comes while the
    turn still runs, so the turn publishes the run event it builds for a cancelled run.
    """
    code = capture.scripts["code"]
    for session_id, run_id, stream in (
        (AGNO_CANCELLED, "run-agno-cancelled", True),
        (AGNO_CANCELLED_BLOCKING, "run-agno-cancelled-blocking", False),
    ):
        answer: tuple[ModelResponse | str, ...] = (
            (
                ModelResponse(content="2 + 3 "),
                _REQUEST_CANCEL,
                ModelResponse(content="= 5.", response_usage=_usage(180, 4)),
            )
            if stream
            else (_REQUEST_CANCEL, ModelResponse(content="2 + 3 = 5.", response_usage=_usage(180, 6)))
        )
        code.load(_calls(_usage(160, 7), _call("add", f"call-{run_id}", a=2, b=3)), answer)
        await capture.agent_turn(session_id, run_id, "Add 2 and 3.", stream=stream, cancels=True)
    assert not code.turns, f"code left unconsumed turns: {code.turns}"


async def _run_scenarios(capture: _Capture) -> None:
    attachment = capture.root / "attachments" / "data.csv"
    attachment.parent.mkdir(parents=True, exist_ok=True)
    await _conversation(capture, attachment)
    await _delegation(capture)
    await _native_statuses(capture)
    await _compacted(capture)
    await _teams(capture)
    await _resumed_approvals(capture)
    await asyncio.to_thread(_openai_compat, capture)
    for name, script in capture.scripts.items():
        assert not script.turns, f"{name} left unconsumed turns: {script.turns}"
    capture.tick()
    agno2 = create_agno_2_sessions_db(capture.paths.storage_root / "agno2" / "agents" / "code" / "sessions" / "code.db")
    create_state_storage("code", agno2.parent.parent, subdir="sessions", session_table="code_sessions").close()


# ---------------------------------------------------------------------------
# Normalization and dumps
# ---------------------------------------------------------------------------


class _Normalizer:
    """Replace random ids with sequential ones in first-appearance order, and capture roots with one path."""

    _PATTERNS = (
        (
            re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9A-Za-z])"),
            "00000000-0000-4000-8000-{:012x}",
        ),
        (re.compile(r"(?<=chatcmpl-)[0-9a-f]{12}(?![0-9A-Za-z])"), "{:012x}"),
        (re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{32}(?![0-9A-Za-z])"), "{:032x}"),
    )

    def __init__(self, roots: tuple[str, ...]) -> None:
        self._roots = roots
        self._ids: dict[str, str] = {}
        self._counts: dict[str, int] = {}

    def __call__(self, text: str) -> str:
        for root in self._roots:
            text = text.replace(root, CAPTURE_ROOT)
        for pattern, template in self._PATTERNS:
            text = pattern.sub(lambda match, template=template: self._replacement(match.group(), template), text)
        return text

    def _replacement(self, value: str, template: str) -> str:
        if value not in self._ids:
            self._counts[template] = self._counts.get(template, 0) + 1
            self._ids[value] = template.format(self._counts[template])
        return self._ids[value]


_DUMP_HEADER = (
    "-- Conversation database written by Agno 3.0.9 through MindRoom's storage path, dumped with iterdump and\n"
    "-- normalized (see tests/fixtures/agno_parity/README.md). Regenerate with\n"
    "-- `python -m tests.agno_parity.capture_storage`; never edit by hand.\n"
)


def _dump(db_file: Path) -> str:
    """Dump ``db_file`` in a stable form.

    Agno creates its indexes in a different order on every run, so they are sorted, and SQLAlchemy's table DDL ends
    lines with a space, which the repository's whitespace hook would strip, so table definitions lose it.
    """
    connection = sqlite3.connect(db_file)
    try:
        statements = [
            "\n".join(line.rstrip() for line in statement.split("\n"))
            if statement.startswith("CREATE TABLE")
            else statement
            for statement in connection.iterdump()
        ]
    finally:
        connection.close()
    indexes = sorted(statement for statement in statements if statement.startswith("CREATE INDEX"))
    rest = [statement for statement in statements if not statement.startswith("CREATE INDEX")]
    assert rest[-1] == "COMMIT;"
    return _DUMP_HEADER + "\n".join([*rest[:-1], *indexes, rest[-1]]) + "\n"


_SESSIONS_DUMP_HEADER = (
    "-- The sessions one approval resume reads, from a conversation database written by Agno 3.0.9 through\n"
    "-- MindRoom's storage path just before the resume, dumped and normalized like the full stores (see\n"
    "-- tests/fixtures/agno_parity/README.md). Regenerate with `python -m tests.agno_parity.capture_storage`.\n"
)


def _pre_resume_path(spec: StoreSpec, run_id: str) -> str:
    return f"storage/{spec.label}__before_{run_id}.sql"


def _sessions_dump(db_file: Path, session_ids: set[str]) -> str:
    """Dump ``db_file`` with only the rows of ``session_ids`` in every table keyed by session."""
    with tempfile.TemporaryDirectory(prefix="agno-parity-sessions-") as scratch:
        copy = Path(scratch) / db_file.name
        source, target = sqlite3.connect(db_file), sqlite3.connect(copy)
        try:
            source.backup(target)
            tables = [name for (name,) in target.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
            for table in tables:
                columns = {column for (_cid, column, *_rest) in target.execute(f'PRAGMA table_info("{table}")')}
                if "session_id" in columns:
                    target.execute(
                        f'DELETE FROM "{table}" WHERE session_id NOT IN (SELECT value FROM json_each(?))',  # noqa: S608
                        (json.dumps(sorted(session_ids)),),
                    )
            target.commit()
        finally:
            source.close()
            target.close()
        return _SESSIONS_DUMP_HEADER + _dump(copy).removeprefix(_DUMP_HEADER)


def _delegation_children(db_file: Path, spec: StoreSpec, run_id: str) -> list[DelegationChild]:
    """Return the delegated children recorded on one stored run."""
    connection = sqlite3.connect(db_file)
    try:
        (run_data,) = connection.execute(
            f'SELECT run_data FROM "{spec.session_table}_runs" WHERE run_id = ?',  # noqa: S608
            (run_id,),
        ).fetchone()
    finally:
        connection.close()
    return DelegationState.from_metadata(json.loads(run_data).get("metadata")).children


def run_row(spec: StoreSpec, db_file: Path, run_id: str) -> dict[str, Any]:
    """Return one runs-table row by column, with its run data decoded."""
    connection = sqlite3.connect(db_file)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            f'SELECT * FROM "{spec.session_table}_runs" WHERE run_id = ?',  # noqa: S608
            (run_id,),
        ).fetchone()
    finally:
        connection.close()
    return {**dict(row), "run_data": json.loads(row["run_data"])}


def _dump_stores(storage_root: Path) -> dict[str, str]:
    written = {path.relative_to(storage_root).as_posix() for path in storage_root.rglob("*.db")}
    dumps = {spec.label: _dump(storage_root / spec.db_path) for spec in STORES}
    expected = {spec.db_path for spec in STORES}
    # Team members never save their own sessions, so their databases stay empty and are not fixtures.
    unexpected = {path for path in written - expected if _has_rows(storage_root / path)}
    assert not unexpected, f"capture wrote conversation databases that are not fixtures: {sorted(unexpected)}"
    return dumps


def _has_rows(db_file: Path) -> bool:
    connection = sqlite3.connect(db_file)
    try:
        tables = [name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return any(connection.execute(f'SELECT 1 FROM "{name}" LIMIT 1').fetchone() for name in tables)  # noqa: S608
    finally:
        connection.close()


def restore_store(spec: StoreSpec, dump: str, storage_root: Path) -> Path:
    """Write ``dump`` as ``spec``'s database under ``storage_root`` and return the database file."""
    db_file = storage_root / spec.db_path
    db_file.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_file)
    try:
        connection.executescript(dump)
    finally:
        connection.close()
    return db_file


def _open_store(spec: StoreSpec, storage_root: Path) -> BaseDb:
    """Open a restored store through MindRoom's storage factory, as a running agent or team would."""
    return create_state_storage(
        spec.name,
        storage_root / spec.state_dir,
        subdir="sessions",
        session_table=spec.session_table,
    )


def _load_session(storage: BaseDb, session_id: str, session_type: str) -> AgentSession | TeamSession:
    """Load one session the way a response loads its scope session."""
    session = (
        get_team_session(storage, session_id) if session_type == "team" else get_agent_session(storage, session_id)
    )
    assert session is not None, f"no {session_type} session {session_id}"
    return session


# ---------------------------------------------------------------------------
# Derived goldens
# ---------------------------------------------------------------------------


def _json(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _collect_stored_values(collector: _Collector, spec: StoreSpec, db_file: Path) -> dict[str, str]:
    """Collect every value stored in runs, summary segments, and session data; return each session's stored type."""
    connection = sqlite3.connect(db_file)
    try:
        for run_id, run_data in connection.execute(
            f'SELECT run_id, run_data FROM "{spec.session_table}_runs" ORDER BY rowid',  # noqa: S608
        ):
            collector.run(f"{spec.label}:{run_id}", json.loads(run_data))
        for row_id, segment in connection.execute(
            f'SELECT id, segment FROM "{spec.session_table}_summaries" WHERE segment IS NOT NULL ORDER BY id',  # noqa: S608
        ):
            collector.segment(f"{spec.label}:segment:{row_id}", json.loads(segment))
        session_types: dict[str, str] = {}
        for session_id, session_type, session_data in connection.execute(
            f'SELECT session_id, session_type, session_data FROM "{spec.session_table}" ORDER BY rowid',  # noqa: S608
        ):
            session_types[session_id] = session_type
            collector.session_metrics(f"{spec.label}:{session_id}", session_data and json.loads(session_data))
    finally:
        connection.close()
    return session_types


class _Collector:
    """Group every serialized value nested in stored runs by the Agno type that wrote it."""

    def __init__(self) -> None:
        self.cases: dict[str, dict[str, Any]] = {}

    def add(self, kind: str, key: str, value: object) -> None:
        if isinstance(value, dict):
            self.cases.setdefault(kind, {})[key] = value

    def run(self, key: str, run: dict[str, Any]) -> None:
        self.add("run_output" if "agent_id" in run else "team_run_output", key, run)
        self._metrics(f"{key}.metrics", run.get("metrics"))
        for index, message in enumerate(run.get("messages") or []):
            self.message(f"{key}.messages[{index}]", message)
        input_content = (run.get("input") or {}).get("input_content")
        for index, message in enumerate(input_content if isinstance(input_content, list) else []):
            self.message(f"{key}.input.input_content[{index}]", message)
        for index, tool in enumerate(run.get("tools") or []):
            self.tool(f"{key}.tools[{index}]", tool)
        for index, requirement in enumerate(run.get("requirements") or []):
            self.requirement(f"{key}.requirements[{index}]", requirement)
        delegation = (run.get("metadata") or {}).get("mindroom_delegation") or {}
        for index, tool in enumerate(delegation.get("pending_tools") or []):
            self.tool(f"{key}.metadata.mindroom_delegation.pending_tools[{index}]", tool)
        for index, requirement in enumerate(delegation.get("pending_requirements") or []):
            self.requirement(f"{key}.metadata.mindroom_delegation.pending_requirements[{index}]", requirement)
        for index, member in enumerate(run.get("member_responses") or []):
            self.run(f"{key}.member_responses[{index}]", member)

    def segment(self, key: str, segment: dict[str, Any]) -> None:
        for index, message in enumerate(segment.get("messages") or []):
            self.message(f"{key}.messages[{index}]", message)
        for index, tool in enumerate(segment.get("tools") or []):
            self.tool(f"{key}.tools[{index}]", tool)
        for index, member in enumerate(segment.get("member_responses") or []):
            self.run(f"{key}.member_responses[{index}]", member)

    def message(self, key: str, message: dict[str, Any]) -> None:
        self.add("message", key, message)
        if message.get("metrics"):
            self.add("message_metrics", f"{key}.metrics", message["metrics"])
        for media_key, kind in (("images", "image"), ("audio", "audio"), ("videos", "video"), ("files", "file")):
            for index, media in enumerate(message.get(media_key) or []):
                self.add(kind, f"{key}.{media_key}[{index}]", media)

    def tool(self, key: str, tool: dict[str, Any]) -> None:
        self.add("tool_execution", key, tool)
        self.add("tool_call_metrics", f"{key}.metrics", tool.get("metrics"))

    def requirement(self, key: str, requirement: dict[str, Any]) -> None:
        self.add("run_requirement", key, requirement)
        self.tool(f"{key}.tool_execution", requirement.get("tool_execution") or {})

    def _metrics(self, key: str, metrics: object) -> None:
        self.add("run_metrics", key, metrics)
        details = metrics.get("details") if isinstance(metrics, dict) else None
        for model_type, entries in (details or {}).items():
            for index, entry in enumerate(entries):
                self.add("model_metrics", f"{key}.details.{model_type}[{index}]", entry)

    def session_metrics(self, key: str, session_data: object) -> None:
        if isinstance(session_data, dict):
            self.add("session_metrics", f"{key}.session_data.session_metrics", session_data.get("session_metrics"))


def _history(session: AgentSession | TeamSession) -> dict[str, Any]:
    selections: dict[str, Any] = {}
    selected: dict[str, list[Message]] = {}
    for name, params in HISTORY_SELECTIONS:
        messages = session.get_messages(**params)
        selected[name] = messages
        selections[name] = {"params": params, "messages": [message.to_dict() for message in messages]}
    filtered: dict[str, Any] = {}
    for selection, max_tool_calls in TOOL_CALL_FILTERS:
        copies = [copy_history_message(message) for message in selected[selection]]
        filter_tool_calls(copies, max_tool_calls)
        filtered[f"{selection}/max_tool_calls_{max_tool_calls}"] = {
            "selection": selection,
            "copy": "copy_history_message",
            "max_tool_calls": max_tool_calls,
            "messages": [message.to_dict() for message in copies],
        }
    return {"get_messages": selections, "filter_tool_calls": filtered}


def _scoped_history(spec: StoreSpec, session: AgentSession | TeamSession) -> dict[str, Any]:
    """Select the session's messages as history replay does, by the store's scope id and by another scope's id."""
    scope_key = "team_id" if spec.scope.kind == "team" else "agent_id"
    selections: dict[str, Any] = {}
    for name, own_scope, params in SCOPED_SELECTIONS:
        scope_id = spec.scope.scope_id if own_scope else _OTHER_SCOPE_IDS[spec.scope.scope_id]
        selection = {scope_key: scope_id, **params}
        messages = session.get_messages(**selection)
        selections[name] = {"params": selection, "messages": [message.to_dict() for message in messages]}
    return selections


def _history_name(spec: StoreSpec, session_id: str) -> str:
    thread = re.sub(r"[^A-Za-z0-9-]+", "_", session_id.removeprefix(f"{ROOM}:")).strip("_")
    return f"history/{spec.label}__{thread}.json"


def derive_goldens(dumps: Mapping[str, str], work_dir: Path) -> dict[str, str]:
    """Derive the loaded-session, run, and history goldens from the normalized store dumps."""
    storage_root = work_dir / "restored"
    collector = _Collector()
    stores: dict[str, Any] = {}
    loaded: dict[str, Any] = {}
    scoped: dict[str, Any] = {}
    goldens: dict[str, str] = {}
    for spec in STORES:
        session_types = _collect_stored_values(collector, spec, restore_store(spec, dumps[spec.label], storage_root))
        stores[spec.label] = {"db_path": spec.db_path, "session_table": spec.session_table, "sessions": session_types}
        storage = _open_store(spec, storage_root)
        try:
            sessions = {
                session_id: _load_session(storage, session_id, session_type)
                for session_id, session_type in session_types.items()
            }
        finally:
            storage.close()
        loaded[spec.label] = {session_id: session.to_dict() for session_id, session in sessions.items()}
        for session_id, session in sessions.items():
            goldens[_history_name(spec, session_id)] = _json(
                {"store": spec.label, "session_id": session_id, **_history(session)},
            )
            scoped[f"{spec.label}:{session_id}"] = _scoped_history(spec, session)
    goldens["history/scoped_selections.json"] = _json(scoped)
    goldens["storage/stores.json"] = _json(stores)
    goldens["storage/loaded_sessions.json"] = _json(loaded)
    return goldens | {f"runs/{kind}.json": _json(cases) for kind, cases in sorted(collector.cases.items())}


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def capture(work_dir: Path) -> dict[str, str]:
    """Run every scenario under ``work_dir`` and return each fixture file's text by path under ``FIXTURES``.

    Agno serializes some sets, so the process must run with ``PYTHONHASHSEED=0`` for byte-stable dumps.
    """
    assert sys.flags.hash_randomization == 0, "run the capture with PYTHONHASHSEED=0"
    clock = _Clock()
    root = work_dir / "capture"
    paths = resolve_runtime_paths(config_path=root / "config.yaml", storage_path=root / "storage", process_env={})
    config = bind_runtime_paths(Config.model_validate(_CONFIG), paths)
    scripts = {name: _Script() for name in config.models}

    def scripted_model(
        _config: Config,
        _runtime_paths: RuntimePaths,
        model_name: str = "default",
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> _ScriptedModel:
        del execution_identity
        return _ScriptedModel(config.models[model_name].id, scripts[model_name])

    recorder = _Capture(root=root, paths=paths, config=config, clock=clock, scripts=scripts)
    with (
        patch("mindroom.providers.model_loading.get_model_instance", new=scripted_model),
        _frozen_clock(clock),
    ):
        await _run_scenarios(recorder)
        store_dumps = _dump_stores(paths.storage_root)
        # These change the stores after their dumps, and write goldens of their own.
        linkage = await _response_linkage(recorder)
        await _agno_cancelled_turns(recorder)
    normalize = _Normalizer((str(work_dir.resolve()), str(work_dir)))
    dumps = {label: normalize(text) for label, text in store_dumps.items()}
    files = {f"storage/{label}.sql": text for label, text in dumps.items()}
    files["events/ai_run.json"] = normalize(_json(recorder.ai_run))
    files["events/openai_compat.json"] = normalize(_json(recorder.openai_compat))
    # Normalized after the files above, so ids they share keep the numbers those files gave them.
    files |= {path: normalize(text) for path, text in recorder.pre_resume.items()}
    files["storage/response_linkage.json"] = normalize(_json(linkage))
    files["events/ai_run_cancelled.json"] = normalize(_json(recorder.ai_run_cancelled))
    return files | derive_goldens(dumps, work_dir)


def owned_goldens(directory: Path) -> dict[str, str]:
    """Return the fixture files this module owns under ``directory``, by relative path."""
    return {
        path.relative_to(directory).as_posix(): path.read_text(encoding="utf-8")
        for owned in OWNED_DIRS
        for path in sorted((directory / owned).glob("*"))
        if path.is_file()
    }


def main(argv: list[str] | None = None) -> None:
    """Rewrite every fixture this module owns, under ``--output`` or the committed fixtures directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=FIXTURES, help="directory to write the fixtures into")
    args = parser.parse_args(argv)
    if sys.flags.hash_randomization:
        command = [sys.executable, "-m", "tests.agno_parity.capture_storage", "--output", str(args.output)]
        raise SystemExit(subprocess.run(command, env={**os.environ, "PYTHONHASHSEED": "0"}, check=False).returncode)
    with tempfile.TemporaryDirectory(prefix="agno-parity-") as work_dir:
        files = asyncio.run(capture(Path(work_dir)))
    for stale in owned_goldens(args.output).keys() - files.keys():
        (args.output / stale).unlink()
    for relative, text in files.items():
        target = args.output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    print(f"wrote {len(files)} files under {args.output}")


if __name__ == "__main__":
    main()
