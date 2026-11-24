"""Tests for io.mindroom.suppress_push propagation, working-state checks, delivery, and agent handoffs."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.attachments.storage import register_local_attachment
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import SKIP_MENTIONS_KEY, STREAM_STATUS_KEY, SUPPRESS_PUSH_KEY
from mindroom.custom_tools.matrix_message import MatrixMessageTools
from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.event_journal import EventClass, EventKind, InboundEvent
from mindroom.matrix.client_delivery import build_edit_event_content
from mindroom.matrix.client_session import MindRoomAsyncClient
from mindroom.matrix.encrypted_event_metadata import encryption_visible_metadata
from mindroom.matrix.large_messages import prepare_large_message
from mindroom.message_target import MessageTarget
from mindroom.reply_turn.request import ResponseRequest
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import publish_error_run_metadata
from mindroom.tool_jobs.conversation import ConversationKey
from mindroom.tool_jobs.follow_ups import (
    _FOLLOW_UP_LIMIT,
    _follow_up_event_id,
    follow_up_event,
    has_working_jobs_for_conversation,
    next_follow_up_id,
)
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobSpec, register_background_runtime
from mindroom.tool_system.events import FailedReply
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context, tool_runtime_context
from mindroom.tool_system.worker_routing import serialize_tool_execution_identity
from mindroom.workflow_runs.agent_nodes import launch_child
from mindroom.workflow_runs.models import WorkflowNode, WorkflowRun, new_workflow_run_id
from mindroom.workflow_runs.store import WorkflowRunStore
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.bot_helpers import unique_room_send_responses
from tests.conftest import (
    bind_runtime_paths,
    delivered_matrix_side_effect,
    make_conversation_reader_mock,
    make_latest_thread_event_id_mock,
    make_matrix_client_mock,
    make_relation_lookup,
    message_origin,
    patch_response_runner_module,
    test_runtime_paths,
    unwrap_extracted_collaborator,
)
from tests.identity_helpers import entity_ids
from tests.response_runner_helpers import _bot, _envelope, _noop_typing, _target
from tests.tool_job_helpers import awaiting_approval, job_owner, start_job, tool_job_runtime, wait_for_status

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.delegation.state import DelegationChild
    from mindroom.response_turn import ResponseTurnContext, TurnSinks
    from mindroom.tool_jobs.runtime import ToolJobRuntime
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


class _UploadClient:
    user_id = "@mindroom_general:example.com"
    device_id = "DEVICE"
    olm = None

    def __init__(self, uri: str) -> None:
        self._uri = uri

    async def upload(self, *_args: object, **_kwargs: object) -> tuple[nio.UploadResponse, None]:
        return nio.UploadResponse(self._uri), None


def _conversation_key(owner: ToolExecutionIdentity, *, silent: bool = False) -> ConversationKey:
    assert owner.room_id is not None
    assert owner.requester_id is not None
    return ConversationKey(
        recipient=owner.recipient,
        room_id=owner.room_id,
        thread_id=owner.resolved_thread_id,
        requester_id=owner.requester_id,
        silent=silent,
    )


def _runner_key(bot: AgentBot) -> ConversationKey:
    return ConversationKey(
        recipient=bot.agent_name,
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@alice:example.com",
        silent=False,
    )


def _runner_owner(key: ConversationKey) -> ToolExecutionIdentity:
    return replace(
        job_owner(),
        agent_name=key.recipient,
        requester_id=key.requester_id,
        room_id=key.room_id,
        thread_id=key.thread_id,
        resolved_thread_id=key.thread_id,
    )


async def _runner_runtime(bot: AgentBot, tmp_path: Path) -> ToolJobRuntime:
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(bot.config, bot.runtime_paths)
    register_background_runtime(bot.runtime_paths, runtime)
    return runtime


async def _start_forever(
    runtime: ToolJobRuntime,
    job_id: str,
    owner: ToolExecutionIdentity,
    *,
    kind: str = "tool",
    adapter: dict[str, Any] | None = None,
    source_kind: str = "message",
) -> None:
    async def forever() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    await start_job(
        runtime,
        job_id,
        tool_name="work",
        kind=kind,
        depth=0,
        adapter=adapter or {},
        owner=owner,
        operation=forever,
        source_kind=source_kind,
    )


async def _start_awaiting_approval(
    runtime: ToolJobRuntime,
    job_id: str,
    owner: ToolExecutionIdentity,
    *,
    kind: str = "tool",
    adapter: dict[str, Any] | None = None,
) -> None:
    await start_job(
        runtime,
        job_id,
        tool_name="approve_me",
        kind=kind,
        depth=0,
        adapter=adapter or {},
        owner=owner,
        operation=partial(awaiting_approval, runtime, job_id),
    )
    await wait_for_status(runtime, job_id, "awaiting_approval")


async def _start_ended(
    runtime: ToolJobRuntime,
    job_id: str,
    outcome: BackgroundOutcome,
    owner: ToolExecutionIdentity,
    *,
    source_event_id: str | None = "$msg",
    source_kind: str = "message",
) -> None:
    async def finish() -> BackgroundOutcome:
        return outcome

    await start_job(
        runtime,
        job_id,
        tool_name="work",
        depth=0,
        adapter={},
        owner=owner,
        operation=finish,
        source_event_id=source_event_id,
        source_kind=source_kind,
    )
    await wait_for_status(runtime, job_id, outcome.status)


def _sent_message_contents(bot: AgentBot) -> list[dict[str, Any]]:
    assert bot.client is not None
    return [
        call.kwargs["content"]
        for call in bot.client.room_send.await_args_list
        if call.kwargs.get("message_type") == "m.room.message"
    ]


def _human_request(key: ConversationKey, event_id: str) -> ResponseRequest:
    target = _target(thread_id=key.thread_id, reply_to_event_id=event_id)
    envelope = replace(
        _envelope(target, source_event_id=event_id),
        origin=message_origin(sender_id=key.requester_id, requester_id=key.requester_id),
    )
    return ResponseRequest(
        thread_history=(),
        prompt="hello",
        response_envelope=envelope,
        sources=ResponseSources((event_id,), (event_id,)),
        user_id=key.requester_id,
    )


async def _admit_human_event(bot: AgentBot, key: ConversationKey, event_id: str) -> None:
    await bot.journal_principal().admit(
        InboundEvent(
            event_id=event_id,
            room_id=key.room_id,
            thread_id=key.thread_id,
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender=key.requester_id,
            origin_server_ts=1,
            source={"content": {"msgtype": "m.text", "body": "hello"}},
        ),
    )


def _matrix_message_context(tmp_path: Path, *, attachment_ids: tuple[str, ...] = ()) -> ToolRuntimeContext:
    MatrixMessageTools._recent_actions.clear()
    paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General"),
                "code": AgentConfig(display_name="Code"),
            },
        ),
        paths,
    )
    ids = entity_ids(config, paths)
    room_id = "!room:example.com"
    client = make_matrix_client_mock(user_id=ids["general"].full_id)
    room = nio.MatrixRoom(room_id, ids["general"].full_id)
    room.add_member(ids["general"].full_id, "General", None)
    room.add_member(ids["code"].full_id, "Code", None)
    room.add_member("@alice:example.com", "Alice", None)
    client.rooms = {room_id: room}
    reader = make_conversation_reader_mock()
    reader.latest_thread_event_id = make_latest_thread_event_id_mock("$thread:example.com")
    return make_test_tool_runtime_context(
        agent_name="general",
        requester_id="@alice:example.com",
        client=client,
        config=config,
        runtime_paths=paths,
        relations=make_relation_lookup(),
        conversation_reader=reader,
        room=room,
        storage_path=tmp_path,
        attachment_ids=attachment_ids,
        target=MessageTarget.resolve(room_id, "$thread:example.com", "$event:example.com"),
    )


def test_encryption_visible_metadata_hoists_suppress_push_only_when_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only an explicit boolean True suppress_push flag is exposed outside encryption."""
    assert encryption_visible_metadata({SUPPRESS_PUSH_KEY: True, STREAM_STATUS_KEY: "completed"}) == {
        STREAM_STATUS_KEY: "completed",
        SUPPRESS_PUSH_KEY: True,
    }
    assert encryption_visible_metadata({}) == {}
    assert encryption_visible_metadata({SUPPRESS_PUSH_KEY: False}) == {}
    assert encryption_visible_metadata({SUPPRESS_PUSH_KEY: "true"}) == {}

    def fake_encrypt(
        _client: nio.AsyncClient,
        _room_id: str,
        _message_type: str,
        _content: dict[Any, Any],
    ) -> tuple[str, dict[str, Any]]:
        return "m.room.encrypted", {"ciphertext": "encrypted"}

    monkeypatch.setattr(nio.AsyncClient, "encrypt", fake_encrypt)
    client = MindRoomAsyncClient("https://example.com", "@mindroom_agent:example.com")
    event_type, encrypted = client.encrypt(
        "!room:example.com",
        "m.room.message",
        {"msgtype": "m.text", "body": "working", SUPPRESS_PUSH_KEY: True},
    )
    assert event_type == "m.room.encrypted"
    assert encrypted[SUPPRESS_PUSH_KEY] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("room_encrypted", [False, True])
