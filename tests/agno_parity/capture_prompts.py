"""Capture the model requests MindRoom builds with Agno 3.0.9 as prompt goldens.

Each scenario builds its agents, teams, or helper agents through the production builders, runs one turn against a
recording model, and returns every request that model received: messages, tools, tool choice, and response format.
Run ``.venv/bin/python -m tests.agno_parity.capture_prompts`` from the repository root to rewrite the fixtures.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import re
import sys
import tempfile
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import nio
from agno.knowledge.knowledge import Knowledge
from agno.models.response import ModelResponse
from agno.session.agent import AgentSession
from graphiti_core.prompts.extract_nodes import ExtractedEntities
from graphiti_core.prompts.models import Message as GraphitiMessage
from pydantic import BaseModel

from mindroom import ai
from mindroom.agent_cli.session import TurnToolRegistry
from mindroom.budgets.monitor import BudgetMonitor
from mindroom.config.main import Config
from mindroom.custom_tools.dynamic_workflow import _aexecute_room_agent_participant
from mindroom.entity_resolution import entity_identity_registry
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.hooks import EnrichmentItem
from mindroom.knowledge_graph.clients import MindRoomLLMClient
from mindroom.matrix_rtc.call_tools import CallAgentTooling, build_call_tools
from mindroom.memory.auto_flush import _extract_memory_summary
from mindroom.message_target import MessageTarget
from mindroom.providers import model_loading
from mindroom.routing import suggest_responder
from mindroom.routing_judgment import ResponderSelection
from mindroom.runtime_state import clear_api_server_address, set_api_server_address
from mindroom.scheduling.tasks import ScheduledWorkflow, _parse_workflow_schedule
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.skill_learning.capture import SkillReviewCapture
from mindroom.skill_learning.reviewer import review_conversation
from mindroom.skill_learning.tools import ReviewProgress
from mindroom.teams import TeamMode, _select_team_mode, team_response_stream
from mindroom.thread_summary import _generate_summary, _ThreadEnrichment
from mindroom.tool_system.runtime_context import (
    ToolRuntimeModelBinding,
    build_execution_identity_from_runtime_context,
    tool_runtime_context,
)
from mindroom.tool_system.skills import agent_workspace_skills_root
from mindroom.tool_system.worker_routing import agent_workspace_root_path, build_tool_execution_identity
from mindroom.topic_generator import generate_room_topic_ai
from mindroom.voice.handler import _process_transcription
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    make_turn_context,
    make_visible_message,
    test_runtime_paths,
)
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

    from agno.models.base import Model
    from agno.models.message import Message

    from mindroom.agent_modes import AgentMode
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "agno_parity" / "prompts"

_ROOM_ID = "!room:localhost"
_THREAD_ID = "$thread:localhost"
_REQUESTER_ID = "@user:localhost"
_SESSION_ID = f"{_ROOM_ID}:{_THREAD_ID}"
_CALL_SESSION_ID = f"{_ROOM_ID}:call:one"
_CURRENT_EVENT_ID = "$current:localhost"
_CURRENT_TIMESTAMP_MS = 1_790_000_060_000
_CORRELATION_ID = "correlation-1"
_EARLIER_MESSAGE = "Our handbook moved to the new wiki last week."
_ANTHROPIC = {"provider": "anthropic", "id": "claude-sonnet-5-5", "extra_kwargs": {"api_key": "test-key"}}
_OPENAI_CHAT = {
    "provider": "openai",
    "id": "gpt-6.1-sol",
    "api": "chat_completions",
    "extra_kwargs": {"api_key": "test-key"},
}
_VERTEX_CLAUDE = {
    "provider": "vertexai_claude",
    "id": "claude-sonnet-5-5",
    "extra_kwargs": {"project_id": "test-project", "region": "us-east5"},
}
_SUMMARY = "Travel policy question answered."
_TAG_VOCABULARY = "travel, budget, onboarding"
_HANDBOOK_DESCRIPTION = "The team handbook and its policies."
_WORKSPACE_FILES = {
    "MEMORY.md": "# Memory\n\n- The user flies to Berlin every quarter.\n",
    "SOUL.md": "You are calm and precise.\n",
    "skills/expense-report/SKILL.md": (
        "---\nname: expense-report\ndescription: File an expense report from receipts.\n---\n\n"
        "Collect the receipts, total them, and fill in the expense form.\n"
    ),
}
_SYSTEM_ENRICHMENT = (EnrichmentItem(key="team_calendar", text="The offsite is on Friday.", cache_policy="stable"),)
_TRANSIENT_ENRICHMENT = (EnrichmentItem(key="weather", text="It is raining in Berlin."),)
_DATE_PATTERN = re.compile(
    r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), "
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December) \d{2}, \d{4}\b",
)


@dataclass(frozen=True)
class ModelCall:
    """One request the recording model received, as the provider adapter would see it."""

    model: Model
    # Models in the order MindRoom built them, which is deterministic where concurrent calls are not.
    model_index: int
    stream: bool
    messages: list[Message]
    tools: list[dict[str, Any]] | None
    tool_choice: str | dict[str, Any] | None
    response_format: dict[str, Any] | type[BaseModel] | None


type Responder = Callable[[ModelCall], ModelResponse]


def answer_ok(_call: ModelCall) -> ModelResponse:
    """Answer every request with one short assistant message."""
    return ModelResponse(role="assistant", content="ok")


@dataclass
class Recorder:
    """Replace every provider call MindRoom loads with a recorder that answers through ``responder``."""

    responder: Responder = answer_ok
    calls: list[ModelCall] = field(default_factory=list)
    built_models: int = 0

    def ordered_calls(self) -> list[ModelCall]:
        """Return the calls grouped by model in build order, each model's calls in the order it received them.

        A team runs its members concurrently, so only each model's own sequence is stable.
        """
        return sorted(self.calls, key=lambda call: call.model_index)

    def _respond(
        self,
        model: Model,
        index: int,
        *,
        stream: bool,
        messages: list[Message],
        kwargs: dict[str, Any],
    ) -> ModelResponse:
        call = ModelCall(
            model=model,
            model_index=index,
            stream=stream,
            # The loop keeps mutating its list after the request, so the record keeps copies.
            messages=[message.model_copy(deep=True) for message in messages],
            tools=deepcopy(kwargs.get("tools")),
            tool_choice=deepcopy(kwargs.get("tool_choice")),
            response_format=kwargs.get("response_format"),
        )
        self.calls.append(call)
        return self.responder(call)

    def _bind(self, model: Model) -> Model:
        respond = partial(self._respond, model, self.built_models)
        self.built_models += 1

        async def ainvoke(messages: list[Message], **kwargs: Any) -> ModelResponse:  # noqa: ANN401
            return respond(stream=False, messages=messages, kwargs=kwargs)

        async def ainvoke_stream(messages: list[Message], **kwargs: Any) -> AsyncIterator[ModelResponse]:  # noqa: ANN401
            yield respond(stream=True, messages=messages, kwargs=kwargs)

        # Bound before MindRoom installs its request hooks, so every hook wraps the recorder like the provider call.
        model.__dict__.update(ainvoke=ainvoke, ainvoke_stream=ainvoke_stream)
        return model

    @contextmanager
    def installed(self) -> Iterator[None]:
        """Record every model that ``model_loading.get_model_instance`` builds while active."""
        create_model = model_loading._create_model_for_provider

        def create_recording_model(*args: Any, **kwargs: Any) -> Model:  # noqa: ANN401
            return self._bind(create_model(*args, **kwargs))

        with patch.object(model_loading, "_create_model_for_provider", create_recording_model):
            yield


@dataclass(frozen=True)
class ScenarioOutput:
    """What one scenario ran, and any model-visible values it hands to a model outside MindRoom's loop."""

    input: dict[str, Any]
    exported: dict[str, Any] | None = None


