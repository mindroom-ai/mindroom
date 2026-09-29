"""Tests for centralized message content extraction with large message support."""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import nio
import pytest
from nio import crypto

import mindroom.matrix.media as media_module
import mindroom.matrix.message_content as message_content_module
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import STREAM_STATUS_KEY, STREAM_WARMUP_SUFFIX_KEY, RuntimePaths
from mindroom.entity_resolution import entity_identity_registry
from mindroom.matrix.client_visible_messages import (
    extract_visible_edit_body,
    message_preview,
    resolve_visible_event_source,
    thread_root_body_preview,
)
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.media import MxcUnavailable
from mindroom.matrix.message_content import (
    _download_mxc_text,
    extract_and_resolve_message,
    extract_edit_body,
    resolve_event_source_content,
    resolve_sidecar_content,
    sidecar_retry_pending,
)
from mindroom.matrix.sidecar_content import holds_unresolved_sidecar, sidecar_mxc_url, unavailable_sidecar_content
from mindroom.matrix.state import MatrixState
from mindroom.matrix.visible_body import (
    strip_matrix_rich_reply_fallback,
    visible_body_from_event_source,
    visible_content_from_content,
)
from tests.conftest import (
    TEST_ACCESS_TOKEN,
    bind_runtime_paths,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import persist_entity_accounts
from tests.matrix_media_helpers import FakeMediaResponse, media_response, requested_mxc


def _trusted_entity_sender_ids(config: Config, runtime_paths: RuntimePaths) -> frozenset[str]:
    return entity_identity_registry(config, runtime_paths)._internal_sender_ids


def _make_message_event(
    *,
    body: str,
    content: dict[str, object],
    event_id: str = "$event",
    sender: str = "@alice:example.com",
    timestamp_ms: int = 1234567890,
) -> nio.RoomMessageText:
    """Create a Matrix text event for message content tests."""
    event = nio.RoomMessageText(
        source={
            "content": content,
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": timestamp_ms,
            "type": "m.room.message",
        },
        body=body,
        formatted_body=None,
        format=None,
    )
    event.sender = sender
    return event


def _make_client() -> AsyncMock:
    """Return one AsyncClient-shaped test mock with a local agent user ID."""
    return make_matrix_client_mock(user_id="@mindroom_general:localhost")


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["complete", "cycle", "missing", "invalid"])
async def test_sidecar_chain_preserves_full_content_and_event_relation(ending: str) -> None:
    """Nested historical sidecars resolve fully or retain explicit unreadability."""
    metadata = {"version": 2, "encoding": "matrix_event_content_json"}
    first = {"msgtype": "m.file", "body": "preview", "url": "mxc://server/first", "io.mindroom.long_text": metadata}
    second = {**first, "url": "mxc://server/second"}
    relation = {"rel_type": "m.replace", "event_id": "$actual"}
    source = {"content": {**first, "m.relates_to": relation}}
    terminal = {
        "msgtype": "m.text",
        "body": "complete body",
        "io.mindroom.tool_trace": {"full": "tool output"},
        "m.relates_to": {"rel_type": "m.thread", "event_id": "$forged"},
    }
    client = _make_client()
    final_payload: bytes | None = json.dumps(terminal if ending == "complete" else first).encode()
    if ending == "missing":
        final_payload = None
    elif ending == "invalid":
        final_payload = b"not JSON"
    client.send.side_effect = [
        media_response(json.dumps({"m.new_content": second}).encode()),
        media_response(final_payload),
    ]

    resolved = await resolve_event_source_content(source, client)

    assert [requested_mxc(call.args[1]) for call in client.send.await_args_list] == [
        "mxc://server/first",
        "mxc://server/second",
    ]
    assert resolved["content"]["m.relates_to"] == relation
    if ending == "complete":
        assert resolved["content"] == {**terminal, "m.relates_to": relation}
    else:
        assert holds_unresolved_sidecar(resolved["content"])


@pytest.mark.asyncio
async def test_sidecar_chain_has_a_bounded_download_budget() -> None:
    """Unique sidecar URLs cannot cause an unbounded walk."""

    def preview(index: int) -> dict:
        return {
            "body": "preview",
            "url": f"mxc://server/{index}",
            "io.mindroom.long_text": {"version": 2, "encoding": "matrix_event_content_json"},
        }

    client = _make_client()
    client.send.side_effect = [media_response(json.dumps(preview(index)).encode()) for index in range(1, 10)]
    resolved = await resolve_sidecar_content(preview(0), client)
    assert client.send.await_count == message_content_module._MAX_SIDECAR_HOPS == 2
    assert holds_unresolved_sidecar(resolved.content)
    assert resolved.permanently_unavailable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "permanent"),
    [
        pytest.param(FakeMediaResponse(status=404), True, id="missing"),
        pytest.param(FakeMediaResponse(status=413), True, id="too-large"),
        pytest.param(media_response(b"\xff\xfe"), True, id="not-utf8"),
        pytest.param(media_response(b"[1, 2]"), True, id="not-an-object"),
        pytest.param(FakeMediaResponse(status=403), False, id="forbidden"),
        pytest.param(FakeMediaResponse(status=500), False, id="server-error"),
        pytest.param(FakeMediaResponse(status=401), False, id="expired-token"),
    ],
)
async def test_unreadable_sidecars_are_classified_and_remembered(response: FakeMediaResponse, permanent: bool) -> None:
    """Only failures intrinsic to the reference are permanent, and neither kind is downloaded again soon."""
    client = _make_client()
    client.send.return_value = response

    first = await resolve_sidecar_content(_SIDECAR_PREVIEW, client)
    second = await resolve_sidecar_content(_SIDECAR_PREVIEW, client)

    assert (first.permanently_unavailable, first.failed_download) == (permanent, True)
    assert (second.permanently_unavailable, second.failed_download) == (permanent, False)
    assert client.send.await_count == 1
    assert holds_unresolved_sidecar(second.content)


@pytest.mark.asyncio
async def test_missing_media_is_remembered_for_every_reference_to_its_url() -> None:
    """A 404 is a fact about the media, so another reference to the same URL is not downloaded again."""
    client = _make_client()
    client.send.return_value = FakeMediaResponse(status=404)
    other_reference = {**_SIDECAR_PREVIEW, "file": {"url": "mxc://server/sidecar", "v": "v2"}}

    first = await resolve_sidecar_content(_SIDECAR_PREVIEW, client)
    second = await resolve_sidecar_content(other_reference, client)

    assert first.permanently_unavailable
    assert second.permanently_unavailable
    assert client.send.await_count == 1


