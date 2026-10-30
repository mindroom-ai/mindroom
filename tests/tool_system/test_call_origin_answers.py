"""What each call origin answers for outcomes its own tests leave unpinned, read at that origin's seam.

The origins are the model loop, CLI-dispatched calls, scheduled calls, background script calls, the MCP gateway,
voice calls, and tool jobs. Each runs a call through ``run_tool_call`` and answers from its ``CallResult`` its own
way, so these pins hold each origin's answer in place.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import TYPE_CHECKING, NoReturn

import pytest

from mindroom.agent_loop.tools import PlannedCall, dispatch_tool_calls
from mindroom.hooks import EVENT_TOOL_BEFORE_CALL, HookRegistry, ToolBeforeCallContext, hook
from mindroom.matrix_rtc.call_tools import _wrap_tool_function
from mindroom.mcp_gateway import tools as gateway
from mindroom.message_target import MessageTarget
from mindroom.oauth.providers import OAuthConnectionRequired
from mindroom.script_runs.models import ScriptCallState
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_system.agent_tool_calls import AgentToolCallStep
from mindroom.tool_system.agno_toolkits import Function, Toolkit, adapt_function
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.tool_function import ToolOutcome
from tests.agent_loop.helpers import context as run_context
from tests.agent_tool_helpers import detached_policy
from tests.conftest import SETTLE_TIMEOUT_SECONDS, test_runtime_paths
from tests.test_agent_tool_calls import _catalog, _events, _toolkit
from tests.test_matrix_rtc_call_tools import (
    _CALL_CONTEXT,
    _EMPTY_SCHEMA,
    _USAGE_OWNER,
    AGENT,
    _authorized_call_operation,
    _unconnected,
)
from tests.test_matrix_rtc_call_tools import _config as _voice_config
from tests.test_matrix_rtc_call_tools import _function as _voice_function
from tests.test_matrix_rtc_call_tools import _runtime_context as _voice_runtime_context
from tests.test_mcp_gateway_tools import (
    _replace_calculator,
    context,  # noqa: F401 - the gateway tests' agent fixture
)
from tests.test_scheduled_tool_approval import _armed, _binding_column, _live_agent, _manager, _run
from tests.test_script_tool_broker import _broker, _call_through_gateway, _replace_calculator_toolkit, _request
from tests.test_tool_hooks import _plugin
from tests.test_tool_job_execution import _managed_context
from tests.tool_job_helpers import call_job_tool, job_actor, job_tool

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import Literal

    from mindroom.api.connection_agents import AgentToolContext
    from mindroom.event_journal import EventJournalStore
    from mindroom.tool_system.invocation import ToolMiddleware

_OAUTH_MESSAGE = "Connect Google to continue."
_CONNECT_URL = "https://mindroom.example/oauth/google/connect"
# What the tool-call policy answers a call whose tool raised ``OAuthConnectionRequired`` with.
_OAUTH_PAYLOAD: dict[str, object] = {
    "error": _OAUTH_MESSAGE,
    "oauth_connection_required": True,
    "provider": "google",
    "connect_url": _CONNECT_URL,
}


def _oauth_required() -> NoReturn:
    raise OAuthConnectionRequired(_OAUTH_MESSAGE, provider_id="google", connect_url=_CONNECT_URL)


def _policy(*hooks: object) -> ToolMiddleware:
    """Return the tool-call policy an agent's calls run under, with ``hooks`` as one plugin's hooks."""
    registry = HookRegistry.from_plugins([_plugin("policy", list(hooks))]) if hooks else HookRegistry.empty()
    return detached_policy(registry)


@pytest.mark.asyncio
async def test_the_model_reads_an_oauth_requirement_as_a_successful_call_whose_text_is_the_payloads_repr() -> None:
    """The model reads an OAuth requirement as a successful call whose text is the payload dict's Python repr."""

    def search_mail(query: str) -> str:
        del query
        _oauth_required()

    function = adapt_function(Function.from_callable(search_mail))
    planned = PlannedCall(function=function, execution=function.planned_execution("call-1", {"query": "inbox"}))

    [answer] = await dispatch_tool_calls([planned], run=run_context(), middleware=[_policy()])

    assert answer.content == repr(_OAUTH_PAYLOAD)
    assert answer.tool_call_error is False
    assert planned.execution.is_error is False