@dataclass(frozen=True)
class Scenario:
    """One golden: what it pins and how to capture it."""

    description: str
    run: Callable[[Path], Awaitable[ScenarioOutput]]
    responder: Responder = answer_ok


def _message_dict(message: Message) -> dict[str, Any]:
    data = {key: value for key, value in message.to_dict().items() if key not in {"id", "created_at", "metrics"}}
    if not message.add_to_agent_memory:
        data["add_to_agent_memory"] = False
    return data


def _response_format_dict(response_format: object) -> object:
    if isinstance(response_format, type) and issubclass(response_format, BaseModel):
        return {"pydantic_model": response_format.__name__, "json_schema": response_format.model_json_schema()}
    return response_format


def _call_dict(call: ModelCall) -> dict[str, Any]:
    return {
        "model": {"id": call.model.id, "provider": call.model.provider},
        "stream": call.stream,
        "messages": [_message_dict(message) for message in call.messages],
        "tools": call.tools,
        "tool_choice": call.tool_choice,
        "response_format": _response_format_dict(call.response_format),
    }


def normalize(value: object, root: Path) -> object:
    """Replace what changes between captures: the capture's temporary root and today's date."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    for path in {str(root.resolve()), str(root)}:
        text = text.replace(path, "<TMP>")
    return json.loads(_DATE_PATTERN.sub("<DATE>", text))


def _bound_config(root: Path, authored: dict[str, Any]) -> tuple[Config, RuntimePaths]:
    runtime_paths = test_runtime_paths(root)
    return bind_runtime_paths(Config.model_validate(authored), runtime_paths), runtime_paths


def _tool_runtime(config: Config, runtime_paths: RuntimePaths, agent_name: str) -> ToolRuntimeContext:
    """Return the tool context a Matrix reply binds around its response, as the response runner does."""
    return make_test_tool_runtime_context(
        agent_name=agent_name,
        target=MessageTarget(
            room_id=_ROOM_ID,
            source_thread_id=_THREAD_ID,
            resolved_thread_id=_THREAD_ID,
            reply_to_event_id=_CURRENT_EVENT_ID,
            session_id=_SESSION_ID,
        ),
        requester_id=_REQUESTER_ID,
        client=SimpleNamespace(user_id=f"@mindroom_{agent_name}:localhost"),
        config=config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        orchestrator=SimpleNamespace(
            agent_cli_registry=TurnToolRegistry(),
            budgets=BudgetMonitor(runtime_paths=runtime_paths, config_provider=lambda: config),
        ),
        correlation_id=_CORRELATION_ID,
        membership_turn_id=_CURRENT_EVENT_ID,
    )


@contextmanager
def _api_server() -> Iterator[None]:
    """Serve the API like ``mindroom run``, which minimal Bash and the shell CLI call back."""
    set_api_server_address("127.0.0.1", 8765)
    try:
        yield
    finally:
        clear_api_server_address()


def _thread_history() -> list[ResolvedVisibleMessage]:
    return [
        make_visible_message(
            sender=_REQUESTER_ID,
            body=_EARLIER_MESSAGE,
            event_id="$earlier:localhost",
            timestamp=1_790_000_000_000,
            thread_id=_THREAD_ID,
        ),
    ]


async def _agent_turn(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    prompt: str,
    *,
    knowledge: Knowledge | None = None,
    agent_mode: AgentMode = "standard",
    system_enrichment: tuple[EnrichmentItem, ...] = (),
    transient_enrichment: tuple[EnrichmentItem, ...] = (),
    skill_review_capture: SkillReviewCapture | None = None,
) -> ToolExecutionIdentity:
    """Stream one Matrix reply through ``ai.stream_agent_response`` with the arguments the response runner passes."""
    runtime = _tool_runtime(config, runtime_paths, agent_name)
    ctx = replace(
        make_turn_context(
            agent_name,
            session_id=_SESSION_ID,
            room_id=_ROOM_ID,
            thread_id=_THREAD_ID,
            requester_id=_REQUESTER_ID,
            reply_to_event_id=_CURRENT_EVENT_ID,
            correlation_id=_CORRELATION_ID,
            transient_enrichment_items=transient_enrichment,
            system_enrichment_items=system_enrichment,
        ),
        agent_mode=agent_mode,
        skill_review_capture=skill_review_capture,
    )
    execution_identity = build_execution_identity_from_runtime_context(runtime)
    recorder = TurnRecorder(user_message=prompt)
    with _api_server(), tool_runtime_context(runtime):
        async for _chunk in ai.stream_agent_response(
            ctx,
            prompt=prompt,
            runtime_paths=runtime_paths,
            config=config,
            thread_history=_thread_history(),
            current_timestamp_ms=_CURRENT_TIMESTAMP_MS,
            knowledge=knowledge,
            execution_identity=execution_identity,
            turn_recorder=recorder,
            supports_native_tool_approval=True,
        ):
            pass
    _assert_answered(recorder)
    return execution_identity


def _assert_answered(recorder: TurnRecorder) -> None:
    """Fail the capture when a reply ended early, because its requests would not be the ones production sends."""
    assert (recorder.outcome, recorder.assistant_text) == ("completed", "ok"), (
        recorder.outcome,
        recorder.assistant_text,
    )


def _write_workspace_files(runtime_paths: RuntimePaths, agent_name: str) -> None:
    workspace = agent_workspace_root_path(runtime_paths.storage_root, agent_name)
    for relative_path, text in _WORKSPACE_FILES.items():
        path = workspace / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _turn_input(prompt: str, **extra: object) -> dict[str, Any]:
    return {
        "entry_point": "mindroom.ai.stream_agent_response",
        "api_server": "127.0.0.1:8765",
        "room_id": _ROOM_ID,
        "thread_id": _THREAD_ID,
        "requester_id": _REQUESTER_ID,
        "thread_history": [_EARLIER_MESSAGE],
        "prompt": prompt,
        **extra,
    }


def _full_agent_config(model: dict[str, Any]) -> dict[str, Any]:
    return {
        "agents": {
            "assistant": {
                "display_name": "Assistant",
                "role": "Answer questions about the team handbook.",
                "instructions": [
                    "Answer in at most three paragraphs.",
                    "Cite the handbook section you used.",
                    "When a policy is unclear:\n- say so\n- name who decides",
                ],
                "tools": ["calculator", "chat_ui", {"name": "wikipedia", "defer": True}],
                "knowledge_bases": ["handbook"],
                "context_files": ["SOUL.md"],
                "delegate_to": ["researcher"],
                "memory_backend": "file",
                "learning": False,
            },
            "researcher": {"display_name": "Researcher", "role": "Research topics in depth.", "learning": False},
        },
        "knowledge_bases": {"handbook": {"description": _HANDBOOK_DESCRIPTION, "path": "./handbook"}},
        "models": {"default": model},
        "timezone": "UTC",
    }


async def _run_full_agent(root: Path, model: dict[str, Any]) -> ScenarioOutput:
    authored = _full_agent_config(model)
    config, runtime_paths = _bound_config(root, authored)
    _write_workspace_files(runtime_paths, "assistant")
    prompt = "What is the travel policy for conferences?"
    await _agent_turn(
        config,
        runtime_paths,
        "assistant",
        prompt,
        # The response runner resolves the published index, which knowledge.utils names after its base.
        knowledge=Knowledge(name="handbook", description=_HANDBOOK_DESCRIPTION),
        system_enrichment=_SYSTEM_ENRICHMENT,
        transient_enrichment=_TRANSIENT_ENRICHMENT,
    )
    return ScenarioOutput(
        {
            "config": authored,
            "workspace_files": _WORKSPACE_FILES,
            "knowledge": {"name": "handbook", "description": _HANDBOOK_DESCRIPTION},
            "system_enrichment": [asdict(item) for item in _SYSTEM_ENRICHMENT],
            "transient_enrichment": [asdict(item) for item in _TRANSIENT_ENRICHMENT],
            "turn": _turn_input(prompt),
        },
    )


def _shell_agent_config(**agent: object) -> dict[str, Any]:
    return {
        "agents": {
            "operator": {
                "display_name": "Operator",
                "role": "Operate the build machine.",
                "instructions": ["Explain each command before running it."],
                "tools": ["shell", "calculator"],
                "memory_backend": "file",
                "learning": False,
                **agent,
            },
        },
        "models": {"default": _ANTHROPIC},
        "timezone": "UTC",
    }


async def _run_shell_agent(root: Path, *, agent_mode: AgentMode, **agent: object) -> ScenarioOutput:
    authored = _shell_agent_config(**agent)
    config, runtime_paths = _bound_config(root, authored)
    prompt = "How much free disk space is left?"
    await _agent_turn(config, runtime_paths, "operator", prompt, agent_mode=agent_mode)
    return ScenarioOutput(
        {"config": authored, "agent_mode": agent_mode, "turn": _turn_input(prompt)},
    )


def _tool_names(call: ModelCall) -> set[str]:
    return {tool["function"]["name"] for tool in call.tools or [] if "function" in tool}


def _tool_call(call_id: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _answers_tools(call: ModelCall) -> bool:
    return any(message.role == "tool" for message in call.messages)


def answer_as_team(call: ModelCall) -> ModelResponse:
    """Delegate once as the team leader, then answer; members answer their task directly."""
    names = _tool_names(call)
    if _answers_tools(call) or not names & {"delegate_task_to_member", "delegate_task_to_members"}:
        return ModelResponse(role="assistant", content="ok")
    if "delegate_task_to_members" in names:
        return ModelResponse(
            role="assistant",
            tool_calls=[_tool_call("call_team_1", "delegate_task_to_members", {"task": "Draft the launch note."})],
        )
    return ModelResponse(
        role="assistant",
        tool_calls=[
            _tool_call("call_team_1", "delegate_task_to_member", {"member_id": "planner", "task": "List the steps."}),
            _tool_call("call_team_2", "delegate_task_to_member", {"member_id": "writer", "task": "Draft the note."}),
        ],
    )


def _team_config(mode: str) -> dict[str, Any]:
    return {
        "agents": {
            "planner": {
                "display_name": "Planner",
                "role": "Break work into ordered steps.",
                "instructions": ["Number every step."],
                "tools": ["calculator"],
                "learning": False,
            },
            "writer": {
                "display_name": "Writer",
                "role": "Write clear announcements.",
                "learning": False,
            },
        },
        "teams": {
            "launch": {
                "display_name": "Launch Team",
                "role": "Plan and announce product launches.",
                "agents": ["planner", "writer"],
                "mode": mode,
            },
        },
        "memory": {"backend": "none"},
        "models": {"default": _ANTHROPIC},
        "timezone": "UTC",
    }


async def _run_team(root: Path, mode: TeamMode) -> ScenarioOutput:
    authored = _team_config(mode.value)
    config, runtime_paths = _bound_config(root, authored)
    member_ids = entity_ids(config, runtime_paths)
    orchestrator = SimpleNamespace(
        config=config,
        runtime_paths=runtime_paths,
        agent_bots={name: SimpleNamespace(running=True) for name in ("planner", "writer")},
        knowledge_refresh_scheduler=None,
    )
    prompt = "Plan the launch and draft the announcement."
    runtime = _tool_runtime(config, runtime_paths, "launch")
    ctx = make_turn_context(
        "launch",
        session_id=_SESSION_ID,
        room_id=_ROOM_ID,
        thread_id=_THREAD_ID,
        requester_id=_REQUESTER_ID,
        reply_to_event_id=_CURRENT_EVENT_ID,
        correlation_id=_CORRELATION_ID,
    )
    recorder = TurnRecorder(user_message=prompt)
    with tool_runtime_context(runtime):
        async for _chunk in team_response_stream(
            agent_ids=[member_ids["planner"], member_ids["writer"]],
            message=prompt,
            orchestrator=orchestrator,
            execution_identity=build_execution_identity_from_runtime_context(runtime),
            ctx=ctx,
            mode=mode,
            thread_history=_thread_history(),
            user_id=_REQUESTER_ID,
            current_timestamp_ms=_CURRENT_TIMESTAMP_MS,
            response_sender_id=member_ids["launch"].full_id,
            configured_team_name="launch",
            turn_recorder=recorder,
        ):
            pass
    delegations = [tool.tool_name for tool in recorder.completed_tools]
    expected = ["delegate_task_to_members"] if mode is TeamMode.COLLABORATE else ["delegate_task_to_member"] * 2
    assert (recorder.outcome, delegations) == ("completed", expected), (recorder.outcome, delegations)
    return ScenarioOutput(
        {"config": authored, "turn": {**_turn_input(prompt), "entry_point": "mindroom.teams.team_response_stream"}},
    )


def answer_with(content: str) -> Responder:
    """Answer every request with ``content``, such as JSON for a structured helper."""

    def respond(_call: ModelCall) -> ModelResponse:
        return ModelResponse(role="assistant", content=content)

    return respond


def answer_after_one_tool_call(name: str, arguments: dict[str, object]) -> Responder:
    """Call one tool on the first request, then answer once its result is back."""

    def respond(call: ModelCall) -> ModelResponse:
        if _answers_tools(call):
            return ModelResponse(role="assistant", content="ok")
        return ModelResponse(role="assistant", tool_calls=[_tool_call("call_1", name, arguments)])

    return respond


def _helper_config(model: dict[str, Any]) -> dict[str, Any]:
    return {
        "agents": {
            "assistant": {"display_name": "Assistant", "role": "Answer questions about the team handbook."},
            "researcher": {"display_name": "Researcher", "role": "Research topics in depth."},
        },
        "teams": {
            "launch": {
                "display_name": "Launch Team",
                "role": "Plan and announce product launches.",
                "agents": ["assistant", "researcher"],
            },
        },
        "router": {"model": "default"},
        "knowledge_graph": {"model": "default"},
        "memory": {"backend": "file"},
        "models": {"default": model},
        "timezone": "UTC",
    }


def _helper_history() -> list[ResolvedVisibleMessage]:
    return [
        make_visible_message(
            sender=_REQUESTER_ID,
            body="Can someone summarize the travel policy?",
            event_id="$first:localhost",
            timestamp=1_790_000_000_000,
            thread_id=_THREAD_ID,
        ),
        make_visible_message(
            sender="@mindroom_assistant:localhost",
            body="Conference travel needs manager approval two weeks ahead.",
            event_id="$second:localhost",
            timestamp=1_790_000_030_000,
            thread_id=_THREAD_ID,
        ),
    ]


type HelperCall = Callable[[Config, RuntimePaths], Awaitable[None]]


async def _run_helper(root: Path, *, model: dict[str, Any], call: HelperCall, entry_point: str) -> ScenarioOutput:
    authored = _helper_config(model)
    config, runtime_paths = _bound_config(root, authored)
    await call(config, runtime_paths)
    return ScenarioOutput({"config": authored, "entry_point": entry_point})


async def _routing(config: Config, runtime_paths: RuntimePaths) -> None:
    selection = await suggest_responder(
        "Who can explain the travel policy?",
        ["assistant", "researcher", "launch"],
        config,
        runtime_paths,
        _helper_history(),
        room_id=_ROOM_ID,
        thread_id=_THREAD_ID,
    )
    assert selection == ResponderSelection("assistant"), selection


async def _team_mode(config: Config, runtime_paths: RuntimePaths) -> None:
    message = "Plan the launch and draft the announcement."
    mode = await _select_team_mode(message, ["assistant", "researcher"], config, runtime_paths)
    assert mode == TeamMode.COORDINATE, mode


async def _schedule_parse(config: Config, runtime_paths: RuntimePaths) -> None:
    responders = entity_ids(config, runtime_paths)
    workflow = await _parse_workflow_schedule(
        "Every weekday at 9am, ask @assistant for the travel policy changes.",
        config,
        runtime_paths,
        [responders["assistant"], responders["researcher"]],
        current_time=datetime(2026, 9, 21, 14, 14, tzinfo=UTC),
    )
    assert isinstance(workflow, ScheduledWorkflow), workflow


async def _thread_summary(config: Config, runtime_paths: RuntimePaths) -> None:
    summary = await _generate_summary(_helper_history(), config, runtime_paths)
    assert summary == _SUMMARY, summary


async def _thread_enrichment(config: Config, runtime_paths: RuntimePaths) -> None:
    enrichment = await _generate_summary(_helper_history(), config, runtime_paths, tag_vocabulary=_TAG_VOCABULARY)
    assert enrichment == _ThreadEnrichment(summary=_SUMMARY, tags=["travel"]), enrichment


async def _room_topic(config: Config, runtime_paths: RuntimePaths) -> None:
    topic = await generate_room_topic_ai("lobby", "Lobby", config, runtime_paths)
    assert topic == "Lobby - team questions", topic


async def _knowledge_graph(config: Config, runtime_paths: RuntimePaths) -> None:
    client = MindRoomLLMClient(lambda: config, runtime_paths, asyncio.get_running_loop())
    messages = [
        GraphitiMessage(role="system", content="You extract entities from conversations."),
        GraphitiMessage(role="user", content="<CURRENT MESSAGE>\nAlice booked the Berlin offsite.\n</CURRENT MESSAGE>"),
    ]
    extracted = await client.generate_response(messages, response_model=ExtractedEntities)
    assert extracted["extracted_entities"][0]["name"] == "Alice", extracted


async def _memory_flush(config: Config, runtime_paths: RuntimePaths) -> None:
    # Auto-flush extracts from a conversation the agent already stored.
    storage = create_session_storage("assistant", config, runtime_paths, execution_identity=None)
    try:
        storage.upsert_session(AgentSession(session_id=_SESSION_ID, agent_id="assistant"))
    finally:
        storage.close()
    summary = await _extract_memory_summary(
        config=config,
        runtime_paths=runtime_paths,
        agent_name="assistant",
        session_id=_SESSION_ID,
        lines=["user: I prefer aisle seats on flights.", "assistant: Noted, aisle seats from now on."],
        existing_context="- The user lives in Berlin.",
        execution_identity=None,
    )
    assert summary == "The user prefers aisle seats.", summary


async def _voice_normalizer(config: Config, runtime_paths: RuntimePaths) -> None:
    normalized = await _process_transcription("hey assistant what is the travel policy", config, runtime_paths)
    assert normalized == "@assistant what is the travel policy", normalized


def _helper(entry_point: str, call: HelperCall, answer: str, *, model: dict[str, Any] = _ANTHROPIC) -> Scenario:
    return Scenario(
        f"Helper agent run by {entry_point} on {model['provider']} {model['id']}.",
        partial(_run_helper, model=model, call=call, entry_point=entry_point),
        answer_with(answer),
    )


def _structured_helper(name: str, entry_point: str, call: HelperCall, answer: str) -> dict[str, Scenario]:
    """Capture a structured-output helper on models with and without native structured outputs.

    Agno passes the schema as the response format to Claude, and asks Vertex Claude for JSON in its system message.
    """
    return {
        f"helper_{name}_anthropic": _helper(entry_point, call, answer),
        f"helper_{name}_vertexai_claude": _helper(entry_point, call, answer, model=_VERTEX_CLAUDE),
    }


def _voice_config() -> dict[str, Any]:
    return {
        "agents": {
            "assistant": {
                "display_name": "Assistant",
                "role": "Answer questions about the team handbook.",
                "instructions": ["Answer in at most three paragraphs."],
                "tools": ["calculator"],
                "context_files": ["SOUL.md"],
                "learning": False,
            },
        },
        "memory": {"backend": "none"},
        "models": {"default": _ANTHROPIC},
        "timezone": "UTC",
    }


@asynccontextmanager
async def _caller_admitted() -> AsyncIterator[bool]:
    yield True


@dataclass
class _CallToolSupport(ToolRuntimeModelBinding):
    """The call manager's tool support, reduced to the context and identity one call binds."""

    context: Callable[[MessageTarget, str], ToolRuntimeContext]
    identity: ToolExecutionIdentity

    def build_context(self, target: MessageTarget, *, user_id: str, **_kwargs: object) -> ToolRuntimeContext:
        return self.context(target, user_id)

    def build_execution_identity(self, **_kwargs: object) -> ToolExecutionIdentity:
        return self.identity

    async def run_in_context[T](self, *, operation: Callable[[], Awaitable[T]], **_kwargs: object) -> T:
        return await operation()


