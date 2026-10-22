"""Acknowledge job result claims once the parent run has saved the exact tool call's result."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.llm.runs import RunRecord, TeamRunRecord
from mindroom.logging_config import get_logger
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.tool_jobs.execution_authority import current_job_call
from mindroom.tool_jobs.results import read_result_payload
from mindroom.tool_system.tool_function import ToolValue

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from mindroom.session_storage.store import ConversationStore
    from mindroom.tool_jobs.execution_authority import JobCall
    from mindroom.tool_jobs.results import ToolResultPayload
    from mindroom.tool_jobs.runtime import BackgroundJob, JobClaim, ToolJobRuntime

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
    returned_inline: bool

    def saved(self, storage: ConversationStore) -> bool:
        """Find the exact tool call, finished with a result, in the saved parent run of the calling agent or team.

        A run stopped or failed after the call is saved without its tools when it did not stream, but keeps the
        call's result message, so either counts.
        """
        run = storage.get_run(self.run_id)
        if not isinstance(run, TeamRunRecord if self.team else RunRecord):
            return False
        return any(
            tool.tool_call_id == self.call_id and not tool.is_paused and tool.result is not None
            for tool in run.tools or []
        ) or any(message.role == "tool" and message.tool_call_id == self.call_id for message in run.messages or [])


@dataclass
class ConsumptionOwner:
    """Claims retained across every attempt of one parent response."""

    storage_factory: Callable[[], ConversationStore] | None = None
    _claims: list[_Consumption] = field(default_factory=list)

    def register(
        self,
        runtime: ToolJobRuntime,
        job_id: str,
        claim: JobClaim,
        *,
        run_id: str,
        team: bool,
        call_id: str,
        returned_inline: bool,
    ) -> None:
        """Keep a claim until the parent run ``run_id`` is saved with its tool call ``call_id`` finished."""
        self._claims.append(_Consumption(runtime, job_id, claim, run_id, team, call_id, returned_inline))
        if returned_inline:
            runtime.hold_inline(job_id, claim)

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
                        returned_inline=consumption.returned_inline,
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


def set_consumption_storage(storage_factory: Callable[[], ConversationStore] | None) -> None:
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


def _merge_session_state(value: Any, job: BackgroundJob, payload: ToolResultPayload, call: JobCall) -> Any:  # noqa: ANN401
    """Apply unconflicted state changes, reporting conflicts in a new value that leaves the payload intact."""
    if job.consumed or job.status != "completed":
        return value
    state = call.context.session_state
    conflicts = []
    for key, change in payload.state_delta.items():
        if (key in state) != change["before_present"] or state.get(key) != change["before"]:
            conflicts.append(key)
        elif change["present"]:
            state[key] = change["value"]
        else:
            state.pop(key, None)
    if conflicts:
        warning = "Session state conflicts: " + ", ".join(sorted(conflicts))
        if isinstance(value, ToolValue):
            return replace(value, content=f"{value.content}\n{warning}")
        return ToolValue(content=f"{value}\n{warning}")
    return value


async def _retain_claim(
    runtime: ToolJobRuntime,
    job_id: str,
    claim: JobClaim,
    call: JobCall | None,
    *,
    returned_inline: bool,
) -> None:
    """Keep a claim until the parent run saves the exact tool call, releasing it when no such run will be saved."""
    owner = _OWNER.get()
    if call is None or owner is None:
        await runtime.release_wait(job_id, claim)
        return
    if not call.context.run_id or not call.context.call_id:
        await runtime.release_wait(job_id, claim)
        msg = "Tool result consumption requires the exact run and tool call"
        raise ValueError(msg)
    owner.register(
        runtime,
        job_id,
        claim,
        run_id=call.context.run_id,
        team=call.team,
        call_id=call.context.call_id,
        returned_inline=returned_inline,
    )


async def retain_parent_call_claim(
    runtime: ToolJobRuntime,
    job_id: str,
    claim: JobClaim,
    *,
    run_id: str,
    team: bool,
    call_id: str,
) -> None:
    """Keep a claim until the parent run saves its tool call ``call_id``, the call that started the job."""
    owner = _OWNER.get()
    if owner is None or not run_id or not call_id:
        await runtime.release_wait(job_id, claim)
        return
    owner.register(runtime, job_id, claim, run_id=run_id, team=team, call_id=call_id, returned_inline=True)


async def consume_tool_job(
    runtime: ToolJobRuntime,
    job: BackgroundJob,
    claim: JobClaim,
    *,
    job_call: JobCall | None = None,
    returned_inline: bool = False,
) -> tuple[Any, ToolResultPayload]:
    """Read a claimed outcome's value and payload, retaining the claim until the parent run saves this tool call.

    ``job_call`` is the call that consumes it, the management call running now when omitted; ``returned_inline``
    says this tool call is the one that started the job.
    """
    call = job_call or current_job_call()
    try:
        payload = await read_result_payload(runtime, job)
        value = payload.value
        if call is not None and _OWNER.get() is not None:
            value = _merge_session_state(value, job, payload, call)
    except BaseException:
        await runtime.release_wait(job.job_id, claim)
        raise
    await _retain_claim(runtime, job.job_id, claim, call, returned_inline=returned_inline)
    return value, payload