@pytest.mark.asyncio
async def test_a_cli_call_completes_an_oauth_requirement_with_the_payloads_repr(tmp_path: Path) -> None:
    """A CLI-dispatched call completes, not failed, with the OAuth payload dict's Python repr as its result."""

    def search_mail() -> str:
        _oauth_required()

    catalog = await _catalog(tmp_path, [_toolkit("mail", search_mail)], middleware=(_policy(),))

    *_, completed = await _events(catalog, "mail", "search_mail")

    assert isinstance(completed, AgentToolCallStep)
    assert completed.kind == "completed"
    assert completed.call.result == repr(_OAUTH_PAYLOAD)
    assert completed.call.is_error is False


class _AsyncOAuthSlackTools(Toolkit):
    """A configured toolkit whose one async function needs an OAuth connection."""

    def __init__(self) -> None:
        super().__init__(name="slack", tools=[self.post_slack_message])

    async def post_slack_message(self, channel: str, text: str = "") -> str:
        """Post a Slack message.

        Args:
            channel: Channel or user ID.
            text: Message text.

        """
        del channel, text
        _oauth_required()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["async", "sync"])
async def test_a_scheduled_call_answers_an_oauth_requirement_with_the_payload_whatever_its_body(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    body: Literal["async", "sync"],
) -> None:
    """An OAuth requirement answers the runner with the payload dict, journaled completed, for an async or sync body.

    The runner waits for a sync body it started without re-raising that body's exception, which the policy had already
    answered with the payload.
    """
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    oauth = OAuthConnectionRequired(_OAUTH_MESSAGE, provider_id="google", connect_url=_CONNECT_URL)
    agent = (
        _live_agent(toolkit=_AsyncOAuthSlackTools(), middleware=(_policy(),))
        if body == "async"
        else _live_agent(outcome=oauth, middleware=(_policy(),))
    )
    try:
        await _armed(manager)

        result = await _run(manager, agent)

        assert result == _OAUTH_PAYLOAD
        assert await _binding_column(journal, "outcome") == "completed"
    finally:
        await manager.shutdown()


class _BlockingSlackTools(Toolkit):
    """A configured toolkit whose one function blocks until released, then raises."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        super().__init__(name="slack", tools=[self.post_slack_message])

    def post_slack_message(self, channel: str, text: str = "") -> str:
        """Post a Slack message.

        Args:
            channel: Channel or user ID.
            text: Message text.

        """
        del channel, text
        self.started.set()
        assert self.release.wait(SETTLE_TIMEOUT_SECONDS)
        msg = "slack is down"
        raise RuntimeError(msg)


@pytest.mark.asyncio
async def test_a_cancelled_scheduled_call_whose_body_then_raises_stays_cancelled(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """A scheduled call cancelled mid-body whose body then raises stays cancelled, and is not journaled failed.

    The runner waits for the body before the cancellation propagates, and the body's exception does not replace it.
    """
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    slack = _BlockingSlackTools()
    try:
        await _armed(manager)
        running = asyncio.create_task(_run(manager, _live_agent(toolkit=slack)))
        assert await asyncio.to_thread(slack.started.wait, SETTLE_TIMEOUT_SECONDS)

        running.cancel()
        slack.release.set()
        with pytest.raises(asyncio.CancelledError):
            await running

        assert await _binding_column(journal, "outcome") is None
    finally:
        await manager.shutdown()


class _OAuthCalculator(Toolkit):
    def __init__(self) -> None:
        super().__init__(name="calculator", tools=[self.add])

    def add(self, a: int, b: int) -> int:
        del a, b
        _oauth_required()


@pytest.mark.asyncio
async def test_a_script_call_completes_an_oauth_requirement_with_the_payload_as_its_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A background script's call completes, not failed, with the OAuth payload dict as its result."""
    _replace_calculator_toolkit(monkeypatch, _OAuthCalculator)
    broker, token = _broker(tmp_path, events=[])

    receipt = await _call_through_gateway(broker, _request(call_id="oauth"), token)

    assert receipt.state is ScriptCallState.COMPLETED
    assert receipt.error is None
    assert receipt.result == _OAUTH_PAYLOAD