async def _build_voice_call(root: Path, **call: object) -> tuple[dict[str, Any], CallAgentTooling]:
    authored = _voice_config()
    config, runtime_paths = _bound_config(root, authored)
    _write_workspace_files(runtime_paths, "assistant")
    identity = build_tool_execution_identity(
        channel="matrix",
        agent_name="assistant",
        runtime_paths=runtime_paths,
        requester_id=_REQUESTER_ID,
        room_id=_ROOM_ID,
        thread_id=None,
        resolved_thread_id=None,
        session_id=_CALL_SESSION_ID,
    )
    support = _CallToolSupport(
        lambda target, user_id: make_test_tool_runtime_context(
            agent_name="assistant",
            target=target,
            requester_id=user_id,
            client=SimpleNamespace(user_id="@mindroom_assistant:localhost"),
            config=config,
            runtime_paths=runtime_paths,
            relations=make_relation_lookup(),
            conversation_reader=make_conversation_reader_mock(),
        ),
        identity,
    )
    tooling = await build_call_tools(
        agent_name="assistant",
        config=config,
        runtime_paths=runtime_paths,
        tool_support=support,
        room_id=_ROOM_ID,
        requester_id=_REQUESTER_ID,
        authorize_operation=_caller_admitted,
        session_id=_CALL_SESSION_ID,
        **call,
    )
    return {"config": authored, "workspace_files": _WORKSPACE_FILES, "call": call}, tooling


