"""Qualified tools bound to one live agent turn, independent of presentation.

The catalog lock serializes materialization and execution. A caller's outer
Bash-window lock must be distinct: Bash waits for calls using this lock.
Authorization and durable continuation remain responsibilities of the turn owner.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field, replace
from functools import wraps
from typing import TYPE_CHECKING, Literal

from mindroom.agent_cli.shell_contract import SHELL_OPERATION_NAMES
from mindroom.background_tasks import wait_for_future_until_complete
from mindroom.llm.runs import RunRequirement, ToolExecution
from mindroom.tool_jobs.control import job_owns_execution
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.dedicated_threads import JOB_CALL_EXECUTOR
from mindroom.tool_system.invocation import execute_tool_call
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.tool_system.tool_access import ToolDescriptor, ToolKey, UnknownToolError, validate_tool_arguments
from mindroom.tool_system.tool_function import close_toolkits, connect_toolkits, first_of_each_name
from mindroom.tool_system.tool_hooks import SyncToolCompletionTracker, track_sync_tool_completion
from mindroom.tool_system.worker_routing import tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence

    from mindroom.agent_loop.spec import RunContext
    from mindroom.llm.runs import RunRecord
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession
    from mindroom.tool_system.invocation import ToolCallContext, ToolMiddleware
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_function import AdaptedToolkit, ToolFunction, ToolkitClose, ToolValue

_INNER: ContextVar[bool] = ContextVar("cli_inner_dispatch", default=False)


def cli_dispatch_active() -> bool:
    """Return whether a CLI-dispatched tool call is executing in this context."""
    return _INNER.get()


@contextmanager
def cli_dispatch_scope() -> Iterator[None]:
    """Mark this context as executing a CLI-dispatched tool call, which ``cli_dispatch_active`` reports."""
    token = _INNER.set(True)
    try:
        yield
    finally:
        _INNER.reset(token)


@dataclass(frozen=True, slots=True)
class DeferredAgentToolkit:
    """Metadata and the owning turn's normal factory; listing never calls it.

    Factory includes normal configuration, filtering, approval policy, and adaptation to the tools the loop runs.
    """

    name: str
    description: str
    materialize: Callable[[], Awaitable[AdaptedToolkit]]


@dataclass(frozen=True, slots=True)
class PreparedAgentToolBinding:
    """A prepared function and the catalog owning its exact live agent turn."""

    key: ToolKey
    function: ToolFunction
    catalog: PreparedAgentToolCatalog = field(repr=False)


@dataclass(frozen=True, slots=True)
class AgentToolCallEvent:
    """One step of a CLI call; ``media`` holds what a successful call returned beside its text."""

    kind: Literal["started", "media", "waiting", "continuation_required", "completed", "failed"]
    call_id: str
    key: ToolKey
    execution: ToolExecution | None = None
    requirement: RunRequirement | None = None
    media: ToolValue | None = None


@dataclass
class PreparedAgentToolCatalog:
    """One turn's catalog of the agent's tools, each under the toolkit it was authored in.

    ``run`` is the turn's run identity, whose session holds the state its calls share; that session and ``store``
    are where the run is stored, and ``record`` is the run itself once known: the live run after its first tool batch
    is planned, or the stored run a recovery continues. ``middleware`` is the build's tool-call policy, which every
    call runs through, and ``tool_call_limit`` bounds the calls of one turn. The toolkits it connects, the ones a
    run reaches only through it and every deferred toolkit it materializes, stay connected until it closes.
    """

    agent_name: str
    run: RunContext
    runtime_context: ToolRuntimeContext
    store: ConversationStore | None = None
    middleware: Sequence[ToolMiddleware] = ()
    tool_call_limit: int | None = None
    record: RunRecord | None = None
    _bindings: dict[ToolKey, PreparedAgentToolBinding] = field(default_factory=dict, init=False)
    _instructions: dict[ToolKey, tuple[str, ...]] = field(default_factory=dict, init=False)
    _deferred: dict[str, DeferredAgentToolkit] = field(default_factory=dict, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _call_count: int = field(default=0, init=False)
    _closed: bool = field(default=False, init=False)
    _closes: list[ToolkitClose] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        """Refuse a run without a session, which the catalog's checkpoints and delegations need."""
        if self.run.session is None:
            msg = "A tool catalog needs a run that belongs to a session"
            raise ValueError(msg)

    @property
    def session(self) -> ConversationSession:
        """Return the session the turn's run is stored to, which the catalog's checkpoints and delegations write."""
        # RunContext is frozen, and construction refused a run without a session.
        assert self.run.session is not None
        return self.run.session

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

    async def prepare(
        self,
        toolkits: Sequence[AdaptedToolkit],
        owners: Mapping[str, str],
        *,
        include: Callable[[ToolFunction], bool] | None = None,
    ) -> None:
        """Bind the run's functions under their authored toolkit; skip functions ``include`` rejects.

        ``owners`` maps each function the run offers to its authored toolkit; a function it does not name was
        generated for the agent, such as the knowledge search and the skill readers, and is reached as ``agent``.
        Only the functions ``first_of_each_name`` selects are bound, because only those are what the run offers.
        """
        async with self._lock:
            self._check_open()
            offered = list(
                first_of_each_name((toolkit, function) for toolkit in toolkits for function in toolkit.functions),
            )
            for toolkit in toolkits:
                functions = [
                    function
                    for owner, function in offered
                    if owner is toolkit and (include is None or include(function))
                ]
                self._prepare(toolkit, functions, owners)

    async def connect(self, toolkits: Sequence[AdaptedToolkit]) -> None:
        """Connect the toolkits among ``toolkits`` that hold a connection for one run, until the catalog closes.

        A run that offers only Bash reaches its agent's toolkits through this catalog, so the catalog connects them.
        """
        async with self._lock:
            self._check_open()
            await connect_toolkits(toolkits, self._closes)

    def _prepare(
        self,
        toolkit: AdaptedToolkit,
        functions: Sequence[ToolFunction],
        owners: Mapping[str, str] | None = None,
    ) -> None:
        """Bind ``functions`` of ``toolkit``, under their owner from ``owners``, or under the toolkit without one."""
        bindings: dict[ToolKey, PreparedAgentToolBinding] = {}
        for function in functions:
            authored = owners is None or function.name in owners
            namespace = ((owners or {}).get(function.name) or toolkit.name) if authored else "agent"
            if namespace == "agent" and authored:
                msg = "The agent toolkit namespace is reserved for generated functions"
                raise ValueError(msg)
            key = ToolKey(namespace, function.name)
            if key in bindings or key in self._bindings:
                msg = f"Duplicate qualified tool: {namespace}.{function.name}"
                raise ValueError(msg)
            bindings[key] = PreparedAgentToolBinding(key, function, self)
        # Nothing is recorded until every function is bound, so a refused toolkit leaves the catalog unchanged.
        self._bindings.update(bindings)
        self._instructions.update(dict.fromkeys(bindings, (toolkit.guidance,) if toolkit.guidance else ()))

    def bindings(self) -> tuple[PreparedAgentToolBinding, ...]:
        """Return the bindings prepared so far, for a check over the whole prepared surface."""
        self._check_open()
        return tuple(self._bindings.values())

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

    def call_context(self, call_id: str) -> ToolCallContext:
        """Return the execution context of one call: the turn's run and the call's own id."""
        return self.run.call_context(call_id)

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
                await connect_toolkits((toolkit,), self._closes)
                self._prepare(replace(toolkit, name=deferred.name), toolkit.functions)
                del self._deferred[key.toolkit]
            binding = self._bindings.get(key)
            if binding is None:
                functions = sorted(known.function for known in self._bindings if known.toolkit == key.toolkit)
                if functions:
                    msg = f"Toolkit {key.toolkit!r} has no function {key.function!r}; its functions: {', '.join(functions)}"
                else:
                    msg = f"No toolkit {key.toolkit!r}"
                raise UnknownToolError(msg)
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
            dict(binding.function.parameters),
            self._instructions[key],
        )

    async def close(self) -> None:
        """Refuse new work, wait for the operation that currently holds the catalog, then close what it connected."""
        self._closed = True
        async with self._lock:
            await close_toolkits(self._closes)


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