@pytest.mark.asyncio
async def test_a_content_failure_is_remembered_only_for_its_own_reference() -> None:
    """A payload that fails for one ``file`` dict says nothing about the same media under another."""
    client = _make_client()
    client.send.return_value = media_response(json.dumps({"body": "whole"}).encode())
    broken = {**_SIDECAR_PREVIEW, "file": {"url": "mxc://server/sidecar", "key": {"k": "wrong"}}}

    failed = await resolve_sidecar_content(broken, client)
    resolved = await resolve_sidecar_content(_SIDECAR_PREVIEW, client)

    assert failed.permanently_unavailable
    assert resolved.content == {"body": "whole"}
    assert client.send.await_count == 2


@pytest.mark.asyncio
async def test_concurrent_resolutions_of_one_reference_share_one_download() -> None:
    """Readers arriving while a download is in flight await it instead of starting their own."""
    release = asyncio.Event()

    async def slow_send(*_args: object, **_kwargs: object) -> FakeMediaResponse:
        await release.wait()
        return media_response(json.dumps({"body": "whole"}).encode())

    client = _make_client()
    client.send.side_effect = slow_send
    pending = [asyncio.create_task(resolve_sidecar_content(_SIDECAR_PREVIEW, client)) for _ in range(3)]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*pending)

    assert client.send.await_count == 1
    assert [result.content for result in results] == [{"body": "whole"}] * 3
    assert results[0].content is not results[1].content


def test_unavailable_sidecar_placeholder_is_plain_text_that_notes_its_body() -> None:
    """The stand-in keeps the event's relation, stops pointing at the file, and says the text is incomplete."""
    metadata = {"version": 2, "encoding": "matrix_event_content_json"}
    relation = {"rel_type": "m.replace", "event_id": "$original"}
    edit = {
        "msgtype": "m.text",
        "body": "* preview",
        "m.new_content": {
            "msgtype": "m.file",
            "body": "preview",
            "url": "mxc://s/x",
            "io.mindroom.long_text": metadata,
        },
        "m.relates_to": relation,
    }

    placeholder = unavailable_sidecar_content(edit)
    bodiless = unavailable_sidecar_content({"url": "mxc://s/y", "io.mindroom.long_text": metadata})

    assert placeholder == {
        "msgtype": "m.text",
        "body": "* preview",
        "m.new_content": {"msgtype": "m.text", "body": "preview\n\n[long message content unavailable]"},
        "m.relates_to": relation,
    }
    assert not holds_unresolved_sidecar(placeholder)
    assert bodiless["body"] == "[long message content unavailable]"


def _clocked_sidecar_client(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[FakeMediaResponse],
) -> tuple[AsyncMock, list[float]]:
    """Return a client serving ``responses`` in order and a mutable clock the sidecar memory reads."""
    clock = [1_000.0]
    monkeypatch.setattr(message_content_module, "monotonic", lambda: clock[0])
    client = _make_client()
    client.send.side_effect = responses
    return client, clock


_SIDECAR_PREVIEW = {
    "body": "preview",
    "url": "mxc://server/sidecar",
    "io.mindroom.long_text": {"version": 2, "encoding": "matrix_event_content_json"},
}


@pytest.mark.asyncio
async def test_transient_sidecar_failures_back_off_up_to_an_hour_and_never_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each further transient failure doubles the pause before a retry, up to an hour, and never becomes permanent."""
    pauses = [30, 60, 120, 240, 480, 960, 1920, 3600, 3600]
    client, clock = _clocked_sidecar_client(
        monkeypatch,
        [*(FakeMediaResponse(status=503) for _ in pauses), media_response(json.dumps({"body": "whole"}).encode())],
    )
    for pause in pauses:
        failed = await resolve_sidecar_content(_SIDECAR_PREVIEW, client)
        clock[0] += pause - 1
        remembered = await resolve_sidecar_content(_SIDECAR_PREVIEW, client)
        assert (failed.permanently_unavailable, failed.failed_download) == (False, True)
        assert (remembered.permanently_unavailable, remembered.failed_download) == (False, False)
        assert sidecar_retry_pending(_SIDECAR_PREVIEW)
        clock[0] += 1
        assert not sidecar_retry_pending(_SIDECAR_PREVIEW)

    assert (await resolve_sidecar_content(_SIDECAR_PREVIEW, client)).content == {"body": "whole"}
    assert client.send.await_count == len(pauses) + 1


@pytest.mark.asyncio
async def test_a_crafted_file_dict_is_remembered_by_a_fixed_size_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reference's failure memory keeps a digest, not the event's own ``file`` dict, however large it is."""
    client, _clock = _clocked_sidecar_client(monkeypatch, [FakeMediaResponse(status=503)])
    crafted = {**_SIDECAR_PREVIEW, "file": {"url": "mxc://server/sidecar", "padding": "x" * 60_000}}

    await resolve_sidecar_content(crafted, client)

    assert [len(key) for key in message_content_module._reference_failures] == [32]
    assert sidecar_retry_pending(crafted)
    assert not sidecar_retry_pending(_SIDECAR_PREVIEW)