async def _run_voice_realtime(root: Path) -> ScenarioOutput:
    scenario_input, tooling = await _build_voice_call(root)
    exported = {"instructions": tooling.instructions, "tools": [tool.info.raw_schema for tool in tooling.tools]}
    return ScenarioOutput(
        {**scenario_input, "entry_point": "mindroom.matrix_rtc.call_tools.build_call_tools"},
        exported,
    )


async def _run_voice_cascaded(root: Path) -> ScenarioOutput:
    call = {
        "enable_responder": True,
        "voice_instructions": "Speak briefly; the caller is on a phone call.",
        "origin_brief": "The caller started this call from the lobby room.",
    }
    scenario_input, tooling = await _build_voice_call(root, **call)
    assert tooling.get_system_prompt is not None
    assert tooling.responder is not None
    assert tooling.close is not None
    transcript = "What is the travel policy?"
    try:
        system_prompt = await tooling.get_system_prompt()
        response = await tooling.responder(transcript, None)
        assert response.text == "ok", response
    finally:
        await tooling.close()
    return ScenarioOutput(
        {**scenario_input, "transcript": transcript, "entry_point": "mindroom.matrix_rtc.call_tools.build_call_tools"},
        {"system_prompt": system_prompt},
    )


def _workflow_config() -> dict[str, Any]:
    return {
        "agents": {
            "assistant": {
                "display_name": "Assistant",
                "role": "Answer questions about the team handbook.",
                "tools": ["dynamic_workflow"],
                "learning": False,
            },
            "researcher": {
                "display_name": "Researcher",
                "role": "Research topics in depth.",
                "instructions": ["Name your sources."],
                "tools": ["calculator"],
                # A room-agent participant must answer this requester in this room.
                "access": {"users": [_REQUESTER_ID]},
                "learning": False,
            },
        },
        "memory": {"backend": "none"},
        "models": {"default": _ANTHROPIC},
        "timezone": "UTC",
    }


