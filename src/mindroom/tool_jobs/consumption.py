"""Acknowledge job result claims once the parent run has saved the exact tool call's result."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.delegation.background import ACKNOWLEDGED_BY, delegation_child
from mindroom.delegation.cost import receive_subagent_cost
from mindroom.llm.runs import RunRecord, TeamRunRecord
from mindroom.logging_config import get_logger
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.tool_jobs.execution_authority import current_job_call
from mindroom.tool_jobs.results import read_result_payload
from mindroom.tool_jobs.runtime import AcknowledgeWait
from mindroom.tool_system.tool_function import ToolOutcome, ToolValue

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.store import ConversationStore
    from mindroom.tool_jobs.execution_authority import JobCall
    from mindroom.tool_jobs.results import ToolResultPayload
    from mindroom.tool_jobs.runtime import BackgroundJob, JobClaim, ToolJobRuntime

_OWNER: ContextVar[ConsumptionOwner | None] = ContextVar("tool_job_consumption", default=None)
logger = get_logger(__name__)

_JOB_FAILED_TEXT = "Background tool job failed."


# LEGACY_COMPAT: Failed tool jobs saved without error text.
# Legacy format: a saved job with status ``failed`` whose result is empty and whose payload, when it has one, holds an
# empty error. The job runtime saved the text of an exception its operation raised as the job's result, and the job
# executor saved a failed call's exception text as both the result and the payload's error; that text is empty for
# exceptions such as ``TimeoutError`` and ``httpx.ReadTimeout``.
# Last legacy release: v2026.10.512; replacement: v2026.10.513, whose writers save the exception's type name when its
# text is empty.
# Handling: such a job reads as ``_JOB_FAILED_TEXT`` wherever its outcome is read: when the call that started it reads
# it, when the job tool waits for it, and when the job tool collects it, so the model reads that the call failed
# instead of an empty failure.
# Coverage: tests/test_tool_job_execution.py::test_a_reattached_call_reads_a_job_saved_failed_without_error_text_as_failed;
# tests/test_job_tools.py::test_job_wait_reads_a_job_saved_failed_without_error_text_as_failed;
# tests/test_job_tools.py::test_collect_reads_a_job_saved_failed_without_error_text_as_failed
def job_failure_text(value: Any, payload: ToolResultPayload) -> str:  # noqa: ANN401 - A consumed job's value.
    """Return the text a consumed job reports for its failure: the payload's error, else the value's text.

    ``value`` and ``payload`` are what ``consume_tool_job`` returned. Which outcomes fail a read of one job is
    ``job_outcome``'s rule.
    """
    text = value.content if isinstance(value, ToolValue) else value
    return str(payload.error or text or _JOB_FAILED_TEXT)


def job_outcome(job: BackgroundJob, value: Any, payload: ToolResultPayload) -> Any:  # noqa: ANN401 - A consumed job's value.
    """Return a completed job's value, or a failed ``ToolOutcome`` preserving media and failure-breaker semantics.

    Every other outcome fails with its reason, such as why an approval was denied, for the call that started the job
    and for a later wait on it.
    """
    if job.status == "completed":
        return value
    failure = job_failure_text(value, payload)
    media = value if isinstance(value, ToolValue) and any(value.media()) else ToolValue(content=failure)
    return ToolOutcome(
        content=failure,
        images=media.images,
        audios=media.audios,
        videos=media.videos,
        files=media.files,
        is_error=True,
        counts_as_tool_failure=payload.counts_as_tool_failure,
    )


@dataclass(frozen=True, slots=True)
class _LoadedRun:
    team: bool
    finished_call_ids: frozenset[str]


def _finished_call_ids(run: RunRecord | TeamRunRecord) -> frozenset[str]:
    """Collect every tool call ID finished with a result in a saved agent or team run.

    A run stopped or failed after the call is saved without its tools when it did not stream, but keeps the
    call's result message, so either counts.
    """
    call_ids = {
        tool.tool_call_id
        for tool in run.tools or ()
        if tool.tool_call_id is not None and not tool.is_paused and tool.result is not None
    }
    call_ids.update(
        message.tool_call_id
        for message in run.messages or ()
        if message.role == "tool" and message.tool_call_id is not None and not message.from_history
    )
    return frozenset(call_ids)


def _load_runs(storage: ConversationStore, run_ids: Sequence[str]) -> dict[str, _LoadedRun | None | Exception]:
    loaded: dict[str, _LoadedRun | None | Exception] = {}
    for run_id in run_ids:
        try:
            run = storage.get_run(run_id)
            if isinstance(run, RunRecord | TeamRunRecord):
                loaded[run_id] = _LoadedRun(
                    team=isinstance(run, TeamRunRecord),
                    finished_call_ids=_finished_call_ids(run),
                )
            else:
                loaded[run_id] = None
        except Exception as error:
            loaded[run_id] = error
    return loaded


def _log_unconfirmed(job_id: str, run_id: str | None, error: Exception) -> None:
    logger.warning(
        "Tool result persistence was not confirmed",
        job_id=job_id,
        run_id=run_id,
        exc_info=error,
    )


async def _release_by_runtime(items: Sequence[tuple[ToolJobRuntime, str, JobClaim]]) -> None:
    by_runtime: dict[ToolJobRuntime, list[tuple[str, JobClaim]]] = {}
    for runtime, job_id, claim in items:
        by_runtime.setdefault(runtime, []).append((job_id, claim))
    for runtime, waits in by_runtime.items():
        await runtime.release_waits(waits)


def _saved_by_runtime(
    consumptions: Sequence[_Consumption],
    loaded_runs: dict[str, _LoadedRun | None | Exception],
) -> dict[ToolJobRuntime, list[_Consumption]]:
    by_runtime: dict[ToolJobRuntime, list[_Consumption]] = {}
    for consumption in consumptions:
        loaded = loaded_runs.get(consumption.run_id)
        if isinstance(loaded, Exception):
            _log_unconfirmed(consumption.job.job_id, consumption.run_id, loaded)
            continue
        if loaded is not None and loaded.team == consumption.team and consumption.call_id in loaded.finished_call_ids:
            by_runtime.setdefault(consumption.runtime, []).append(consumption)
    return by_runtime


async def _acknowledge_batch(runtime: ToolJobRuntime, batch: Sequence[_Consumption]) -> bool:
    waits = [
        AcknowledgeWait(
            consumption.job.job_id,
            consumption.claim,
            returned_inline=consumption.returned_inline,
            # A tool's job keeps the adapter it was admitted with, which a call that reattaches to it matches.
            adapter={ACKNOWLEDGED_BY: consumption.run_id} if consumption.job.kind == "delegation" else None,
        )
        for consumption in batch
    ]
    try:
        errors = await runtime.acknowledge_waits(waits)
    except Exception as error:
        for consumption in batch:
            _log_unconfirmed(consumption.job.job_id, consumption.run_id, error)
        return False
    if errors:
        run_ids_by_job = {consumption.job.job_id: consumption.run_id for consumption in batch}
        for job_id, ack_error in errors:
            _log_unconfirmed(job_id, run_ids_by_job.get(job_id), ack_error)
    return True


@dataclass(frozen=True)
class _Consumption:
    runtime: ToolJobRuntime
    job: BackgroundJob
    claim: JobClaim
    run_id: str
    team: bool
    call_id: str
    returned_inline: bool


@dataclass
class ConsumptionOwner:
    """Claims retained across every attempt of one parent response."""

    storage_factory: Callable[[], ConversationStore] | None = None
    _claims: list[_Consumption] = field(default_factory=list)

    def register(
        self,
        runtime: ToolJobRuntime,
        job: BackgroundJob,
        claim: JobClaim,
        *,
        run_id: str,
        team: bool,
        call_id: str,
        returned_inline: bool,
    ) -> None:
        """Keep a claim until the parent run ``run_id`` is saved with its tool call ``call_id`` finished."""
        self._claims.append(_Consumption(runtime, job, claim, run_id, team, call_id, returned_inline))
        if returned_inline:
            runtime.hold_inline(job.job_id, claim)

    async def _acknowledge_saved_run(self, run: AnyRunRecord) -> None:
        """Acknowledge claims already proved by a durably saved run record."""
        if not self._claims:
            return
        team = isinstance(run, TeamRunRecord)
        finished = _finished_call_ids(run)
        by_runtime: dict[ToolJobRuntime, list[_Consumption]] = {}
        remaining: list[_Consumption] = []
        for consumption in self._claims:
            if consumption.run_id == run.run_id and consumption.team == team and consumption.call_id in finished:
                by_runtime.setdefault(consumption.runtime, []).append(consumption)
            else:
                remaining.append(consumption)
        if not by_runtime:
            return
        self._claims = remaining

        async def settle() -> None:
            for runtime, batch in by_runtime.items():
                if not await _acknowledge_batch(runtime, batch):
                    self._claims.extend(batch)

        await run_coroutine_until_complete(settle())

    async def finalize(self) -> None:
        """Acknowledge exact saved rows; release missing, failed, or unsaved evidence."""
        consumptions, self._claims = self._claims, []
        if not consumptions:
            return
        try:
            if self.storage_factory is None:
                return
            run_ids = tuple(dict.fromkeys(consumption.run_id for consumption in consumptions))
            try:
                loaded_runs = await run_session_storage_operation(
                    self.storage_factory,
                    lambda storage: _load_runs(storage, run_ids),
                )
            except Exception as error:
                for consumption in consumptions:
                    _log_unconfirmed(consumption.job.job_id, consumption.run_id, error)
                return
            for runtime, batch in _saved_by_runtime(consumptions, loaded_runs).items():
                await _acknowledge_batch(runtime, batch)
        finally:
            await _release_by_runtime([(c.runtime, c.job.job_id, c.claim) for c in consumptions])


@contextmanager
def consumption_context(owner: ConsumptionOwner) -> Iterator[None]:
    """Bind the response's claim owner for one call or stream pull."""
    token = _OWNER.set(owner)
    try:
        yield
    finally:
        _OWNER.reset(token)