def _check_resolves(
    requirement: RunRequirement,
    binding: PreparedAgentToolBinding,
    call_id: str,
    arguments: dict[str, object],
) -> None:
    """Refuse a decision unless it is a confirmation of exactly this call to this function."""
    execution = requirement.tool_execution
    function = binding.function
    if (
        execution.tool_call_id != call_id
        or execution.tool_name != binding.key.function
        or json.dumps(execution.tool_args, sort_keys=True, allow_nan=False)
        != json.dumps(arguments, sort_keys=True, allow_nan=False)
        or execution.requires_confirmation is not True
        or not requirement.is_resolved()
        or requirement.confirmation is None
        or function.external_execution
        or (function.requires_confirmation and function.approval_type != execution.approval_type)
    ):
        msg = "Approval does not resolve this exact prepared call"
        raise ValueError(msg)


def _paused_execution(function: ToolFunction, call_id: str, arguments: dict[str, object]) -> ToolExecution | None:
    """Return the waiting execution of a call its function pauses for a decision, or None when it runs now."""
    if not (function.requires_confirmation or function.external_execution):
        return None
    execution = ToolExecution(tool_call_id=call_id, tool_name=function.name, tool_args=deepcopy(arguments))
    if function.requires_confirmation:
        execution.requires_confirmation = True
        execution.approval_type = function.approval_type
    if function.external_execution:
        execution.external_execution_required = True
        execution.external_execution_silent = function.external_execution_silent
    return execution


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
    shell_leaf: Callable[[dict[str, object]], Awaitable[object]],
    authorize: Callable[[], Awaitable[None]] | None = None,
    requirement: RunRequirement | None = None,
) -> AsyncIterator[AgentToolCallEvent]:
    """Keep canonical hooks and pauses; release mutation only around the shell's own IO.

    Only the response owner supplies this leaf, which runs the binding's own shell
    function, so CLI calls from inside the command can use the catalog meanwhile.
    """
    if binding.key.function not in SHELL_OPERATION_NAMES:
        msg = "Shell leaf replacement requires a canonical shell operation"
        raise ValueError(msg)
    stream = _execute_agent_tool_call(
        binding,
        call_id,
        arguments,
        shell_leaf=shell_leaf,
        authorize=authorize,
        requirement=requirement,
    )
    async with closing_async_stream(stream):
        async for event in stream:
            yield event