async def _run_dynamic_workflow(root: Path) -> ScenarioOutput:
    authored = _workflow_config()
    config, runtime_paths = _bound_config(root, authored)
    registry = entity_identity_registry(config, runtime_paths)
    room = nio.MatrixRoom(room_id=_ROOM_ID, own_user_id=registry.current_id("assistant").full_id)
    for agent_name in ("assistant", "researcher"):
        room.add_member(registry.current_id(agent_name).full_id, config.agents[agent_name].display_name, None)
    room.members_synced = True
    context = replace(_tool_runtime(config, runtime_paths, "assistant"), room=room)
    participant = {"id": "writer_a", "kind": "room_agent", "agent": "researcher"}
    prompt = "Summarize the travel policy."
    output = await _aexecute_room_agent_participant(context, participant, prompt, run_scope="travel-report:run_1")
    assert output == "ok", output
    return ScenarioOutput(
        {
            "config": authored,
            "participant": participant,
            "prompt": prompt,
            "run_scope": "travel-report:run_1",
            "entry_point": "mindroom.custom_tools.dynamic_workflow._aexecute_room_agent_participant",
        },
    )


def _skill_learning_config() -> dict[str, Any]:
    return {
        "agents": {
            "assistant": {
                "display_name": "Assistant",
                "role": "Answer questions about the team handbook.",
                "tools": ["calculator"],
                "skill_learning": {"enabled": True},
                "learning": False,
            },
        },
        "memory": {"backend": "none"},
        "models": {"default": _ANTHROPIC},
        "timezone": "UTC",
    }


