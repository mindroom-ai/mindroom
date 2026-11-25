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
from mindroom.constants import INLINE_MEDIA_CONTENT_KEY, resolve_runtime_paths
from mindroom.custom_tools.attachments import AttachmentTools
from mindroom.delivery_gateway import DeliveryGateway, DeliveryGatewayDeps
from mindroom.event_journal import DeliveryStage
from mindroom.matrix.client_delivery import (
    build_edit_event_content,
    edit_message_outcome,
    prepare_message_content,
    send_message_outcome,
)
from mindroom.matrix.encrypted_file import encrypted_file_content_from_values
from mindroom.matrix.inline_media import (
    attach_inline_media,
    clear_inline_media_registry,
    register_inline_media,
)
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
    clear_inline_media_registry()
    yield
    clear_inline_media_registry()


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
        patch("mindroom.custom_tools.attachments.upload_media_bytes_as_mxc", new=uploads),
        tool_runtime_context(ctx),
    ):
        res1 = json.loads(await AttachmentTools().inline_image(rec1.attachment_id, alt="Chart"))
        res2 = json.loads(await AttachmentTools().inline_image(rec2.attachment_id, alt="Unused"))

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
        patch("mindroom.custom_tools.attachments.upload_media_bytes_as_mxc", new=AsyncMock()) as upload,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await AttachmentTools().inline_image("att_screen", alt="Desktop"))

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

    clear_inline_media_registry()
    await send_message_outcome(client, room_id, prepared, content_is_prepared=True)
    assert client.room_send.await_args.kwargs["content"][INLINE_MEDIA_CONTENT_KEY] == {"mxc://hs/frozen1": file1}


def test_segmented_messages_filter_inline_media_per_segment_and_keep_tokens_intact() -> None:
    """Split encrypted replies and edits give each segment only the keys its own HTML references."""
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
    assert INLINE_MEDIA_CONTENT_KEY not in segmented.continuations[-1] or segmented.continuations[-1][
        INLINE_MEDIA_CONTENT_KEY
    ] == {"mxc://hs/seg2": file2}
    # Exactly one continuation holds seg2 and none carry seg1.
    cont_maps = [c.get(INLINE_MEDIA_CONTENT_KEY) for c in segmented.continuations if INLINE_MEDIA_CONTENT_KEY in c]
    assert cont_maps == [{"mxc://hs/seg2": file2}]


@pytest.mark.asyncio
async def test_long_text_sidecar_keeps_full_inline_media_and_omits_from_preview() -> None:
    """Oversized encrypted messages store io.mindroom.inline_media in the encrypted sidecar, not the truncated preview."""
    room_id = "!enc:localhost"
    client = _encrypted_client(room_id, encrypted=True)
    file1 = _encrypted_file("mxc://hs/sidecar1")
    register_inline_media(room_id, "mxc://hs/sidecar1", file1)

    huge_body = "![big](mxc://hs/sidecar1)\n\n" + ("word " * 15000)
    attached = attach_inline_media(build_message_content(huge_body), room_id, room_encrypted=True)
    captured_sidecar: dict[str, Any] = {}

    async def fake_upload(
        _client: object,
        _room_id: str,
        payload: dict[str, Any],
        **_kwargs: object,
    ) -> tuple[str, dict[str, Any]]:
        captured_sidecar["payload"] = payload
        return "mxc://hs/sidecar-json", _encrypted_file("mxc://hs/sidecar-json", mime_type="application/json")

    with patch("mindroom.matrix.large_messages.upload_json_sidecar", new=AsyncMock(side_effect=fake_upload)):
        preview = await prepare_large_message(client, room_id, attached, room_encrypted=True)

    assert "io.mindroom.long_text" in preview
    assert INLINE_MEDIA_CONTENT_KEY not in preview
    assert captured_sidecar["payload"][INLINE_MEDIA_CONTENT_KEY] == {"mxc://hs/sidecar1": file1}


def test_segmented_messages_never_splits_inline_image_token_in_half() -> None:
    """When an image token straddles a chunk cut on a long line, the cut backs up before the token."""
    room_id = "!enc:localhost"
    file1 = _encrypted_file("mxc://hs/nosplit1")
    register_inline_media(room_id, "mxc://hs/nosplit1", file1)
    # Single line with no newlines where the image token lands right across the encrypted split budget.
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
async def test_long_text_edit_sidecar_keeps_inline_media_in_new_content_not_outer_wrapper() -> None:
    """Oversized encrypted edits keep io.mindroom.inline_media inside m.new_content in the sidecar, never on preview/wrapper."""
    room_id = "!enc:localhost"
    client = _encrypted_client(room_id, encrypted=True)
    file1 = _encrypted_file("mxc://hs/sidecar-edit1")
    register_inline_media(room_id, "mxc://hs/sidecar-edit1", file1)

    huge_body = "![big](mxc://hs/sidecar-edit1)\n\n" + ("word " * 15000)
    edit_content = build_edit_event_content(
        event_id="$orig",
        new_content=build_message_content(huge_body),
        new_text=huge_body,
    )
    attached = attach_inline_media(edit_content, room_id, room_encrypted=True)
    captured_sidecar: dict[str, Any] = {}

    async def fake_upload(
        _client: object,
        _room_id: str,
        payload: dict[str, Any],
        **_kwargs: object,
    ) -> tuple[str, dict[str, Any]]:
        captured_sidecar["payload"] = payload
        return "mxc://hs/sidecar-edit-json", _encrypted_file("mxc://hs/sidecar-edit-json", mime_type="application/json")

    with patch("mindroom.matrix.large_messages.upload_json_sidecar", new=AsyncMock(side_effect=fake_upload)):
        preview = await prepare_large_message(client, room_id, attached, room_encrypted=True)

    assert "io.mindroom.long_text" in preview["m.new_content"]
    assert INLINE_MEDIA_CONTENT_KEY not in preview
    assert INLINE_MEDIA_CONTENT_KEY not in preview["m.new_content"]
    assert INLINE_MEDIA_CONTENT_KEY not in captured_sidecar["payload"]
    assert captured_sidecar["payload"]["m.new_content"][INLINE_MEDIA_CONTENT_KEY] == {"mxc://hs/sidecar-edit1": file1}


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
