"""The prompt-golden scenarios: MindRoom's production entry points driven against a recording model.

Each scenario builds its agents, teams, or helper agents through the production builders and runs one turn; the
recorder keeps every request the model received: messages, tools, tool choice, and response format.
Requests MindRoom sends through its own provider layer, the helper calls, are recorded below the request pipeline,
where the provider adapter would receive them; the rest are still recorded at the Agno model call.
`tests/fixtures/agno_parity/README.md` (Prompt goldens) describes the goldens these scenarios pin.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import re
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
from agno.media import Image
from agno.models.response import ModelResponse
from agno.session.agent import AgentSession
from fastapi import FastAPI
from fastapi.testclient import TestClient
from graphiti_core.prompts.extract_nodes import ExtractedEntities
from graphiti_core.prompts.models import Message as GraphitiMessage
from pydantic import BaseModel

from mindroom import ai
from mindroom.agent_cli.session import TurnToolRegistry
from mindroom.api import openai_compat
from mindroom.api.main import initialize_api_app
from mindroom.budgets.monitor import BudgetMonitor
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.dynamic_workflow import _aexecute_room_agent_participant
from mindroom.entity_resolution import entity_identity_registry
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.hooks import EnrichmentItem
from mindroom.knowledge_graph.clients import MindRoomLLMClient
from mindroom.llm.messages import Message as OwnedMessage
from mindroom.llm.provider import TextDelta, TurnCompleted
from mindroom.matrix_rtc.call_tools import CallAgentTooling, build_call_tools
from mindroom.media_inputs import MediaInputs
from mindroom.memory.auto_flush import _extract_memory_summary
from mindroom.message_target import MessageTarget
from mindroom.participation import ParticipationGate
from mindroom.providers import model_loading, registry
from mindroom.providers.claude_prompt_cache import claude_deferred_tool_names
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
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence

    from agno.models.base import Model
    from agno.models.message import Message

    from mindroom.agent_modes import AgentMode
    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import Capabilities, ModelIdentity, ModelRequest, StreamEvent
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
_CEREBRAS = {"provider": "cerebras", "id": "qwen-3.8-27b", "extra_kwargs": {"api_key": "test-key"}}
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
# A delegation's tool result names its record, filed under the day the delegation started, by its random id.
_DELEGATION_RECORD_DATE = re.compile(r"(?<=\.mindroom/delegations/)\d{4}-\d{2}-\d{2}(?=/)")
_HEX_ID = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")


@dataclass(frozen=True)
class ModelCall:
    """One request the recording model received, as the provider adapter would see it."""

    # The stored ``model`` and ``model_provider`` of the model that received the request.
    model_id: str
    model_provider: str
    # Models in the order MindRoom built them, which is deterministic where concurrent calls are not.
    model_index: int
    stream: bool
    messages: list[Message] | list[OwnedMessage]
    tools: list[dict[str, Any]] | None
    tool_choice: str | dict[str, Any] | None
    response_format: dict[str, Any] | type[BaseModel] | None
    # Claude's request hook sends these tools with defer_loading below the recorder, so the record names them.
    deferred_tool_names: list[str]


type Responder = Callable[[ModelCall], ModelResponse]


def answer_ok(_call: ModelCall) -> ModelResponse:
    """Answer every request with one short assistant message."""
    return ModelResponse(role="assistant", content="ok")


@dataclass
class Recorder:
    """Replace every provider call MindRoom loads with a recorder that answers through ``responder``."""

    responder: Responder = answer_ok
    calls: list[ModelCall] = field(default_factory=list)
    _built_models: int = field(default=0, init=False)

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
            model_id=model.id,
            model_provider=model.provider,
            model_index=index,
            stream=stream,
            # The loop keeps mutating its list after the request, so the record keeps copies.
            messages=[message.model_copy(deep=True) for message in messages],
            tools=deepcopy(kwargs.get("tools")),
            tool_choice=deepcopy(kwargs.get("tool_choice")),
            response_format=kwargs.get("response_format"),
            deferred_tool_names=sorted(claude_deferred_tool_names(model)),
        )
        self.calls.append(call)
        return self.responder(call)

    def _respond_to_request(self, identity: ModelIdentity, index: int, request: ModelRequest) -> ModelResponse:
        # Helper requests offer no tools; recording tool definitions here waits for the agent requests.
        assert not request.tools, request.tools
        call = ModelCall(
            model_id=identity.id,
            model_provider=identity.provider,
            model_index=index,
            stream=request.stream,
            messages=deepcopy(list(request.messages)),
            tools=[],
            tool_choice=deepcopy(request.tool_choice),
            response_format=request.response_format,
            deferred_tool_names=sorted(request.deferred_tool_names),
        )
        self.calls.append(call)
        return self.responder(call)

    def _next_index(self) -> int:
        index = self._built_models
        self._built_models += 1
        return index

    def _bind(self, model: Model) -> Model:
        respond = partial(self._respond, model, self._next_index())

        async def ainvoke(messages: list[Message], **kwargs: Any) -> ModelResponse:  # noqa: ANN401
            return respond(stream=False, messages=messages, kwargs=kwargs)

        async def ainvoke_stream(messages: list[Message], **kwargs: Any) -> AsyncIterator[ModelResponse]:  # noqa: ANN401
            yield respond(stream=True, messages=messages, kwargs=kwargs)

        # Bound before MindRoom installs its request hooks, so every hook wraps the recorder like the provider call.
        model.__dict__.update(ainvoke=ainvoke, ainvoke_stream=ainvoke_stream)
        return model

    def _bind_adapter(self, adapter: Any) -> _RecordingAdapter:  # noqa: ANN401 - any family's adapter
        return _RecordingAdapter(adapter, partial(self._respond_to_request, adapter.identity, self._next_index()))

    @contextmanager
    def installed(self) -> Iterator[None]:
        """Record every model ``model_loading.get_model_instance`` and every adapter the provider registry builds."""
        create_model = model_loading._create_model_for_provider
        build_adapter = registry._build_adapter

        def create_recording_model(*args: Any, **kwargs: Any) -> Model:  # noqa: ANN401
            return self._bind(create_model(*args, **kwargs))

        def build_recording_adapter(*args: Any, **kwargs: Any) -> _RecordingAdapter:  # noqa: ANN401
            return self._bind_adapter(build_adapter(*args, **kwargs))

        with (
            patch.object(model_loading, "_create_model_for_provider", create_recording_model),
            patch.object(registry, "_build_adapter", build_recording_adapter),
        ):
            yield


@dataclass(frozen=True)
class _RecordingAdapter:
    """A built provider adapter whose requests the recorder answers instead of the provider.

    The registry wraps it in the request pipeline as it wraps the adapter, so every request is recorded as the
    adapter would receive it, and the identity, capabilities, and settings are the configured route's own.
    """

    adapter: Any
    respond: Callable[[ModelRequest], ModelResponse]

    @property
    def identity(self) -> ModelIdentity:
        """The adapter's identity."""
        return self.adapter.identity

    @property
    def capabilities(self) -> Capabilities:
        """The adapter's capabilities."""
        return self.adapter.capabilities

    @property
    def settings(self) -> object:
        """The adapter's settings, which the request log records."""
        return self.adapter.settings

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Answer the request with the responder's message as one finished turn."""
        response = self.respond(request)
        if response.content:
            yield TextDelta(response.content)
        message = OwnedMessage(role="assistant", content=response.content, tool_calls=response.tool_calls or None)
        yield TurnCompleted(message=message, stop_reason="tool_use" if response.tool_calls else "end")

    def format_tool_results(self, messages: Sequence[OwnedMessage], results: Sequence[OwnedMessage]) -> list[Any]:
        """Format tool results the adapter's way."""
        return self.adapter.format_tool_results(messages, results)


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