def set_consumption_storage(storage_factory: Callable[[], ConversationStore] | None) -> None:
    """Bind registered canonical storage after the response loads its scope."""
    owner = _OWNER.get()
    if owner is not None:
        owner.storage_factory = storage_factory


async def acknowledge_saved_run(run: AnyRunRecord) -> None:
    """Acknowledge claims already proved by ``run`` after its canonical save succeeded."""
    owner = _OWNER.get()
    if owner is not None:
        await owner._acknowledge_saved_run(run)


async def finalize_consumption() -> None:
    """Settle the response's job claims against the parent runs its attempts saved."""
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


def _apply_state_change(state: dict[str, Any], key: str, change: dict[str, Any]) -> bool:
    """Apply one baseline-checked key change to ``state``; return False when the live value no longer matches."""
    if (key in state) != change["before_present"] or state.get(key) != change["before"]:
        return False
    if change["present"]:
        state[key] = change["value"]
    else:
        state.pop(key, None)
    return True


def _apply_prepared_changes(state: dict[str, Any], changes: dict[str, dict[str, Any]]) -> None:
    """Apply accepted per-key changes against live ``state``, skipping any key whose baseline moved."""
    for key, change in changes.items():
        _apply_state_change(state, key, change)


def _merge_session_state(
    state: dict[str, Any],
    value: Any,  # noqa: ANN401
    job: BackgroundJob,
    payload: ToolResultPayload,
    accepted: dict[str, dict[str, Any]],
) -> Any:  # noqa: ANN401
    """Apply unconflicted state changes to ``state``, reporting conflicts in a new value that leaves the payload intact."""
    if job.consumed or job.status != "completed":
        return value
    conflicts: list[str] = []
    for key, change in payload.state_delta.items():
        if _apply_state_change(state, key, change):
            if key in accepted:
                # Preserve the original live baseline and advance only the final target across chained jobs.
                accepted[key] = {**accepted[key], "present": change["present"], "value": change["value"]}
            else:
                accepted[key] = change
        else:
            conflicts.append(key)
    if conflicts:
        warning = "Session state conflicts: " + ", ".join(sorted(conflicts))
        if isinstance(value, ToolValue):
            return replace(value, content=f"{value.content}\n{warning}")
        return ToolValue(content=f"{value}\n{warning}")
    return value


