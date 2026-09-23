"""Exact durable parent evidence for foreground and later job result claims."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agno.exceptions import AgentRunException
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.tools.function import FunctionCall, ToolResult

from mindroom.agent_storage import run_session_storage_operation
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.logging_config import get_logger
from mindroom.tool_jobs.results import decode_tool_result

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agno.db.base import BaseDb

    from mindroom.tool_jobs.runtime import BackgroundJob, ToolJobRuntime

_RECEIPTS = "mindroom_tool_job_receipts"
_CALL: ContextVar[FunctionCall | None] = ContextVar("tool_job_consumer_call", default=None)
_OWNER: ContextVar[ConsumptionOwner | None] = ContextVar("tool_job_consumption", default=None)
logger = get_logger(__name__)


@dataclass
class _Consumption:
    runtime: ToolJobRuntime
    job_id: str
    token: str
    run_id: str
    session_id: str
    user_id: str | None
    actor_id: str | None
    team: bool
    owning_team_id: str | None
    call_id: str
    tool_name: str
    arguments: dict[str, Any] | None
    receipt: dict[str, Any]

    def saved(self, storage: BaseDb) -> bool:  # noqa: PLR0911 - Reject each independent evidence mismatch.
        run = storage.get_run(self.run_id)
        if not isinstance(run, TeamRunOutput if self.team else RunOutput):
            return False
        if run.session_id != self.session_id or run.user_id != self.user_id:
            return False
        actor_id = run.team_id if isinstance(run, TeamRunOutput) else run.agent_id
        if actor_id != self.actor_id:
            return False
        if self.owning_team_id:
            if not run.parent_run_id:
                return False
            parent = storage.get_run(run.parent_run_id)
            if not isinstance(parent, TeamRunOutput) or parent.team_id != self.owning_team_id:
                return False
        receipts = (run.session_state or {}).get(_RECEIPTS, {})
        if receipts.get(f"{self.run_id}:{self.call_id}") != self.receipt:
            return False
        return any(
            tool.tool_call_id == self.call_id
            and tool.tool_name == self.tool_name
            and tool.tool_args == self.arguments
            and not tool.is_paused
            and tool.result is not None
            for tool in run.tools or []
        )


@dataclass
class ConsumptionOwner:
    """Claims retained across every attempt of one parent response."""

    storage_factory: Callable[[], BaseDb] | None = None
    _claims: list[_Consumption] = field(default_factory=list)

    def register(self, runtime: ToolJobRuntime, job: BackgroundJob, token: str, call: FunctionCall) -> None:
        """Bind a unique generation receipt to the exact SDK run and tool result."""
        context = call.function._run_context
        if context is None or context.session_state is None or not call.call_id:
            msg = "Tool result consumption requires exact run and tool-call identity"
            raise ValueError(msg)
        actor = call.function._agent or call.function._team
        if actor is None:
            msg = "Tool result consumption requires a concrete agent or team"
            raise ValueError(msg)
        receipt = {"job_id": job.job_id, "generation": job.generation, "token": token, "run_id": context.run_id}
        context.session_state.setdefault(_RECEIPTS, {})[f"{context.run_id}:{call.call_id}"] = receipt
        self._claims.append(
            _Consumption(
                runtime,
                job.job_id,
                token,
                context.run_id,
                context.session_id,
                context.user_id,
                actor.id,
                call.function._agent is None,
                call.function._agent.team_id if call.function._agent is not None else None,
                call.call_id,
                call.function.name,
                deepcopy(call.arguments),
                receipt,
            ),
        )

    async def finalize(self) -> None:
        """Acknowledge exact saved rows; release missing, failed, or unsaved evidence."""
        claims, self._claims = self._claims, []
        for claim in claims:
            try:
                saved = self.storage_factory is not None and await run_session_storage_operation(
                    self.storage_factory,
                    claim.saved,
                )
                if saved:
                    await claim.runtime.acknowledge_wait(claim.job_id, claim.token)
            except Exception:
                logger.warning(
                    "Tool result persistence was not confirmed",
                    job_id=claim.job_id,
                    run_id=claim.run_id,
                    exc_info=True,
                )
            finally:
                await claim.runtime.release_wait(claim.job_id, claim.token)


@contextmanager
def consumption_context(owner: ConsumptionOwner) -> Iterator[None]:
    """Bind the response's receipt owner for one call or stream pull."""
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
        if key != _RECEIPTS and (key not in before or key not in after or before[key] != after[key])
    }


def _merge_session_state(value: Any, job: BackgroundJob, call: FunctionCall) -> Any:  # noqa: ANN401
    if job.wait_acknowledged or job.status != "completed" or not job.result_payload:
        return value
    context = call.function._run_context
    state = context.session_state if context is not None else None
    conflicts = []
    for key, change in decode_tool_result(job.result_payload["state_delta"]).items():
        if state is None or (key in state) != change["before_present"] or state.get(key) != change["before"]:
            conflicts.append(key)
        elif change["present"]:
            state[key] = change["value"]
        else:
            state.pop(key, None)
    if conflicts:
        warning = "Session state conflicts: " + ", ".join(sorted(conflicts))
        if isinstance(value, ToolResult):
            value.content += "\n" + warning
            value.metadata = {**(value.metadata or {}), "session_state_conflicts": conflicts}
        else:
            value = ToolResult(content=f"{value}\n{warning}", metadata={"session_state_conflicts": conflicts})
    return value


async def record_tool_job_receipt(
    runtime: ToolJobRuntime,
    job: BackgroundJob,
    token: str,
    *,
    function_call: FunctionCall | None = None,
) -> None:
    """Retain a control or result receipt until its exact parent tool call is saved."""
    call = function_call or _CALL.get()
    owner = _OWNER.get()
    try:
        if call is not None and owner is not None:
            owner.register(runtime, job, token, call)
        else:
            await runtime.release_wait(job.job_id, token)
    except BaseException:
        await runtime.release_wait(job.job_id, token)
        raise


async def consume_tool_job(
    runtime: ToolJobRuntime,
    job: BackgroundJob,
    token: str | None,
    *,
    function_call: FunctionCall | None = None,
) -> Any:  # noqa: ANN401 - SDK tool values are intentionally heterogeneous.
    """Decode one ready outcome and retain its claim until exact parent readback."""
    from mindroom.custom_tools.job import is_job_function  # noqa: PLC0415 - Controls also use consumption receipts.

    call = function_call or _CALL.get()
    value = decode_tool_result(job.result_payload["value"]) if job.result_payload else job.result
    owner = _OWNER.get()
    if token is None:
        return value
    if call is None or owner is None:
        await runtime.release_wait(job.job_id, token)
        return value
    try:
        value = _merge_session_state(value, job, call)
        await record_tool_job_receipt(runtime, job, token, function_call=call)
    except BaseException:
        await runtime.release_wait(job.job_id, token)
        raise
    if job.result_payload and job.result_payload.get("control"):
        control = decode_tool_result(job.result_payload["control"])
        raise AgentRunException(control.pop("message"), **control)
    if is_job_function(call.function) and job.status == "failed":
        error = job.result_payload.get("error") if isinstance(job.result_payload, dict) else None
        raise RuntimeError(str(error or job.result or "Background tool job failed."))
    if is_job_function(call.function) and job.result_payload and job.result_payload.get("events"):
        events = decode_tool_result(job.result_payload["events"])
        if events:
            if not isinstance(value, ToolResult):
                value = ToolResult(content=str(value))
            value.metadata = {**(value.metadata or {}), "tool_job_events": events}
    return value
