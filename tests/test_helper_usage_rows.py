"""Each kind of independently recorded usage stores its ledger row exactly as the golden holds it.

Every case runs the code that records that kind, on a scripted model or a fake provider response, under a frozen
clock, and compares the stored rows text for text with ``fixtures/helper_usage_rows.json``. Random ids are replaced by
placeholders of their format, so a change of id format still shows.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
import time
from contextlib import closing
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import httpx
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from openai.types import CompletionUsage

from mindroom import routing, topic_generator
from mindroom.agents import create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionOverrideConfig, ModelConfig
from mindroom.config.resolution import entity_model_name
from mindroom.config.voice import VoiceConfig, VoiceSTTConfig
from mindroom.custom_tools import claude_agent
from mindroom.custom_tools.dynamic_workflow import _run_room_agent_turn
from mindroom.helper_usage import HelperUsageOwner, helper_usage_owner_context
from mindroom.history.runtime import _compact_before_reply, resolve_entity_preparation_inputs
from mindroom.history.storage import read_scope_state, with_force_compaction
from mindroom.knowledge.chunk_context import chunk_contexts
from mindroom.knowledge_graph.clients import MindRoomLLMClient
from mindroom.knowledge_graph.usage import UsageTarget, usage_target
from mindroom.llm.messages import AGGREGATE_USAGE_KEY, Message
from mindroom.llm.provider import ModelIdentity, TurnCompleted
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.llm.usage import TokenCounts
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.identity import MatrixID
from mindroom.matrix_rtc.call_tools import record_call_token_usage, record_call_voice_usage
from mindroom.matrix_rtc.voice_agent import LiveVoiceUsage, RealtimeCallUsage, _RealtimeResponseUsage
from mindroom.memory._mem0_backend import Mem0MemoryBackend
from mindroom.memory._mem0_usage import counted_mem0_usage, install_mem0_usage_capture
from mindroom.memory.auto_flush import _extract_memory_summary
from mindroom.scheduling import tasks as scheduling
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.skill_learning.reviewer import review_conversation
from mindroom.skill_learning.tools import ReviewProgress
from mindroom.teams.formation import select_ad_hoc_team_mode
from mindroom.thread_summary.generation import generate_summary
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from mindroom.voice import handler as voice_handler
from tests.conftest import bind_runtime_paths, install_model_providers, seed_session, test_runtime_paths
from tests.fakes.scripted_model import ScriptedModel, answer, usage
from tests.fakes.token_counter import fake_token_counter
from tests.history_helpers import _forced_compaction_context, _make_config, _reported, _session
from tests.identity_helpers import persist_actual_entity_accounts
from tests.turn_model_helpers import turn_model

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.constants import RuntimePaths

GOLDEN = Path(__file__).parent / "fixtures" / "helper_usage_rows.json"
_IDENTITY = ModelIdentity(id="scripted-model", provider="Scripted")
# Not a whole second, so a row shows whether its time was truncated to an integer.
NOW = 1_790_000_000.5
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HEX_ID = re.compile(r"\b[0-9a-f]{32}\b")


def _freeze_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``time.time`` return ``NOW``, also where a MindRoom module imported the function itself."""
    real = time.time

    def frozen() -> float:
        return NOW

    monkeypatch.setattr(time, "time", frozen)
    for name, module in list(sys.modules.items()):
        if name.startswith("mindroom") and getattr(module, "time", None) is real:
            monkeypatch.setattr(module, "time", frozen)


def _ids_as_formats(text: str) -> str:
    return _HEX_ID.sub("<hex id>", _UUID.sub("<uuid>", text))


def _usage_rows(storage_root: Path) -> list[list[str]]:
    """Return every usage row under ``storage_root``: its database, session, run id, and stored text."""
    rows: list[list[str]] = []
    for database in sorted(storage_root.rglob("*.db")):
        with closing(sqlite3.connect(database)) as connection:
            tables = [
                name
                for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                if name.endswith("_usage")
            ]
            for table in tables:
                query = f'SELECT session_id, run_id, usage_data FROM "{table}" ORDER BY id'  # noqa: S608
                for session_id, run_id, usage_data in connection.execute(query):
                    label = str(database.relative_to(storage_root))
                    rows.append([label, session_id, _ids_as_formats(run_id), _ids_as_formats(usage_data)])
    return rows


