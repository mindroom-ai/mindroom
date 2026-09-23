"""Acknowledge job result claims once the parent run has saved the exact tool call's result."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agno.exceptions import AgentRunException
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.tools.function import FunctionCall, ToolResult

from mindroom.agent_storage import run_session_storage_operation
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.logging_config import get_logger
from mindroom.tool_jobs.agno_compat_functions import function_actor, function_agent, function_run_context
from mindroom.tool_jobs.results import read_result_payload
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agno.db.base import BaseDb

    from mindroom.tool_jobs.results import ToolResultPayload
    from mindroom.tool_jobs.runtime import BackgroundJob, JobClaim, ToolJobRuntime

_CALL: ContextVar[FunctionCall | None] = ContextVar("tool_job_consumer_call", default=None)
_OWNER: ContextVar[ConsumptionOwner | None] = ContextVar("tool_job_consumption", default=None)
logger = get_logger(__name__)


@dataclass(frozen=True)
class _Consumption:
    runtime: ToolJobRuntime
    job_id: str
    claim: JobClaim
    run_id: str
    team: bool
    call_id: str
    # The admitted turn whose reply consumes the result; recovery of that unfinished reply may still read it.
    source_event_id: str | None

    def saved(self, storage: BaseDb) -> bool:
        """Find the exact tool call, finished with a result, in the saved parent run of the calling agent or team."""
        run = storage.get_run(self.run_id)
        return isinstance(run, TeamRunOutput if self.team else RunOutput) and any(
            tool.tool_call_id == self.call_id and not tool.is_paused and tool.result is not None
            for tool in run.tools or []
        )


@dataclass
class ConsumptionOwner:
    """Claims retained across every attempt of one parent response."""

    storage_factory: Callable[[], BaseDb] | None = None
    _claims: list[_Consumption] = field(default_factory=list)

    def register(self, runtime: ToolJobRuntime, job_id: str, claim: JobClaim, call: FunctionCall) -> None:
        """Keep a claim until the parent run that made this exact tool call is saved."""
        context = function_run_context(call.function)
        if context is None or not call.call_id:
            msg = "Tool result consumption requires exact run and tool-call identity"
            raise ValueError(msg)
        if function_actor(call.function) is None:
            msg = "Tool result consumption requires a concrete agent or team"
            raise ValueError(msg)
        team = function_agent(call.function) is None
        tool_context = get_tool_runtime_context()
        source = tool_context.membership_turn_id if tool_context is not None else None
        self._claims.append(_Consumption(runtime, job_id, claim, context.run_id, team, call.call_id, source))

    async def finalize(self) -> None:
        """Acknowledge exact saved rows; release missing, failed, or unsaved evidence."""
        consumptions, self._claims = self._claims, []
        for consumption in consumptions:
            try:
                saved = self.storage_factory is not None and await run_session_storage_operation(
                    self.storage_factory,
                    consumption.saved,
                )
                if saved:
                    await consumption.runtime.acknowledge_wait(
                        consumption.job_id,
                        consumption.claim,
                        source_event_id=consumption.source_event_id,
                    )
            except Exception:
                logger.warning(
                    "Tool result persistence was not confirmed",
                    job_id=consumption.job_id,
                    run_id=consumption.run_id,
                    exc_info=True,
                )
            finally:
                await consumption.runtime.release_wait(consumption.job_id, consumption.claim)


@contextmanager
def consumption_context(owner: ConsumptionOwner) -> Iterator[None]:
    """Bind the response's claim owner for one call or stream pull."""
    token = _OWNER.set(owner)
    try:
        yield
    finally:
        _OWNER.reset(token)


@contextmanager
def consuming_function_call(call: FunctionCall) -> Iterator[None]:
    """Expose the management tool's exact caller without adding schema arguments."""
    token = _CALL.set(call)
    try:
        yield
    finally:
        _CALL.reset(token)


def set_consumption_storage(storage_factory: Callable[[], BaseDb] | None) -> None:
    """Bind registered canonical storage after the response opens its scope."""
    owner = _OWNER.get()
    if owner is not None:
        owner.storage_factory = storage_factory


async def finalize_consumption() -> None:
    """Read exact persisted parent evidence before any attempt storage is closed."""
    owner = _OWNER.get()
    if owner is not None:
        await run_coroutine_until_complete(owner.finalize())


def session_state_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Describe changed keys against frozen baseline, including deleted keys."""
    return {
        key: {
            "before_present": key in before,
            "before": before.get(key),
            "present": key in after,
            "value": after.get(key),
        }
        for key in before.keys() | after.keys()
        if key not in before or key not in after or before[key] != after[key]
    }


def _merge_session_state(value: Any, job: BackgroundJob, payload: ToolResultPayload, call: FunctionCall) -> Any:  # noqa: ANN401
    """Apply unconflicted state changes, reporting conflicts in a new value that leaves the payload intact."""
    if job.consumed or job.status != "completed":
        return value
    context = function_run_context(call.function)
    state = context.session_state if context is not None else None
    conflicts = []
    for key, change in payload.state_delta.items():
        if state is None or (key in state) != change["before_present"] or state.get(key) != change["before"]:
            conflicts.append(key)
        elif change["present"]:
            state[key] = change["value"]
        else:
            state.pop(key, None)
    if conflicts:
        warning = "Session state conflicts: " + ", ".join(sorted(conflicts))
        metadata = {"session_state_conflicts": conflicts}
        if isinstance(value, ToolResult):
            return value.model_copy(
                update={"content": f"{value.content}\n{warning}", "metadata": {**(value.metadata or {}), **metadata}},
            )
        return ToolResult(content=f"{value}\n{warning}", metadata=metadata)
    return value


def restore_control(control: dict[str, Any]) -> AgentRunException:
    """Rebuild the SDK control exception a job's tool raised."""
    values = dict(control)
    return AgentRunException(values.pop("message"), **values)


async def retain_claim(
    runtime: ToolJobRuntime,
    job_id: str,
    claim: JobClaim,
    function_call: FunctionCall | None = None,
) -> None:
    """Keep a claim until the parent run saves the exact tool call, releasing it when no such run will be saved."""
    call = function_call or _CALL.get()
    owner = _OWNER.get()
    if call is None or owner is None:
        await runtime.release_wait(job_id, claim)
        return
    try:
        owner.register(runtime, job_id, claim, call)
    except BaseException:
        await runtime.release_wait(job_id, claim)
        raise


async def consume_tool_job(
    runtime: ToolJobRuntime,
    job: BackgroundJob,
    claim: JobClaim,
    *,
    function_call: FunctionCall | None = None,
) -> tuple[Any, ToolResultPayload]:
    """Read a claimed outcome's value and payload, retaining the claim until the parent run saves this tool call."""
    call = function_call or _CALL.get()
    try:
        payload = await read_result_payload(runtime, job)
        value = payload.value
        if call is not None and _OWNER.get() is not None:
            value = _merge_session_state(value, job, payload, call)
    except BaseException:
        await runtime.release_wait(job.job_id, claim)
        raise
    await retain_claim(runtime, job.job_id, claim, call)
    return value, payload