def message_dict(message: Message | OwnedMessage) -> dict[str, Any]:
    """Return a request message as the prompt goldens record it: no volatile keys, transient context marked."""
    data = {key: value for key, value in message.to_dict().items() if key not in {"id", "created_at", "metrics"}}
    if not message.add_to_agent_memory:
        data["add_to_agent_memory"] = False
    return data


def response_format_dict(response_format: object) -> object:
    """Return a request's response format as the prompt goldens record it."""
    if isinstance(response_format, type) and issubclass(response_format, BaseModel):
        return {"pydantic_model": response_format.__name__, "json_schema": response_format.model_json_schema()}
    return response_format


def call_dict(call: ModelCall) -> dict[str, Any]:
    """Return one recorded request as the prompt goldens record it."""
    return {
        "model": {"id": call.model_id, "provider": call.model_provider},
        "stream": call.stream,
        "messages": [message_dict(message) for message in call.messages],
        "tools": call.tools,
        "tool_choice": call.tool_choice,
        "response_format": response_format_dict(call.response_format),
        "deferred_tool_names": call.deferred_tool_names,
    }


def normalize(value: object, root: Path) -> object:
    """Replace what changes between captures: the capture's temporary root, today's date, and random ids."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    for path in {str(root.resolve()), str(root)}:
        text = text.replace(path, "<TMP>")
    text = _DELEGATION_RECORD_DATE.sub("<DATE>", _DATE_PATTERN.sub("<DATE>", text))
    ids: dict[str, str] = {}
    text = _HEX_ID.sub(lambda match: ids.setdefault(match.group(), f"<ID-{len(ids) + 1}>"), text)
    return json.loads(text)


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
    participation: ParticipationGate | None = None,
    media: MediaInputs | None = None,
    run_id: str | None = None,
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
            run_id=run_id,
            transient_enrichment_items=transient_enrichment,
            system_enrichment_items=system_enrichment,
        ),
        agent_mode=agent_mode,
        skill_review_capture=skill_review_capture,
        participation=participation,
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
            media=media,
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


def _turn_input(prompt: str) -> dict[str, Any]:
    return {
        "entry_point": "mindroom.ai.stream_agent_response",
        "api_server": "127.0.0.1:8765",
        "room_id": _ROOM_ID,
        "thread_id": _THREAD_ID,
        "requester_id": _REQUESTER_ID,
        "thread_history": [_EARLIER_MESSAGE],
        "prompt": prompt,
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
        return answer_ok(call)
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
    with _api_server(), tool_runtime_context(runtime):
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
            return answer_ok(call)
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
    """Capture a structured-output helper on each of Agno's three structured-output paths.

    Agno passes the schema as the response format to Claude, asks Vertex Claude for JSON in its system message, and
    sends Cerebras a JSON-schema response format without the JSON prompt.
    """
    return {
        f"helper_{name}_anthropic": _helper(entry_point, call, answer),
        f"helper_{name}_vertexai_claude": _helper(entry_point, call, answer, model=_VERTEX_CLAUDE),
        f"helper_{name}_cerebras": _helper(entry_point, call, answer, model=_CEREBRAS),
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


def _assistant_config(*, model: dict[str, Any] = _ANTHROPIC, **agent: object) -> dict[str, Any]:
    """Return a one-agent config without default tools, which keeps the scenario's requests small."""
    return {
        "defaults": {"tools": []},
        "agents": {
            "assistant": {
                "display_name": "Assistant",
                "role": "Answer questions about the team handbook.",
                "tools": ["calculator"],
                "learning": False,
                **agent,
            },
        },
        "memory": {"backend": "none"},
        "models": {"default": model},
        "timezone": "UTC",
    }


