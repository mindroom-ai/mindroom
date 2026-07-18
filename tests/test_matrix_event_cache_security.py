"""Security and plaintext-lifecycle contract tests for every durable cache backend."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any, cast

import pytest

from mindroom.matrix.cache import (
    ConversationEventCache,
    SharedConversationEventCache,
    postgres_event_cache_events,
    sqlite_event_cache_events,
)
from mindroom.matrix.cache.postgres_event_cache import PostgresEventCache
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _sidecar_content(mxc_url: str, *, encrypted: bool) -> dict[str, Any]:
    content: dict[str, Any] = {
        "body": "preview",
        "msgtype": "m.file",
        "io.mindroom.long_text": {
            "version": 2,
            "encoding": "matrix_event_content_json",
        },
    }
    if encrypted:
        content["file"] = {"url": mxc_url, "key": {"k": "secret"}}
    else:
        content["url"] = mxc_url
    return content


def _event(
    event_id: str,
    timestamp: int,
    *,
    body: str = "message",
    sidecar_url: str | None = None,
    encrypted: bool = False,
    sidecar_in_new_content: bool = False,
    edit_of: str | None = None,
) -> dict[str, Any]:
    content: dict[str, Any] = {"body": body, "msgtype": "m.text"}
    if sidecar_url is not None:
        sidecar = _sidecar_content(sidecar_url, encrypted=encrypted)
        if sidecar_in_new_content:
            content["m.new_content"] = sidecar
        else:
            content = sidecar
    if edit_of is not None:
        content["m.relates_to"] = {"rel_type": "m.replace", "event_id": edit_of}
    return {
        "event_id": event_id,
        "sender": "@agent:localhost",
        "origin_server_ts": timestamp,
        "type": "m.room.message",
        "content": content,
    }


def _shared_cache(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> SharedConversationEventCache:
    cache = event_cache_factory()
    assert isinstance(cache, SharedConversationEventCache)
    return cast("SharedConversationEventCache", cache)


@pytest.mark.asyncio
async def test_room_scope_is_part_of_event_and_plaintext_identity(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """The same event ID or MXC URL in two rooms must never cross room boundaries."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    cache = root.for_principal("@alice:localhost")
    mxc_url = "mxc://server/shared-name"
    try:
        room_a_event = _event("$same", 1, body="room A", sidecar_url=mxc_url)
        room_b_event = _event("$same", 2, body="room B", sidecar_url=mxc_url)
        await cache.store_event("$same", "!a:localhost", room_a_event)
        await cache.store_event("$same", "!b:localhost", room_b_event)
        assert await cache.store_mxc_text("!a:localhost", "$same", mxc_url, "plaintext A")
        assert await cache.store_mxc_text("!b:localhost", "$same", mxc_url, "plaintext B")

        assert await cache.get_event("!a:localhost", "$same") == room_a_event
        assert await cache.get_event("!b:localhost", "$same") == room_b_event
        assert await cache.get_event("!wrong:localhost", "$same") is None
        assert await cache.get_mxc_text("!a:localhost", "$same", mxc_url) == "plaintext A"
        assert await cache.get_mxc_text("!b:localhost", "$same", mxc_url) == "plaintext B"
        assert await cache.get_mxc_text("!wrong:localhost", "$same", mxc_url) is None
    finally:
        await root.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("decrypted_principal_first", [True, False])
