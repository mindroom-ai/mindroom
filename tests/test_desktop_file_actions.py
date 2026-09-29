"""Tests for desktop folder actions and their inline reply budget."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mindroom.desktop.filesystem import DesktopFilesystem
from mindroom.desktop.protocol import MAX_INLINE_RESPONSE_BYTES
from tests.desktop_bridge_helpers import (
    _LONGEST_SESSION_ID,
    _MAX_PROTOCOL_IDENTIFIER_LENGTH,
    _command,
    _event,
    _handle,
    _local_bridge,
    _longest_request_id,
    _response,
    _root_id,
)
from tests.desktop_bridge_helpers import selected_root as selected_root  # noqa: PLC0414
from tests.desktop_bridge_helpers import transport as transport  # noqa: PLC0414

if TYPE_CHECKING:
    from pathlib import Path
    from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_file_only_bridge_lists_and_reads_selected_folder_without_gui(
    transport: AsyncMock,
    selected_root: Path,
) -> None:
    """Folder reads work through the remote channel without any GUI provider or app selection."""
    files = DesktopFilesystem((selected_root,))
    bridge = _local_bridge(filesystem=files)
    root_id = _root_id(files)

    await _handle(bridge, _event(_command("list_folders")))
    assert _response(transport).result["folders"] == [{"id": root_id, "name": "selected", "path": str(selected_root)}]
    await _handle(
        bridge,
        _event(_command("list_directory", request_id="r2", sequence=2, parameters={"root_id": root_id})),
    )
    assert _response(transport).result["entries"] == [
        {"name": "docs", "type": "directory"},
        {"name": "link", "type": "symlink"},
        {"name": "note.txt", "type": "file"},
    ]
    assert _response(transport).result["truncated"] is False
    await _handle(
        bridge,
        _event(
            _command("list_directory", request_id="r3", sequence=3, parameters={"root_id": root_id, "path": "docs"}),
        ),
    )
    assert _response(transport).result["entries"] == [{"name": "readme.md", "type": "file"}]
    await _handle(
        bridge,
        _event(
            _command(
                "read_file",
                request_id="r4",
                sequence=4,
                parameters={"root_id": root_id, "path": "note.txt", "offset": 8},
            ),
        ),
    )
    assert _response(transport).result["text"] == "text"
    assert _response(transport).result["eof"] is True
    await _handle(bridge, _event(_command("status", request_id="r5", sequence=5)))
    status = _response(transport).result
    assert status["gui_available"] is False
    assert status["bridge"]["gui_available"] is False
    assert status["bridge"]["file_roots"] == [{"id": root_id, "name": "selected", "path": str(selected_root)}]
    assert status["bridge"]["shell"] == {
        "enabled": False,
        "pending": False,
        "auto_approve_remaining_seconds": 0,
        "auto_approve_until_revoked": False,
        "active_request_id": None,
        "handles": [],
    }
    assert type(status["bridge"]["shell"]["auto_approve_remaining_seconds"]) is int
    await _handle(bridge, _event(_command("list_apps", request_id="r6", sequence=6)))
    assert _response(transport).result == {"apps": [], "metrics": _response(transport).result["metrics"]}
    await _handle(bridge, _event(_command("get_app_state", request_id="r7", sequence=7)))
    assert _response(transport).error == "Desktop command must target an application in the local allowlist."
    bridge.close()


@pytest.mark.asyncio
async def test_list_directory_reply_is_bounded_by_the_real_enveloped_response(
    transport: AsyncMock,
    tmp_path: Path,
) -> None:
    """The trimmed listing must fit the real Olm-encrypted envelope, not just the bare entries dict."""
    root = tmp_path / "root"
    root.mkdir()
    names = sorted(("字" * 60 + f"{number:03}") for number in range(200))
    for name in names:
        (root / name).write_text("x")
    files = DesktopFilesystem((root,))
    bridge = _local_bridge(filesystem=files)
    root_id = _root_id(files)
    command = _command(
        "list_directory",
        request_id="r" * _MAX_PROTOCOL_IDENTIFIER_LENGTH,
        session_id="s" * _MAX_PROTOCOL_IDENTIFIER_LENGTH,
        parameters={"root_id": root_id},
    )
    await _handle(bridge, _event(command))
    response = _response(transport)
    assert response.content_bytes() <= MAX_INLINE_RESPONSE_BYTES
    entries = response.result["entries"]
    assert isinstance(entries, list)
    assert response.result["truncated"] is True
    assert 0 < len(entries) < 200
    # The kept prefix stays in the existing deterministic (sorted) order.
    assert [entry["name"] for entry in entries] == names[: len(entries)]
    bridge.close()


@pytest.mark.asyncio
async def test_list_folders_reply_is_bounded_by_the_real_enveloped_response(
    transport: AsyncMock,
    tmp_path: Path,
) -> None:
    """Many long root paths must fit the real Olm-encrypted envelope, not just the bare folders dict."""
    base = tmp_path / ("a" * 200) / ("b" * 200)
    base.mkdir(parents=True)
    roots = []
    for number in range(80):
        leaf = base / f"root-{number:03}-{'c' * 200}"
        leaf.mkdir()
        roots.append(leaf)
    files = DesktopFilesystem(tuple(roots))
    bridge = _local_bridge(filesystem=files)
    command = _command(
        "list_folders",
        request_id="r" * _MAX_PROTOCOL_IDENTIFIER_LENGTH,
        session_id="s" * _MAX_PROTOCOL_IDENTIFIER_LENGTH,
    )
    await _handle(bridge, _event(command))
    response = _response(transport)
    assert response.content_bytes() <= MAX_INLINE_RESPONSE_BYTES
    folders = response.result["folders"]
    assert isinstance(folders, list)
    assert response.result["truncated"] is True
    assert 0 < len(folders) < len(roots)
    bridge.close()


@pytest.mark.asyncio
async def test_read_file_replies_fit_as_recorded_and_sent_and_rebuild_the_file(
    transport: AsyncMock,
    tmp_path: Path,
) -> None:
    """Escape-heavy text arrives in replies within the budget, and reading on from next_offset rebuilds it exactly."""
    root = (tmp_path / "selected").resolve()
    root.mkdir()
    content = ("é" * 20_000 + "€" * 5_000 + "\U0001f600" * 3_000 + '"\\\t\n' * 2_000).encode()
    (root / "notes.txt").write_bytes(content)
    files = DesktopFilesystem((root,))
    bridge = _local_bridge(filesystem=files)
    root_id = _root_id(files)
    received, offset, reads = b"", 0, 0
    while not received or offset < len(content):
        reads += 1
        request_id = _longest_request_id(f"read-{reads}-")
        command = _command(
            "read_file",
            request_id=request_id,
            session_id=_LONGEST_SESSION_ID,
            sequence=reads,
            parameters={"root_id": root_id, "path": "notes.txt", "offset": offset},
        )
        await _handle(bridge, _event(command))
        sent = _response(transport)
        assert sent.to_content() == bridge._journal.get(request_id).response.to_content()
        assert sent.content_bytes() <= MAX_INLINE_RESPONSE_BYTES
        shown = str(sent.result["text"]).encode()
        assert shown
        assert (sent.result["offset"], sent.result["next_offset"]) == (offset, offset + len(shown))
        assert sent.result["eof"] is (offset + len(shown) == len(content))
        assert sent.result["truncated"] is not sent.result["eof"]
        received, offset = received + shown, offset + len(shown)
    assert received == content
    assert reads > len(content) // 16_384 + 1
    bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "parameters"),
    [
        ("list_directory", {"path": ".."}),
        ("list_directory", {"path": "link"}),
        ("read_file", {"path": "link"}),
        ("read_file", {"path": "../outside.txt"}),
        ("read_file", {"path": "/etc/hosts"}),
        ("read_file", {"root_id": "unknown", "path": "note.txt"}),
    ],
)
async def test_file_reads_stay_inside_selected_folder(
    transport: AsyncMock,
    selected_root: Path,
    action: str,
    parameters: dict[str, object],
) -> None:
    """Parent paths, absolute paths, unknown roots, and links never return outside bytes."""
    files = DesktopFilesystem((selected_root,))
    bridge = _local_bridge(filesystem=files)
    await _handle(bridge, _event(_command(action, parameters={"root_id": _root_id(files), **parameters})))
    response = _response(transport)
    assert not response.ok
    assert "outside secret" not in json.dumps(response.to_content())
    bridge.close()
