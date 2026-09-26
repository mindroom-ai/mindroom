"""Response-owned CLI operations admitted only inside active Bash windows; no model loop here."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import Context, copy_context
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Protocol, cast

from agno.run.agent import ToolCallCompletedEvent, ToolCallStartedEvent
from agno.tools.function import ToolResult

from mindroom.agent_cli.approval import CliApprovalCall
from mindroom.agent_cli.delegation import advance_cli_delegation, approval_calls_for_cli_pause
from mindroom.agent_cli.events import emit_cli_run_event, emit_cli_tool_event, project_cli_execution
from mindroom.agent_cli.json_io import canonical_json, read_json
from mindroom.agent_cli.projection import project_cli_result, register_cli_media, schema_context_document
from mindroom.agent_cli.protocol import (
    ContextListOperation,
    ContextReadOperation,
    ToolCallOperation,
    ToolCallReceipt,
    ToolDescribeOperation,
    ToolListOperation,
    ToolSearchOperation,
)
from mindroom.agent_cli.session import (
    CliAuthenticationError,
    CliBashWindowRequiredError,
    CliCallConflictError,
    CliOperationError,
    TurnToolBridge,
)
from mindroom.agno_compat_cli_checkpoint import ProviderBatchCheckpoint
from mindroom.agno_compat_prepared_tools import stop_function_call
from mindroom.background_tasks import wait_for_future_until_complete
from mindroom.cancellation import current_task_is_process_shutdown, request_task_cancel, task_is_process_shutdown
from mindroom.response_turn import PausedAttempt
from mindroom.tool_system.agent_tool_calls import AgentToolCallEvent, execute_agent_shell_call, execute_agent_tool_call
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.events import CollectedStreamPresentation
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context
from mindroom.tool_system.tool_access import ToolKey, search_tool_metadata

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping

    from agno.models.response import ModelResponse, ToolExecution
    from agno.run.requirement import RunRequirement
    from agno.tools.function import FunctionCall

    from mindroom.agent_cli.protocol import AgentCliOperation
    from mindroom.agent_cli.session import CliTurnOwner
    from mindroom.delegation.state import ChildResponseRunner
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.tool_system.agent_tool_calls import (
        PreparedAgentToolBinding,
        PreparedAgentToolCatalog,
    )
    from mindroom.tool_system.output_files import ToolOutputFilePolicy


# Bound one response's orchestrator-side CLI state against runaway Bash loops.
# The catalog separately refuses calls past the agent's per-turn tool-call budget.
_MAX_CALL_RECEIPTS = 1024
_MAX_ACTIVE_OPERATIONS = 64


class _StaleAttemptError(ValueError):
    """Admitted work cannot execute after a control boundary."""


class _ShellWorker(Protocol):
    async def invoke_shell(self, function_name: str, arguments: dict[str, object]) -> object: ...


@dataclass
class _Queued:
    operation: ToolCallOperation | ToolDescribeOperation
    future: asyncio.Future[dict[str, object]] | None = None

    def settle_waiter(self, _task: asyncio.Task[None]) -> None:
        """Release a describe waiter even when its task was cancelled before start."""
        if self.future is not None and not self.future.done():
            self.future.set_exception(CliAuthenticationError("Agent CLI authority is unavailable"))


@dataclass
class _Call:
    """One response's immutable input and observed live execution."""

    arguments_json: str
    receipt: ToolCallReceipt


def _page(items: list[dict[str, object]], cursor: str | None, limit: int) -> dict[str, object]:
    offset = int(cursor or "0")
    if offset < 0 or offset > len(items):
        msg = "Invalid discovery cursor"
        raise CliOperationError(msg)
    selected: list[dict[str, object]] = []
    for item in items[offset : offset + limit]:
        candidate = {"items": [*selected, item], "next_cursor": str(offset + len(selected) + 1)}
        try:
            canonical_json(candidate)
        except ValueError:
            if not selected:
                raise
            break
        selected.append(item)
    end = offset + len(selected)
    return {"items": selected, "next_cursor": str(end) if end < len(items) else None}


