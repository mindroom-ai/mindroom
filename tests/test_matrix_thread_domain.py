"""Direct tests for Matrix thread resolution and bookkeeping helpers."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.attachment_helpers import resolve_canonical_tool_thread_target
from mindroom.matrix import thread_bookkeeping, thread_membership
from mindroom.matrix.cache import ThreadHistoryResult
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_bookkeeping import (
    MutationThreadImpact,
    MutationThreadImpactState,
    ThreadMutationResolver,
    resolve_event_thread_impact_for_client,
    resolve_redaction_thread_impact_for_client,
)
from mindroom.matrix.thread_diagnostics import (
    THREAD_HISTORY_DEGRADED_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_DEGRADED,
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
)
from mindroom.matrix.thread_membership import (
    ThreadMembershipAccess,
    ThreadMembershipLookupError,
    ThreadResolution,
    ThreadResolutionState,
    ThreadRootProof,
    map_backed_thread_membership_access,
    page_event_info_counts_as_thread_child_proof,
    resolve_event_thread_membership,
    resolve_related_event_thread_membership,
    thread_messages_thread_membership_access,
)
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import ToolRuntimeContext
from tests.conftest import bind_runtime_paths, make_event_cache_mock, runtime_paths_for, test_runtime_paths


def _message_event_info(content: dict[str, object]) -> EventInfo:
    return EventInfo.from_event(
        {
            "type": "m.room.message",
            "content": content,
        },
    )


def test_page_event_info_counts_as_thread_child_proof_preserves_thread_semantics() -> None:
    """Page-local root proof should count only non-root children of the candidate thread."""
    thread_root_id = "$thread-root:localhost"
    explicit_child = _message_event_info(
        {
            "body": "thread reply",
            "msgtype": "m.text",
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": thread_root_id,
            },
        },
    )
    edit_child = _message_event_info(
        {
            "body": "* edited reply",
            "msgtype": "m.text",
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": "$reply:localhost",
            },
            "m.new_content": {
                "body": "edited reply",
                "msgtype": "m.text",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": thread_root_id,
                },
            },
        },
    )
    root_info = _message_event_info(
        {
            "body": "root",
            "msgtype": "m.text",
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": thread_root_id,
            },
        },
    )
    unrelated = _message_event_info(
        {
            "body": "other thread",
            "msgtype": "m.text",
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$other-root:localhost",
            },
        },
    )

    assert page_event_info_counts_as_thread_child_proof(
        thread_root_id,
        event_id="$reply:localhost",
        event_info=explicit_child,
    )
    assert page_event_info_counts_as_thread_child_proof(
        thread_root_id,
        event_id="$reply-edit:localhost",
        event_info=edit_child,
    )
    assert not page_event_info_counts_as_thread_child_proof(
        thread_root_id,
        event_id=thread_root_id,
        event_info=root_info,
    )
    assert not page_event_info_counts_as_thread_child_proof(
        thread_root_id,
        event_id="$other-reply:localhost",
        event_info=unrelated,
    )


def _tool_context(
    *,
    room_id: str = "!room:localhost",
    thread_id: str | None = "$thread:localhost",
) -> ToolRuntimeContext:
    runtime_root = Path(tempfile.mkdtemp())
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General Agent")}),
        test_runtime_paths(runtime_root),
    )
    return ToolRuntimeContext(
        agent_name="general",
        target=MessageTarget.resolve(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=None,
        ),
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        conversation_cache=AsyncMock(),
        event_cache=make_event_cache_mock(),
        room=None,
        storage_path=runtime_root,
    )


@pytest.mark.asyncio
async def test_resolve_event_thread_membership_promotes_plain_reply_transitively() -> None:
    """Plain replies should inherit thread membership from a threaded ancestor."""
    event_infos = {
        "$plain-two:localhost": _message_event_info(
            {
                "body": "plain two",
                "msgtype": "m.text",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$plain-one:localhost"}},
            },
        ),
        "$plain-one:localhost": _message_event_info(
            {
                "body": "plain one",
                "msgtype": "m.text",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-reply:localhost"}},
            },
        ),
        "$thread-reply:localhost": _message_event_info(
            {
                "body": "thread reply",
                "msgtype": "m.text",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread-root:localhost",
                },
            },
        ),
    }

    resolution = await resolve_event_thread_membership(
        "!room:localhost",
        event_infos["$plain-two:localhost"],
        access=map_backed_thread_membership_access(
            event_infos=event_infos,
            resolved_thread_ids={},
        ),
    )

    assert resolution == ThreadResolution.threaded("$thread-root:localhost")


@pytest.mark.asyncio
async def test_resolve_event_thread_membership_proves_current_root_when_allowed() -> None:
    """A root event should normalize to itself only when descendants prove it is threaded."""
    event_infos = {
        "$thread-root:localhost": _message_event_info(
            {
                "body": "root",
                "msgtype": "m.text",
            },
        ),
        "$thread-reply:localhost": _message_event_info(
            {
                "body": "thread reply",
                "msgtype": "m.text",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread-root:localhost",
                },
            },
        ),
    }

    resolution = await resolve_event_thread_membership(
        "!room:localhost",
        event_infos["$thread-root:localhost"],
        event_id="$thread-root:localhost",
        allow_current_root=True,
        access=map_backed_thread_membership_access(
            event_infos=event_infos,
            resolved_thread_ids={},
        ),
    )

    assert resolution == ThreadResolution.threaded("$thread-root:localhost")


@pytest.mark.asyncio
async def test_resolve_related_event_thread_membership_terminates_on_relation_cycle() -> None:
    """A reply cycle must fail closed instead of becoming a trusted room-level relation."""
    event_infos = {
        "$cycle-a:localhost": _message_event_info(
            {
                "body": "a",
                "msgtype": "m.text",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$cycle-b:localhost"}},
            },
        ),
        "$cycle-b:localhost": _message_event_info(
            {
                "body": "b",
                "msgtype": "m.text",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$cycle-a:localhost"}},
            },
        ),
    }
    fetched_event_ids: list[str] = []

    async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
        return None

    async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
        fetched_event_ids.append(event_id)
        return event_infos[event_id]

    async def prove_thread_root(_room_id: str, _thread_root_id: str) -> ThreadRootProof:
        msg = "events with relations can never become thread roots"
        raise AssertionError(msg)

    resolution = await resolve_related_event_thread_membership(
        "!room:localhost",
        "$cycle-a:localhost",
        access=ThreadMembershipAccess(
            lookup_thread_id=lookup_thread_id,
            fetch_event_info=fetch_event_info,
            prove_thread_root=prove_thread_root,
        ),
    )

    assert resolution.state is ThreadResolutionState.INDETERMINATE
    assert resolution.candidate_thread_root_id == "$cycle-a:localhost"
    assert isinstance(resolution.error, ThreadMembershipLookupError)
    assert fetched_event_ids == ["$cycle-a:localhost", "$cycle-b:localhost"]


@pytest.mark.asyncio
async def test_resolve_related_event_thread_membership_fails_closed_on_self_cycle() -> None:
    """A self-relation must remain visible to cycle detection."""
    event_info = _message_event_info(
        {
            "body": "self cycle",
            "msgtype": "m.text",
            "m.relates_to": {"m.in_reply_to": {"event_id": "$self:localhost"}},
        },
    )

    resolution = await resolve_related_event_thread_membership(
        "!room:localhost",
        "$self:localhost",
        access=map_backed_thread_membership_access(
            event_infos={"$self:localhost": event_info},
            resolved_thread_ids={},
        ),
    )

    assert resolution.state is ThreadResolutionState.INDETERMINATE
    assert resolution.candidate_thread_root_id == "$self:localhost"
    assert isinstance(resolution.error, ThreadMembershipLookupError)


@pytest.mark.asyncio
async def test_resolve_related_event_thread_membership_fails_closed_beyond_hop_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truncated relation chain cannot prove room-level impact."""
    monkeypatch.setattr(thread_membership, "_MAX_THREAD_MEMBERSHIP_HOPS", 2)
    event_infos = {
        "$a:localhost": _message_event_info(
            {
                "body": "a",
                "msgtype": "m.text",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$b:localhost"}},
            },
        ),
        "$b:localhost": _message_event_info(
            {
                "body": "b",
                "msgtype": "m.text",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$c:localhost"}},
            },
        ),
    }
    fetched_event_ids: list[str] = []

    async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
        return None

    async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
        fetched_event_ids.append(event_id)
        return event_infos[event_id]

    resolution = await resolve_related_event_thread_membership(
        "!room:localhost",
        "$a:localhost",
        access=ThreadMembershipAccess(
            lookup_thread_id=lookup_thread_id,
            fetch_event_info=fetch_event_info,
            prove_thread_root=AsyncMock(),
        ),
    )

    assert resolution.state is ThreadResolutionState.INDETERMINATE
    assert resolution.candidate_thread_root_id == "$c:localhost"
    assert isinstance(resolution.error, ThreadMembershipLookupError)
    assert fetched_event_ids == ["$a:localhost", "$b:localhost"]


