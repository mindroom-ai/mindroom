"""Tests that fitted desktop replies stay within one encrypted to-device message."""

from __future__ import annotations

import json
import shlex
import sys
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.desktop.bridge import _WIDEST_METRICS, DesktopBridge
from mindroom.desktop.media import download_encrypted_media
from mindroom.desktop.protocol import MAX_INLINE_RESPONSE_BYTES, DesktopCommand, DesktopResponse, EncryptedDesktopMedia
from mindroom.matrix.olm_to_device import PinnedMatrixDevice
from tests.desktop_bridge_helpers import (
    _LONGEST_SESSION_ID,
    _MAX_PROTOCOL_IDENTIFIER_LENGTH,
    NOW_SECONDS,
    _command,
    _event,
    _execute,
    _local_shell,
    _policy,
    _run_shell,
)
from tests.test_olm_to_device import olm_transport

if TYPE_CHECKING:
    from pathlib import Path

# The Matrix spec sets no to-device or EDU size limit (matrix-org/matrix-doc#3121). Synapse 1.148 caps
# request bodies at 200 * 65,536 bytes and Tuwunel at 24 MiB, and one federation transaction carries up to
# 50 PDUs and 100 EDUs within that cap. Each encrypted to-device request therefore stays within the
# 65,536-byte event limit, so a full transaction of them still fits. Only this test checks it: production
# never reads this value, so it lives here rather than in mindroom.desktop.protocol.
_MAX_TO_DEVICE_BYTES = 65_536


def _inline_content_bytes(command: DesktopCommand, output: str) -> int:
    """Size the exact completed reply shape the bridge measures, with the metrics room it reserves."""
    return DesktopResponse(
        request_id=command.request_id,
        session_id=command.session_id,
        ok=True,
        result={
            "state": "completed",
            "handle": None,
            "exit_code": 0,
            "output": output,
            "output_bytes": len(output.encode()),
            "output_truncated": False,
            "output_attachment": None,
            "output_start": 0,
            "next_offset": len(output.encode()),
            "metrics": _WIDEST_METRICS,
        },
    ).content_bytes()


def _largest_inline_count(command: DesktopCommand, character: str) -> int:
    count = (MAX_INLINE_RESPONSE_BYTES - _inline_content_bytes(command, "")) // len(json.dumps(character))
    while _inline_content_bytes(command, character * (count + 1)) <= MAX_INLINE_RESPONSE_BYTES:
        count += 1
    while _inline_content_bytes(command, character * count) > MAX_INLINE_RESPONSE_BYTES:
        count -= 1
    return count


@pytest.mark.parametrize("character", ["\x01", "€", "😀"], ids=["control", "bmp", "astral"])
@pytest.mark.asyncio
async def test_worst_case_escaped_output_is_inline_only_while_the_encrypted_reply_fits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    character: str,
) -> None:
    """Measured through real Olm encryption, the largest inline reply and its receipt stay one to-device message."""
    monkeypatch.setattr("mindroom.desktop.bridge.authenticated_sender_matches", lambda *_args: True)
    uploaded: list[bytes] = []

    async def upload(_client: object, content: bytes, *, content_type: str, filename: str) -> nio.UploadResponse:
        del content_type, filename
        uploaded.append(content)
        return nio.UploadResponse("mxc://example.org/shell-output")

    monkeypatch.setattr("mindroom.desktop.media.upload_media_bytes", upload)
    desktop_user = "@" + "d" * 240 + ":example.org"
    controller_user = "@" + "c" * 240 + ":example.org"
    async with olm_transport(sender=desktop_user, recipient=controller_user) as (client, peer, requests, _):
        assert peer.olm is not None
        controller = PinnedMatrixDevice(controller_user, "DESKTOP", peer.olm.account.identity_keys["ed25519"])
        shell = _local_shell()
        shell.grant(60)
        bridge = DesktopBridge(
            client=client,
            provider=None,
            policy=replace(_policy(), controller=controller, allowed_app_ids=frozenset(), shell_enabled=True),
            shell=shell,
            clock=lambda: NOW_SECONDS,
        )
        inline_id, attached_id = "i" * _MAX_PROTOCOL_IDENTIFIER_LENGTH, "a" * _MAX_PROTOCOL_IDENTIFIER_LENGTH
        probe = replace(
            _run_shell("true", tmp_path, request_id=inline_id, expires_at_ms=120_000),
            session_id=_LONGEST_SESSION_ID,
        )
        count = _largest_inline_count(probe, character)
        results = []
        for sequence, (request_id, repeat) in enumerate(((inline_id, count), (attached_id, count + 1)), start=1):
            script = f"import sys; sys.stdout.buffer.write(({character!r} * {repeat}).encode())"
            command = _run_shell(
                f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}",
                tmp_path,
                request_id=request_id,
                sequence=sequence,
                timeout_seconds=30,
                expires_at_ms=120_000,
            )
            await bridge.on_to_device_event(_event(replace(command, session_id=_LONGEST_SESSION_ID)))
            await _execute(bridge)
            await bridge.deliver_pending()
            body = requests[-1]["body"]
            assert "/sendToDevice/m.room.encrypted/" in requests[-1]["path"]
            assert len(json.dumps(body, separators=(",", ":")).encode()) <= _MAX_TO_DEVICE_BYTES
            recorded = bridge._journal.get(request_id).response
            assert recorded.content_bytes() <= MAX_INLINE_RESPONSE_BYTES
            results.append(recorded.result)
        receipt = _command(
            "request_status",
            request_id="q" * _MAX_PROTOCOL_IDENTIFIER_LENGTH,
            session_id=_LONGEST_SESSION_ID,
            sequence=3,
            parameters={"request_id": inline_id},
        )
        await bridge.on_to_device_event(_event(receipt))
        await bridge.deliver_pending()
        assert len(json.dumps(requests[-1]["body"], separators=(",", ":")).encode()) <= _MAX_TO_DEVICE_BYTES
        await bridge.stop()
        bridge.close()

    inline, attached = results
    assert (inline["output"], inline["output_attachment"]) == (character * count, None)
    assert attached["output"] == ""
    media = EncryptedDesktopMedia.from_content(attached["output_attachment"], kind="output_attachment")
    download = AsyncMock(spec=nio.AsyncClient)
    download.download.return_value = nio.DownloadResponse(uploaded[0], "application/octet-stream", None)
    assert await download_encrypted_media(download, media, timeout_seconds=1) == (character * (count + 1)).encode()