class LiveTurnTools(TurnToolBridge):
    """Own call receipts separately from admitted operation lifetimes.

    The outer mutex spans canonical hooks. Admission opens only inside the
    dedicated worker leaf; tool calls and describes arriving outside an active
    Bash call are rejected. Each admitted lifetime gets its own task so a nested
    shell can wait on another CLI call without blocking its parent window.
    """

    def __init__(
        self,
        owner: CliTurnOwner,
        *,
        catalog: PreparedAgentToolCatalog,
        worker: _ShellWorker | None,
        authorize: Callable[[ToolKey, dict[str, object]], Awaitable[None]],
        context: Mapping[str, str] | None = None,
        on_context_read: Callable[[str], None] | None = None,
        output_file_policy: ToolOutputFilePolicy | None = None,
        run_child: ChildResponseRunner | None = None,
        delegation_depth: int = 0,
        refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    ) -> None:
        if owner.execution_identity != build_execution_identity_from_runtime_context(catalog.runtime_context):
            msg = "CLI catalog execution identity does not match its turn owner"
            raise ValueError(msg)
        super().__init__(owner)
        self.catalog = catalog
        self.checkpoint = ProviderBatchCheckpoint(catalog)
        self._worker = worker
        self._authorize = authorize
        self._context = dict(context or {})
        self._on_context_read = on_context_read
        self._output_file_policy = output_file_policy
        self._media: list[ModelResponse] = []
        self._run_child = run_child
        self._delegation_depth = delegation_depth
        self._refresh_scheduler = refresh_scheduler
        self._outer = asyncio.Lock()
        self._delegation = asyncio.Lock()
        self._admission = asyncio.Lock()
        self._calls: dict[str, _Call] = {}
        self._schema_documents: dict[ToolKey, str] = {}
        self._active: set[asyncio.Task[None]] = set()
        self._parent: str | None = None
        self._window_context: Context | None = None
        self._closed = False
        self._binding_retired = False
        self.control_executions: list[ToolExecution] = []
        self._close_task: asyncio.Task[None] | None = None
        self.close_for_shutdown = False
        self._response_task: asyncio.Task | None = None

    def bind_response_task(self, task: asyncio.Task | None) -> None:
        """Retain the real response task before Agno creates unmarked children."""
        self._response_task = task

    def _is_shutdown(self) -> bool:
        return (
            self.close_for_shutdown
            or task_is_process_shutdown(self._response_task)
            or current_task_is_process_shutdown()
        )

    def _check_live(self) -> None:
        if self._closed or self._revoked:
            msg = "Agent CLI authority is unavailable"
            raise CliAuthenticationError(msg)

    async def operation(self, operation: AgentCliOperation) -> dict[str, object]:
        """Metadata reads are pure; describes and calls run only inside an active Bash window."""
        self._check_live()
        if self._binding_retired and isinstance(operation, ToolListOperation | ToolSearchOperation):
            msg = "Agent tool catalog is being rebuilt; retry shortly"
            raise CliOperationError(msg)
        if isinstance(operation, ToolListOperation):
            return _page(self.catalog.metadata(), operation.cursor, operation.limit)
        if isinstance(operation, ToolSearchOperation):
            metadata = self.catalog.metadata()
            if operation.toolkit:
                metadata = [item for item in metadata if item["toolkit"] == operation.toolkit]
            items = search_tool_metadata(metadata, operation.query, operation.limit)
            result: dict[str, object] = {"items": items}
            if len(canonical_json(result).encode()) > 16 * 1024:
                msg = "Tool search exceeds 16 KiB; narrow the query"
                raise CliOperationError(msg)
            return result
        if isinstance(operation, ContextListOperation):
            return _page(
                [{"name": key, "length": len(value)} for key, value in self._context.items()],
                operation.cursor,
                operation.limit,
            )
        if isinstance(operation, ContextReadOperation):
            return self._read_context(operation)
        async with self._admission:
            self._check_live()
            if isinstance(operation, ToolCallOperation):
                return self._submit_call(operation)
            self._require_bash_window()
            self._require_capacity()
            future = asyncio.get_running_loop().create_future()
            self._admit(_Queued(operation, future))
        return await future

    def _submit_call(self, operation: ToolCallOperation) -> dict[str, object]:
        """Return an existing receipt for a repeated call ID, or admit a new call."""
        call_id = str(operation.call_id)
        arguments = operation.canonical_arguments_json
        call = self._calls.get(call_id)
        if call is not None:
            if (call.receipt.toolkit, call.receipt.function, call.arguments_json) != (
                operation.toolkit,
                operation.function,
                arguments,
            ):
                msg = "Call ID already belongs to a different operation"
                raise CliCallConflictError(msg)
            return call.receipt.model_dump(mode="json")
        self._require_bash_window()
        if len(self._calls) >= _MAX_CALL_RECEIPTS:
            msg = f"This response already holds {_MAX_CALL_RECEIPTS} CLI call receipts"
            raise CliOperationError(msg)
        self._require_capacity()
        call = _Call(
            arguments,
            ToolCallReceipt(
                call_id=operation.call_id,
                toolkit=operation.toolkit,
                function=operation.function,
                status="queued",
            ),
        )
        self._calls[call_id] = call
        self._admit(_Queued(operation.model_copy(deep=True)))
        return call.receipt.model_dump(mode="json")

    def _require_bash_window(self) -> None:
        if self._parent is None:
            msg = "Agent CLI tool commands require an active Bash call"
            raise CliBashWindowRequiredError(msg)

    def _require_capacity(self) -> None:
        # Finished tasks stay in the set until the window drains and reports their failures.
        if sum(not task.done() for task in self._active) >= _MAX_ACTIVE_OPERATIONS:
            msg = f"At most {_MAX_ACTIVE_OPERATIONS} CLI operations may run at once; wait for some to finish"
            raise CliOperationError(msg)

    def _read_context(self, operation: ContextReadOperation) -> dict[str, object]:
        value = self._context.get(operation.name)
        if value is None:
            msg = "Context document is unavailable"
            raise CliOperationError(msg)
        if operation.offset == 0 and self._on_context_read is not None:
            self._on_context_read(operation.name)
        end = min(len(value), operation.offset + operation.limit)
        while True:
            page = {
                "name": operation.name,
                "text": value[operation.offset : end],
                "next_offset": end if end < len(value) else None,
            }
            try:
                canonical_json(page)
            except ValueError:
                if end <= operation.offset:
                    raise
                end = operation.offset + (end - operation.offset) // 2
            else:
                return page

    async def get_call(self, call_id: str) -> dict[str, object]:
        """Receipt reads do not acquire the mutation or window locks."""
        self._check_live()
        call = self._calls.get(call_id)
        if call is None:
            msg = "Agent CLI authority is unavailable"
            raise CliAuthenticationError(msg)
        return call.receipt.model_dump(mode="json")

    def _admit(self, queued: _Queued) -> None:
        call = self._calls[str(queued.operation.call_id)] if isinstance(queued.operation, ToolCallOperation) else None
        if call is not None:
            call.receipt = call.receipt.model_copy(update={"parent_bash_call_id": self._parent})
        assert self._window_context is not None
        task = asyncio.create_task(
            self._dispatch(queued),
            name="agent-cli-admitted",
            context=self._window_context.copy(),
        )
        task.add_done_callback(queued.settle_waiter)
        self._active.add(task)

    @asynccontextmanager
    async def _window(self, parent: str) -> AsyncIterator[None]:
        async with self._admission:
            self._check_live()
            self._parent = parent
            self._window_context = copy_context()
        try:
            yield
        except asyncio.CancelledError:
            self._cancel_active()
            raise
        finally:
            await wait_for_future_until_complete(
                asyncio.create_task(self._drain_window(), name="agent-cli-window-drain"),
                on_cancel=self._cancel_active,
            )

    def _cancel_active(self) -> None:
        self.revoke()
        self.close_for_shutdown = self._is_shutdown()
        for task in self._active:
            request_task_cancel(task, process_shutdown=self.close_for_shutdown)

    async def _drain_window(self) -> None:
        failures: list[Exception] = []
        while True:
            async with self._admission:
                tasks = tuple(self._active)
                if not tasks:
                    # Recursive children may join while their admitted shell
                    # settles. Only quiescence closes admission for this Bash.
                    self._parent = None
                    self._window_context = None
                    break
            results = await asyncio.gather(*tasks, return_exceptions=True)
            self._active.difference_update(tasks)
            failures.extend(result for result in results if isinstance(result, Exception))
        if failures:
            msg = "Agent CLI admitted operation failed"
            raise ExceptionGroup(msg, failures)

    async def execute_bash(self, key: ToolKey, arguments: dict[str, object], fc: FunctionCall) -> str | ToolResult:
        """Own canonical execution and forward the response's cancellation reason."""
        task = asyncio.create_task(self._execute_bash(key, arguments, fc), name="agent-cli-bash")

        def cancel() -> None:
            self.revoke()
            self.close_for_shutdown = self._is_shutdown()
            request_task_cancel(task, process_shutdown=self.close_for_shutdown)

        return await wait_for_future_until_complete(task, on_cancel=cancel)

    async def _execute_bash(self, key: ToolKey, arguments: dict[str, object], fc: FunctionCall) -> str | ToolResult:
        """Checkpoint exact provider history before any binding, hook or worker IO."""
        async with self._outer:
            self._check_live()
            if self.control_executions:
                stop_function_call(fc)
                return "Bash cancelled before dispatch: continuation requires a rebuilt tool catalog."
            if self._binding_retired or key.toolkit != "shell" or not fc.call_id:
                msg = "Bash binding is unavailable"
                raise ValueError(msg)
            await self.checkpoint.persist(fc)
            await self._authorize(key, arguments)
            binding = await self.catalog.bind(key)

            async def authorize() -> None:
                self._check_live()
                await self._authorize(key, arguments)

            events = self.execute_shell(
                binding,
                fc.call_id,
                arguments,
                parent=fc.call_id,
                authorize=authorize,
            )
            media_results = []
            async with closing_async_stream(events):
                terminal = None
                async for event in events:
                    if event.media is not None:
                        media_results.append(event.media)
                    if event.execution is not None:
                        terminal = event.execution
            if terminal is None:
                msg = "Canonical shell returned no terminal execution"
                raise RuntimeError(msg)
            result = str(terminal.result or "")
            if self.control_executions:
                stop_function_call(fc)
            result_with_media: str | ToolResult = result
            if media_results:
                result_with_media = ToolResult(
                    content=result,
                    images=[item for media in media_results for item in media.images or []],
                    audios=[item for media in media_results for item in media.audios or []],
                    videos=[item for media in media_results for item in media.videos or []],
                    files=[item for media in media_results for item in media.files or []],
                )
            self.checkpoint.complete(fc, result_with_media)
            return result_with_media

    async def execute_shell(
        self,
        binding: PreparedAgentToolBinding,
        call_id: str,
        arguments: dict[str, object],
        *,
        parent: str,
        authorize: Callable[[], Awaitable[None]],
        requirement: RunRequirement | None = None,
    ) -> AsyncIterator[AgentToolCallEvent]:
        """Drain shell work and nested media for both live Bash and exact recovery."""
        self._media = []

        async def leaf(values: dict[str, object]) -> str:
            async with self._window(parent):
                return await self._invoke_worker(binding.key, values)

        events = self._execute(
            binding,
            call_id,
            arguments,
            parent=parent,
            authorize=authorize,
            worker_leaf=leaf,
            requirement=requirement,
        )
        async with closing_async_stream(events):
            async for event in events:
                self._check_control(event)
                yield event
        for media in self._media:
            yield AgentToolCallEvent("media", call_id, binding.key, media=media)

    async def _invoke_worker(self, key: ToolKey, arguments: dict[str, object]) -> str:
        self._check_live()
        if self._worker is None:
            msg = "Dedicated CLI shell worker is unavailable"
            raise RuntimeError(msg)
        result = await self._worker.invoke_shell(key.function, arguments)
        if not isinstance(result, str):
            msg = "Dedicated shell worker returned a non-text result"
            raise TypeError(msg)
        return result

    async def _execute(
        self,
        binding: PreparedAgentToolBinding,
        call_id: str,
        arguments: dict[str, object],
        *,
        parent: str | None,
        authorize: Callable[[], Awaitable[None]],
        worker_leaf: Callable[[dict[str, object]], Awaitable[str]] | None = None,
        requirement: RunRequirement | None = None,
    ) -> AsyncIterator[AgentToolCallEvent]:
        while True:
            events = (
                execute_agent_tool_call(binding, call_id, arguments, authorize=authorize, requirement=requirement)
                if worker_leaf is None
                else execute_agent_shell_call(
                    binding,
                    call_id,
                    arguments,
                    worker_leaf=worker_leaf,
                    authorize=authorize,
                    requirement=requirement,
                )
            )
            waiting = None
            async with closing_async_stream(events):
                async for event in events:
                    emit_cli_tool_event(event, parent)
                    if event.kind == "waiting":
                        waiting = event.requirement
                    yield event
            if waiting is None:
                return
            if waiting.needs_external_execution and binding.key.toolkit == "delegate" and parent is not None:
                async with self._delegation:
                    delegated = self._execute_delegation(binding, waiting, parent=parent, authorize=authorize)
                    async with closing_async_stream(delegated):
                        async for event in delegated:
                            emit_cli_tool_event(event, parent)
                            yield event
                return
            if not waiting.needs_confirmation or waiting.needs_user_input or waiting.needs_external_execution:
                msg = "CLI approval contains an unsupported non-confirmation requirement"
                raise RuntimeError(msg)
            handler = self.catalog.runtime_context.cli_approval_handler
            if handler is None or parent is None or waiting.tool_execution is None:
                msg = "CLI approval requires its response owner"
                raise RuntimeError(msg)
            # Native iterator has closed; no catalog mutation lock spans human time.
            await self.checkpoint.persist_approval(parent)
            presentation = CollectedStreamPresentation(show_tool_calls=True, track_hidden_tools=True)
            presentation.start_tool(
                project_cli_execution(waiting.tool_execution, parent=parent, toolkit_name=binding.key.toolkit),
            )
            resolved = await handler(
                PausedAttempt(
                    session_id=self.catalog.session.session_id,
                    run_id=self.catalog.run_context.run_id,
                    tools=(waiting.tool_execution,),
                    toolkit_owners={
                        (self.catalog.runtime_context.agent_name, binding.key.function): binding.key.toolkit,
                    },
                    requirements=(waiting,),
                    runtime_model_name=self.catalog.runtime_context.active_model_name,
                    response_text=presentation.final_text(),
                    tool_trace=tuple(presentation.tool_trace),
                    cli_call=CliApprovalCall(
                        toolkit=binding.key.toolkit,
                        function=binding.key.function,
                        arguments=arguments,
                        call_id=call_id,
                        parent_bash_call_id=parent,
                        requirements=(waiting,),
                        delegation_depth=self._delegation_depth,
                    ).to_dict(),
                ),
            )
            if len(resolved) != 1:
                msg = "CLI approval no longer identifies its exact call"
                raise ValueError(msg)
            requirement = resolved[0]

    async def _execute_delegation(
        self,
        binding: PreparedAgentToolBinding,
        requirement: RunRequirement,
        *,
        parent: str,
        authorize: Callable[[], Awaitable[None]],
    ) -> AsyncIterator[AgentToolCallEvent]:
        decisions = None
        denial_reasons = None
        approval_calls = ()
        started = False
        child_toolkits: dict[str, str | None] = {}

        def on_child_event(event: object) -> None:
            if isinstance(event, ToolCallStartedEvent | ToolCallCompletedEvent) and event.tool is not None:
                child_call_id = str(event.tool.tool_call_id)
                if child_call_id in child_toolkits:
                    emit_cli_run_event(event, parent=parent, toolkit_name=child_toolkits[child_call_id])

        while True:
            async with self.catalog.dispatch(binding, authorize):
                if not started:
                    started = True
                    tool = requirement.tool_execution
                    assert tool is not None
                    yield AgentToolCallEvent("started", str(tool.tool_call_id), binding.key, execution=tool)
                result = await advance_cli_delegation(
                    binding,
                    requirement,
                    parent_bash_call_id=parent,
                    delegation_depth=self._delegation_depth,
                    run_child=self._run_child,
                    refresh_scheduler=self._refresh_scheduler,
                    decisions=decisions,
                    denial_reasons=denial_reasons,
                    approval_calls=approval_calls,
                    on_event=on_child_event,
                )
            if not isinstance(result, PausedAttempt):
                yield AgentToolCallEvent("completed", str(result.tool_call_id), binding.key, execution=result)
                return
            handler = self.catalog.runtime_context.cli_approval_handler
            if handler is None:
                msg = "CLI approval requires its response owner"
                raise RuntimeError(msg)
            await self.checkpoint.persist_approval(parent)
            presentation = CollectedStreamPresentation(show_tool_calls=True, track_hidden_tools=True)
            for tool in result.tools:
                child_toolkit = result.toolkit_owners.get(
                    (result.approval_agent_name or self.catalog.runtime_context.agent_name, str(tool.tool_name)),
                )
                child_toolkits[str(tool.tool_call_id)] = child_toolkit
                emit_cli_run_event(
                    ToolCallStartedEvent(
                        tool=tool,
                        run_id=result.run_id,
                        session_id=result.session_id,
                        agent_id=result.approval_agent_name or self.catalog.runtime_context.agent_name,
                    ),
                    parent=parent,
                    toolkit_name=child_toolkit,
                )
                presentation.start_tool(project_cli_execution(tool, parent=parent, toolkit_name=child_toolkit))
            paused = replace(result, response_text=presentation.final_text(), tool_trace=tuple(presentation.tool_trace))
            resolved = await handler(paused)
            approval_calls = approval_calls_for_cli_pause(paused, self.catalog.runtime_context.agent_name)
            decisions = {
                str(item.tool_execution.tool_call_id): bool(item.confirmation)
                for item in resolved
                if item.tool_execution is not None
            }
            denial_reasons = {
                str(item.tool_execution.tool_call_id): item.confirmation_note
                for item in resolved
                if item.tool_execution is not None
            }

    def _check_control(self, event: AgentToolCallEvent) -> None:
        if event.kind == "continuation_required" and event.execution is not None:
            self.control_executions.append(deepcopy(event.execution))
            # Synchronous fencing happens while the executing call owns the catalog
            # lock. Already executing lifetimes drain; waiting calls fail at dispatch.
            self._parent = None

    def _check_dispatch(self) -> None:
        self._check_live()
        if self.control_executions:
            msg = "Call cancelled before dispatch: continuation requires a rebuilt tool catalog"
            raise _StaleAttemptError(msg)

    async def _describe(self, queued: _Queued, key: ToolKey) -> None:
        try:
            self._check_dispatch()
            try:
                await self._authorize(key, {})
            except PermissionError as exc:
                msg = "Tool is unavailable"
                raise CliOperationError(msg) from exc
            descriptor = await self.catalog.describe(key, check_current=self._check_dispatch)
            self._check_dispatch()
            result: dict[str, object] = {
                "toolkit": key.toolkit,
                "function": key.function,
                "description": descriptor.description,
                "input_schema": descriptor.input_schema,
                "instructions": list(descriptor.instructions),
            }
            document = schema_context_document(result)
            if len(document.encode()) > 32 * 1024:
                name = self._schema_documents.setdefault(key, f"tool-schema-{len(self._schema_documents)}")
                self._context[name] = document
                result = {
                    "toolkit": key.toolkit,
                    "function": key.function,
                    "context": {"name": name, "length": len(document), "operation": "context.read"},
                }
            if queued.future is not None and not queued.future.done():
                queued.future.set_result(result)
        except Exception as exc:
            if queued.future is not None and not queued.future.done():
                queued.future.set_exception(exc)

    async def _dispatch(self, queued: _Queued) -> None:  # noqa: C901, PLR0912, PLR0915
        operation = queued.operation
        key = ToolKey(operation.toolkit, operation.function)
        if isinstance(operation, ToolDescribeOperation):
            await self._describe(queued, key)
            return
        call_id = str(operation.call_id)
        call = self._calls[call_id]
        if call.receipt.status != "queued":
            return
        arguments = cast("dict[str, object]", read_json(call.arguments_json))
        call.receipt = call.receipt.model_copy(update={"status": "running"})
        started = False
        status = "failed"
        result: object = "Tool did not return a terminal execution"
        media_results: list[ModelResponse] = []
        try:
            self._check_dispatch()
            await self._authorize(key, arguments)
            binding = await self.catalog.bind(key, check_current=self._check_dispatch)

            async def authorize() -> None:
                if started:
                    self._check_live()
                else:
                    self._check_dispatch()
                await self._authorize(key, arguments)

            events = self._execute(
                binding,
                call_id,
                arguments,
                parent=call.receipt.parent_bash_call_id,
                worker_leaf=partial(self._invoke_worker, key) if key.toolkit == "shell" else None,
                authorize=authorize,
            )
            async with closing_async_stream(events):
                async for event in events:
                    self._check_control(event)
                    if event.media is not None:
                        self._media.append(event.media)
                        media_results.append(event.media)
                    if event.kind == "started":
                        started = True
                        call.receipt = call.receipt.model_copy(update={"status": "running"})
                    if event.kind == "waiting":
                        call.receipt = call.receipt.model_copy(update={"status": "waiting"})
                    if event.kind in {"completed", "failed", "continuation_required"} and event.execution is not None:
                        status = "failed" if event.execution.tool_call_error else "completed"
                        result = event.execution.result
        except _StaleAttemptError as exc:
            status = "cancelled"
            result = str(exc)
        except (ValueError, PermissionError):
            result = "Tool is unavailable or arguments are invalid"
        except asyncio.CancelledError:
            status = "cancelled"
            result = "Call interrupted after dispatch; effects may have occurred"
            raise
        except Exception:
            result = "Internal CLI operation failed"
            raise
        finally:
            try:
                attachments = [
                    reference
                    for media in media_results
                    for reference in register_cli_media(
                        media,
                        context=self.catalog.runtime_context,
                        policy=self._output_file_policy,
                        call_id=call_id,
                    )
                ]
                result = project_cli_result(result, self._output_file_policy, key.function)
                call.receipt = ToolCallReceipt.model_validate(
                    call.receipt.model_dump() | {"status": status, "outcome": result, "attachments": attachments},
                )
            except Exception:  # Projection failure cannot undo an observed completed execution.
                call.receipt = call.receipt.model_copy(
                    update={
                        "status": status,
                        "outcome": {"error": "Tool result projection failed; execution status is retained"},
                    },
                )

    async def retire_binding(self) -> None:
        """Retire this attempt while retaining receipts and worker authority."""
        async with self._outer:
            self._binding_retired = True
            await self.catalog.close()

    def bind_catalog(self, catalog: PreparedAgentToolCatalog, *, context: Mapping[str, str] | None = None) -> None:
        """Install the next attempt only after retiring the previous catalog."""
        if not self._binding_retired:
            msg = "Previous CLI catalog must retire before replacement"
            raise RuntimeError(msg)
        if self.owner.execution_identity != build_execution_identity_from_runtime_context(catalog.runtime_context):
            msg = "Replacement CLI catalog execution identity does not match its turn owner"
            raise ValueError(msg)
        self.catalog = catalog
        if context is not None:
            self._context = dict(context)
        self.checkpoint = ProviderBatchCheckpoint(catalog)
        self.control_executions = []
        self._binding_retired = False

    async def close(self) -> None:
        """Revoke first, preserve cancellation reason, then settle all owned work."""
        if self._close_task is None:
            self.revoke()
            self._closed = True
            self.close_for_shutdown = self._is_shutdown()
            self._close_task = asyncio.create_task(self._close(), name="agent-cli-turn-close")
        await wait_for_future_until_complete(self._close_task)

    async def _close(self) -> None:
        async with self._admission:
            self._parent = None
            for task in self._active:
                request_task_cancel(task, process_shutdown=self.close_for_shutdown)
            tasks = tuple(self._active)
            self._active.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._calls.clear()
        await self.catalog.close()