@pytest.mark.asyncio
async def test_thread_root_proof_from_degraded_history_is_indeterminate() -> None:
    """An empty degraded thread read must yield an indeterminate result, never room-level demotion."""
    thread_root_id = "$thread-root:localhost"
    degraded_history = ThreadHistoryResult(
        [],
        is_full_history=False,
        diagnostics={
            THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_DEGRADED,
            THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True,
        },
    )

    async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
        return None

    async def fetch_event_info(_room_id: str, _event_id: str) -> EventInfo | None:
        return _message_event_info({"body": "root", "msgtype": "m.text"})

    async def fetch_thread_messages(_room_id: str, _thread_id: str) -> ThreadHistoryResult:
        return degraded_history

    resolution = await resolve_related_event_thread_membership(
        "!room:localhost",
        thread_root_id,
        access=thread_messages_thread_membership_access(
            lookup_thread_id=lookup_thread_id,
            fetch_event_info=fetch_event_info,
            fetch_thread_messages=fetch_thread_messages,
        ),
    )

    assert resolution.state is ThreadResolutionState.INDETERMINATE
    assert resolution.candidate_thread_root_id == thread_root_id
    assert resolution.thread_id is None


@pytest.mark.asyncio
async def test_thread_bookkeeping_removes_boolean_wrapper_entrypoints() -> None:
    """Thread bookkeeping should expose only the shared impact API entrypoints."""
    assert "event_requires_thread_bookkeeping" not in vars(thread_bookkeeping)
    assert "redaction_requires_thread_bookkeeping" not in vars(thread_bookkeeping)