async def _run_skill_review_fork(root: Path) -> ScenarioOutput:
    authored = _skill_learning_config()
    config, runtime_paths = _bound_config(root, authored)
    _write_workspace_files(runtime_paths, "assistant")
    capture = SkillReviewCapture()
    prompt = "How do I file an expense report?"
    identity = await _agent_turn(config, runtime_paths, "assistant", prompt, skill_review_capture=capture)
    assert capture.latest is not None
    skills_root = agent_workspace_skills_root(
        runtime_paths,
        "assistant",
        workspace_root=agent_workspace_root_path(runtime_paths.storage_root, "assistant"),
    )
    review = review_conversation(
        config=config,
        runtime_paths=runtime_paths,
        agent_name="assistant",
        session_id=_SESSION_ID,
        identity=identity,
        skills_root=skills_root,
        captured=capture.latest,
        progress=ReviewProgress(),
    )
    # The review runner starts each review in a fresh context, apart from the response's turn state.
    await asyncio.create_task(review, context=contextvars.Context())
    return ScenarioOutput(
        {
            "config": authored,
            "workspace_files": _WORKSPACE_FILES,
            "turn": _turn_input(prompt),
            "review_entry_point": "mindroom.skill_learning.reviewer.review_conversation",
        },
    )


SCENARIOS: dict[str, Scenario] = {
    "agent_anthropic": Scenario(
        "Representative Claude agent answering in a Matrix thread: role, instructions, markdown, knowledge, skills, "
        "toolkit instructions, workspace context, file memory, delegation, a natively deferred toolkit, and hook "
        "enrichment; it calls one tool, so the second request carries the tool result.",
        lambda root: _run_full_agent(root, _ANTHROPIC),
        answer_after_one_tool_call("add", {"a": 2, "b": 3}),
    ),
    "agent_openai_chat": Scenario(
        "The representative agent on OpenAI Chat Completions, whose deferred toolkit uses portable tool search.",
        lambda root: _run_full_agent(root, _OPENAI_CHAT),
    ),
    "agent_minimal_mode": Scenario(
        "A shell agent in minimal mode: one Bash facade and the agent context as context documents.",
        lambda root: _run_shell_agent(
            root,
            agent_mode="minimal",
            minimal_instructions=["Prefer read-only commands."],
        ),
    ),
    "agent_cli_shell": Scenario(
        "A standard shell agent whose shell commands can call its other tools through the mindroom-agent CLI.",
        lambda root: _run_shell_agent(root, agent_mode="standard"),
    ),
    "team_coordinate": Scenario(
        "Configured coordinate team: the leader's roster prompt and delegate_task_to_member schema, two parallel "
        "member runs, and the leader's request with both member results.",
        lambda root: _run_team(root, TeamMode.COORDINATE),
        answer_as_team,
    ),
    "team_collaborate": Scenario(
        "Configured collaborate team: the leader's roster prompt and delegate_task_to_members schema, both member "
        "runs, and the leader's request with the combined member result.",
        lambda root: _run_team(root, TeamMode.COLLABORATE),
        answer_as_team,
    ),
    "voice_call_realtime": Scenario(
        "Realtime voice call: the chat system prompt and the tool schemas handed to the realtime model.",
        _run_voice_realtime,
    ),
    "voice_call_cascaded": Scenario(
        "Cascaded voice call: the Live model's system prompt and one transcript answered by the call's reused agent.",
        _run_voice_cascaded,
    ),
    "dynamic_workflow_room_agent": Scenario(
        "Dynamic workflow room-agent participant: a restricted agent without runtime capabilities runs one step.",
        _run_dynamic_workflow,
    ),
    "skill_review_fork": Scenario(
        "Skill review forking a response's final request: the response's request, then the review's request with "
        "the same messages, the review prompt appended, and the agent's tools.",
        _run_skill_review_fork,
    ),
    **_structured_helper(
        "routing",
        "mindroom.routing.suggest_responder",
        _routing,
        '{"entity_name": "assistant", "reasoning": "Handbook questions."}',
    ),
    **_structured_helper(
        "team_mode",
        "mindroom.teams._select_team_mode",
        _team_mode,
        '{"mode": "coordinate", "reasoning": "Separate sub-tasks."}',
    ),
    **_structured_helper(
        "schedule_parse",
        "mindroom.scheduling.tasks._parse_workflow_schedule",
        _schedule_parse,
        '{"schedule_type": "cron", "cron_schedule": {"minute": "0", "hour": "9", "day": "*", "month": "*", '
        '"weekday": "1-5"}, "message": "@assistant What changed in the travel policy?", '
        '"description": "Weekday travel policy check"}',
    ),
    **_structured_helper(
        "thread_summary",
        "mindroom.thread_summary._generate_summary",
        _thread_summary,
        json.dumps({"summary": _SUMMARY}),
    ),
    **_structured_helper(
        "thread_enrichment",
        "mindroom.thread_summary._generate_summary",
        _thread_enrichment,
        json.dumps({"summary": _SUMMARY, "tags": ["travel"]}),
    ),
    **_structured_helper(
        "topic",
        "mindroom.topic_generator.generate_room_topic_ai",
        _room_topic,
        '{"topic": "Lobby - team questions"}',
    ),
    **_structured_helper(
        "knowledge_graph",
        "mindroom.knowledge_graph.clients.MindRoomLLMClient.generate_response",
        _knowledge_graph,
        '{"extracted_entities": [{"name": "Alice", "entity_type_id": 0}]}',
    ),
    "helper_memory_flush": _helper(
        "mindroom.memory.auto_flush._extract_memory_summary",
        _memory_flush,
        "The user prefers aisle seats.",
    ),
    "helper_voice_normalizer": _helper(
        "mindroom.voice.handler._process_transcription",
        _voice_normalizer,
        "@assistant what is the travel policy",
    ),
}


async def capture(name: str, root: Path) -> dict[str, Any]:
    """Run one scenario under ``root`` and return its normalized golden."""
    scenario = SCENARIOS[name]
    recorder = Recorder(responder=scenario.responder)
    with recorder.installed():
        output = await scenario.run(root)
    golden = {
        "scenario": name,
        "description": scenario.description,
        "input": output.input,
        "requests": [_call_dict(call) for call in recorder.ordered_calls()],
    }
    if output.exported is not None:
        golden["exported"] = output.exported
    return normalize(golden, root)


def write_fixture(name: str, golden: dict[str, Any]) -> None:
    """Write one golden as stable, reviewable JSON."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(golden, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    (FIXTURE_DIR / f"{name}.json").write_text(text, encoding="utf-8")


async def _capture_all(names: list[str]) -> None:
    for name in names:
        with tempfile.TemporaryDirectory(prefix="prompt-golden-") as root:
            write_fixture(name, await capture(name, Path(root)))


def main() -> None:
    """Capture the named scenarios, or all of them."""
    asyncio.run(_capture_all(sys.argv[1:] or sorted(SCENARIOS)))


if __name__ == "__main__":
    main()
