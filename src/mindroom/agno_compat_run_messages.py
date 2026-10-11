"""Preserve Agno's current request messages when a run is interrupted."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from functools import wraps
from importlib.metadata import version
from typing import TYPE_CHECKING, Any, cast

from agno.agent import _run as agent_run
from agno.metrics import MessageMetrics, accumulate_model_metrics
from agno.models.base import MessageData, Model
from agno.models.response import ModelResponse
from agno.run import cancel as agno_cancel
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.team import _default_tools as team_tools
from agno.team import _run as team_run

from mindroom.usage_storage import has_token_usage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

    from agno.agent import Agent
    from agno.models.message import Message
    from agno.run.agent import RunOutput
    from agno.run.base import RunContext
    from agno.run.messages import RunMessages
    from agno.session.team import TeamSession
    from agno.team import Team
    from pydantic import BaseModel

_SUPPORTED_VERSION = "3.0.9"
_PATCHED = False
_LOCK = threading.Lock()


@dataclass(frozen=True, eq=False)
class _ModelRequest:
    """Provider stream state that terminal cleanup needs if the stream is abandoned."""

    model: Model
    messages: list[Message]
    assistant_message: Message
    stream_data: MessageData
    run_response: RunOutput | TeamRunOutput | None


# Keyed by run identity; one run streams at most one model request at a time.
_ACTIVE_REQUESTS: dict[int, _ModelRequest] = {}


# AGNO_COMPAT: Stopping an async run abandons its suspended model stream.
# Reason: Agno's async run and model generators iterate nested streams without closing
# them. Consumer closure or a cancellation check between chunks persists the run while
# the model stream is suspended; garbage collection later finalizes that chain outermost
# first, so received usage misses the run, session, and request totals.
# Upstream issue: No matching issue identified; https://github.com/agno-agi/agno/issues/9489
# covers related abandoned-generation bookkeeping, not usage settlement.
# Upstream PR: None identified.
# Remove when: Agno closes in-flight model streams innermost first before terminal
# cancellation or error persistence, including consumer closure between chunks.
# Coverage: tests/test_openai_responses_stream.py::test_received_request_usage_survives_abandoned_stream;
# tests/test_openai_responses_stream.py::test_received_request_usage_survives_cancel_request.
def _settle_abandoned_request(run_response: RunOutput | TeamRunOutput) -> None:
    request = _ACTIVE_REQUESTS.pop(id(run_response), None)
    if request is None:
        return
    settled = request.assistant_message.model_copy()
    # Reuse provider accounting, including counters retained from failed attempts.
    request.model._populate_assistant_message_from_stream_data(
        settled,
        MessageData(response_metrics=request.stream_data.response_metrics),
    )
    # Late finalization of the abandoned stream must not count this request again.
    request.assistant_message.metrics = MessageMetrics()
    if not has_token_usage(settled.metrics.to_dict()):
        return
    request.messages.append(settled)
    accumulate_model_metrics(
        ModelResponse(response_usage=settled.metrics),
        request.model,
        request.model.model_type,
        run_response.metrics,
    )


# AGNO_COMPAT: Terminal cleanup retains stale checkpoint or continuation messages.
# Reason: Agent and Team flush in-flight messages only if the saved list is empty,
# so later requests disappear from failed/cancelled runs despite retained totals.
# Upstream issue: No matching issue identified; terminal snapshot refresh is untracked.
# Upstream PR: None identified.
# Remove when: Both error and cancellation cleanup save the latest in-flight
# messages, including resumed runs, while respecting add_to_agent_memory.
# Coverage: tests/test_agno_compat_run_messages.py::test_terminal_snapshot_keeps_requests_after_checkpoint;
# tests/test_agno_compat_run_messages.py::test_interrupted_continuation_exports_every_completed_request.
def _flush_messages(run_response: RunOutput | TeamRunOutput, run_messages: RunMessages | None) -> None:
    _settle_abandoned_request(run_response)
    _hold_member_run(run_response)
    if run_messages is not None:
        run_response.messages = [message for message in run_messages.messages if message.add_to_agent_memory]


def _with_current_messages(original: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(original)
    def cancel(
        run_response: RunOutput | TeamRunOutput,
        error: BaseException,
        run_messages: RunMessages | None = None,
        *args: object,
        **kwargs: object,
    ) -> RunOutput | TeamRunOutput:
        _settle_abandoned_request(run_response)
        _hold_member_run(run_response)
        if run_messages is not None:
            # Let Agno retain its partial-content, approval, and member cleanup.
            run_response.messages = None
        return original(run_response, error, run_messages, *args, **kwargs)

    return cancel


# AGNO_COMPAT: A team member stopped or failing mid-stream never reaches its team run.
# Reason: A delegated member run is saved only through the team run it is attached to,
# and Agno attaches it only when the member hands back its run output. A member whose
# run is hard-cancelled re-raises without one, and a streaming member that fails yields
# only an error event, so all of that member's usage is lost from every report. A
# hard-cancelled team run also leaves its members' delegation tasks running, so they
# finish, still spending, after the team run is saved; stopping them reads Agno's
# private per-run task set. MindRoom runs teams only asynchronously, so the
# synchronous delegation path is left alone. The hold runs from the terminal-snapshot
# and cancellation hooks in this module, so removing those must keep calling it.
# Upstream issue: No matching issue identified; member-run attachment on cancellation
# and streaming errors, and stopping member tasks of a cancelled team run, are untracked.
# Upstream PR: None identified.
# Remove when: Agno attaches every delegated member run to its team run when the member
# is cancelled or fails, in streaming and non-streaming delegation, exactly once, and
# stops in-flight member tasks before saving a cancelled team run.
# Coverage: tests/test_team_member_usage.py::test_stopped_team_member_keeps_its_usage;
# tests/test_team_member_usage.py::test_failed_team_member_keeps_its_usage_once;
# tests/test_team_member_usage.py::test_finished_team_member_counts_once;
# tests/test_team_member_usage.py::test_member_stopped_after_approval_keeps_its_usage.
_MEMBER_TEAM_RUN_IDS: dict[str, str] = {}
_UNATTACHED_MEMBER_RUNS: dict[str, list[RunOutput | TeamRunOutput]] = {}


def _with_member_team_run(original: Callable[[str, str], Awaitable[None]]) -> Callable[[str, str], Awaitable[None]]:
    @wraps(original)
    async def register(team_run_id: str, member_run_id: str) -> None:
        _MEMBER_TEAM_RUN_IDS[member_run_id] = team_run_id
        await original(team_run_id, member_run_id)

    return register


def _hold_member_run(run_response: RunOutput | TeamRunOutput) -> None:
    """Keep a stopped or failed member run for its team run, which may never receive it otherwise."""
    team_run_id = _MEMBER_TEAM_RUN_IDS.pop(run_response.run_id, None) if run_response.run_id else None
    if team_run_id is not None:
        _UNATTACHED_MEMBER_RUNS.setdefault(team_run_id, []).append(run_response)


async def _attach_member_run(
    team: Team,
    session: TeamSession,
    team_response: TeamRunOutput,
    member_run: RunOutput | TeamRunOutput,
) -> None:
    """Attach a member run the way Agno's delegation does when the member hands it back.

    A member resumed after an approval may already be attached as the run loaded from storage, so its entry is
    replaced by the live run and its session row is saved again.
    """
    member_run.parent_run_id = team_response.run_id
    responses = team_response.member_responses
    index = next((index for index, attached in enumerate(responses) if attached.run_id == member_run.run_id), None)
    if index is None:
        team_response.add_member_run(member_run)
    else:
        responses[index] = member_run
    member_id = member_run.team_id if isinstance(member_run, TeamRunOutput) else member_run.agent_id
    members = cast("list[Agent | Team]", team.members) if isinstance(team.members, list) else []
    member = next((member for member in members if member.id == member_id), None)
    if member is not None and not (member.store_media and member.store_tool_messages and member.store_history_messages):
        # Agno's delegation scrubs nested team members with the same agent helper.
        agent_run.scrub_run_output_for_storage(cast("Agent", member), run_response=cast("RunOutput", member_run))
    # Agno saves member rows from the team session's runs, as a member replays its own history from them.
    session.upsert_run(await team_run._amember_run_for_storage(team, session, member_run))


def _with_member_runs(original: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    @wraps(original)
    async def cleanup_and_store(
        team: Team,
        run_response: TeamRunOutput,
        session: TeamSession,
        run_context: RunContext | None = None,
    ) -> None:
        if run_response.run_id and run_response.status == RunStatus.cancelled:
            # A stopped team run leaves its members' delegation tasks running; stop them so they hand over their runs.
            for task in agno_cancel._member_drain_tasks.get(run_response.run_id, set()):
                task.cancel()
            await agno_cancel.adrain_member_tasks(run_response.run_id)
        for member_run in _UNATTACHED_MEMBER_RUNS.pop(run_response.run_id, []) if run_response.run_id else []:
            await _attach_member_run(team, session, run_response, member_run)
        for member_run_id in [key for key, value in _MEMBER_TEAM_RUN_IDS.items() if value == run_response.run_id]:
            del _MEMBER_TEAM_RUN_IDS[member_run_id]
        await original(team, run_response=run_response, session=session, run_context=run_context)

    return cleanup_and_store


# AGNO_COMPAT: Interrupted model streams count usage without retaining its message.
# Reason: Model accumulates assistant metrics in finally but appends the message
# only after the stream returns successfully. Retain that same metered message
# before the exception reaches terminal cleanup; leave aggregate accounting alone.
# Upstream issue: https://github.com/agno-agi/agno/issues/9489 describes the same
# lost-message boundary; request-detail preservation is not separately tracked.
# Upstream PR: None identified.
# Remove when: Model preserves metered assistant messages on stream failure and
# cancellation without duplicating successful requests or inventing usage.
# Coverage: tests/test_openai_responses_stream.py::test_terminal_usage_survives_stream_failure;
# tests/test_openai_responses_stream.py::test_received_request_usage_survives_task_cancellation;
# tests/test_openai_responses_stream.py::test_terminal_usage_survives_retry.
def _retain_metered_message(messages: list[Message], assistant_message: Message) -> None:
    if has_token_usage(assistant_message.metrics.to_dict()) and not any(
        message is assistant_message for message in messages
    ):
        messages.append(assistant_message)


# AGNO_COMPAT: Request messages omit the model that incurred their usage.
# Reason: Run-level model details can span multiple models after continuation,
# so request timestamps and counters alone cannot be attributed to one model.
# Upstream issue: None identified; per-request attribution is an extension gap.
# Upstream PR: None identified.
# Remove when: Agno persists each assistant request's model and provider.
# Coverage: tests/test_request_usage.py::test_mixed_model_requests_keep_models_and_actual_dates;
# tests/test_openai_responses_stream.py::test_mixed_model_failed_stream_keeps_request_attribution.
def _record_request_model(model: Model, assistant_message: Message) -> None:
    assistant_message.provider_data = {
        **(assistant_message.provider_data or {}),
        "mindroom_model": {"id": model.id, "provider": model.get_provider()},
    }


def _with_request_model[T](original: Callable[..., T]) -> Callable[..., T]:
    @wraps(original)
    def populate(model: Model, assistant_message: Message, *args: object, **kwargs: object) -> T:
        result = original(model, assistant_message, *args, **kwargs)
        _record_request_model(model, assistant_message)
        return result

    return populate


def _begin_request(request: _ModelRequest) -> None:
    _record_request_model(request.model, request.assistant_message)
    if request.run_response is not None:
        _ACTIVE_REQUESTS[id(request.run_response)] = request


def _finish_request(request: _ModelRequest) -> bool:
    """Return whether the stream still owns its request, rather than terminal cleanup."""
    if request.run_response is None:
        return True
    key = id(request.run_response)
    if _ACTIVE_REQUESTS.get(key) is not request:
        return False
    del _ACTIVE_REQUESTS[key]
    return True


def _with_metered_messages(
    original: Callable[..., Iterator[ModelResponse]],
) -> Callable[..., Iterator[ModelResponse]]:
    @wraps(original)
    def stream(
        model: Model,
        messages: list[Message],
        assistant_message: Message,
        stream_data: MessageData,
        response_format: dict[str, Any] | type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        run_response: RunOutput | TeamRunOutput | None = None,
        compress_tool_results: bool = False,
    ) -> Iterator[ModelResponse]:
        request = _ModelRequest(model, messages, assistant_message, stream_data, run_response)
        _begin_request(request)
        try:
            yield from original(
                model,
                messages,
                assistant_message,
                stream_data,
                response_format=response_format,
                tools=tools,
                tool_choice=tool_choice,
                run_response=run_response,
                compress_tool_results=compress_tool_results,
            )
        except BaseException:
            if _finish_request(request):
                _retain_metered_message(messages, assistant_message)
            raise
        _finish_request(request)

    return stream


def _with_metered_messages_async(
    original: Callable[..., AsyncIterator[ModelResponse]],
) -> Callable[..., AsyncIterator[ModelResponse]]:
    @wraps(original)
    async def stream(
        model: Model,
        messages: list[Message],
        assistant_message: Message,
        stream_data: MessageData,
        response_format: dict[str, Any] | type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        run_response: RunOutput | TeamRunOutput | None = None,
        compress_tool_results: bool = False,
    ) -> AsyncIterator[ModelResponse]:
        request = _ModelRequest(model, messages, assistant_message, stream_data, run_response)
        _begin_request(request)
        try:
            async for response in original(
                model,
                messages,
                assistant_message,
                stream_data,
                response_format=response_format,
                tools=tools,
                tool_choice=tool_choice,
                run_response=run_response,
                compress_tool_results=compress_tool_results,
            ):
                yield response
        except BaseException:
            if _finish_request(request):
                _retain_metered_message(messages, assistant_message)
            raise
        _finish_request(request)

    return stream


def install_patch() -> None:
    """Install the repair once for the pinned SDK before using owned storage."""
    global _PATCHED
    with _LOCK:
        if _PATCHED:
            return
        if version("agno") != _SUPPORTED_VERSION:
            msg = "Unsupported Agno interrupted-message implementation"
            raise RuntimeError(msg)
        agent_run.flush_in_flight_messages_on_error = cast("Any", _flush_messages)
        team_run.flush_in_flight_messages_on_error_team = cast("Any", _flush_messages)
        agent_run._handle_run_cancellation = cast("Any", _with_current_messages(agent_run._handle_run_cancellation))
        team_run._handle_team_run_cancellation = cast(
            "Any",
            _with_current_messages(team_run._handle_team_run_cancellation),
        )
        # Delegation and approval continuations each register member runs through their own module's binding.
        team_tools.aregister_member_run = cast("Any", _with_member_team_run(team_tools.aregister_member_run))
        team_run.aregister_member_run = cast("Any", _with_member_team_run(team_run.aregister_member_run))
        team_run._acleanup_and_store = cast("Any", _with_member_runs(team_run._acleanup_and_store))
        Model.process_response_stream = cast("Any", _with_metered_messages(Model.process_response_stream))
        Model.aprocess_response_stream = cast("Any", _with_metered_messages_async(Model.aprocess_response_stream))
        Model._populate_assistant_message = cast("Any", _with_request_model(Model._populate_assistant_message))
        Model._populate_assistant_message_from_stream_data = cast(
            "Any",
            _with_request_model(Model._populate_assistant_message_from_stream_data),
        )
        _PATCHED = True