@pytest.mark.asyncio
async def test_resolve_event_thread_impact_for_client_returns_threaded_impact() -> None:
    """Client-side message classification should expose the canonical threaded impact, not only a bool."""
    client = AsyncMock()
    conversation_cache = AsyncMock()
    conversation_cache.get_thread_id_for_event.side_effect = lambda room_id, event_id: (
        "$thread-root:localhost" if (room_id, event_id) == ("!room:localhost", "$thread-reply:localhost") else None
    )

    impact = await resolve_event_thread_impact_for_client(
        client,
        "!room:localhost",
        event_type="m.room.message",
        content={
            "body": "bridged reply",
            "msgtype": "m.text",
            "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-reply:localhost"}},
        },
        conversation_cache=conversation_cache,
    )

    assert impact == MutationThreadImpact.threaded("$thread-root:localhost")


@pytest.mark.asyncio
async def test_resolve_event_thread_impact_for_client_preserves_unknown_lookup_failures() -> None:
    """Lookup failures must stay explicit in the authoritative impact API."""
    client = AsyncMock()
    conversation_cache = AsyncMock()
    conversation_cache.get_thread_id_for_event.return_value = None
    conversation_cache.get_event.side_effect = RuntimeError("boom")

    impact = await resolve_event_thread_impact_for_client(
        client,
        "!room:localhost",
        event_type="m.room.message",
        content={
            "body": "bridged reply",
            "msgtype": "m.text",
            "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-reply:localhost"}},
        },
        conversation_cache=conversation_cache,
    )

    assert impact.state is MutationThreadImpactState.UNKNOWN
    assert impact.thread_id is None


@pytest.mark.asyncio
async def test_resolve_redaction_thread_impact_for_client_returns_room_level_for_reactions() -> None:
    """Client-side redaction classification should expose room-level reaction handling directly."""
    client = AsyncMock()
    conversation_cache = AsyncMock()
    conversation_cache.get_event.return_value = nio.RoomGetEventResponse.from_dict(
        {
            "event_id": "$reaction:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1,
            "room_id": "!room:localhost",
            "type": "m.reaction",
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$thread-reply:localhost",
                    "key": "👍",
                },
            },
        },
    )

    impact = await resolve_redaction_thread_impact_for_client(
        client,
        "!room:localhost",
        event_id="$reaction:localhost",
        conversation_cache=conversation_cache,
    )

    assert impact == MutationThreadImpact.room_level()


def _mutation_resolver_with_index(
    *,
    fetch_event_info: AsyncMock,
    get_thread_id_for_event: AsyncMock,
    logger: Mock | None = None,
) -> ThreadMutationResolver:
    """Build one mutation resolver over a stub runtime exposing only the event->thread index."""
    resolved_logger = logger if logger is not None else Mock()
    return ThreadMutationResolver(
        logger_getter=lambda: resolved_logger,
        runtime=SimpleNamespace(event_cache=SimpleNamespace(get_thread_id_for_event=get_thread_id_for_event)),
        fetch_event_info_for_thread_resolution=fetch_event_info,
    )