@pytest.mark.parametrize("is_edit", [False, True])
async def test_large_messages_preserve_suppress_push(*, is_edit: bool, room_encrypted: bool) -> None:
    """Oversized messages and edits keep io.mindroom.suppress_push on preview and edit wrapper."""
    body = "x" * 70_000
    base: dict[str, Any] = {
        "msgtype": "m.text",
        "body": body,
        STREAM_STATUS_KEY: "completed",
        SUPPRESS_PUSH_KEY: True,
    }
    content = build_edit_event_content(event_id="$orig", new_content=base, new_text=body) if is_edit else base

    prepared = await prepare_large_message(
        _UploadClient("mxc://example.com/sidecar"),
        "!room:example.com",
        content,
        room_encrypted=room_encrypted,
    )

    assert prepared[SUPPRESS_PUSH_KEY] is True
    if is_edit:
        assert prepared["m.new_content"][SUPPRESS_PUSH_KEY] is True


@pytest.mark.asyncio
async def test_has_working_jobs_for_conversation_states(tmp_path: Path) -> None:
    """Query distinguishes active, approval, follow-up, silent, consumed, cancelled, and capped jobs."""
    runtime = await tool_job_runtime(tmp_path)
    owner = job_owner()
    key = _conversation_key(owner)
    silent_key = _conversation_key(owner, silent=True)
    try:
        assert not await has_working_jobs_for_conversation(runtime, key)

        # Cancelled and follow-up-capped jobs do not trigger follow-ups.
        await _start_ended(runtime, "cancelled_job", BackgroundOutcome("cancelled"), owner)
        capped_source = _follow_up_event_id(key, (), _FOLLOW_UP_LIMIT)
        await _start_ended(
            runtime,
            "capped_job",
            BackgroundOutcome("completed", "done"),
            owner,
            source_event_id=capped_source,
        )
        assert not await has_working_jobs_for_conversation(runtime, key)

        # Silent-schedule ended jobs get silent follow-ups with distinct IDs and stay isolated from visible turns.
        await _start_ended(
            runtime,
            "silent_job",
            BackgroundOutcome("completed", "done"),
            owner,
            source_kind=SILENT_SCHEDULE_SOURCE_KIND,
        )
        assert not await has_working_jobs_for_conversation(runtime, key)
        assert await has_working_jobs_for_conversation(runtime, silent_key)
        silent_ended = await runtime.ended_jobs(silent_key)
        silent_follow_up_id = next_follow_up_id(silent_key, silent_ended)
        assert not await has_working_jobs_for_conversation(
            runtime,
            silent_key,
            current_follow_up_id=silent_follow_up_id,
        )

        # Unconsumed ended follow-up job returns True unless matching current or already-settled follow-up.
        await _start_ended(runtime, "ended_job", BackgroundOutcome("completed", "done"), owner)
        assert await has_working_jobs_for_conversation(runtime, key)
        ended_snapshot = await runtime.ended_jobs(key)
        follow_up_id = next_follow_up_id(key, [j for j in ended_snapshot if j.job_id == "ended_job"])
        assert not await has_working_jobs_for_conversation(runtime, key, current_follow_up_id=follow_up_id)

        async def settled(event_id: str) -> bool:
            return event_id == follow_up_id

        assert not await has_working_jobs_for_conversation(runtime, key, is_follow_up_settled=settled)

        # A second ended job changes the follow-up digest so a new follow-up will still run.
        await _start_ended(runtime, "ended_job_2", BackgroundOutcome("completed", "done2"), owner)
        assert await has_working_jobs_for_conversation(runtime, key, current_follow_up_id=follow_up_id)

        for jid in ("ended_job", "ended_job_2"):
            waited = await runtime.wait(jid, owner=owner, depth=0, timeout=0)
            await runtime.acknowledge_wait(jid, waited.claim)
        assert not await has_working_jobs_for_conversation(runtime, key)

        # Active running job returns True; any awaiting_approval job overrides it to False immediately.
        await _start_forever(runtime, "running_job", owner)
        assert await has_working_jobs_for_conversation(runtime, key)

        await _start_awaiting_approval(runtime, "approval_job", owner)
        assert not await has_working_jobs_for_conversation(runtime, key)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_workflow_node_awaiting_approval_does_not_suppress_push(tmp_path: Path) -> None:
    """A running parent workflow with a workflow-node child awaiting approval puts the thread in Needs You."""
    runtime = await tool_job_runtime(tmp_path)
    owner = job_owner()
    key = _conversation_key(owner)
    try:
        await _start_forever(runtime, "parent_workflow", owner, kind="workflow", adapter={"workflow": "deploy"})
        assert await has_working_jobs_for_conversation(runtime, key)

        await _start_awaiting_approval(
            runtime,
            "wf_node_approval",
            owner,
            kind="delegation",
            adapter={"child": {"workflow_run_id": "wf-1"}, "wait": "approval"},
        )
        assert not await has_working_jobs_for_conversation(runtime, key)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_silent_workflow_child_approval_preserves_source_kind_and_isolates_visible_conversation(
    tmp_path: Path,
) -> None:
    """Workflow-node children inherit a silent parent workflow's source_kind without leaking into visible turns."""
    runtime = await tool_job_runtime(tmp_path)
    context = _matrix_message_context(tmp_path)
    context.config.agents["general"].tools = ["workflow"]
    context.config.agents["general"].delegate_to = ["code"]
    owner = context.execution_identity
    visible_key = _conversation_key(owner, silent=False)
    silent_key = _conversation_key(owner, silent=True)
    store = WorkflowRunStore(tmp_path / "workflow_runs.sqlite3")
    seen_context_source_kinds: list[str | None] = []

    def fake_delegation_job(child: DelegationChild, *, job_id: str, **_kwargs: object) -> JobSpec:
        active_ctx = get_tool_runtime_context()
        seen_context_source_kinds.append(None if active_ctx is None else active_ctx.source_kind)
        return JobSpec(
            tool_name="run_subagent",
            kind="delegation",
            adapter={"child": {"workflow_run_id": child.workflow_run_id}, "wait": "approval"},
            operation=partial(awaiting_approval, runtime, job_id),
        )

    try:
        # Running silent parent workflow + independent visible running job.
        await _start_forever(
            runtime,
            "silent_workflow",
            owner,
            kind="workflow",
            adapter={"workflow": "nightly"},
            source_kind=SILENT_SCHEDULE_SOURCE_KIND,
        )
        await _start_forever(runtime, "visible_job", owner)
        assert await has_working_jobs_for_conversation(runtime, silent_key)
        assert await has_working_jobs_for_conversation(runtime, visible_key)

        run, _ = store.reserve_run(
            WorkflowRun(
                run_id=new_workflow_run_id(),
                agent_name=context.agent_name,
                owner_user_id=context.requester_id,
                room_id=context.room_id,
                thread_root_event_id=context.resolved_thread_id,
                execution_identity=serialize_tool_execution_identity(owner),
                persona=None,
                title=None,
                source="pass",
                source_digest="digest",
                args=None,
                max_concurrent=4,
                max_cost_usd=None,
                resource_profile=None,
                runtime_seconds_left=None,
                origin_job_id="silent_workflow",
                owner="silent_workflow",
                job_id="silent_workflow",
            ),
        )
        node = store.put_node(
            WorkflowNode(
                run_id=run.run_id,
                key="@0",
                kind="agent",
                lane="main",
                idx=0,
                digest="d1",
                request={"agent": "code", "prompt": "check"},
                attempt=0,
                label="check",
            ),
            epoch=run.epoch,
        )
        # Rebuilt workflow contexts arrive with source_kind=None; launch_child must inherit the parent job's.
        assert context.source_kind is None
        with patch("mindroom.workflow_runs.agent_nodes.delegation_call_job", side_effect=fake_delegation_job):
            launched = await launch_child(context, run, node, run.epoch, store=store, jobs=runtime)

        assert launched.handle is not None
        child_job_id = launched.handle.ref
        await wait_for_status(runtime, child_job_id, "awaiting_approval")
        child_job = runtime.retained_job(child_job_id)
        assert child_job is not None
        assert child_job.source_kind == SILENT_SCHEDULE_SOURCE_KIND
        assert seen_context_source_kinds == [SILENT_SCHEDULE_SOURCE_KIND]

        # Silent query surfaces Needs You (False); visible query still sees its own running work (True).
        assert not await has_working_jobs_for_conversation(runtime, silent_key)
        assert await has_working_jobs_for_conversation(runtime, visible_key)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