@pytest.mark.asyncio
async def test_a_gateway_call_whose_tool_answers_a_failed_outcome_reads_as_failed(
    context: AgentToolContext,  # noqa: F811 - the imported fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An MCP gateway call whose tool itself returns a failed outcome answers ``tool_failed`` with the tool's text."""

    def account() -> ToolOutcome:
        return ToolOutcome(content="The account is locked.", is_error=True)

    _replace_calculator(monkeypatch, Toolkit(name="calculator", tools=[account]))

    result = await gateway.invoke_tool(context, toolkit="calculator", function="account", arguments={})

    assert result == {"error": {"code": "tool_failed", "message": "The account is locked."}}


async def _voice_answer(tmp_path: Path, entrypoint: Callable[[], str], *hooks: object) -> str:
    """Return what the realtime voice model reads for one call of ``entrypoint`` under a policy of ``hooks``.

    The call's turn binds ``hooks`` as one plugin's hooks, as a voice call's bot binds its plugins'.
    """
    registry = HookRegistry.from_plugins([_plugin("policy", list(hooks))]) if hooks else HookRegistry.empty()
    runtime_context = _voice_runtime_context(
        config=_voice_config(),
        runtime_paths=test_runtime_paths(tmp_path),
        target=MessageTarget.resolve(room_id="!room:example.org", thread_id=None, reply_to_event_id=None),
        hook_registry=registry,
    )
    tool = _wrap_tool_function(
        adapt_function(_voice_function(entrypoint, _EMPTY_SCHEMA, name="search_mail")),
        middleware=(detached_policy(registry),),
        call_context=_CALL_CONTEXT,
        context=runtime_context,
        agent_name=AGENT,
        authorize_operation=_authorized_call_operation,
        usage_owner=_USAGE_OWNER,
        connect=_unconnected,
    )
    return await tool({})


@pytest.mark.asyncio
async def test_a_voice_call_reads_an_oauth_requirement_as_the_payloads_json(tmp_path: Path) -> None:
    """The realtime voice model reads an OAuth requirement as the payload dict encoded as JSON, not as a failure."""

    def search_mail() -> str:
        _oauth_required()

    assert await _voice_answer(tmp_path, search_mail) == json.dumps(_OAUTH_PAYLOAD)


@pytest.mark.asyncio
async def test_a_voice_call_a_plugin_hook_declines_reads_the_decline_text_without_a_failure_marker(
    tmp_path: Path,
) -> None:
    """The realtime voice model reads a plugin hook's decline as the bare decline text, with no failure wording."""
    ran: list[str] = []

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def decline(ctx: ToolBeforeCallContext) -> None:
        ctx.decline("mail is paused")

    def search_mail() -> str:
        ran.append("body")
        return "mail"

    assert await _voice_answer(tmp_path, search_mail, decline) == (
        "[TOOL CALL DECLINED]\n"
        "Tool: search_mail\n"
        "Reason: mail is paused\n\n"
        "Adjust your approach — try a different tool or different arguments."
    )
    assert ran == []


@pytest.mark.asyncio
async def test_a_managed_job_completes_an_oauth_requirement_with_the_payloads_repr(tmp_path: Path) -> None:
    """A managed call's job completes on an OAuth requirement, and its call reads the payload dict's Python repr."""

    def search_mail() -> str:
        _oauth_required()

    turn_context, runtime = await _managed_context(tmp_path)
    owner = turn_context.execution_identity
    try:
        async with execution_resources():
            with tool_runtime_context(turn_context):
                outcome = await call_job_tool(
                    job_actor(job_tool(search_mail)),
                    "search_mail",
                    call_id="mail-call",
                    run_id="mail-run",
                    middleware=(_policy(),),
                )
        assert outcome == ToolOutcome(content=repr(_OAUTH_PAYLOAD))
        job = (await runtime.list_jobs(owner=owner, depth=0))[0]
        assert (job.status, job.result) == ("completed", repr(_OAUTH_PAYLOAD))
    finally:
        await runtime.shutdown()