class TestResolvedMessageExtraction:
    """Tests for coherent visible message extraction."""

    def test_sidecar_url_validation_prefers_the_encrypted_file_url(self) -> None:
        """Hydration must pick the MXC URL, not the plain HTTP one beside it."""
        content = {
            "body": "Preview body",
            "msgtype": "m.file",
            "io.mindroom.long_text": {
                "version": 2,
                "encoding": "matrix_event_content_json",
            },
            "url": "https://example.test/not-mxc",
            "file": {"url": "mxc://server/encrypted-sidecar"},
        }

        assert sidecar_mxc_url(content) == "mxc://server/encrypted-sidecar"

    @pytest.mark.parametrize("malformed_url", ["mxc://", "mxc://server"])
    def test_sidecar_url_validation_rejects_incomplete_mxc_uris(self, malformed_url: str) -> None:
        """Incomplete content URIs must not enter hydration."""
        metadata = {
            "version": 2,
            "encoding": "matrix_event_content_json",
        }
        direct_content = {
            "io.mindroom.long_text": metadata,
            "url": malformed_url,
        }
        encrypted_content = {
            "io.mindroom.long_text": metadata,
            "file": {"url": malformed_url},
        }

        assert sidecar_mxc_url(direct_content) is None
        assert sidecar_mxc_url(encrypted_content) is None

    @pytest.mark.parametrize(
        "overlong_url",
        [f"mxc://{'s' * 256}/media", f"mxc://server/{'m' * 256}", f"mxc://server/{'m' * 100_000}"],
    )
    def test_sidecar_url_validation_rejects_parts_past_their_length_limits(self, overlong_url: str) -> None:
        """A server name or media ID longer than a homeserver could mint never becomes a sidecar reference."""
        metadata = {"version": 2, "encoding": "matrix_event_content_json"}

        assert sidecar_mxc_url({"io.mindroom.long_text": metadata, "url": overlong_url}) is None
        assert sidecar_mxc_url({"io.mindroom.long_text": metadata, "file": {"url": overlong_url}}) is None
        assert sidecar_mxc_url({"io.mindroom.long_text": metadata, "url": f"mxc://{'s' * 255}/{'m' * 255}"})

    @pytest.mark.asyncio
    async def test_extract_and_resolve_message_hydrates_v2_sidecar_content(self) -> None:
        """Regular v2 sidecars should return the canonical content and body."""
        original_content = {
            "msgtype": "m.text",
            "body": "Full response body",
            "io.mindroom.tool_trace": {"version": 1, "events": [{"tool": "shell"}]},
        }
        event = _make_message_event(
            body="Preview body",
            content={
                "msgtype": "m.file",
                "body": "Preview body",
                "info": {"mimetype": "application/json"},
                "io.mindroom.long_text": {
                    "version": 2,
                    "encoding": "matrix_event_content_json",
                },
                "url": "mxc://server/sidecar",
            },
        )
        client = _make_client()
        client.send = AsyncMock(
            return_value=media_response(
                json.dumps(original_content).encode("utf-8"),
            ),
        )
        resolved = await extract_and_resolve_message(event, client)

        assert resolved["body"] == "Full response body"
        assert resolved["content"] == original_content

    @pytest.mark.asyncio
    async def test_extract_and_resolve_message_hydrates_v2_edit_wrapper(self) -> None:
        """Edit-sidecar events should resolve to the canonical outer replacement payload."""
        canonical_content = {
            "msgtype": "m.text",
            "body": "* Full edit body",
            "m.new_content": {
                "msgtype": "m.text",
                "body": "Full edit body",
                "io.mindroom.tool_trace": {"version": 1, "events": [{"tool": "shell"}]},
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
        }
        event = _make_message_event(
            body="* Preview edit",
            content={
                "msgtype": "m.text",
                "body": "* Preview edit",
                "m.new_content": {
                    "msgtype": "m.file",
                    "body": "Preview edit",
                    "info": {"mimetype": "application/json"},
                    "io.mindroom.long_text": {
                        "version": 2,
                        "encoding": "matrix_event_content_json",
                    },
                    "url": "mxc://server/edit-sidecar",
                },
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
            },
        )
        client = _make_client()
        client.send = AsyncMock(
            return_value=media_response(
                json.dumps(canonical_content).encode("utf-8"),
            ),
        )

        resolved = await extract_and_resolve_message(event, client)

        assert resolved["body"] == "* Full edit body"
        assert resolved["content"] == canonical_content
        assert resolved["content"]["body"] == resolved["body"]

    @pytest.mark.asyncio
    async def test_extract_edit_body_hydrates_v2_edit_sidecar(self) -> None:
        """Edit extraction should return the canonical m.new_content from a v2 sidecar."""
        canonical_content = {
            "msgtype": "m.text",
            "body": "* Full edit body",
            "m.new_content": {
                "msgtype": "m.text",
                "body": "Full edit body",
                "io.mindroom.tool_trace": {"version": 1, "events": [{"tool": "shell"}]},
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
        }
        client = _make_client()
        client.send = AsyncMock(
            return_value=media_response(
                json.dumps(canonical_content).encode("utf-8"),
            ),
        )

        body, content = await extract_edit_body(
            {
                "content": {
                    "msgtype": "m.text",
                    "body": "* Preview edit",
                    "m.new_content": {
                        "msgtype": "m.file",
                        "body": "Preview edit",
                        "info": {"mimetype": "application/json"},
                        "io.mindroom.long_text": {
                            "version": 2,
                            "encoding": "matrix_event_content_json",
                        },
                        "url": "mxc://server/edit-sidecar",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
                },
            },
            client,
        )

        assert body == "Full edit body"
        assert content == canonical_content["m.new_content"]

    @pytest.mark.asyncio
    async def test_extract_and_resolve_message_leaves_legacy_v1_preview_untouched(self) -> None:
        """Unsupported v1 sidecars should stay on the preview payload without download."""
        event = _make_message_event(
            body="Preview body",
            content={
                "msgtype": "m.file",
                "body": "Preview body",
                "io.mindroom.long_text": {
                    "version": 1,
                    "original_size": 100000,
                },
                "url": "mxc://server/legacy-sidecar",
            },
        )
        client = _make_client()
        client.send = AsyncMock()

        resolved = await extract_and_resolve_message(event, client)

        assert resolved["body"] == "Preview body"
        assert resolved["content"]["body"] == "Preview body"
        client.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_edit_body_leaves_legacy_v1_preview_untouched(self) -> None:
        """Unsupported v1 edit sidecars should keep the preview body/content coherent."""
        client = _make_client()
        client.send = AsyncMock()

        body, content = await extract_edit_body(
            {
                "content": {
                    "msgtype": "m.text",
                    "body": "* Preview edit",
                    "m.new_content": {
                        "msgtype": "m.file",
                        "body": "Preview edit",
                        "io.mindroom.long_text": {
                            "version": 1,
                            "original_size": 100000,
                        },
                        "url": "mxc://server/legacy-edit-sidecar",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
                },
            },
            client,
        )

        assert body == "Preview edit"
        assert content == {
            "msgtype": "m.file",
            "body": "Preview edit",
            "io.mindroom.long_text": {
                "version": 1,
                "original_size": 100000,
            },
            "url": "mxc://server/legacy-edit-sidecar",
        }
        client.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_event_source_content_hydrates_v2_edit_payload(self) -> None:
        """Event-source hydration should expose canonical edit metadata for mention routing."""
        canonical_content = {
            "msgtype": "m.text",
            "body": "* @agent full edit",
            "m.new_content": {
                "msgtype": "m.text",
                "body": "@agent full edit",
                "m.mentions": {"user_ids": ["@mindroom_agent:example.com"]},
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
        }
        client = _make_client()
        client.send = AsyncMock(
            return_value=media_response(
                json.dumps(canonical_content).encode("utf-8"),
            ),
        )

        event_source = await resolve_event_source_content(
            {
                "content": {
                    "msgtype": "m.text",
                    "body": "* Preview edit",
                    "m.new_content": {
                        "msgtype": "m.file",
                        "body": "Preview edit",
                        "info": {"mimetype": "application/json"},
                        "io.mindroom.long_text": {
                            "version": 2,
                            "encoding": "matrix_event_content_json",
                        },
                        "url": "mxc://server/context-edit-sidecar",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
                },
            },
            client,
        )

        assert event_source["content"] == canonical_content

    @pytest.mark.asyncio
    async def test_hydration_keeps_the_relation_the_event_carries(self) -> None:
        """A sidecar payload supplies text, never the event's place in the relation graph.

        The metadata naming the sidecar sits inside ``m.new_content``, so a payload that restated
        ``m.relates_to`` would erase the ``m.replace`` and hand whoever uploaded the file the choice
        of which conversation the message joins - a thread nothing about the event agrees with.
        """
        client = _make_client()
        client.send = AsyncMock(
            return_value=media_response(
                json.dumps(
                    {
                        "msgtype": "m.text",
                        "body": "Full text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$claimed"},
                    },
                ).encode("utf-8"),
            ),
        )

        event_source = await resolve_event_source_content(
            {
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.text",
                    "body": "* Preview edit",
                    "m.new_content": {
                        "msgtype": "m.file",
                        "body": "Preview edit",
                        "io.mindroom.long_text": {
                            "version": 2,
                            "encoding": "matrix_event_content_json",
                        },
                        "url": "mxc://server/crafted-edit-sidecar",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
                },
            },
            client,
        )

        # The text is hydrated, so this pins preservation rather than a skipped download.
        assert event_source["content"]["body"] == "Full text"
        event_info = EventInfo.from_event(event_source)
        assert event_info.is_edit is True
        assert event_info.original_event_id == "$original"
        assert event_info.thread_id is None

    @pytest.mark.asyncio
    async def test_hydration_cannot_give_a_relation_free_event_a_relation(self) -> None:
        """An event that relates to nothing still relates to nothing once its text arrives."""
        client = _make_client()
        client.send = AsyncMock(
            return_value=media_response(
                json.dumps(
                    {
                        "msgtype": "m.text",
                        "body": "Full text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$claimed"},
                    },
                ).encode("utf-8"),
            ),
        )

        event_source = await resolve_event_source_content(
            {
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.file",
                    "body": "Preview",
                    "io.mindroom.long_text": {
                        "version": 2,
                        "encoding": "matrix_event_content_json",
                    },
                    "url": "mxc://server/crafted-plain-sidecar",
                },
            },
            client,
        )

        assert event_source["content"]["body"] == "Full text"
        assert "m.relates_to" not in event_source["content"]
        assert EventInfo.from_event(event_source).thread_id is None

    def test_visible_body_from_event_source_prefers_visible_edit_content(self) -> None:
        """Visible-body extraction should use m.new_content when present."""
        event_source = {
            "content": {
                "msgtype": "m.text",
                "body": "* Preview edit",
                "m.new_content": {
                    "msgtype": "m.text",
                    "body": "Full edit body",
                },
            },
        }

        assert visible_body_from_event_source(event_source, "* Preview edit") == "Full edit body"

    def test_visible_body_from_event_source_prefers_canonical_stream_body(self) -> None:
        """Visible-body extraction should prefer canonical stream text over transient warmup suffixes."""
        event_source = {
            "sender": "@mindroom_general:localhost",
            "content": {
                "msgtype": "m.text",
                "body": "hello\n\n⏳ Preparing isolated worker...",
                "io.mindroom.visible_body": "hello",
            },
        }

        assert (
            visible_body_from_event_source(
                event_source,
                "hello",
                trusted_sender_ids={"@mindroom_general:localhost"},
            )
            == "hello"
        )

    def test_visible_body_from_event_source_uses_explicit_warmup_suffix_metadata(self) -> None:
        """Trusted streamed previews may remove only the exact suffix that was explicitly appended."""
        warmup_suffix = "⏳ Preparing isolated worker..."
        event_source = {
            "sender": "@mindroom_general:localhost",
            "content": {
                "msgtype": "m.text",
                "body": f"hello\n\n{warmup_suffix}",
                STREAM_WARMUP_SUFFIX_KEY: warmup_suffix,
            },
        }

        assert (
            visible_body_from_event_source(
                event_source,
                "hello",
                trusted_sender_ids={"@mindroom_general:localhost"},
            )
            == "hello"
        )

    def test_visible_body_from_event_source_ignores_empty_canonical_stream_body(self) -> None:
        """Empty canonical stream metadata should fall back to the actual Matrix body."""
        event_source = {
            "sender": "@mindroom_general:localhost",
            "content": {
                "msgtype": "m.text",
                "body": "Thinking...",
                "io.mindroom.visible_body": "",
            },
        }

        assert visible_body_from_event_source(
            event_source,
            "Thinking...",
            trusted_sender_ids={"@mindroom_general:localhost"},
        ) == ("Thinking...")

    def test_visible_body_from_event_source_ignores_untrusted_visible_body(self) -> None:
        """Untrusted inbound events should not override the real Matrix body via visible_body."""
        event_source = {
            "sender": "@mindroom_fake:localhost",
            "content": {
                "msgtype": "m.text",
                "body": "benign body",
                "io.mindroom.visible_body": "spoofed body",
            },
        }

        assert visible_body_from_event_source(
            event_source,
            "benign body",
            trusted_sender_ids={"@mindroom_general:localhost"},
        ) == ("benign body")

    def test_visible_body_from_event_source_does_not_strip_literal_status_text_without_explicit_metadata(self) -> None:
        """Legitimate final content should stay intact when no explicit warmup metadata is present."""
        event_source = {
            "sender": "@mindroom_general:localhost",
            "content": {
                "msgtype": "m.text",
                "body": "Diagnosis follows\n\n⚠️ Worker startup failed for shell.run: intentional example.",
                STREAM_STATUS_KEY: "completed",
            },
        }

        assert visible_body_from_event_source(
            event_source,
            "Diagnosis follows",
            trusted_sender_ids={"@mindroom_general:localhost"},
        ) == ("Diagnosis follows\n\n⚠️ Worker startup failed for shell.run: intentional example.")

    def test_strip_matrix_rich_reply_fallback_removes_quoted_prefix(self) -> None:
        """Rich-reply denial reasons should keep only the user-authored reply body."""
        body = "> <@alice:localhost> Approval required\n> quoted details\n\nNo, too risky."

        assert strip_matrix_rich_reply_fallback(body) == "No, too risky."

    def test_strip_matrix_rich_reply_fallback_allows_empty_reply_body(self) -> None:
        """Quote-only rich replies should not preserve the Matrix fallback."""
        body = "> <@alice:localhost> Approval required\n> quoted details\n\n"

        assert strip_matrix_rich_reply_fallback(body) == ""

    def test_strip_matrix_rich_reply_fallback_leaves_plain_quotes_alone(self) -> None:
        """Quoted text without the Matrix blank separator is normal message content."""
        body = "> keep this quoted line\nNo Matrix rich-reply separator"

        assert strip_matrix_rich_reply_fallback(body) == body

    def test_visible_content_from_content_prefers_replacement_content(self) -> None:
        """Matrix edit content unwrapping should be shared across consumers."""
        content = {
            "body": "* old",
            "m.new_content": {"body": "new", "status": "expired"},
        }

        assert visible_content_from_content(content) == {"body": "new", "status": "expired"}

    def test_visible_body_from_event_source_ignores_removed_agent_sender_ids(self, tmp_path: Path) -> None:
        """Removed managed senders must not keep overriding canonical-body metadata."""
        config = bind_runtime_paths(
            Config(agents={"general": AgentConfig(display_name="General Agent")}),
            test_runtime_paths(tmp_path),
        )
        runtime_paths = runtime_paths_for(config)
        persist_entity_accounts(config, runtime_paths)
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account("agent_removed", "mindroom_removed", "pw", domain="legacy.example.com")
        state.save(runtime_paths=runtime_paths)

        event_source = {
            "sender": "@mindroom_removed:legacy.example.com",
            "content": {
                "msgtype": "m.text",
                "body": "hello\n\n⏳ Preparing isolated worker...",
                "io.mindroom.visible_body": "hello",
            },
        }

        assert (
            visible_body_from_event_source(
                event_source,
                "hello",
                trusted_sender_ids=_trusted_entity_sender_ids(config, runtime_paths),
            )
            == "hello\n\n⏳ Preparing isolated worker..."
        )

    def test_visible_body_from_event_source_trusts_persisted_runtime_usernames(self, tmp_path: Path) -> None:
        """Persisted current usernames should stay trusted on the current runtime domain."""
        config = bind_runtime_paths(
            Config(agents={"general": AgentConfig(display_name="General Agent")}),
            test_runtime_paths(tmp_path),
        )
        runtime_paths = runtime_paths_for(config)
        persist_entity_accounts(config, runtime_paths, usernames={"general": "mindroom_general_oldns"})
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account("agent_general", "mindroom_general_oldns", "pw", domain=config.get_domain(runtime_paths))
        state.save(runtime_paths=runtime_paths)
        current_domain = config.get_domain(runtime_paths)

        event_source = {
            "sender": f"@mindroom_general_oldns:{current_domain}",
            "content": {
                "msgtype": "m.text",
                "body": "hello\n\n⏳ Preparing isolated worker...",
                "io.mindroom.visible_body": "hello",
            },
        }

        assert (
            visible_body_from_event_source(
                event_source,
                "hello",
                trusted_sender_ids=_trusted_entity_sender_ids(config, runtime_paths),
            )
            == "hello"
        )

    def test_visible_body_from_event_source_ignores_previous_persisted_sender_ids(self, tmp_path: Path) -> None:
        """Earlier persisted usernames must not stay trusted after a rename."""
        config = bind_runtime_paths(
            Config(agents={"general": AgentConfig(display_name="General Agent")}),
            test_runtime_paths(tmp_path),
        )
        runtime_paths = runtime_paths_for(config)
        persist_entity_accounts(config, runtime_paths, usernames={"general": "mindroom_general_v2"})
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account("agent_general", "mindroom_general_v1", "pw", domain="legacy.example.com")
        state.add_account("agent_general", "mindroom_general_v2", "pw", domain=config.get_domain(runtime_paths))
        state.save(runtime_paths=runtime_paths)

        event_source = {
            "sender": f"@mindroom_general_v1:{config.get_domain(runtime_paths)}",
            "content": {
                "msgtype": "m.text",
                "body": "hello\n\n⏳ Preparing isolated worker...",
                "io.mindroom.visible_body": "hello",
            },
        }

        assert (
            visible_body_from_event_source(
                event_source,
                "hello",
                trusted_sender_ids=_trusted_entity_sender_ids(config, runtime_paths),
            )
            == "hello\n\n⏳ Preparing isolated worker..."
        )

    @pytest.mark.asyncio
    async def test_resolve_visible_event_source_trusts_runtime_sender_ids(self, tmp_path: Path) -> None:
        """High-level visible-source resolution should derive trust from runtime config."""
        config = bind_runtime_paths(
            Config(agents={"general": AgentConfig(display_name="General Agent")}),
            test_runtime_paths(tmp_path),
        )
        runtime_paths = runtime_paths_for(config)
        persist_entity_accounts(
            config,
            runtime_paths,
            usernames={"router": "mindroom_router", "general": "mindroom_general"},
        )
        current_domain = config.get_domain(runtime_paths)
        event_source = {
            "sender": f"@mindroom_general:{current_domain}",
            "content": {
                "msgtype": "m.text",
                "body": "hello\n\n⏳ Preparing isolated worker...",
                "io.mindroom.visible_body": "hello",
            },
        }

        resolved_source, visible_body = await resolve_visible_event_source(
            event_source,
            None,
            fallback_body="hello",
            config=config,
            runtime_paths=runtime_paths,
        )

        assert resolved_source == event_source
        assert visible_body == "hello"

    @pytest.mark.asyncio
    async def test_extract_visible_edit_body_trusts_runtime_sender_ids(self, tmp_path: Path) -> None:
        """High-level edit extraction should derive trusted visible-body rules from runtime config."""
        config = bind_runtime_paths(
            Config(agents={"general": AgentConfig(display_name="General Agent")}),
            test_runtime_paths(tmp_path),
        )
        runtime_paths = runtime_paths_for(config)
        persist_entity_accounts(
            config,
            runtime_paths,
            usernames={"router": "mindroom_router", "general": "mindroom_general"},
        )
        current_domain = config.get_domain(runtime_paths)

        body, content = await extract_visible_edit_body(
            {
                "sender": f"@mindroom_general:{current_domain}",
                "content": {
                    "msgtype": "m.text",
                    "body": "* Preview edit",
                    "m.new_content": {
                        "msgtype": "m.text",
                        "body": "hello\n\n⏳ Preparing isolated worker...",
                        "io.mindroom.visible_body": "hello",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
                },
            },
            None,
            config=config,
            runtime_paths=runtime_paths,
        )

        assert body == "hello"
        assert content == {
            "msgtype": "m.text",
            "body": "hello",
            "io.mindroom.visible_body": "hello",
        }

    @pytest.mark.asyncio
    async def test_thread_root_body_preview_uses_runtime_sender_ids_for_bundled_edits(
        self,
        tmp_path: Path,
    ) -> None:
        """Thread previews should resolve trusted bundled edits without raw trusted-sender plumbing."""
        config = bind_runtime_paths(
            Config(agents={"general": AgentConfig(display_name="General Agent")}),
            test_runtime_paths(tmp_path),
        )
        runtime_paths = runtime_paths_for(config)
        persist_entity_accounts(
            config,
            runtime_paths,
            usernames={"router": "mindroom_router", "general": "mindroom_general"},
        )
        current_domain = config.get_domain(runtime_paths)
        event = _make_message_event(
            body="Original root",
            content={"msgtype": "m.text", "body": "Original root"},
            event_id="$thread-root",
            sender="@user:example.com",
        )
        event.source["unsigned"] = {
            "m.relations": {
                "m.replace": {
                    "event_id": "$thread-root-edit",
                    "sender": f"@mindroom_general:{current_domain}",
                    "origin_server_ts": 2000,
                    "type": "m.room.message",
                    "content": {
                        "body": "* Edited body\n\n⏳ Preparing isolated worker...",
                        "msgtype": "m.text",
                        "m.new_content": {
                            "body": "Edited body\n\n⏳ Preparing isolated worker...",
                            "msgtype": "m.text",
                            "io.mindroom.visible_body": "Edited body",
                        },
                        "m.relates_to": {"rel_type": "m.replace", "event_id": "$thread-root"},
                    },
                },
            },
        }

        preview = await thread_root_body_preview(
            event,
            client=_make_client(),
            config=config,
            runtime_paths=runtime_paths,
        )

        assert preview == "Edited body"

    @pytest.mark.asyncio
    async def test_thread_root_body_preview_passes_precomputed_trusted_sender_ids_to_nested_helpers(
        self,
        tmp_path: Path,
    ) -> None:
        """Thread previews should reuse one caller-provided trust set through nested helpers."""
        config = bind_runtime_paths(
            Config(agents={"general": AgentConfig(display_name="General Agent")}),
            test_runtime_paths(tmp_path),
        )
        runtime_paths = runtime_paths_for(config)
        persist_entity_accounts(
            config,
            runtime_paths,
            usernames={"router": "mindroom_router", "general": "mindroom_general"},
        )
        event = _make_message_event(
            body="Original root",
            content={"msgtype": "m.text", "body": "Original root"},
            event_id="$thread-root",
            sender="@user:example.com",
        )
        client = _make_client()
        trusted_sender_ids = frozenset({"@mindroom_general:localhost"})

        with (
            patch(
                "mindroom.matrix.client_visible_messages.bundled_replacement_body",
                new=AsyncMock(return_value=None),
            ) as mock_bundled,
            patch(
                "mindroom.matrix.client_visible_messages.resolve_visible_event_source",
                new=AsyncMock(return_value=(event.source, "Resolved root")),
            ) as mock_resolve,
        ):
            preview = await thread_root_body_preview(
                event,
                client=client,
                config=config,
                runtime_paths=runtime_paths,
                trusted_sender_ids=trusted_sender_ids,
            )

        assert preview == "Resolved root"
        mock_bundled.assert_awaited_once_with(
            event.source,
            client=client,
            config=config,
            runtime_paths=runtime_paths,
            trusted_sender_ids=trusted_sender_ids,
        )
        mock_resolve.assert_awaited_once_with(
            event.source,
            client,
            fallback_body="Original root",
            config=config,
            runtime_paths=runtime_paths,
            trusted_sender_ids=trusted_sender_ids,
        )

    def test_message_preview_compacts_whitespace_and_truncates(self) -> None:
        """Shared preview compaction should live in the Matrix visible-message layer."""
        assert message_preview("  alpha   beta  \n gamma  ", max_length=12) == "alpha bet..."


class TestDownloadMxcText:
    """Tests for _download_mxc_text function."""

    @pytest.mark.asyncio
    async def test_invalid_mxc_url(self) -> None:
        """Test handling of invalid MXC URL."""
        client = AsyncMock()
        result = await _download_mxc_text(client, "http://not-mxc-url")
        assert result == MxcUnavailable(permanent=True)

    @pytest.mark.asyncio
    async def test_malformed_mxc_url(self) -> None:
        """Test handling of malformed MXC URL."""
        client = AsyncMock()
        result = await _download_mxc_text(client, "mxc://no-media-id")
        assert result == MxcUnavailable(permanent=True)

    @pytest.mark.asyncio
    async def test_successful_download(self) -> None:
        """Test successful text download."""
        client = _make_client()
        client.send.return_value = media_response(b"Downloaded text content")

        result = await _download_mxc_text(client, "mxc://server/media123")
        assert result == "Downloaded text content"
        client.send.assert_awaited_once_with(
            "GET",
            "/_matrix/client/v1/media/download/server/media123?allow_remote=true",
            headers={"Accept-Encoding": "identity", "Authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
            timeout=media_module._download_timeout_seconds(message_content_module._MXC_TEXT_MAX_BYTES),
        )
        assert client.send.return_value.released
        assert await _download_mxc_text(client, "mxc://server/media123") == "Downloaded text content"
        assert client.send.await_count == 2

    @pytest.mark.asyncio
    async def test_successful_encrypted_download(self) -> None:
        """Encrypted sidecars should use the JWK key value emitted by nio."""
        plaintext = b"Downloaded encrypted text content"
        encrypted, file_info = crypto.attachments.encrypt_attachment(plaintext)
        client = _make_client()
        client.send.return_value = media_response(encrypted)

        assert await _download_mxc_text(client, "mxc://server/encrypted", file_info) == plaintext.decode()

    @pytest.mark.asyncio
    async def test_hydration_does_not_reuse_plaintext_across_calls(self) -> None:
        """Sidecar resolution keeps no durable memory of what it resolved.

        The resolved text belongs to the visible revision it is the body of,
        and the projection stores it there. A second copy here would need its
        own invalidation, and one that missed a redaction would serve deleted
        content.
        """
        client = _make_client()
        client.send.side_effect = [media_response(b"first"), media_response(b"second")]

        assert await _download_mxc_text(client, "mxc://server/incomplete") == "first"
        assert await _download_mxc_text(client, "mxc://server/incomplete") == "second"
        assert client.send.await_count == 2

    @pytest.mark.asyncio
    async def test_download_failure(self) -> None:
        """Test handling of download failure."""
        client = _make_client()
        client.send.return_value = media_response(None)

        result = await _download_mxc_text(client, "mxc://server/media123")
        assert result == MxcUnavailable(permanent=True)
        assert client.send.return_value.released

    @pytest.mark.asyncio
    async def test_rate_limited_download_waits_and_retries(self) -> None:
        """A 429 is retried after the homeserver's Retry-After delay, as nio's request loop did."""
        client = _make_client()
        rate_limited = FakeMediaResponse(status=429, headers={"Retry-After": "0"})
        client.send.side_effect = [rate_limited, media_response(b"after the wait")]

        assert await _download_mxc_text(client, "mxc://server/limited") == "after the wait"
        assert client.send.await_count == 2
        assert rate_limited.released

    @pytest.mark.asyncio
    async def test_rate_limit_without_a_numeric_delay_waits_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A missing or non-numeric Retry-After falls back to nio's default wait instead of failing."""
        monkeypatch.setattr(media_module, "_MXC_RATE_LIMIT_DEFAULT_WAIT_SECONDS", 0)
        client = _make_client()
        client.send.side_effect = [
            FakeMediaResponse(status=429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}),
            FakeMediaResponse(status=429, headers={"Retry-After": "1.5e9"}),
            media_response(b"after the wait"),
        ]

        assert await _download_mxc_text(client, "mxc://server/limited") == "after the wait"

    @pytest.mark.asyncio
    async def test_download_gives_up_after_repeated_rate_limits(self) -> None:
        """Retries are bounded, so a homeserver that keeps rate limiting leaves the sidecar unresolved."""
        client = _make_client()
        client.send.side_effect = [FakeMediaResponse(status=429, headers={"Retry-After": "0"}) for _ in range(5)]

        assert await _download_mxc_text(client, "mxc://server/limited") == MxcUnavailable(permanent=False)
        assert client.send.await_count == 3

    @pytest.mark.asyncio
    async def test_download_rejects_plaintext_over_byte_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Oversized sidecar bytes should not be decoded."""
        monkeypatch.setattr(message_content_module, "_MXC_TEXT_MAX_BYTES", 5)
        client = _make_client()
        client.send.return_value = FakeMediaResponse(chunks=[b"123", b"456"])

        assert await _download_mxc_text(client, "mxc://server/oversized") == MxcUnavailable(permanent=True)
        assert client.send.return_value.released

    @pytest.mark.asyncio
    async def test_download_rejects_declared_length_over_byte_limit_without_reading(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A Content-Length above the limit is refused before any body byte is read."""
        monkeypatch.setattr(message_content_module, "_MXC_TEXT_MAX_BYTES", 5)

        def unread_body() -> Iterator[bytes]:
            pytest.fail("Read the body of a sidecar that declared an oversized length")
            yield b""

        client = _make_client()
        client.send.return_value = FakeMediaResponse(chunks=unread_body(), content_length=6)

        assert await _download_mxc_text(client, "mxc://server/declared-oversized") == MxcUnavailable(permanent=True)
        assert client.send.return_value.released

    @pytest.mark.asyncio
    async def test_download_stops_reading_once_the_payload_crosses_the_limit(self) -> None:
        """A sidecar pointing at huge media never buffers more than the limit.

        nio's download reads the whole body before its size can be checked, so the
        payload is streamed from the homeserver instead.
        """
        chunk = b"x" * (64 * 1024)
        served: list[int] = []

        def body() -> Iterator[bytes]:
            for index in range(128):
                served.append(index)
                yield chunk

        client = _make_client()
        client.send.return_value = FakeMediaResponse(chunks=body())

        assert await _download_mxc_text(client, "mxc://server/huge") == MxcUnavailable(permanent=True)
        assert len(served) * len(chunk) <= message_content_module._MXC_TEXT_MAX_BYTES + len(chunk)

    @pytest.mark.asyncio
    async def test_download_rejects_encrypted_sidecar_over_byte_limit_before_decrypt(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Oversized encrypted sidecars should be rejected before decryption allocates plaintext."""
        monkeypatch.setattr(message_content_module, "_MXC_TEXT_MAX_BYTES", 5)
        client = _make_client()
        client.send.return_value = media_response(b"123456")
        file_info = {"key": {"k": "key"}, "hashes": {"sha256": "hash"}, "iv": "iv"}

        with patch("mindroom.matrix.message_content.crypto.attachments.decrypt_attachment") as mock_decrypt:
            result = await _download_mxc_text(client, "mxc://server/encrypted-oversized", file_info)

        assert result == MxcUnavailable(permanent=True)
        mock_decrypt.assert_not_called()

    @pytest.mark.asyncio
    async def test_download_rejects_decrypted_sidecar_over_byte_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Decrypted sidecar bytes should be capped before UTF-8 decode and JSON parsing."""
        monkeypatch.setattr(message_content_module, "_MXC_TEXT_MAX_BYTES", 5)
        client = _make_client()
        client.send.return_value = media_response(b"small")
        file_info = {"key": {"k": "key"}, "hashes": {"sha256": "hash"}, "iv": "iv"}

        with patch("mindroom.matrix.message_content.crypto.attachments.decrypt_attachment", return_value=b"123456"):
            result = await _download_mxc_text(client, "mxc://server/decrypted-oversized", file_info)

        assert result == MxcUnavailable(permanent=True)


class TestCanonicalContentResolution:
    """Tests for sidecar-backed canonical content extraction."""

    @pytest.mark.asyncio
    async def test_extract_and_resolve_message_hydrates_v2_content_metadata(self) -> None:
        """Large-message v2 previews should resolve canonical content keys from the sidecar."""
        client = _make_client()
        client.send.return_value = media_response(
            b'{"body":"Full body","msgtype":"m.text","io.mindroom.tool_trace":{"version":1,"events":[{"tool":"shell"}]}}',
        )
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "msgtype": "m.file",
                    "body": "Preview...",
                    "info": {"mimetype": "application/json"},
                    "io.mindroom.long_text": {"version": 2, "encoding": "matrix_event_content_json"},
                    "io.mindroom.ai_run": {"version": 1, "run_id": "run-preview"},
                    "url": "mxc://server/file-json",
                },
                "event_id": "$event",
                "sender": "@agent:example.com",
                "origin_server_ts": 123,
                "type": "m.room.message",
                "room_id": "!room:example.com",
            },
        )

        result = await extract_and_resolve_message(event, client)

        assert result["body"] == "Full body"
        assert result["content"]["io.mindroom.tool_trace"] == {"version": 1, "events": [{"tool": "shell"}]}
        assert "io.mindroom.long_text" not in result["content"]

    @pytest.mark.asyncio
    async def test_extract_edit_body_hydrates_v2_sidecar_new_content(self) -> None:
        """Edit extraction should use canonical m.new_content from a v2 sidecar payload."""
        client = _make_client()
        client.send.return_value = media_response(
            b'{"msgtype":"m.text","body":"* Full edit wrapper","m.new_content":{"body":"Full edit body","msgtype":"m.text",'
            b'"io.mindroom.tool_trace":{"version":1,"events":[{"tool":"web_search"}]}}}',
        )
        event_source = {
            "content": {
                "body": "* Preview edit",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "Preview edit...",
                    "msgtype": "m.file",
                    "info": {"mimetype": "application/json"},
                    "io.mindroom.long_text": {"version": 2, "encoding": "matrix_event_content_json"},
                    "url": "mxc://server/edit-json",
                },
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
            },
        }

        body, resolved_content = await extract_edit_body(event_source, client)

        assert body == "Full edit body"
        assert resolved_content == {
            "body": "Full edit body",
            "msgtype": "m.text",
            "io.mindroom.tool_trace": {"version": 1, "events": [{"tool": "web_search"}]},
        }

    @pytest.mark.asyncio
    async def test_extract_edit_body_prefers_canonical_stream_body(self) -> None:
        """Edit extraction should drop transient warmup suffixes when canonical stream text is present."""
        event_source = {
            "sender": "@mindroom_general:localhost",
            "content": {
                "body": "* hello",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "hello\n\n⏳ Preparing isolated worker...",
                    "msgtype": "m.text",
                    "io.mindroom.visible_body": "hello",
                },
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
            },
        }

        body, resolved_content = await extract_edit_body(
            event_source,
            trusted_sender_ids={"@mindroom_general:localhost"},
        )

        assert body == "hello"
        assert resolved_content == {
            "body": "hello",
            "msgtype": "m.text",
            "io.mindroom.visible_body": "hello",
        }

    @pytest.mark.asyncio
    async def test_extract_edit_body_ignores_untrusted_visible_body(self) -> None:
        """Edit extraction should not trust canonical-body overrides from arbitrary room senders."""
        event_source = {
            "sender": "@alice:localhost",
            "content": {
                "body": "* hello",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "hello\n\n⏳ Preparing isolated worker...",
                    "msgtype": "m.text",
                    "io.mindroom.visible_body": "hello",
                },
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
            },
        }

        body, resolved_content = await extract_edit_body(
            event_source,
            trusted_sender_ids={"@mindroom_general:localhost"},
        )

        assert body == "hello\n\n⏳ Preparing isolated worker..."
        assert resolved_content == {
            "body": "hello\n\n⏳ Preparing isolated worker...",
            "msgtype": "m.text",
            "io.mindroom.visible_body": "hello",
        }

    @pytest.mark.asyncio
    async def test_extract_edit_body_preserves_explicit_empty_string_body(self) -> None:
        """Edit extraction should keep explicit empty-string bodies instead of dropping the edit."""
        event_source = {
            "sender": "@mindroom_general:localhost",
            "content": {
                "body": "* Preview edit",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "",
                    "msgtype": "m.text",
                },
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
            },
        }

        body, resolved_content = await extract_edit_body(
            event_source,
            trusted_sender_ids={"@mindroom_general:localhost"},
        )

        assert body == ""
        assert resolved_content == {
            "body": "",
            "msgtype": "m.text",
        }