def _last_user_text(call: ModelCall) -> str:
    return next((str(message.content) for message in reversed(call.messages) if message.role == "user"), "")


_PARTICIPATION_INSTRUCTIONS = "Reply only when the message asks about the handbook."


def answer_participation(call: ModelCall) -> ModelResponse:
    """Decide to respond on the decision request, which alone disables tool choice, then answer."""
    if call.tool_choice == "none":
        return ModelResponse(role="assistant", content='{"action": "respond", "reason": "A direct handbook question."}')
    return answer_ok(call)


async def _run_participation_decision(root: Path) -> ScenarioOutput:
    authored = _assistant_config(participation={"instructions": _PARTICIPATION_INSTRUCTIONS})
    config, runtime_paths = _bound_config(root, authored)
    prompt = "Does the handbook cover conference travel?"
    # The response runner's gate for an agent whose participation config sets no judgment backend.
    gate = ParticipationGate(instructions=_PARTICIPATION_INSTRUCTIONS)
    await _agent_turn(config, runtime_paths, "assistant", prompt, participation=gate)
    assert gate.approved, gate.decision
    return ScenarioOutput({"config": authored, "turn": _turn_input(prompt)})


_COMPACT_PROMPT = "Please compact our conversation before we continue."
_COMPACTION_SUMMARY_TEXT = "Alice asked the assistant to compact the conversation; it scheduled compaction."