def _system_config(paths: RuntimePaths) -> Config:
    return bind_runtime_paths(
        Config(
            agents={"helper": AgentConfig(display_name="Helper", role="Help")},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        paths,
    )


def _scripted(monkeypatch: pytest.MonkeyPatch, text: str, *, sums_attempts: bool = False) -> None:
    """Answer the default model's one request with ``text``."""
    turn = answer(text, usage(20, 2, cache_read_tokens=3))
    if sums_attempts:
        # The retrying stage marks a finished turn whose usage adds up failed attempts.
        [completed] = [step for step in turn if isinstance(step, TurnCompleted)]
        completed.message.provider_data = {AGGREGATE_USAGE_KEY: True}
    install_model_providers(monkeypatch, {"default": ScriptedModel([turn], identity=_IDENTITY)})


async def _routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, sums_attempts: bool = False) -> Path:
    paths = test_runtime_paths(tmp_path)
    config = _system_config(paths)
    _scripted(monkeypatch, json.dumps({"entity_name": "helper", "reasoning": "Matched"}), sums_attempts=sums_attempts)
    await routing.suggest_responder("Help", ["helper"], config, paths)
    return paths.storage_root


async def _schedule_parse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    paths = test_runtime_paths(tmp_path)
    config = _system_config(paths)
    workflow = scheduling.ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=scheduling.CronSchedule(minute="0", hour="9"),
        message="Check",
        description="Check",
    )
    _scripted(monkeypatch, workflow.model_dump_json(exclude_unset=True))
    responders = [MatrixID(username="helper", domain="localhost")]
    await scheduling._parse_workflow_schedule("Daily at 9", config, paths, responders, timezone="UTC")
    return paths.storage_root


async def _room_topic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    paths = test_runtime_paths(tmp_path)
    config = _system_config(paths)
    _scripted(monkeypatch, json.dumps({"topic": "A room topic"}))
    await topic_generator.generate_room_topic_ai("lobby", "Lobby", config, paths)
    return paths.storage_root


async def _thread_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    paths = test_runtime_paths(tmp_path)
    config = _system_config(paths)
    _scripted(monkeypatch, json.dumps({"summary": "A summary"}))
    await generate_summary(
        [ResolvedVisibleMessage.synthetic(sender="@person:localhost", body="Help", event_id="$message")],
        config,
        paths,
        model_name="default",
        tag_vocabulary=None,
    )
    return paths.storage_root


async def _team_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    paths = test_runtime_paths(tmp_path)
    config = _system_config(paths)
    _scripted(monkeypatch, json.dumps({"mode": "coordinate", "reasoning": "Steps depend on each other"}))
    await select_ad_hoc_team_mode("Plan the launch", ["helper"], config, paths)
    return paths.storage_root


async def _knowledge_chunk_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    paths = test_runtime_paths(tmp_path)
    config = _system_config(paths)
    _scripted(monkeypatch, "Where the chunk sits.")
    await chunk_contexts(config, paths, "default", ["alpha one"])
    return paths.storage_root


async def _voice_normalization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    paths = test_runtime_paths(tmp_path)
    config = Config(voice=VoiceConfig(enabled=True))
    persist_actual_entity_accounts(config, paths, password="test-password")  # noqa: S106 - local fixture account
    _scripted(monkeypatch, "normalized transcript")
    await voice_handler._process_transcription("raw transcript", config, paths)
    return paths.storage_root


async def _voice_transcription(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    paths = test_runtime_paths(tmp_path)
    config = Config(voice=VoiceConfig(enabled=True, stt=VoiceSTTConfig(api_key="test-key", model="gpt-transcribe")))
    client_type = httpx.AsyncClient
    reply = {
        "text": "transcript",
        "usage": {
            "type": "tokens",
            "input_tokens": 12,
            "output_tokens": 3,
            "total_tokens": 15,
            "input_token_details": {"audio_tokens": 9, "text_tokens": 3},
        },
    }
    monkeypatch.setattr(
        voice_handler.httpx,
        "AsyncClient",
        lambda *, timeout: client_type(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=reply)),
            timeout=timeout,
        ),
    )
    await voice_handler._transcribe_audio(b"audio", config, paths)
    return paths.storage_root


