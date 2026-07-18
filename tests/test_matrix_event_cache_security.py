"""Security and plaintext-lifecycle contract tests for every durable cache backend."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from mindroom.matrix.cache import ConversationEventCache, SharedConversationEventCache

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
