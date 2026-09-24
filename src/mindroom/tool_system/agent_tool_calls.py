"""Qualified tools bound to one live Agent turn, independent of presentation.

The catalog lock serializes materialization and execution. A caller's outer
Bash-window lock must be distinct: Bash waits for calls using this lock.
Authorization and durable continuation remain responsibilities of the turn owner.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from functools import wraps
from typing import TYPE_CHECKING, Literal

from agno.models.response import ModelResponse, ModelResponseEvent, ToolExecution
from agno.run.requirement import RunRequirement
from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

from mindroom.agent_cli.worker_protocol import SHELL_OPERATION_NAMES
from mindroom.agno_compat_cli_checkpoint import inner_cli_dispatch
from mindroom.agno_compat_prepared_tools import OwnedAgentFunctionCall, prepare_agent_tools, temporary_tool_instructions
from mindroom.background_tasks import wait_for_future_until_complete
from mindroom.tool_system.context_bound_streams import closing_async_stream, context_bound_async_stream
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.tool_system.tool_access import (
    ToolDescriptor,
    ToolKey,
    function_schema,
    validate_tool_arguments,
)
from mindroom.tool_system.tool_hooks import SyncToolCompletionTracker, track_sync_tool_completion
from mindroom.tool_system.worker_routing import tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

    from agno.agent import Agent
    from agno.models.message import Message
    from agno.run import RunContext
    from agno.run.agent import RunOutput, RunOutputEvent
    from agno.run.team import TeamRunOutputEvent
    from agno.session.agent import AgentSession

    from mindroom.tool_system.runtime_context import ToolRuntimeContext


@dataclass(frozen=True, slots=True)
class DeferredAgentToolkit:
    """Metadata and the owning turn's normal factory; listing never calls it.

    Factory includes normal configuration, filtering, hooks and connection.
    Materialized toolkits join Agent.tools and use its normal cleanup.
    """

    name: str
    description: str
    materialize: Callable[[], Awaitable[Toolkit]]


@dataclass(frozen=True, slots=True)
class PreparedAgentToolBinding:
    """A prepared Function and the catalog owning its exact live Agent turn, never a new Agent."""

    key: ToolKey
    function: Function
    catalog: PreparedAgentToolCatalog = field(repr=False)


@dataclass(frozen=True, slots=True)
class AgentToolCallEvent:
    """In-process event retaining Agno objects; transports must project safely."""

    kind: Literal["started", "progress", "media", "waiting", "continuation_required", "completed", "failed"]
    call_id: str
    key: ToolKey
    execution: ToolExecution | None = None
    requirement: RunRequirement | None = None
    media: ModelResponse | None = None
    progress: ModelResponse | RunOutputEvent | TeamRunOutputEvent | None = None


@dataclass
class PreparedAgentToolCatalog:
    """One turn's catalog; prepare only the effective tools from Agent.aget_tools."""

    agent: Agent
    run_context: RunContext
    run_response: RunOutput
    session: AgentSession
    runtime_context: ToolRuntimeContext
    _bindings: dict[ToolKey, PreparedAgentToolBinding] = field(default_factory=dict, init=False)
    _instructions: dict[ToolKey, tuple[str, ...]] = field(default_factory=dict, init=False)
    _deferred: dict[str, DeferredAgentToolkit] = field(default_factory=dict, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _call_count: int = field(default=0, init=False)
    _closed: bool = field(default=False, init=False)

    @contextmanager
    def execution_context(self) -> Iterator[None]:
        """Bind trusted runtime and credential routing only while doing owner work."""
        with (
            tool_runtime_context(self.runtime_context),
            tool_execution_identity(build_execution_identity_from_runtime_context(self.runtime_context)),
        ):
            yield

    def owns(self, binding: PreparedAgentToolBinding) -> bool:
        """Check the exact prepared binding, including its live catalog lifetime."""
        return not self._closed and self._bindings.get(binding.key) is binding

    @asynccontextmanager
    async def dispatch(
        self,
        binding: PreparedAgentToolBinding,
        authorize: Callable[[], Awaitable[None]] | None,
    ) -> AsyncIterator[None]:
        """Serialize ordinary or external execution under the same authority gate."""
        async with self._lock:
            self._check_open()
            if not self.owns(binding):
                msg = "Stale Agent tool binding"
                raise ValueError(msg)
            if authorize is not None:
                await authorize()
            yield

    def add_deferred(self, toolkit: DeferredAgentToolkit) -> None:
        """Register metadata before dispatch; reject reserved or duplicate owners."""
        self._check_open()
        if toolkit.name == "agent":
            msg = "The agent toolkit namespace is reserved for generated functions"
            raise ValueError(msg)
        if toolkit.name in self._deferred or any(key.toolkit == toolkit.name for key in self._bindings):
            msg = f"Duplicate toolkit: {toolkit.name}"
            raise ValueError(msg)
        self._deferred[toolkit.name] = toolkit

    def _check_open(self) -> None:
        if self._closed:
            msg = "Agent tool catalog is closed"
            raise RuntimeError(msg)

    async def prepare(self, processed_tools: list[Toolkit | Callable | Function | dict]) -> None:
        """Prepare qualified entries separately so Agno cannot flatten collisions."""
        async with self._lock:
            self._check_open()
            with self.execution_context():
                self._prepare(processed_tools)

    def _prepare(self, processed_tools: list[Toolkit | Callable | Function | dict], owner: str | None = None) -> None:
        bindings: dict[ToolKey, PreparedAgentToolBinding] = {}
        instructions: dict[ToolKey, tuple[str, ...]] = {}
        authored_ids = {id(tool) for tool in self.agent.tools or []} if isinstance(self.agent.tools, list) else set()
        for tool in processed_tools:
            if isinstance(tool, dict):
                msg = "Provider-native tools cannot execute through the Agent catalog"
                raise TypeError(msg)
            if isinstance(tool, Toolkit):
                functions = list(tool.get_async_functions().values())
                toolkit_instructions = (tool.instructions,) if tool.instructions else ()
            else:
                functions = [tool if isinstance(tool, Function) else Function.from_callable(tool)]
                source_toolkit = functions[0].source_toolkit
                toolkit_instructions = (
                    (source_toolkit.instructions,)
                    if isinstance(source_toolkit, Toolkit) and source_toolkit.instructions
                    else ()
                )
            for source in functions:
                authored = isinstance(tool, Toolkit) or id(tool) in authored_ids or source.owning_toolkit is not None
                namespace = (
                    owner
                    or source.owning_toolkit
                    or (tool.name if isinstance(tool, Toolkit) else "tools" if authored else "agent")
                )
                if namespace == "agent" and authored:
                    msg = "The agent toolkit namespace is reserved for generated functions"
                    raise ValueError(msg)
                key = ToolKey(namespace, source.name)
                if key in bindings or key in self._bindings:
                    msg = f"Duplicate qualified tool: {namespace}.{source.name}"
                    raise ValueError(msg)
                # Agent preparation may mutate instructions; hidden details must not
                # leak into provider presentation. The compatibility helper restores it.
                with temporary_tool_instructions(self.agent, ()):
                    prepared = prepare_agent_tools(
                        self.agent,
                        processed_tools=[source],
                        run_response=self.run_response,
                        run_context=self.run_context,
                        session=self.session,
                    )
                function = prepared[0]
                assert isinstance(function, Function)
                bindings[key] = PreparedAgentToolBinding(key, function, self)
                instructions[key] = toolkit_instructions + ((source.instructions,) if source.instructions else ())
        self._bindings.update(bindings)
        self._instructions.update(instructions)

    def prepared_functions(self) -> tuple[Function, ...]:
        """Expose already prepared hidden functions for the same Agent's context renderer."""
        self._check_open()
        return tuple(binding.function for binding in self._bindings.values())

    def metadata(self) -> list[dict[str, object]]:
        """Return discovery records without preparing deferred schemas."""
        self._check_open()
        return [
            {"toolkit": key.toolkit, "function": key.function, "description": binding.function.description or ""}
            for key, binding in self._bindings.items()
        ] + [
            {"toolkit": item.name, "description": item.description, "deferred": True}
            for item in self._deferred.values()
        ]

    async def bind(
        self,
        key: ToolKey,
        *,
        check_current: Callable[[], None] | None = None,
    ) -> PreparedAgentToolBinding:
        """Materialize one deferred toolkit once, under the owning turn's context."""
        async with self._lock:
            self._check_open()
            if check_current is not None:
                check_current()
            deferred = self._deferred.get(key.toolkit)
            if deferred is not None:
                with self.execution_context():
                    toolkit = await deferred.materialize()
                    # Transfer lifetime before schema processing can fail.
                    if not isinstance(self.agent.tools, list) or not any(tool is toolkit for tool in self.agent.tools):
                        self.agent.add_tool(toolkit)
                    self._prepare([toolkit], owner=deferred.name)
                    del self._deferred[key.toolkit]
            binding = self._bindings.get(key)
            if binding is None:
                msg = "Tool is unavailable"
                raise ValueError(msg)
            return binding

    async def describe(
        self,
        key: ToolKey,
        *,
        check_current: Callable[[], None] | None = None,
    ) -> ToolDescriptor:
        """Return the actual prepared schema and toolkit instructions."""
        binding = await self.bind(key, check_current=check_current)
        return ToolDescriptor(
            key,
            binding.function.description or "",
            function_schema(binding.function),
            self._instructions[key],
        )

    async def close(self) -> None:
        """Refuse new work, then wait for the operation that currently holds the catalog."""
        self._closed = True
        async with self._lock:
            pass


def _failure(
    binding: PreparedAgentToolBinding,
    call_id: str,
    arguments: dict[str, object],
    message: str,
) -> AgentToolCallEvent:
    return AgentToolCallEvent(
        "failed",
        call_id,
        binding.key,
        execution=ToolExecution(
            tool_call_id=call_id,
            tool_name=binding.key.function,
            tool_args=arguments,
            tool_call_error=True,
            result=message,
        ),
    )


def _response_events(
    binding: PreparedAgentToolBinding,
    call_id: str,
    response: ModelResponse | RunOutputEvent | TeamRunOutputEvent,
) -> Iterator[AgentToolCallEvent]:
    if not isinstance(response, ModelResponse):
        yield AgentToolCallEvent("progress", call_id, binding.key, progress=response)
    elif response.event == ModelResponseEvent.tool_call_started.value:
        assert response.tool_executions
        yield AgentToolCallEvent("started", call_id, binding.key, execution=response.tool_executions[0])
    elif response.event == ModelResponseEvent.tool_call_paused.value:
        for execution in response.tool_executions or []:
            yield AgentToolCallEvent(
                "waiting",
                call_id,
                binding.key,
                execution=execution,
                requirement=RunRequirement(execution),
            )
    elif response.event == ModelResponseEvent.tool_call_completed.value:
        assert response.tool_executions
        execution = response.tool_executions[0]
        if response.images or response.audios or response.videos or response.files:
            yield AgentToolCallEvent("media", call_id, binding.key, media=response)
        kind = (
            "continuation_required"
            if execution.stop_after_tool_call
            else "failed"
            if execution.tool_call_error
            else "completed"
        )
        yield AgentToolCallEvent(kind, call_id, binding.key, execution=execution)
    else:
        yield AgentToolCallEvent("progress", call_id, binding.key, progress=response)


def execute_agent_tool_call(
    binding: PreparedAgentToolBinding,
    call_id: str,
    arguments: dict[str, object],
    *,
    authorize: Callable[[], Awaitable[None]] | None = None,
    requirement: RunRequirement | None = None,
) -> AsyncIterator[AgentToolCallEvent]:
    """Execute a prepared call without a provider request or transcript insertion.

    Caller must authorize immediately before dispatch. Waiting and continuation
    events require the response owner's control adapter; never bypass pause checks.
    Closing/cancelling waits for a synchronous leaf before releasing the catalog.
    """
    return _execute_agent_tool_call(binding, call_id, arguments, authorize=authorize, requirement=requirement)


async def execute_agent_shell_call(
    binding: PreparedAgentToolBinding,
    call_id: str,
    arguments: dict[str, object],
    *,
    worker_leaf: Callable[[dict[str, object]], Awaitable[str]],
    authorize: Callable[[], Awaitable[None]] | None = None,
    requirement: RunRequirement | None = None,
) -> AsyncIterator[AgentToolCallEvent]:
    """Keep canonical hooks and pauses; release mutation only around pinned IO.

    Only the response owner supplies this leaf, after checking the effective
    shell toolkit. Never use it for an arbitrary tool or ordinary worker.
    """
    if binding.key.function not in SHELL_OPERATION_NAMES:
        msg = "Worker leaf replacement requires a canonical shell operation"
        raise ValueError(msg)
    stream = _execute_agent_tool_call(
        binding,
        call_id,
        arguments,
        worker_leaf=worker_leaf,
        authorize=authorize,
        requirement=requirement,
    )
    async with closing_async_stream(stream):
        async for event in stream:
            yield event


async def _execute_agent_tool_call(  # noqa: C901 - one canonical execution and cleanup lifetime
    binding: PreparedAgentToolBinding,
    call_id: str,
    arguments: dict[str, object],
    *,
    worker_leaf: Callable[[dict[str, object]], Awaitable[str]] | None = None,
    authorize: Callable[[], Awaitable[None]] | None = None,
    requirement: RunRequirement | None = None,
) -> AsyncIterator[AgentToolCallEvent]:
    catalog = binding.catalog
    async with catalog.dispatch(binding, authorize):
        if requirement is not None:
            execution = requirement.tool_execution
            if (
                execution is None
                or execution.tool_call_id != call_id
                or execution.tool_name != binding.key.function
                or json.dumps(execution.tool_args, sort_keys=True, allow_nan=False)
                != json.dumps(arguments, sort_keys=True, allow_nan=False)
                or execution.requires_confirmation is not True
                or not requirement.is_resolved()
                or requirement.confirmation is None
                or requirement.needs_user_input
                or requirement.needs_user_feedback
                or requirement.needs_external_execution
                or binding.function.requires_user_input
                or binding.function.external_execution
                or (
                    binding.function.requires_confirmation and binding.function.approval_type != execution.approval_type
                )
            ):
                msg = "Approval does not resolve this exact prepared call"
                raise ValueError(msg)
            if not requirement.confirmation:
                yield _failure(
                    binding,
                    call_id,
                    arguments,
                    requirement.confirmation_note or "Not approved by requester",
                )
                return
        try:
            validate_tool_arguments(function_schema(binding.function), arguments)
        except ValueError:
            yield _failure(binding, call_id, arguments, "Invalid tool arguments")
            return
        limit = catalog.agent.tool_call_limit
        if limit is not None and catalog._call_count >= limit:
            yield _failure(binding, call_id, arguments, "Tool call limit reached")
            return
        model = catalog.agent.model
        assert model is not None
        # A shallow execution copy preserves private prepared bindings. Receipt-owned
        # calls cannot reuse Function caches or mutate the shared control flags.
        function = binding.function.model_copy(deep=False)
        function.cache_results = False
        if worker_leaf is not None:
            original = function.entrypoint
            assert original is not None

            @wraps(original)
            async def pinned_leaf(**kwargs: object) -> str:
                catalog._lock.release()
                try:
                    return await worker_leaf(kwargs)
                finally:
                    # Hook unwinding must regain exclusive Agent ownership even
                    # under repeated cancellation of the response task.
                    await wait_for_future_until_complete(asyncio.create_task(catalog._lock.acquire()))

            function.entrypoint = pinned_leaf
        call = OwnedAgentFunctionCall(function=function, call_id=call_id, arguments=arguments)
        messages: list[Message] = []
        tracker = SyncToolCompletionTracker()

        @contextmanager
        def context() -> Iterator[None]:
            with catalog.execution_context(), track_sync_tool_completion(tracker), inner_cli_dispatch():
                yield

        stream = context_bound_async_stream(
            context_factory=context,
            stream_factory=lambda: model.arun_function_calls(
                [call],
                messages,
                current_function_call_count=catalog._call_count,
                function_call_limit=limit,
                skip_pause_check=requirement is not None,
            ),
        )
        try:
            async with closing_async_stream(stream):
                async for response in stream:
                    for event in _response_events(binding, call_id, response):
                        if event.kind == "started" or (
                            event.kind == "waiting"
                            and event.requirement is not None
                            and event.requirement.needs_external_execution
                        ):
                            catalog._call_count += 1
                        yield event
        finally:
            await _settle_call(binding, call, tracker)


async def _settle_call(
    binding: PreparedAgentToolBinding,
    call: OwnedAgentFunctionCall,
    tracker: SyncToolCompletionTracker,
) -> None:
    async def settle() -> None:
        with binding.catalog.execution_context():
            await call.close_result()
            pending = tracker.started_task()
            if pending is not None:
                await pending

    await wait_for_future_until_complete(asyncio.create_task(settle(), name="agent-tool-call-cleanup"))