def _conversation(tmp_path: Path) -> tuple[Config, RuntimePaths, HelperUsageOwner, object]:
    """Return an agent conversation that helper usage counts against, its owner, and its runtime context."""
    config, paths, _storage, _scope, context = _forced_compaction_context(tmp_path, session=_session("session"))
    owner = HelperUsageOwner(
        storage_factory=partial(create_session_storage, "test_agent", config, paths, execution_identity=None),
        session_id="session",
    )
    return config, paths, owner, context


async def _memory_auto_flush(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config, paths, _owner, context = _conversation(tmp_path)
    _scripted(monkeypatch, "Remember the sample preference.")
    await _extract_memory_summary(
        config=config,
        runtime_paths=paths,
        agent_name="test_agent",
        session_id="session",
        lines=["Synthetic conversation excerpt."],
        existing_context="",
        execution_identity=context.execution_identity,
    )
    return paths.storage_root


async def _knowledge_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config, paths, _owner, _context = _conversation(tmp_path)
    config.knowledge_graph.model = "default"
    _scripted(monkeypatch, json.dumps({"names": ["Alice"]}))
    client = MindRoomLLMClient(lambda: config, paths, asyncio.get_running_loop())
    with usage_target(UsageTarget(agent_name="test_agent", session_id="session", execution_identity=None)):
        await client._generate_response([SimpleNamespace(role="user", content="Alice runs Atlas.")], None)
    return paths.storage_root


async def _memory_extraction(tmp_path: Path, *, owned: bool) -> Path:
    _config, paths, owner, _context = _conversation(tmp_path)
    reported = CompletionUsage(prompt_tokens=120, completion_tokens=30, total_tokens=150)
    create = MagicMock(return_value=SimpleNamespace(usage=reported))
    llm = SimpleNamespace(
        config=SimpleNamespace(model="gpt-6-luna"),
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    install_mem0_usage_capture(llm, "openai")
    with counted_mem0_usage() as calls:
        llm.client.chat.completions.create()
        llm.client.chat.completions.create()
    backend = Mem0MemoryBackend(runtime_paths=paths, create_memory=MagicMock())
    await backend._record_extraction_usage(calls, owner if owned else None, "@user:localhost")
    return paths.storage_root


async def _claude_agent(tmp_path: Path, _monkeypatch: pytest.MonkeyPatch) -> Path:
    _config, paths, owner, _context = _conversation(tmp_path)
    response = AssistantMessage(
        content=[TextBlock(text="Fixed it")],
        model="claude-sonnet-5-5",
        usage={"input_tokens": 40, "output_tokens": 1, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 2},
        message_id="msg-1",
    )
    result = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="claude-session",
        usage={"input_tokens": 50, "output_tokens": 9, "cache_read_input_tokens": 5},
    )
    turn = claude_agent._ClaudeTurn(responses={"msg-1": response}, result=result)
    with helper_usage_owner_context(owner):
        await claude_agent._record_turn_usage(turn, None)
    return paths.storage_root


async def _dynamic_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config, paths, owner, context = _conversation(tmp_path)
    _scripted(monkeypatch, "Sample helper output.")
    identity = context.execution_identity
    build = create_agent(
        "test_agent",
        config,
        paths,
        identity,
        knowledge=None,
        model=turn_model(config, paths, "test_agent"),
        plugins=config_plugins(config, paths),
    )
    with helper_usage_owner_context(owner):
        await _run_room_agent_turn(context, build, "Sample task.")
    return paths.storage_root


async def _skill_learning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config, paths, _owner, _context = _conversation(tmp_path)
    _scripted(monkeypatch, "Nothing worth keeping as a skill.")
    await review_conversation(
        config=config,
        runtime_paths=paths,
        agent_name="test_agent",
        session_id="session",
        identity=None,
        skills_root=tmp_path / "skills",
        captured=None,
        progress=ReviewProgress(),
    )
    return paths.storage_root


async def _compaction_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Compact a stored run before the next reply, with one summary request to the scripted model."""
    config, paths = _make_config(tmp_path, compaction=CompactionOverrideConfig(enabled=True))
    storage = create_session_storage("test_agent", config, paths, execution_identity=None)
    run = RunRecord(
        run_id="run-1",
        agent_id="test_agent",
        session_id="session",
        status=RunStatus.completed,
        messages=[Message(role="user", content="Synthetic question."), Message(role="assistant", content="Answer.")],
    )
    session = _session(
        "session",
        runs=[run],
        metadata=with_force_compaction(None),
    )
    seed_session(storage, session)
    _scripted(monkeypatch, "A summary of the conversation.")
    inputs = resolve_entity_preparation_inputs(
        config=config,
        runtime_paths=paths,
        history_settings=config.resolve_scope("test_agent").history_settings,
        static_prompt_tokens=0,
        active_model_name=entity_model_name(config, "test_agent"),
        active_context_window=None,
    )
    outcome = await _compact_before_reply(
        None,
        _reported,
        storage=storage,
        session=session,
        state=read_scope_state(session),
        resolved_inputs=inputs,
        history_budget=None,
        before_tokens=0,
        counter=fake_token_counter(),
        config=config,
        runtime_paths=paths,
    )
    assert outcome is not None
    return paths.storage_root


def _call_identity() -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="helper",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id=None,
        resolved_thread_id=None,
        session_id="call",
    )


async def _live_voice(tmp_path: Path, _monkeypatch: pytest.MonkeyPatch) -> Path:
    paths = test_runtime_paths(tmp_path)
    config = Config(agents={"helper": AgentConfig(display_name="Helper")})
    usage = LiveVoiceUsage("provider-session", "gpt-live-1", NOW, duration_seconds=12.5, finalized=True)
    await record_call_voice_usage(usage, config=config, runtime_paths=paths, execution_identity=_call_identity())
    return paths.storage_root


async def _realtime_voice(tmp_path: Path, _monkeypatch: pytest.MonkeyPatch) -> Path:
    paths = test_runtime_paths(tmp_path)
    config = Config(agents={"helper": AgentConfig(display_name="Helper")})
    responses = (
        _RealtimeResponseUsage(
            created_at=NOW + 10,
            tokens=TokenCounts(input_tokens=900, output_tokens=300, cache_read_tokens=400, audio_input_tokens=800),
        ),
        # A response that reported no tokens.
        _RealtimeResponseUsage(created_at=NOW + 20, tokens=TokenCounts()),
    )
    usage = RealtimeCallUsage(usage_id="call-1", model="gpt-realtime-2.1", created_at=NOW, responses=responses)
    await record_call_token_usage(usage, config=config, runtime_paths=paths, execution_identity=_call_identity())
    return paths.storage_root


_CASES: dict[str, Callable[[Path, pytest.MonkeyPatch], Awaitable[Path]]] = {
    "claude_agent": _claude_agent,
    "compaction_summary": _compaction_summary,
    "dynamic_workflow": _dynamic_workflow,
    "knowledge_chunk_context": _knowledge_chunk_context,
    "knowledge_graph": _knowledge_graph,
    "live_voice": _live_voice,
    "memory_auto_flush": _memory_auto_flush,
    "memory_extraction": lambda tmp_path, _monkeypatch: _memory_extraction(tmp_path, owned=True),
    "memory_extraction_system": lambda tmp_path, _monkeypatch: _memory_extraction(tmp_path, owned=False),
    "realtime_voice": _realtime_voice,
    "room_topic": _room_topic,
    "routing": _routing,
    "routing_retried": lambda tmp_path, monkeypatch: _routing(tmp_path, monkeypatch, sums_attempts=True),
    "schedule_parse": _schedule_parse,
    "skill_learning": _skill_learning,
    "team_mode": _team_mode,
    "thread_summary": _thread_summary,
    "voice_normalization": _voice_normalization,
    "voice_transcription": _voice_transcription,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_CASES))
async def test_each_kind_stores_its_usage_row_as_the_golden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """The stored rows of one kind's recording equal the golden's, byte for byte apart from random ids."""
    _freeze_clock(monkeypatch)
    storage_root = await _CASES[case](tmp_path, monkeypatch)
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert _usage_rows(storage_root) == golden[case]