@pytest.mark.parametrize(
    ("scenario", "expect_suppressed"),
    [
        ("running_job", True),
        ("no_jobs", False),
        ("awaiting_approval", False),
        ("interactive_with_running_job", False),
        ("error_with_running_job", False),
    ],
)
async def test_final_delivery_suppress_push(
    tmp_path: Path,
    *,
    streaming: bool,
    scenario: str,
    expect_suppressed: bool,
) -> None:
    """Streamed and non-streamed final replies suppress push only for normal replies with active working jobs."""
    bot = _bot(tmp_path)
    await bot._take_over_replies()
    unique_room_send_responses(bot.client)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    key = _runner_key(bot)
    owner = _runner_owner(key)
    runtime = await _runner_runtime(bot, tmp_path)

    async def stream(
        _ctx: ResponseTurnContext,
        *_args: object,
        sinks: TurnSinks | None = None,
        **_kwargs: object,
    ) -> AsyncIterator[object]:
        if scenario == "interactive_with_running_job":
            payload = json.dumps(
                {
                    "question": "Proceed?",
                    "options": [{"emoji": "👍", "label": "Yes", "value": "yes"}],
                },
            )
            yield f"```interactive\n{payload}\n```"
            return
        if scenario == "error_with_running_job":
            if sinks is not None:
                publish_error_run_metadata(sinks)
            yield FailedReply("Something went wrong.", RuntimeError("boom"))
            return
        yield "Working on it."

    try:
        if scenario in {"running_job", "interactive_with_running_job", "error_with_running_job"}:
            await _start_forever(runtime, "bg_running", owner)
        elif scenario == "awaiting_approval":
            await _start_awaiting_approval(runtime, "bg_approval", owner)

        event_id = "$human-1"
        await _admit_human_event(bot, key, event_id)
        with patch_response_runner_module(
            stream_agent_response=stream,
            should_use_streaming=AsyncMock(return_value=streaming),
            typing_indicator=_noop_typing,
        ):
            await runner.generate_response(_human_request(key, event_id))

        sent = _sent_message_contents(bot)
        assert sent
        for progress_content in sent[:-1]:
            assert SUPPRESS_PUSH_KEY not in progress_content
        final_content = sent[-1]
        assert (final_content.get(SUPPRESS_PUSH_KEY) is True) is expect_suppressed
        if isinstance(new_content := final_content.get("m.new_content"), dict):
            assert (new_content.get(SUPPRESS_PUSH_KEY) is True) is expect_suppressed
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
@pytest.mark.parametrize("case", ["collected", "uncollected", "still_running"])
async def test_job_follow_up_final_delivery_suppress_push(
    tmp_path: Path,
    *,
    streaming: bool,
    case: str,
) -> None:
    """Final JOB_FOLLOW_UP replies notify whether jobs were collected or left uncollected, unless work remains."""
    bot = _bot(tmp_path)
    await bot._take_over_replies()
    unique_room_send_responses(bot.client)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    key = _runner_key(bot)
    owner = _runner_owner(key)
    runtime = await _runner_runtime(bot, tmp_path)

    async def stream(_ctx: ResponseTurnContext, *_args: object, **_kwargs: object) -> AsyncIterator[str]:
        if case == "collected":
            waited = await runtime.wait("job_1", owner=owner, depth=0, timeout=0)
            await runtime.acknowledge_wait("job_1", waited.claim)
        yield "Follow-up summary."

    try:
        await _start_ended(runtime, "job_1", BackgroundOutcome("completed", "done"), owner)
        if case == "still_running":
            await _start_forever(runtime, "job_2", owner)

        ended = await runtime.ended_jobs(key)
        follow_up_id = next_follow_up_id(key, ended)
        await bot.journal_principal().admit(
            follow_up_event(key, follow_up_id, sender_id=bot.matrix_id.full_id, now_ms=1),
        )
        event = await bot.journal_principal().load_event(follow_up_id)
        assert event is not None

        with patch_response_runner_module(
            stream_agent_response=stream,
            should_use_streaming=AsyncMock(return_value=streaming),
            typing_indicator=_noop_typing,
        ):
            await runner._run_job_follow_up(event)

        sent = _sent_message_contents(bot)
        assert sent
        assert (sent[-1].get(SUPPRESS_PUSH_KEY) is True) is (case == "still_running")

        if case == "uncollected":
            # A later human turn must not suppress push because of the already-settled follow-up.
            async def human_stream(_ctx: ResponseTurnContext, *_args: object, **_kwargs: object) -> AsyncIterator[str]:
                yield "Normal reply."

            await _admit_human_event(bot, key, "$human-later")
            with patch_response_runner_module(
                stream_agent_response=human_stream,
                should_use_streaming=AsyncMock(return_value=streaming),
                typing_indicator=_noop_typing,
            ):
                await runner.generate_response(_human_request(key, "$human-later"))

            assert SUPPRESS_PUSH_KEY not in _sent_message_contents(bot)[-1]
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(("recipient", "expect_suppressed"), [("code", True), ("general", True), (None, False)])
async def test_matrix_message_suppresses_push_only_for_agent_handoffs(
    tmp_path: Path,
    *,
    recipient: str | None,
    expect_suppressed: bool,
) -> None:
    """matrix_message sets io.mindroom.suppress_push on recipient handoffs and omits it for human messages."""
    context = _matrix_message_context(tmp_path)
    tool = MatrixMessageTools()
    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$sent")),
        ) as mock_send,
        tool_runtime_context(context),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                message="Please handle this task.",
                recipient=recipient,
            ),
        )

    assert payload["status"] == "ok"
    sent_content = mock_send.await_args.args[2]
    assert (sent_content.get(SUPPRESS_PUSH_KEY) is True) is expect_suppressed