@pytest.mark.asyncio
async def test_metadata_less_redaction_scopes_to_cached_thread_index() -> None:
    """A redaction whose target metadata is gone should invalidate only the cache-known thread."""
    logger = Mock()
    resolver = _mutation_resolver_with_index(
        fetch_event_info=AsyncMock(return_value=None),
        get_thread_id_for_event=AsyncMock(return_value="$thread-root:localhost"),
        logger=logger,
    )

    impact = await resolver.resolve_redaction_thread_impact(
        "!room:localhost",
        "$old-target:localhost",
        failure_message="Failed to apply sync redaction to cache",
        event_id="$redaction:localhost",
    )

    assert impact == MutationThreadImpact.threaded("$thread-root:localhost")
    logger.info.assert_called_once_with(
        "Scoped metadata-less redaction to its cached thread",
        room_id="!room:localhost",
        event_id="$redaction:localhost",
        redacted_event_id="$old-target:localhost",
        thread_id="$thread-root:localhost",
    )


@pytest.mark.asyncio
async def test_metadata_less_redaction_stays_unknown_without_index_entry() -> None:
    """A redaction target absent from the cache index must keep failing closed."""
    resolver = _mutation_resolver_with_index(
        fetch_event_info=AsyncMock(return_value=None),
        get_thread_id_for_event=AsyncMock(return_value=None),
    )

    impact = await resolver.resolve_redaction_thread_impact(
        "!room:localhost",
        "$untracked-target:localhost",
        failure_message="Failed to apply sync redaction to cache",
    )

    assert impact == MutationThreadImpact.unknown()


@pytest.mark.asyncio
async def test_metadata_less_redaction_stays_unknown_when_index_lookup_fails() -> None:
    """An index lookup error must not weaken the fail-closed redaction path."""
    logger = Mock()
    resolver = _mutation_resolver_with_index(
        fetch_event_info=AsyncMock(return_value=None),
        get_thread_id_for_event=AsyncMock(side_effect=RuntimeError("cache unavailable")),
        logger=logger,
    )

    impact = await resolver.resolve_redaction_thread_impact(
        "!room:localhost",
        "$old-target:localhost",
        failure_message="Failed to apply sync redaction to cache",
        event_id="$redaction:localhost",
    )

    assert impact == MutationThreadImpact.unknown()
    logger.warning.assert_called_once_with(
        "Redaction thread-index fallback failed; failing closed",
        room_id="!room:localhost",
        event_id="$redaction:localhost",
        redacted_event_id="$old-target:localhost",
        error="cache unavailable",
    )


@pytest.mark.asyncio
async def test_resolve_canonical_tool_thread_target_uses_context_thread() -> None:
    """Tool-facing normalization should share one helper that applies context fallback before canonicalization."""
    context = _tool_context(thread_id="$ctx-thread:localhost")
    normalize_thread_id = AsyncMock(return_value="$thread-root:localhost")

    target = await resolve_canonical_tool_thread_target(
        context,
        room_id=context.room_id,
        thread_id=None,
        normalize_thread_id=normalize_thread_id,
    )

    assert target.requested_thread_id == "$ctx-thread:localhost"
    assert target.canonical_thread_id == "$thread-root:localhost"
    assert target.error is None
    normalize_thread_id.assert_awaited_once_with(context.room_id, "$ctx-thread:localhost")


@pytest.mark.asyncio
async def test_resolve_canonical_tool_thread_target_requires_thread_context() -> None:
    """Tool-facing normalization should return the shared missing-thread error when no target is available."""
    context = _tool_context(thread_id=None)
    normalize_thread_id = AsyncMock(return_value="$thread-root:localhost")

    target = await resolve_canonical_tool_thread_target(
        context,
        room_id=context.room_id,
        thread_id=None,
        normalize_thread_id=normalize_thread_id,
    )

    assert target.requested_thread_id is None
    assert target.canonical_thread_id is None
    assert target.error == "thread_id is required when no active thread context is available for the target room."
    normalize_thread_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_canonical_tool_thread_target_fail_closed_preserves_requested_thread_id() -> None:
    """Fail-closed tool normalization should keep the resolved request target for error reporting."""
    context = _tool_context(thread_id="$ctx-thread:localhost")
    normalize_thread_id = AsyncMock(side_effect=TimeoutError("timed out"))

    target = await resolve_canonical_tool_thread_target(
        context,
        room_id=context.room_id,
        thread_id=None,
        normalize_thread_id=normalize_thread_id,
        fail_closed_on_normalization_error=True,
    )

    assert target.requested_thread_id == "$ctx-thread:localhost"
    assert target.canonical_thread_id is None
    assert target.error == "Failed to resolve a canonical thread root for the target event."
    normalize_thread_id.assert_awaited_once_with(context.room_id, "$ctx-thread:localhost")