def answer_compaction(call: ModelCall) -> ModelResponse:
    """Call compact_context on the first turn, summarize on the summary request, and answer the rest."""
    if not call.stream:
        return ModelResponse(role="assistant", content=_COMPACTION_SUMMARY_TEXT)
    if not _answers_tools(call) and _COMPACT_PROMPT in _last_user_text(call):
        return ModelResponse(role="assistant", tool_calls=[_tool_call("call_compact", "compact_context", {})])
    return answer_ok(call)


async def _run_compaction_summary(root: Path) -> ScenarioOutput:
    authored = _assistant_config(model={**_ANTHROPIC, "context_window": 200000}, tools=["compact_context"])
    config, runtime_paths = _bound_config(root, authored)
    follow_up = "What is the travel policy?"
    # The summary input names the compacted run, so the turns use fixed run ids.
    await _agent_turn(config, runtime_paths, "assistant", _COMPACT_PROMPT, run_id="run-compact")
    # The next reply in the scope compacts its history first, with a non-streaming summary request.
    await _agent_turn(config, runtime_paths, "assistant", follow_up, run_id="run-follow-up")
    return ScenarioOutput({"config": authored, "turns": [_turn_input(_COMPACT_PROMPT), _turn_input(follow_up)]})


_CRITIC_PROFILE = (
    "---\ndescription: Finds the biggest risk in a plan.\ntools: [calculator]\n---\n"
    "You are a careful critic. Name the single biggest risk.\n"
)


def answer_as_delegating_parent(call: ModelCall) -> ModelResponse:
    """Delegate to the researcher, then to the critic profile, then answer; children answer directly."""
    if "run_subagent" not in _tool_names(call):
        return answer_ok(call)
    results = sum(1 for message in call.messages if message.role == "tool")
    if results == 0:
        arguments: dict[str, object] = {"agent_name": "researcher", "task": "Summarize the travel policy."}
        return ModelResponse(role="assistant", tool_calls=[_tool_call("call_child", "run_subagent", arguments)])
    if results == 1:
        arguments = {"profile": "critic", "task": "Name the biggest risk in the travel policy."}
        return ModelResponse(role="assistant", tool_calls=[_tool_call("call_persona", "run_subagent", arguments)])
    return answer_ok(call)