@pytest.mark.asyncio
@pytest.mark.parametrize(("recipient", "expect_suppressed"), [("code", True), (None, False)])
async def test_matrix_message_attachments_suppress_push_only_for_agent_handoffs(
    tmp_path: Path,
    *,
    recipient: str | None,
    expect_suppressed: bool,
) -> None:
    """Attachments sent with a recipient handoff suppress push; ordinary human file sends do not."""
    sample_file = tmp_path / "report.txt"
    sample_file.write_text("payload", encoding="utf-8")
    attachment = register_local_attachment(tmp_path, sample_file, kind="file", attachment_id="att_handoff")
    assert attachment is not None
    context = _matrix_message_context(tmp_path, attachment_ids=("att_handoff",))

    tool = MatrixMessageTools()
    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$sent")),
        ) as mock_send,
        patch(
            "mindroom.custom_tools.attachments.send_file_message",
            new=AsyncMock(return_value="$file_evt"),
        ) as mock_send_file,
        tool_runtime_context(context),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                message="See attached file.",
                recipient=recipient,
                attachments=["att_handoff"],
            ),
        )

    assert payload["status"] == "ok"
    expected_attachment_extra: dict[str, Any] = {SKIP_MENTIONS_KEY: True}
    if expect_suppressed:
        expected_attachment_extra[SUPPRESS_PUSH_KEY] = True
    assert mock_send_file.await_args.kwargs["extra_content"] == expected_attachment_extra
    assert (mock_send.await_args.args[2].get(SUPPRESS_PUSH_KEY) is True) is expect_suppressed