class TestExtractAndResolveMessage:
    """Tests for extracted read/thread payload formatting."""

    @pytest.mark.asyncio
    async def test_text_message_includes_msgtype(self) -> None:
        """Plain text messages should preserve their Matrix msgtype."""
        event = nio.RoomMessageText.from_dict(
            {
                "type": "m.room.message",
                "event_id": "$text",
                "sender": "@alice:localhost",
                "origin_server_ts": 1,
                "content": {"msgtype": "m.text", "body": "hello"},
            },
        )

        result = await extract_and_resolve_message(event)

        assert result == {
            "sender": "@alice:localhost",
            "body": "hello",
            "timestamp": 1,
            "event_id": "$text",
            "content": {"msgtype": "m.text", "body": "hello"},
            "msgtype": "m.text",
        }

    @pytest.mark.asyncio
    async def test_notice_message_includes_msgtype(self) -> None:
        """Notices should expose msgtype so callers can distinguish them from text."""
        event = nio.RoomMessageNotice.from_dict(
            {
                "type": "m.room.message",
                "event_id": "$notice",
                "sender": "@mindroom:localhost",
                "origin_server_ts": 2,
                "content": {"msgtype": "m.notice", "body": "Compacted 12 messages"},
            },
        )

        result = await extract_and_resolve_message(event)

        assert result == {
            "sender": "@mindroom:localhost",
            "body": "Compacted 12 messages",
            "timestamp": 2,
            "event_id": "$notice",
            "content": {"msgtype": "m.notice", "body": "Compacted 12 messages"},
            "msgtype": "m.notice",
        }

    @pytest.mark.asyncio
    async def test_extract_and_resolve_message_prefers_canonical_body_for_trusted_edit_event(self) -> None:
        """Trusted local agent edit events should resolve to canonical body text."""
        event = nio.RoomMessageText.from_dict(
            {
                "type": "m.room.message",
                "event_id": "$edit",
                "sender": "@mindroom_general:localhost",
                "origin_server_ts": 3,
                "content": {
                    "msgtype": "m.text",
                    "body": "* hello",
                    "m.new_content": {
                        "msgtype": "m.text",
                        "body": "hello\n\n⏳ Preparing isolated worker...",
                        "io.mindroom.visible_body": "hello",
                        STREAM_STATUS_KEY: "streaming",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
                },
            },
        )

        result = await extract_and_resolve_message(
            event,
            trusted_sender_ids={"@mindroom_general:localhost"},
        )

        assert result["body"] == "hello"

    @pytest.mark.asyncio
    async def test_extract_and_resolve_message_ignores_spoofed_visible_body(self) -> None:
        """Arbitrary inbound events should not override the real body via visible_body."""
        event = nio.RoomMessageText.from_dict(
            {
                "type": "m.room.message",
                "event_id": "$spoof",
                "sender": "@alice:localhost",
                "origin_server_ts": 4,
                "content": {
                    "msgtype": "m.text",
                    "body": "benign body",
                    "io.mindroom.visible_body": "spoofed body",
                },
            },
        )

        result = await extract_and_resolve_message(
            event,
            trusted_sender_ids={"@mindroom_general:localhost"},
        )

        assert result["body"] == "benign body"