async def _run_delegated_child_run(root: Path) -> ScenarioOutput:
    authored = {
        "defaults": {"tools": []},
        "agents": {
            "assistant": {
                "display_name": "Assistant",
                "role": "Answer questions about the team handbook.",
                "tools": ["calculator"],
                "delegate_to": ["assistant", "researcher"],
                "memory_backend": "file",
                # A delegated child must be allowed to answer this requester.
                "access": {"users": [_REQUESTER_ID]},
                "learning": False,
            },
            "researcher": {
                "display_name": "Researcher",
                "role": "Research topics in depth.",
                "instructions": ["Name your sources."],
                "tools": ["calculator"],
                "access": {"users": [_REQUESTER_ID]},
                "learning": False,
            },
        },
        "memory": {"backend": "none"},
        "models": {"default": _ANTHROPIC},
        "timezone": "UTC",
    }
    config, runtime_paths = _bound_config(root, authored)
    workspace_files = {"subagents/critic.md": _CRITIC_PROFILE}
    profile = agent_workspace_root_path(runtime_paths.storage_root, "assistant") / "subagents" / "critic.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(_CRITIC_PROFILE, encoding="utf-8")
    prompt = "Check the travel policy and its biggest risk."
    await _agent_turn(config, runtime_paths, "assistant", prompt)
    return ScenarioOutput({"config": authored, "workspace_files": workspace_files, "turn": _turn_input(prompt)})


_PNG_PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
)


async def _run_media_current_message(root: Path) -> ScenarioOutput:
    authored = _assistant_config()
    config, runtime_paths = _bound_config(root, authored)
    prompt = "What does this picture show?"
    image = Image(id="parity-image", content=_PNG_PIXEL, mime_type="image/png", format="png")
    await _agent_turn(config, runtime_paths, "assistant", prompt, media=MediaInputs(images=[image]))
    return ScenarioOutput(
        {
            "config": authored,
            "turn": _turn_input(prompt),
            "media": {"images": [{"id": image.id, "mime_type": "image/png"}]},
        },
    )


def _post_openai_compat(config: Config, runtime_paths: RuntimePaths, body: dict[str, Any]) -> None:
    """Send one request to the OpenAI-compatible API, as a chat client such as LibreChat does."""
    app = FastAPI()
    app.include_router(openai_compat.router)
    initialize_api_app(app, runtime_paths)
    with (
        patch("mindroom.api.openai_compat._load_config", return_value=(config, runtime_paths)),
        TestClient(app, base_url="http://localhost") as client,
    ):
        response = client.post("/v1/chat/completions", headers={"X-Session-Id": "api-travel"}, json=body)
    assert response.status_code == 200, response.text


async def _run_openai_compat_api(root: Path) -> ScenarioOutput:
    authored = _assistant_config()
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=root / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
            "OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true",
        },
    )
    config = bind_runtime_paths(Config.model_validate(authored), runtime_paths)
    body = {
        "model": "assistant",
        "stream": True,
        "messages": [
            {"role": "user", "content": "Hi there."},
            {"role": "assistant", "content": "Hello! How can I help?"},
            {"role": "user", "content": "What is the travel policy?"},
        ],
    }
    await asyncio.to_thread(_post_openai_compat, config, runtime_paths, body)
    return ScenarioOutput(
        {
            "config": authored,
            "request": {"headers": {"X-Session-Id": "api-travel"}, "json": body},
            "entry_point": "POST /v1/chat/completions",
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
    "participation_decision": Scenario(
        "Participation check of an agent with participation config and no judgment backend: the reply model's "
        "non-streaming decision request with tool_choice none and the decision prompt appended, then the reply.",
        _run_participation_decision,
        answer_participation,
    ),
    "compaction_summary": Scenario(
        "History compaction: a turn calls compact_context, so the next reply first sends the non-streaming summary "
        "request, then replies with the summary in place of the compacted run.",
        _run_compaction_summary,
        answer_compaction,
    ),
    "delegated_child_run": Scenario(
        "Delegation: run_subagent to a configured agent, then to an authored subagent profile from the workspace, "
        "each child's request, and the parent's requests with both results.",
        _run_delegated_child_run,
        answer_as_delegating_parent,
    ),
    "media_current_message": Scenario(
        "An image attached to the current Matrix message, carried on the current user message.",
        _run_media_current_message,
    ),
    "openai_compat_api": Scenario(
        "A streamed /v1/chat/completions request with earlier turns, through the OpenAI-compatible API path.",
        _run_openai_compat_api,
    ),
}


def load_golden(name: str) -> dict[str, Any]:
    """Return one prompt golden."""
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
