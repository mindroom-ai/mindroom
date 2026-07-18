"""Security and plaintext-lifecycle contract tests for every durable cache backend."""

from __future__ import annotations

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
        assert await bob.get_event(room_id, event_id) == bob_event
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
        assert await cache.get_event(room_id, event_id) is None
        assert cache.pending_durable_write_room_ids() == ()
    finally:
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

        await cache.store_event(event_id, room_id, _event(event_id, 2))

        assert await cache.get_thread_id_for_event(room_id, event_id) is None
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
