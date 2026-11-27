"""Tests for encrypted-room inline images across tool upload, delivery, edits, splits, and sidecars."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.attachments.storage import register_local_attachment
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import (
    AI_RUN_METADATA_KEY,
    INLINE_MEDIA_CONTENT_KEY,
    STREAM_STATUS_KEY,
    STREAM_STATUS_STREAMING,
    TOOL_TRACE_CONTENT_KEY,
    resolve_runtime_paths,
)
from mindroom.custom_tools.attachments import AttachmentTools
from mindroom.custom_tools.matrix_api import MatrixApiTools
from mindroom.custom_tools.matrix_conversation_operations import _message_payload
from mindroom.delivery_gateway import DeliveryGateway, DeliveryGatewayDeps, _reply_row_wire_content
from mindroom.event_journal import DeliveryStage
from mindroom.event_journal.models import MatrixDelivery, ReplyRowFacts
from mindroom.matrix.client_delivery import (
    build_edit_event_content,
    edit_message_outcome,
    prepare_message_content,
    send_message_outcome,
)
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.encrypted_file import encrypted_file_content_from_values
from mindroom.matrix.inline_media import _REGISTRY, attach_inline_media, register_inline_media
from mindroom.matrix.large_messages import prepare_large_message
from mindroom.matrix.message_builder import build_message_content
from mindroom.matrix.runtime_media import RuntimeEncryptedMediaAttachment
from mindroom.matrix.segmented_messages import segment_matrix_content
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    register_tool_runtime_media_attachment,
    tool_runtime_context,
)
from tests.access_schema_support import with_current_room_member_access
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import bind_runtime_paths, make_relation_lookup
from tests.test_media_delivery import image_bytes

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _reset_inline_media_registry() -> Iterator[None]:
    _REGISTRY.clear()
    yield
    _REGISTRY.clear()


def _sidecar_upload_mock() -> AsyncMock:
    return AsyncMock(
        return_value=("mxc://hs/sidecar-json", _encrypted_file("mxc://hs/sidecar-json", mime_type="application/json")),
    )


def _encrypted_file(mxc_uri: str, *, mime_type: str = "image/png", size: int = 123) -> dict[str, Any]:
    return encrypted_file_content_from_values(
        url=mxc_uri,
        key="k-secret-value",
        iv="iv-value",
        sha256="sha256-value",
        mime_type=mime_type,
        size=size,
    )


def _encrypted_client(room_id: str, *, encrypted: bool = True, has_olm: bool = True) -> MagicMock:
    client = MagicMock(spec=nio.AsyncClient)
    room = MagicMock(spec=nio.MatrixRoom)
    room.encrypted = encrypted
    client.rooms = {room_id: room}
    client.olm = MagicMock() if has_olm else None
    client.user_id = "@mindroom_agent:localhost"
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$sent", room_id=room_id))
    return client


def _tool_context(
    tmp_path: Path,
    *,
    room_id: str = "!enc:localhost",
    encrypted: bool = True,
    has_olm: bool = True,
    attachment_ids: tuple[str, ...] = (),
) -> ToolRuntimeContext:
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    config = bind_runtime_paths(
        with_current_room_member_access(
            Config(agents={"openclaw": AgentConfig(display_name="OpenClaw")}, authorization={}),
        ),
        runtime_paths,
    )
    return make_test_tool_runtime_context(
        agent_name="openclaw",
        target=MessageTarget.resolve(room_id=room_id, thread_id="$thread", reply_to_event_id=None),
        requester_id="@user:localhost",
        client=_encrypted_client(room_id, encrypted=encrypted, has_olm=has_olm),
        config=config,
        runtime_paths=runtime_paths,
        storage_path=tmp_path,
        conversation_reader=AsyncMock(),
        relations=make_relation_lookup(),
        attachment_ids=attachment_ids,
    )


@pytest.mark.asyncio
async def test_inline_image_encrypted_upload_registers_and_attaches_only_referenced_same_room(tmp_path: Path) -> None:
    """Encrypted inline_image registers keys without exposing them, and sends attach only matching same-room <img> URIs."""
    img1 = tmp_path / "chart.png"
    img2 = tmp_path / "unused.png"
    img1.write_bytes(image_bytes())
    img2.write_bytes(image_bytes())
    rec1 = register_local_attachment(tmp_path, img1, kind="image", mime_type="image/png")
    rec2 = register_local_attachment(tmp_path, img2, kind="image", mime_type="image/png")
    assert rec1 is not None
    assert rec2 is not None

    ctx = _tool_context(tmp_path, room_id="!enc:localhost", attachment_ids=(rec1.attachment_id, rec2.attachment_id))
    file1 = _encrypted_file("mxc://hs/img1")
    file2 = _encrypted_file("mxc://hs/img2")
    uploads = AsyncMock(
        side_effect=[
            ("mxc://hs/img1", {"info": {"mimetype": "image/png"}, "file": file1}),
            ("mxc://hs/img2", {"info": {"mimetype": "image/png"}, "file": file2}),
        ],
    )
    with (
        patch("mindroom.custom_tools.attachments.upload_media_bytes_as_mxc_outcome", new=uploads),
        tool_runtime_context(ctx),
    ):
        res1 = json.loads(
            await AttachmentTools().attachment("inline_image", attachment_id=rec1.attachment_id, alt="Chart"),
        )
        res2 = json.loads(
            await AttachmentTools().attachment("inline_image", attachment_id=rec2.attachment_id, alt="Unused"),
        )

    assert res1["status"] == "ok"
    assert res1["markdown"] == "![Chart](mxc://hs/img1)"
    assert "k-secret-value" not in json.dumps(res1)
    assert res2["status"] == "ok"

    # Mention img2 only in prose, link, inline code, and fenced code — only img1 is a real <img>.
    body = (
        f"Here is the chart: {res1['markdown']}\n\n"
        "Prose mxc://hs/img2, [link](mxc://hs/img2), `![x](mxc://hs/img2)`\n"
        '```html\n<img src="mxc://hs/img2">\n```'
    )
    content = build_message_content(body)
    outcome = await send_message_outcome(ctx.client, "!enc:localhost", content)
    assert outcome is not None
    sent = ctx.client.room_send.await_args.kwargs["content"]
    assert sent[INLINE_MEDIA_CONTENT_KEY] == {"mxc://hs/img1": file1}
    assert sent[INLINE_MEDIA_CONTENT_KEY]["mxc://hs/img1"]["url"] == "mxc://hs/img1"

    # Cross-room isolation: another encrypted room cannot attach !enc:localhost's key.
    other_client = _encrypted_client("!other:localhost", encrypted=True)
    await send_message_outcome(other_client, "!other:localhost", build_message_content(res1["markdown"]))
    assert INLINE_MEDIA_CONTENT_KEY not in other_client.room_send.await_args.kwargs["content"]

    # Plaintext room: never attaches inline_media, even for the same room_id.
    plain_client = _encrypted_client("!enc:localhost", encrypted=False)
    await send_message_outcome(plain_client, "!enc:localhost", build_message_content(res1["markdown"]))
    assert INLINE_MEDIA_CONTENT_KEY not in plain_client.room_send.await_args.kwargs["content"]


@pytest.mark.asyncio
async def test_inline_image_reuses_runtime_encrypted_media_in_encrypted_room(tmp_path: Path) -> None:
    """Turn-scoped RuntimeEncryptedMediaAttachment reuses its encrypted descriptor without re-uploading."""
    ctx = _tool_context(tmp_path, room_id="!enc:localhost", encrypted=True)
    media = RuntimeEncryptedMediaAttachment(
        attachment_id="att_screen",
        filename="screen.png",
        url="mxc://hs/screen1",
        key="screen-key",
        iv="screen-iv",
        sha256="screen-sha",
        mime_type="image/png",
        size=512,
    )
    register_tool_runtime_media_attachment(ctx, media)
    with (
        patch("mindroom.custom_tools.attachments.upload_media_bytes_as_mxc_outcome", new=AsyncMock()) as upload,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await AttachmentTools().attachment("inline_image", attachment_id="att_screen", alt="Desktop"),
        )

    assert payload["status"] == "ok"
    assert payload["markdown"] == "![Desktop](mxc://hs/screen1)"
    upload.assert_not_awaited()

    attached = attach_inline_media(build_message_content(payload["markdown"]), "!enc:localhost", room_encrypted=True)
    assert attached[INLINE_MEDIA_CONTENT_KEY] == {"mxc://hs/screen1": media.encrypted_file_content()}


@pytest.mark.asyncio
async def test_edit_puts_inline_media_only_in_new_content_and_recomputes_per_revision() -> None:
    """Edits attach io.mindroom.inline_media inside m.new_content only and drop removed images."""
    room_id = "!enc:localhost"
    client = _encrypted_client(room_id, encrypted=True)
    file1 = _encrypted_file("mxc://hs/edit1")
    file2 = _encrypted_file("mxc://hs/edit2")
    register_inline_media(room_id, "mxc://hs/edit1", file1)
    register_inline_media(room_id, "mxc://hs/edit2", file2)

    await edit_message_outcome(
        client,
        room_id,
        "$orig",
        build_message_content("![one](mxc://hs/edit1) ![two](mxc://hs/edit2)"),
        "![one](mxc://hs/edit1) ![two](mxc://hs/edit2)",
    )
    first_edit = client.room_send.await_args.kwargs["content"]
    assert INLINE_MEDIA_CONTENT_KEY not in first_edit
    assert first_edit["m.new_content"][INLINE_MEDIA_CONTENT_KEY] == {
        "mxc://hs/edit1": file1,
        "mxc://hs/edit2": file2,
    }

    # Revision removing edit1 keeps only edit2; revision removing all drops the key.
    await edit_message_outcome(
        client,
        room_id,
        "$orig",
        build_message_content("Only ![two](mxc://hs/edit2)"),
        "Only ![two](mxc://hs/edit2)",
    )
    second_edit = client.room_send.await_args.kwargs["content"]
    assert INLINE_MEDIA_CONTENT_KEY not in second_edit
    assert second_edit["m.new_content"][INLINE_MEDIA_CONTENT_KEY] == {"mxc://hs/edit2": file2}

    await edit_message_outcome(client, room_id, "$orig", build_message_content("No images now"), "No images now")
    third_edit = client.room_send.await_args.kwargs["content"]
    assert INLINE_MEDIA_CONTENT_KEY not in third_edit
    assert INLINE_MEDIA_CONTENT_KEY not in third_edit["m.new_content"]


@pytest.mark.asyncio
async def test_prepare_message_content_freezes_inline_media_across_registry_clear() -> None:
    """Prepared outbox/idempotent payloads freeze keys so retries do not depend on the live registry."""
    room_id = "!enc:localhost"
    client = _encrypted_client(room_id, encrypted=True)
    file1 = _encrypted_file("mxc://hs/frozen1")
    register_inline_media(room_id, "mxc://hs/frozen1", file1)

    prepared = await prepare_message_content(client, room_id, build_message_content("![a](mxc://hs/frozen1)"))
    assert isinstance(prepared, dict)
    assert prepared[INLINE_MEDIA_CONTENT_KEY] == {"mxc://hs/frozen1": file1}

    _REGISTRY.clear()
    await send_message_outcome(client, room_id, prepared, content_is_prepared=True)
    assert client.room_send.await_args.kwargs["content"][INLINE_MEDIA_CONTENT_KEY] == {"mxc://hs/frozen1": file1}


def test_segmented_edit_filters_inline_media_per_segment() -> None:
    """Split encrypted edits give each segment only the keys its own HTML references."""
    room_id = "!enc:localhost"
    file1 = _encrypted_file("mxc://hs/seg1")
    file2 = _encrypted_file("mxc://hs/seg2")
    register_inline_media(room_id, "mxc://hs/seg1", file1)
    register_inline_media(room_id, "mxc://hs/seg2", file2)

    filler = ("paragraph line with content " * 40 + "\n\n") * 20
    body = f"First image: ![one](mxc://hs/seg1)\n\n{filler}Second image: ![two](mxc://hs/seg2)"
    edit_content = build_edit_event_content(
        event_id="$placeholder",
        new_content=build_message_content(body),
        new_text=body,
    )
    attached = attach_inline_media(edit_content, room_id, room_encrypted=True)
    segmented = segment_matrix_content(attached, room_encrypted=True, continuation_thread_id="$thread")
    assert segmented is not None
    assert len(segmented.continuations) >= 1

    assert INLINE_MEDIA_CONTENT_KEY not in segmented.first
    assert segmented.first["m.new_content"][INLINE_MEDIA_CONTENT_KEY] == {"mxc://hs/seg1": file1}
    # Exactly one continuation holds seg2 and none carry seg1.
    cont_maps = [c.get(INLINE_MEDIA_CONTENT_KEY) for c in segmented.continuations if INLINE_MEDIA_CONTENT_KEY in c]
    assert cont_maps == [{"mxc://hs/seg2": file2}]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["reply", "edit", "streaming_edit"])
async def test_long_text_preview_carries_filtered_inline_media_and_sidecar_keeps_full_map(mode: str) -> None:
    """Long encrypted previews carry only keys for images in the preview HTML; the sidecar keeps the full map."""
    room_id = "!enc:localhost"
    client = _encrypted_client(room_id, encrypted=True)
    top_file = _encrypted_file("mxc://hs/top")
    bottom_file = _encrypted_file("mxc://hs/bottom")
    register_inline_media(room_id, "mxc://hs/top", top_file)
    register_inline_media(room_id, "mxc://hs/bottom", bottom_file)

    huge_body = "![top](mxc://hs/top)\n\n" + ("word " * 15000) + "\n\n![bottom](mxc://hs/bottom)"
    content = build_message_content(huge_body)
    is_edit = mode != "reply"
    if mode == "streaming_edit":
        content[STREAM_STATUS_KEY] = STREAM_STATUS_STREAMING
    if is_edit:
        content = build_edit_event_content(event_id="$orig", new_content=content, new_text=huge_body)
    attached = attach_inline_media(content, room_id, room_encrypted=True)
    upload = _sidecar_upload_mock()
    with patch("mindroom.matrix.large_messages.upload_json_sidecar", new=upload):
        preview = await prepare_large_message(client, room_id, attached, room_encrypted=True)

    captured_payload = upload.await_args.args[2]
    preview_visible = preview["m.new_content"] if is_edit else preview
    sidecar_visible = captured_payload["m.new_content"] if is_edit else captured_payload
    assert "io.mindroom.long_text" in preview_visible
    assert "mxc://hs/top" in preview_visible["formatted_body"]
    assert "mxc://hs/bottom" not in preview_visible["formatted_body"]
    assert preview_visible[INLINE_MEDIA_CONTENT_KEY] == {"mxc://hs/top": top_file}
    if is_edit:
        assert INLINE_MEDIA_CONTENT_KEY not in preview
        assert INLINE_MEDIA_CONTENT_KEY not in captured_payload
    assert sidecar_visible[INLINE_MEDIA_CONTENT_KEY] == {
        "mxc://hs/top": top_file,
        "mxc://hs/bottom": bottom_file,
    }


@pytest.mark.asyncio
async def test_long_text_preview_omits_inline_media_when_image_is_truncated_or_unencrypted() -> None:
    """Truncated-out images and plaintext rooms do not attach io.mindroom.inline_media onto previews."""
    room_id = "!enc:localhost"
    client = _encrypted_client(room_id, encrypted=True)
    bottom_file = _encrypted_file("mxc://hs/only-bottom")
    register_inline_media(room_id, "mxc://hs/only-bottom", bottom_file)

    bottom_only = attach_inline_media(
        build_message_content(("word " * 15000) + "\n\n![bottom](mxc://hs/only-bottom)"),
        room_id,
        room_encrypted=True,
    )
    with patch("mindroom.matrix.large_messages.upload_json_sidecar", new=_sidecar_upload_mock()):
        encrypted_preview = await prepare_large_message(client, room_id, bottom_only, room_encrypted=True)
    assert INLINE_MEDIA_CONTENT_KEY not in encrypted_preview

    top_with_key = build_message_content("![top](mxc://hs/only-bottom)\n\n" + ("word " * 15000))
    top_with_key[INLINE_MEDIA_CONTENT_KEY] = {"mxc://hs/only-bottom": bottom_file}
    with patch("mindroom.matrix.large_messages.upload_json_sidecar", new=_sidecar_upload_mock()):
        plain_preview = await prepare_large_message(
            client,
            room_id,
            top_with_key,
            room_encrypted=False,
            prepare_for_encrypted_delivery=True,
        )
    assert INLINE_MEDIA_CONTENT_KEY not in plain_preview


@pytest.mark.asyncio
@pytest.mark.parametrize("is_edit", [False, True], ids=["reply", "edit"])
async def test_finished_agent_answer_first_sidecar_preview_keeps_only_answer_inline_media(is_edit: bool) -> None:
    """Answer-first previews on finished agent replies/edits carry only the answer image key while the sidecar keeps both."""
    room_id = "!enc:localhost"
    client = _encrypted_client(room_id, encrypted=True)
    pre_file = _encrypted_file("mxc://hs/pre-tool")
    answer_file = _encrypted_file("mxc://hs/answer")
    register_inline_media(room_id, "mxc://hs/pre-tool", pre_file)
    register_inline_media(room_id, "mxc://hs/answer", answer_file)

    work = "![pre](mxc://hs/pre-tool)\n\n" + ("Investigating workspace state.\n\n" * 1600)
    answer = "Final answer: ![answer](mxc://hs/answer)"
    body = f"{work}\n\n🔧 `read_file` [1]\n\n{answer}"
    content = build_message_content(body)
    content[AI_RUN_METADATA_KEY] = {"version": 1, "status": "completed"}
    content[STREAM_STATUS_KEY] = "completed"
    content[TOOL_TRACE_CONTENT_KEY] = {
        "version": 2,
        "events": [{"type": "tool_call_completed", "tool_name": "read_file", "result_preview": "ok"}],
    }
    if is_edit:
        content = build_edit_event_content(event_id="$orig", new_content=content, new_text=body)
    attached = attach_inline_media(content, room_id, room_encrypted=True)

    upload = _sidecar_upload_mock()
    with patch("mindroom.matrix.large_messages.upload_json_sidecar", new=upload):
        preview = await prepare_large_message(client, room_id, attached, room_encrypted=True)

    captured_payload = upload.await_args.args[2]
    preview_visible = preview["m.new_content"] if is_edit else preview
    sidecar_visible = captured_payload["m.new_content"] if is_edit else captured_payload
    assert preview_visible["body"] == answer
    assert "mxc://hs/pre-tool" not in preview_visible["formatted_body"]
    assert "mxc://hs/answer" in preview_visible["formatted_body"]
    assert preview_visible[INLINE_MEDIA_CONTENT_KEY] == {"mxc://hs/answer": answer_file}
    if is_edit:
        assert INLINE_MEDIA_CONTENT_KEY not in preview
        assert INLINE_MEDIA_CONTENT_KEY not in captured_payload
    assert sidecar_visible[INLINE_MEDIA_CONTENT_KEY] == {
        "mxc://hs/pre-tool": pre_file,
        "mxc://hs/answer": answer_file,
    }


def test_segmented_messages_never_splits_inline_image_token_in_half() -> None:
    """When an image token straddles a chunk cut on a long line, the cut backs up before the token."""
    room_id = "!enc:localhost"
    file1 = _encrypted_file("mxc://hs/nosplit1")
    register_inline_media(room_id, "mxc://hs/nosplit1", file1)
    image_markdown = "![chart](mxc://hs/nosplit1)"
    body = ("a" * 14_800) + image_markdown + ("b" * 18_000)
    attached = attach_inline_media(build_message_content(body), room_id, room_encrypted=True)
    segmented = segment_matrix_content(attached, room_encrypted=True, continuation_thread_id="$thread")
    assert segmented is not None
    all_segments = [segmented.first, *segmented.continuations]
    assert any(image_markdown in seg["body"] for seg in all_segments)
    maps = [seg[INLINE_MEDIA_CONTENT_KEY] for seg in all_segments if INLINE_MEDIA_CONTENT_KEY in seg]
    assert maps == [{"mxc://hs/nosplit1": file1}]


@pytest.mark.asyncio
async def test_delivery_gateway_prepared_for_wire_attaches_inline_media_in_encrypted_rooms_only(tmp_path: Path) -> None:
    """DeliveryGateway._prepared_for_the_wire attaches keys before sizing/freezing in encrypted rooms only."""
    room_id = "!enc:localhost"
    file1 = _encrypted_file("mxc://hs/gw1")
    register_inline_media(room_id, "mxc://hs/gw1", file1)

    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    config = bind_runtime_paths(Config(agents={"agent": AgentConfig(display_name="Agent")}), runtime_paths)
    outbox = SimpleNamespace(principal_id="p1", load_matrix_delivery=AsyncMock(return_value=None))

    def _make_gateway(*, encrypted: bool) -> DeliveryGateway:
        client = _encrypted_client(room_id, encrypted=encrypted)
        return DeliveryGateway(
            DeliveryGatewayDeps(
                runtime=SimpleNamespace(client=client, config=config, orchestrator=None),
                runtime_paths=runtime_paths,
                agent_name="agent",
                logger=MagicMock(),
                resolver=SimpleNamespace(),
                response_hooks=SimpleNamespace(),
                outbox=outbox,
                turn_handoff=None,
                sending_device_id=lambda: "DEV1",
            ),
        )

    content = build_message_content("![gw](mxc://hs/gw1)")
    enc_prepared = await _make_gateway(encrypted=True)._prepared_for_the_wire(
        room_id=room_id,
        turn_id="turn-1",
        stage=DeliveryStage.FINAL,
        content=content,
    )
    assert enc_prepared.content[INLINE_MEDIA_CONTENT_KEY] == {"mxc://hs/gw1": file1}

    plain_prepared = await _make_gateway(encrypted=False)._prepared_for_the_wire(
        room_id=room_id,
        turn_id="turn-2",
        stage=DeliveryStage.FINAL,
        content=content,
    )
    assert INLINE_MEDIA_CONTENT_KEY not in plain_prepared.content


def test_attach_inline_media_strips_preexisting_map_when_unencrypted() -> None:
    """Plaintext/unknown rooms always strip any pre-existing io.mindroom.inline_media from top-level and m.new_content."""
    file1 = _encrypted_file("mxc://hs/drop1")
    plain = {**build_message_content("![a](mxc://hs/drop1)"), INLINE_MEDIA_CONTENT_KEY: {"mxc://hs/drop1": file1}}
    assert INLINE_MEDIA_CONTENT_KEY not in attach_inline_media(plain, "!plain:localhost", room_encrypted=False)

    edit = {
        **build_edit_event_content(
            event_id="$orig",
            new_content=plain,
            new_text="![a](mxc://hs/drop1)",
        ),
        INLINE_MEDIA_CONTENT_KEY: {"mxc://hs/drop1": file1},
    }
    stripped_edit = attach_inline_media(edit, "!plain:localhost", room_encrypted=False)
    assert INLINE_MEDIA_CONTENT_KEY not in stripped_edit
    assert INLINE_MEDIA_CONTENT_KEY not in stripped_edit["m.new_content"]


def test_late_bound_edit_drops_inline_media_from_outer_envelope() -> None:
    """Late-bound outbox edit wrapping keeps io.mindroom.inline_media only inside m.new_content."""
    file1 = _encrypted_file("mxc://hs/late1")
    frozen = {**build_message_content("![a](mxc://hs/late1)"), INLINE_MEDIA_CONTENT_KEY: {"mxc://hs/late1": file1}}
    claimed = MatrixDelivery(
        delivery_id="d1",
        stage=DeliveryStage.FINAL,
        room_id="!enc:localhost",
        membership_epoch=1,
        thread_id="$thread",
        transaction_id="tx1",
        payload=frozen,
        edits_event_id="$target",
        acknowledged_event_id=None,
        created_at_ns=1,
        reply_id="r1",
        reply_row=ReplyRowFacts(placeholder_only=False, new_text="![a](mxc://hs/late1)"),
    )
    envelope = _reply_row_wire_content(claimed)
    assert INLINE_MEDIA_CONTENT_KEY not in envelope
    assert envelope["m.new_content"][INLINE_MEDIA_CONTENT_KEY] == {"mxc://hs/late1": file1}


@pytest.mark.asyncio
async def test_model_read_boundaries_strip_inline_media_keys(tmp_path: Path) -> None:
    """matrix_message read and matrix_api get_event strip io.mindroom.inline_media from top-level and m.new_content."""
    room_id = "!enc:localhost"
    file1 = _encrypted_file("mxc://hs/read1")
    register_inline_media(room_id, "mxc://hs/read1", file1)
    ctx = _tool_context(tmp_path, room_id=room_id, encrypted=True)

    await send_message_outcome(ctx.client, room_id, build_message_content("![a](mxc://hs/read1)"))
    sent = ctx.client.room_send.await_args.kwargs["content"]
    assert INLINE_MEDIA_CONTENT_KEY in sent

    # matrix_message(action="read") boundary
    msg = ResolvedVisibleMessage.synthetic(
        sender="@mindroom_agent:localhost",
        body="![a](mxc://hs/read1)",
        event_id="$e1",
        content=sent,
    )
    read_payload = _message_payload(msg)
    assert INLINE_MEDIA_CONTENT_KEY not in read_payload["content"]
    assert "k-secret-value" not in json.dumps(read_payload)

    # matrix_api(action="get_event") boundary (both top-level and m.new_content)
    edit_with_keys = {
        **build_edit_event_content(event_id="$e1", new_content=sent, new_text="![a](mxc://hs/read1)"),
        INLINE_MEDIA_CONTENT_KEY: {"mxc://hs/read1": file1},
    }
    raw_response = nio.RoomGetEventResponse.from_dict(
        {
            "type": "m.room.message",
            "event_id": "$e2",
            "sender": "@mindroom_agent:localhost",
            "origin_server_ts": 1000,
            "content": edit_with_keys,
        },
    )
    ctx.client.room_get_event = AsyncMock(return_value=raw_response)
    with tool_runtime_context(ctx):
        api_payload = json.loads(await MatrixApiTools().matrix_api(action="get_event", event_id="$e2"))
    assert api_payload["status"] == "ok"
    assert INLINE_MEDIA_CONTENT_KEY not in api_payload["event"]["content"]
    assert INLINE_MEDIA_CONTENT_KEY not in api_payload["event"]["content"]["m.new_content"]
    assert "k-secret-value" not in json.dumps(api_payload)


def test_attach_inline_media_ignores_data_src_attribute() -> None:
    """Only a real src attribute on <img> matches, not hyphenated attributes like data-src."""
    room_id = "!enc:localhost"
    fake = _encrypted_file("mxc://hs/fake")
    real = _encrypted_file("mxc://hs/real")
    register_inline_media(room_id, "mxc://hs/fake", fake)
    register_inline_media(room_id, "mxc://hs/real", real)

    only_data_src = {
        "msgtype": "m.text",
        "body": "lazy",
        "format": "org.matrix.custom.html",
        "formatted_body": '<p><img data-src="mxc://hs/fake" alt="lazy"></p>',
    }
    assert INLINE_MEDIA_CONTENT_KEY not in attach_inline_media(only_data_src, room_id, room_encrypted=True)

    both_attrs = {
        "msgtype": "m.text",
        "body": "both",
        "format": "org.matrix.custom.html",
        "formatted_body": '<p><img data-src="mxc://hs/fake" src="mxc://hs/real" alt="both"></p>',
    }
    attached = attach_inline_media(both_attrs, room_id, room_encrypted=True)
    assert attached[INLINE_MEDIA_CONTENT_KEY] == {"mxc://hs/real": real}