async def test_principal_isolation_survives_asymmetric_decryption_and_leave(
    event_cache_factory: Callable[[], ConversationEventCache],
    *,
    decrypted_principal_first: bool,
) -> None:
    """One joined bot's decrypted plaintext must remain invisible to every other bot."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    alice = root.for_principal("@alice:localhost")
    bob = root.for_principal("@bob:localhost")
    room_id = "!encrypted:localhost"
    event_id = "$encrypted-sidecar"
    mxc_url = "mxc://server/encrypted"
    alice_event = _event(event_id, 1, sidecar_url=mxc_url, encrypted=True)
    bob_opaque_event = _event(event_id, 1, body="unable to decrypt")
    ordered_writes = (
        ((alice, alice_event), (bob, bob_opaque_event))
        if decrypted_principal_first
        else ((bob, bob_opaque_event), (alice, alice_event))
    )
    try:
        for cache, event in ordered_writes:
            await cache.store_event(event_id, room_id, event)
        assert await alice.store_mxc_text(room_id, event_id, mxc_url, "alice plaintext")
        assert await bob.get_mxc_text(room_id, event_id, mxc_url) is None
        assert await bob.store_mxc_text(room_id, event_id, mxc_url, "stolen") is False

        bob_event = _event(event_id, 2, sidecar_url=mxc_url, encrypted=True)
        await bob.store_event(event_id, room_id, bob_event)
        assert await bob.get_mxc_text(room_id, event_id, mxc_url) is None
        assert await bob.store_mxc_text(room_id, event_id, mxc_url, "bob plaintext")
        assert await alice.get_mxc_text(room_id, event_id, mxc_url) == "alice plaintext"

        await alice.purge_room(room_id)
        assert await alice.get_event(room_id, event_id) is None
        assert await alice.get_mxc_text(room_id, event_id, mxc_url) is None
        await alice.store_event("$late", room_id, _event("$late", 3, sidecar_url=mxc_url))
        assert await alice.store_mxc_text(room_id, "$late", mxc_url, "late plaintext") is False
        assert await alice.get_event(room_id, "$late") is None
        assert await alice.get_mxc_text(room_id, "$late", mxc_url) is None
        assert await bob.get_event(room_id, event_id) == bob_event
        assert await bob.get_mxc_text(room_id, event_id, mxc_url) == "bob plaintext"

        await alice.mark_room_joined(room_id)
        rejoined_event = _event("$rejoined", 4, sidecar_url=mxc_url)
        await alice.store_event("$rejoined", room_id, rejoined_event)
        assert await alice.store_mxc_text(room_id, "$rejoined", mxc_url, "rejoined plaintext")
        assert await alice.get_event(room_id, "$rejoined") == rejoined_event
        assert await bob.get_mxc_text(room_id, event_id, mxc_url) == "bob plaintext"
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_closing_principal_view_does_not_close_shared_runtime(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """A bot stopping must not close cache storage still used by another bot."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    alice = root.for_principal("@alice:localhost")
    bob = root.for_principal("@bob:localhost")
    room_id = "!room:localhost"
    try:
        await alice.store_event("$alice", room_id, _event("$alice", 1))
        await bob.store_event("$bob", room_id, _event("$bob", 2))

        await alice.close()

        assert await bob.get_event(room_id, "$bob") == _event("$bob", 2)
        await bob.store_event("$bob-after-close", room_id, _event("$bob-after-close", 3))
        assert await bob.get_event(room_id, "$bob-after-close") == _event("$bob-after-close", 3)
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_disabling_principal_view_does_not_disable_other_principals(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """A principal-scoped safety failure must not take down another bot's cache view."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    alice = root.for_principal("@alice:localhost")
    bob = root.for_principal("@bob:localhost")
    room_id = "!room:localhost"
    alice_event = _event("$alice", 1)
    bob_event = _event("$bob", 2)
    try:
        await alice.store_event("$alice", room_id, alice_event)
        await bob.store_event("$bob", room_id, bob_event)

        alice.disable("principal checkpoint failure")

        assert alice.durable_writes_available is False
        assert await alice.get_event(room_id, "$alice") is None
        assert bob.durable_writes_available is True
        assert await bob.get_event(room_id, "$bob") == bob_event

        root.disable("shared schema failure")

        assert bob.durable_writes_available is False
        assert await bob.get_event(room_id, "$bob") is None
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_failed_room_purge_blocks_reads_until_recovery(
    event_cache_factory: Callable[[], ConversationEventCache],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient leave cleanup failure must remain pending and flush before later reads."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    cache = root.for_principal("@alice:localhost")
    room_id = "!left:localhost"
    event_id = "$event"
    event = _event(event_id, 1)
    await cache.store_event(event_id, room_id, event)

    if isinstance(cache, SqliteEventCache):
        module = sqlite_event_cache_events
    else:
        assert isinstance(cache, PostgresEventCache)
        module = postgres_event_cache_events
    original_purge = module.purge_room_locked
    failure_reason = "temporary purge failure"

    async def fail_purge(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(failure_reason)

    try:
        monkeypatch.setattr(module, "purge_room_locked", fail_purge)
        with pytest.raises(RuntimeError, match="temporary purge failure"):
            await cache.purge_room(room_id)
        assert cache.pending_durable_write_room_ids() == (room_id,)

        monkeypatch.setattr(module, "purge_room_locked", original_purge)
        await cache.flush_pending_durable_writes(room_id)
        assert await cache.get_event(room_id, event_id) is None
        assert cache.pending_durable_write_room_ids() == ()
    finally:
        await root.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup_kind", ["event", "mxc"])
async def test_departure_discards_read_that_started_before_fence(
    event_cache_factory: Callable[[], ConversationEventCache],
    monkeypatch: pytest.MonkeyPatch,
    *,
    lookup_kind: str,
) -> None:
    """An in-flight cache read must not expose its result after a confirmed leave."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    cache = root.for_principal("@alice:localhost")
    room_id = "!left:localhost"
    event_id = "$event"
    mxc_url = "mxc://server/plaintext"
    event = _event(event_id, 1, sidecar_url=mxc_url)
    read_obtained_result = asyncio.Event()
    release_read = asyncio.Event()
    module = sqlite_event_cache_events if isinstance(cache, SqliteEventCache) else postgres_event_cache_events
    loader_name = "load_event" if lookup_kind == "event" else "load_mxc_text"
    original_loader = getattr(module, loader_name)

    async def pause_after_read(*args: object, **kwargs: object) -> object:
        result = await original_loader(*args, **kwargs)
        read_obtained_result.set()
        await release_read.wait()
        return result

    try:
        await cache.store_event(event_id, room_id, event)
        assert await cache.store_mxc_text(room_id, event_id, mxc_url, "plaintext")
        monkeypatch.setattr(module, loader_name, pause_after_read)
        if lookup_kind == "event":
            read_task = asyncio.create_task(cache.get_event(room_id, event_id))
        else:
            read_task = asyncio.create_task(cache.get_mxc_text(room_id, event_id, mxc_url))
        await read_obtained_result.wait()

        cache.mark_room_departed(room_id)
        release_read.set()

        assert await read_task is None
    finally:
        release_read.set()
        await root.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("purge_scope", ["room", "principal"])
async def test_recovered_purge_discards_the_operation_that_flushes_it(
    event_cache_factory: Callable[[], ConversationEventCache],
    monkeypatch: pytest.MonkeyPatch,
    *,
    purge_scope: str,
) -> None:
    """A late write must not recreate rows in the transaction that commits a pending purge."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    cache = root.for_principal("@alice:localhost")
    room_id = "!left:localhost"
    old_event = _event("$old", 1)
    late_event = _event("$late", 2)
    await cache.store_event("$old", room_id, old_event)

    if isinstance(cache, SqliteEventCache):
        module = sqlite_event_cache_events
    else:
        assert isinstance(cache, PostgresEventCache)
        module = postgres_event_cache_events
    purge_name = f"purge_{purge_scope}_locked"
    original_purge = getattr(module, purge_name)
    failure_reason = "temporary purge failure"

    async def fail_purge(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(failure_reason)

    try:
        monkeypatch.setattr(module, purge_name, fail_purge)
        purge = (lambda: cache.purge_room(room_id)) if purge_scope == "room" else cache.purge_principal
        with pytest.raises(RuntimeError, match=failure_reason):
            await purge()

        monkeypatch.setattr(module, purge_name, original_purge)
        await cache.store_event("$late", room_id, late_event)

        assert await cache.get_event(room_id, "$old") is None
        assert await cache.get_event(room_id, "$late") is None
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_restoring_event_without_thread_relation_removes_stale_mapping(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Replacing an event must rebuild rather than accumulate its thread lookup index."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    cache = root.for_principal("@alice:localhost")
    room_id = "!room:localhost"
    event_id = "$event"
    threaded_event = _event(event_id, 1)
    threaded_event["content"]["m.relates_to"] = {
        "rel_type": "m.thread",
        "event_id": "$thread-root",
    }
    try:
        await cache.store_event(event_id, room_id, threaded_event)
        assert await cache.get_thread_id_for_event(room_id, event_id) == "$thread-root"
        assert await cache.get_thread_id_for_event(room_id, "$thread-root") == "$thread-root"

        await cache.store_event(event_id, room_id, _event(event_id, 2))

        assert await cache.get_thread_id_for_event(room_id, event_id) is None
        assert await cache.get_thread_id_for_event(room_id, "$thread-root") is None
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_storing_thread_root_preserves_child_proven_self_mapping(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """A relation-less root event must not erase the self-mapping proven by a surviving child."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    cache = root.for_principal("@alice:localhost")
    room_id = "!room:localhost"
    root_event_id = "$thread-root"
    child_event = _event("$child", 1)
    child_event["content"]["m.relates_to"] = {
        "rel_type": "m.thread",
        "event_id": root_event_id,
    }
    try:
        await cache.store_event("$child", room_id, child_event)
        assert await cache.get_thread_id_for_event(room_id, root_event_id) == root_event_id

        await cache.store_event(root_event_id, room_id, _event(root_event_id, 2))

        assert await cache.get_thread_id_for_event(room_id, "$child") == root_event_id
        assert await cache.get_thread_id_for_event(room_id, root_event_id) == root_event_id
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_redacting_last_thread_child_removes_orphan_root_mapping(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """The synthetic root self-index must exist exactly while a visible child proves it."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    cache = root.for_principal("@alice:localhost")
    room_id = "!room:localhost"
    root_event_id = "$thread-root"
    first_child = _event("$first-child", 1)
    first_child["content"]["m.relates_to"] = {
        "rel_type": "m.thread",
        "event_id": root_event_id,
    }
    second_child = _event("$second-child", 2)
    second_child["content"]["m.relates_to"] = {
        "rel_type": "m.thread",
        "event_id": root_event_id,
    }
    try:
        await cache.store_events_batch(
            [
                ("$first-child", room_id, first_child),
                ("$second-child", room_id, second_child),
            ],
        )

        assert await cache.redact_event(room_id, "$first-child")
        assert await cache.get_thread_id_for_event(room_id, "$first-child") is None
        assert await cache.get_thread_id_for_event(room_id, root_event_id) == root_event_id

        assert await cache.redact_event(room_id, "$second-child")
        assert await cache.get_thread_id_for_event(room_id, "$second-child") is None
        assert await cache.get_thread_id_for_event(room_id, root_event_id) is None
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_principal_purge_removes_only_that_principals_rows(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Cold-start cleanup must remove one principal without harming another."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    alice = root.for_principal("@alice:localhost")
    bob = root.for_principal("@bob:localhost")
    room_id = "!room:localhost"
    event_id = "$event"
    event = _event(event_id, 1)
    try:
        await alice.store_event(event_id, room_id, event)
        await bob.store_event(event_id, room_id, event)

        await alice.purge_principal()

        assert await alice.get_event(room_id, event_id) is None
        assert await bob.get_event(room_id, event_id) == event
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_failed_principal_purge_blocks_generation_and_reads_until_recovery(
    event_cache_factory: Callable[[], ConversationEventCache],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed cold-start cleanup must remain fail-closed for the current runtime."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    cache = root.for_principal("@alice:localhost")
    room_id = "!left:localhost"
    event_id = "$event"
    event = _event(event_id, 1)
    await cache.store_event(event_id, room_id, event)

    if isinstance(cache, SqliteEventCache):
        module = sqlite_event_cache_events
    else:
        assert isinstance(cache, PostgresEventCache)
        module = postgres_event_cache_events
    original_purge = module.purge_principal_locked
    failure_reason = "temporary principal purge failure"

    async def fail_purge(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(failure_reason)

    try:
        monkeypatch.setattr(module, "purge_principal_locked", fail_purge)
        with pytest.raises(RuntimeError, match="temporary principal purge failure"):
            await cache.purge_principal()
        assert cache.cache_generation is None

        monkeypatch.setattr(module, "purge_principal_locked", original_purge)
        assert await cache.get_event(room_id, event_id) is None
        assert cache.cache_generation is not None
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_redaction_reference_lifecycle_is_durable_and_non_resurrecting(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Redaction preserves shared plaintext, removes the last reference, and tombstones replays."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    principal_id = "@alice:localhost"
    cache = root.for_principal(principal_id)
    room_id = "!room:localhost"
    shared_mxc = "mxc://server/shared"
    dependent_mxc = "mxc://server/dependent"
    top_level = _event("$top", 1, sidecar_url=shared_mxc)
    new_content = _event(
        "$new-content",
        2,
        sidecar_url=shared_mxc,
        encrypted=True,
        sidecar_in_new_content=True,
    )
    original = _event("$original", 3, sidecar_url=dependent_mxc)
    edit = _event(
        "$edit",
        4,
        sidecar_url=dependent_mxc,
        encrypted=True,
        sidecar_in_new_content=True,
        edit_of="$original",
    )
    try:
        await cache.store_events_batch(
            [
                ("$top", room_id, top_level),
                ("$new-content", room_id, new_content),
                ("$original", room_id, original),
                ("$edit", room_id, edit),
            ],
        )
        assert await cache.store_mxc_text(room_id, "$top", shared_mxc, "shared plaintext")
        assert await cache.store_mxc_text(room_id, "$original", dependent_mxc, "dependent plaintext")

        assert await cache.redact_event(room_id, "$top")
        assert await cache.get_mxc_text(room_id, "$top", shared_mxc) is None
        assert await cache.get_mxc_text(room_id, "$new-content", shared_mxc) == "shared plaintext"

        assert await cache.redact_event(room_id, "$new-content")
        assert await cache.get_mxc_text(room_id, "$new-content", shared_mxc) is None

        assert await cache.redact_event(room_id, "$original")
        assert await cache.get_event(room_id, "$original") is None
        assert await cache.get_event(room_id, "$edit") is None
        assert await cache.get_mxc_text(room_id, "$edit", dependent_mxc) is None
    finally:
        await root.close()

    reopened_root = _shared_cache(event_cache_factory)
    await reopened_root.initialize()
    reopened = reopened_root.for_principal(principal_id)
    try:
        await reopened.store_events_batch(
            [
                ("$top", room_id, top_level),
                ("$new-content", room_id, new_content),
                ("$original", room_id, original),
                ("$edit", room_id, edit),
            ],
        )
        assert await reopened.get_event(room_id, "$top") is None
        assert await reopened.get_event(room_id, "$new-content") is None
        assert await reopened.get_event(room_id, "$original") is None
        assert await reopened.get_event(room_id, "$edit") is None
        assert await reopened.store_mxc_text(room_id, "$top", shared_mxc, "late plaintext") is False
        assert await reopened.store_mxc_text(room_id, "$edit", dependent_mxc, "late plaintext") is False
    finally:
        await reopened_root.close()


@pytest.mark.asyncio
async def test_postgres_principal_purge_excludes_other_runtime_operations(
    postgres_event_cache_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A namespace purge transaction must exclude room operations from another runtime."""
    namespace = f"purge_lock_{uuid.uuid4().hex}"
    first = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    second = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    room_id = "!room:localhost"
    event_id = "$after-purge"
    event = _event(event_id, 1)
    purge_deleted = asyncio.Event()
    release_purge = asyncio.Event()
    original_purge = postgres_event_cache_events.purge_principal_locked

    async def pause_after_delete(*args: object, **kwargs: object) -> None:
        await original_purge(*args, **kwargs)
        purge_deleted.set()
        await release_purge.wait()

    await first.initialize()
    await second.initialize()
    monkeypatch.setattr(postgres_event_cache_events, "purge_principal_locked", pause_after_delete)
    purge_task = asyncio.create_task(first.purge_principal())
    store_task: asyncio.Task[None] | None = None
    try:
        await purge_deleted.wait()
        store_task = asyncio.create_task(second.store_event(event_id, room_id, event))
        await asyncio.sleep(0.05)

        assert not store_task.done()

        release_purge.set()
        await purge_task
        await store_task
        assert await second.get_event(room_id, event_id) == event
    finally:
        release_purge.set()
        if not purge_task.done():
            await purge_task
        if store_task is not None and not store_task.done():
            await store_task
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_postgres_resumed_principal_purge_upgrades_to_exclusive_namespace_lock(
    postgres_event_cache_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary operation resuming a failed purge must still exclude every room writer."""
    namespace = f"resumed_purge_lock_{uuid.uuid4().hex}"
    first = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    second = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    first_room_id = "!first:localhost"
    second_room_id = "!second:localhost"
    event_id = "$after-purge"
    event = _event(event_id, 1)
    purge_deleted = asyncio.Event()
    release_purge = asyncio.Event()
    original_purge = postgres_event_cache_events.purge_principal_locked
    fail_initial_purge = True
    failure_reason = "temporary purge failure"

    async def control_purge(*args: object, **kwargs: object) -> None:
        if fail_initial_purge:
            raise RuntimeError(failure_reason)
        await original_purge(*args, **kwargs)
        purge_deleted.set()
        await release_purge.wait()

    await first.initialize()
    await second.initialize()
    monkeypatch.setattr(postgres_event_cache_events, "purge_principal_locked", control_purge)
    try:
        with pytest.raises(RuntimeError, match="temporary purge failure"):
            await first.purge_principal()
        fail_initial_purge = False

        resumed_purge = asyncio.create_task(first.get_event(first_room_id, "$missing"))
        await purge_deleted.wait()
        store_task = asyncio.create_task(second.store_event(event_id, second_room_id, event))
        await asyncio.sleep(0.05)

        assert not store_task.done()

        release_purge.set()
        assert await resumed_purge is None
        await store_task
        assert await second.get_event(second_room_id, event_id) == event
    finally:
        release_purge.set()
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_sqlite_write_transaction_serializes_tombstone_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite must lock before read-based event tombstone authorization."""
    db_path = tmp_path / "event-cache.db"
    principal_id = "@alice:localhost"
    first = SqliteEventCache(db_path, principal_id=principal_id)
    second = SqliteEventCache(db_path, principal_id=principal_id)
    room_id = "!room:localhost"
    event_id = "$late-event"
    event = _event(event_id, 1)
    event_checked = asyncio.Event()
    release_event_write = asyncio.Event()
    original_filter = sqlite_event_cache_events.filter_cacheable_events

    async def pause_after_tombstone_check(*args: object, **kwargs: object) -> object:
        cacheable = await original_filter(*args, **kwargs)
        event_checked.set()
        await release_event_write.wait()
        return cacheable

    await first.initialize()
    await second.initialize()
    try:
        monkeypatch.setattr(sqlite_event_cache_events, "filter_cacheable_events", pause_after_tombstone_check)
        late_store = asyncio.create_task(first.store_event(event_id, room_id, event))
        await event_checked.wait()
        redact_late_event = asyncio.create_task(second.redact_event(room_id, event_id))
        await asyncio.sleep(0.05)
        assert not redact_late_event.done()

        release_event_write.set()
        await late_store
        assert await redact_late_event
        assert await first.get_event(room_id, event_id) is None
    finally:
        release_event_write.set()
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_sqlite_write_transaction_serializes_mxc_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite must lock before read-based plaintext ownership authorization."""
    db_path = tmp_path / "event-cache.db"
    principal_id = "@alice:localhost"
    first = SqliteEventCache(db_path, principal_id=principal_id)
    second = SqliteEventCache(db_path, principal_id=principal_id)
    room_id = "!room:localhost"
    sidecar_event_id = "$sidecar"
    mxc_url = "mxc://server/plaintext"
    ownership_checked = asyncio.Event()
    release_plaintext_write = asyncio.Event()
    original_ownership_check = sqlite_event_cache_events._event_owns_mxc_text

    async def pause_after_ownership_check(*args: object, **kwargs: object) -> object:
        owns_plaintext = await original_ownership_check(*args, **kwargs)
        ownership_checked.set()
        await release_plaintext_write.wait()
        return owns_plaintext

    await first.initialize()
    await second.initialize()
    try:
        await first.store_event(
            sidecar_event_id,
            room_id,
            _event(sidecar_event_id, 2, sidecar_url=mxc_url),
        )
        monkeypatch.setattr(sqlite_event_cache_events, "_event_owns_mxc_text", pause_after_ownership_check)
        plaintext_store = asyncio.create_task(
            first.store_mxc_text(room_id, sidecar_event_id, mxc_url, "plaintext"),
        )
        await ownership_checked.wait()
        redact_sidecar = asyncio.create_task(second.redact_event(room_id, sidecar_event_id))
        await asyncio.sleep(0.05)
        assert not redact_sidecar.done()

        release_plaintext_write.set()
        assert await plaintext_store
        assert await redact_sidecar
        assert await first.get_mxc_text(room_id, sidecar_event_id, mxc_url) is None
        cursor = await first._runtime.require_db().execute(
            """
            SELECT COUNT(*)
            FROM mxc_text_cache
            WHERE principal_id = ? AND room_id = ? AND mxc_url = ?
            """,
            (principal_id, room_id, mxc_url),
        )
        assert await cursor.fetchone() == (0,)
        await cursor.close()
    finally:
        release_plaintext_write.set()
        await first.close()
        await second.close()