def _require_call_identity(call: JobCall) -> tuple[str, str]:
    run_id, call_id = call.context.run_id, call.context.call_id
    if not run_id or not call_id:
        msg = "Tool result consumption requires the exact run and tool call"
        raise ValueError(msg)
    return run_id, call_id


@dataclass(frozen=True, slots=True)
class PreparedConsumption:
    """One staged outcome after merging its state delta against a fresh copy of live session state."""

    job: BackgroundJob
    value: Any
    payload: ToolResultPayload


@dataclass(frozen=True, slots=True)
class _StagedPayload:
    runtime: ToolJobRuntime
    job: BackgroundJob
    claim: JobClaim
    payload: ToolResultPayload
    returned_inline: bool
    subagent_cost: tuple[str, float | None] | None


@dataclass
class StagedConsumptions:
    """Payloads read by one tool call whose session state, cost, and claims commit only if the call returns them."""

    job_call: JobCall | None = None
    _items: list[_StagedPayload] = field(default_factory=list, init=False)
    _prepared_changes: dict[str, dict[str, Any]] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        """Capture the active management call when none was supplied explicitly."""
        if self.job_call is None:
            self.job_call = current_job_call()

    async def stage(
        self,
        runtime: ToolJobRuntime,
        job: BackgroundJob,
        claim: JobClaim,
        *,
        returned_inline: bool = False,
    ) -> int:
        """Read one claimed outcome without touching session state, subagent cost, or retaining its claim yet."""
        owner = _OWNER.get()
        try:
            payload = await read_result_payload(runtime, job)
            if self.job_call is not None and owner is not None:
                _require_call_identity(self.job_call)
            subagent_cost = (
                (child.delegation_id, child.cost_usd)
                if job.kind == "delegation" and not job.consumed and (child := delegation_child(job))
                else None
            )
        except BaseException:
            await runtime.release_wait(job.job_id, claim)
            raise
        self._prepared_changes = None
        self._items.append(_StagedPayload(runtime, job, claim, payload, returned_inline, subagent_cost))
        return len(self._items) - 1

    def prepare_values(self) -> list[PreparedConsumption]:
        """Merge all staged payloads synchronously against a fresh copy of live session state."""
        owner = _OWNER.get()
        staged_state = (
            dict(self.job_call.context.session_state)
            if self.job_call is not None
            and owner is not None
            and any(
                not item.job.consumed and item.job.status == "completed" and item.payload.state_delta
                for item in self._items
            )
            else None
        )
        accepted: dict[str, dict[str, Any]] = {}
        prepared = [
            PreparedConsumption(
                job=item.job,
                value=(
                    _merge_session_state(staged_state, item.payload.value, item.job, item.payload, accepted)
                    if staged_state is not None
                    else item.payload.value
                ),
                payload=item.payload,
            )
            for item in self._items
        ]
        self._prepared_changes = accepted
        return prepared

    async def commit(self) -> None:
        """Apply prepared session-state changes, subagent costs, and claim retentions."""
        if not self._items:
            return
        owner, call = _OWNER.get(), self.job_call
        if owner is None or call is None:
            await _release_by_runtime([(item.runtime, item.job.job_id, item.claim) for item in self._items])
            items, self._items, self._prepared_changes = self._items, [], None
            for item in items:
                if item.subagent_cost is not None:
                    receive_subagent_cost(*item.subagent_cost)
            return
        try:
            run_id, call_id = _require_call_identity(call)
        except ValueError:
            await self.release()
            raise
        if self._prepared_changes is None:
            self.prepare_values()
        items, prepared_changes = self._items, self._prepared_changes
        self._items, self._prepared_changes = [], None
        if prepared_changes:
            _apply_prepared_changes(call.context.session_state, prepared_changes)
        for item in items:
            if item.subagent_cost is not None:
                # The turn that reads a subagent job's result first receives what its child cost.
                receive_subagent_cost(*item.subagent_cost)
            owner.register(
                item.runtime,
                item.job,
                item.claim,
                run_id=run_id,
                team=call.team,
                call_id=call_id,
                returned_inline=item.returned_inline,
            )

    async def release(self) -> None:
        """Release every still-staged claim in one lock acquisition per runtime."""
        items, self._items, self._prepared_changes = self._items, [], None
        if items:
            await _release_by_runtime([(item.runtime, item.job.job_id, item.claim) for item in items])


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
    staging = StagedConsumptions(job_call)
    try:
        await staging.stage(runtime, job, claim, returned_inline=returned_inline)
        (prepared,) = staging.prepare_values()
        await staging.commit()
    except BaseException:
        await staging.release()
        raise
    return prepared.value, prepared.payload