async def _execute_agent_tool_call(
    binding: PreparedAgentToolBinding,
    call_id: str,
    arguments: dict[str, object],
    *,
    shell_leaf: Callable[[dict[str, object]], Awaitable[object]] | None = None,
    authorize: Callable[[], Awaitable[None]] | None = None,
    requirement: RunRequirement | None = None,
) -> AsyncIterator[AgentToolCallEvent]:
    catalog = binding.catalog
    # The call runs with the arguments it was admitted with, whatever the caller does with its dict later.
    arguments = deepcopy(arguments)
    async with catalog.dispatch(binding, authorize):
        if requirement is not None:
            _check_resolves(requirement, binding, call_id, arguments)
            if not requirement.confirmation:
                yield _failure(binding, call_id, arguments, requirement.denial_note)
                return
        try:
            validate_tool_arguments(dict(binding.function.parameters), arguments)
        except ValueError:
            yield _failure(binding, call_id, arguments, "Invalid tool arguments")
            return
        limit = catalog.tool_call_limit
        if limit is not None and catalog._call_count >= limit:
            yield _failure(binding, call_id, arguments, "Tool call limit reached")
            return
        paused = None if requirement is not None else _paused_execution(binding.function, call_id, arguments)
        if paused is not None:
            if paused.external_execution_required:
                catalog._call_count += 1
            yield AgentToolCallEvent(
                "waiting",
                call_id,
                binding.key,
                execution=paused,
                requirement=RunRequirement(paused),
            )
            return
        catalog._call_count += 1
        execution = ToolExecution(tool_call_id=call_id, tool_name=binding.key.function, tool_args=deepcopy(arguments))
        yield AgentToolCallEvent("started", call_id, binding.key, execution=deepcopy(execution))
        function = binding.function
        if shell_leaf is not None:
            original = function.entrypoint

            @wraps(original)
            async def pinned_leaf(**kwargs: object) -> object:
                catalog._lock.release()
                try:
                    return await shell_leaf(kwargs)
                finally:
                    # Hook unwinding must regain exclusive catalog ownership even
                    # under repeated cancellation of the response task.
                    await wait_for_future_until_complete(asyncio.create_task(catalog._lock.acquire()))

            function = replace(function, entrypoint=pinned_leaf)
        # A job's call has no time limit, so what it offloads must not hold a shared pool's thread.
        tracker = SyncToolCompletionTracker(executor=JOB_CALL_EXECUTOR if job_owns_execution() else None)
        try:
            with catalog.execution_context(), track_sync_tool_completion(tracker), cli_dispatch_scope():
                outcome = await execute_tool_call(
                    function,
                    arguments,
                    catalog.call_context(call_id),
                    middleware=catalog.middleware,
                )
        finally:
            await _settle_call(binding, tracker)
        execution.result = outcome.content
        execution.tool_call_error = outcome.is_error
        execution.stop_after_tool_call = outcome.stop_after_call
        if outcome.images or outcome.audios or outcome.videos or outcome.files:
            yield AgentToolCallEvent("media", call_id, binding.key, media=outcome)
        kind = "continuation_required" if outcome.stop_after_call else "failed" if outcome.is_error else "completed"
        yield AgentToolCallEvent(kind, call_id, binding.key, execution=execution)


async def _settle_call(binding: PreparedAgentToolBinding, tracker: SyncToolCompletionTracker) -> None:
    """Wait for a synchronous leaf the call started, so its thread never outlives the catalog."""

    async def settle() -> None:
        with binding.catalog.execution_context():
            pending = tracker.started_task()
            if pending is not None:
                # Its failure was already reported as this call's own; waiting must not raise it again.
                await asyncio.gather(pending, return_exceptions=True)

    await wait_for_future_until_complete(asyncio.create_task(settle(), name="agent-tool-call-cleanup"))


__all__ = [
    "AgentToolCallEvent",
    "DeferredAgentToolkit",
    "PreparedAgentToolBinding",
    "PreparedAgentToolCatalog",
    "cli_dispatch_active",
    "cli_dispatch_scope",
    "execute_agent_shell_call",
    "execute_agent_tool_call",
]
