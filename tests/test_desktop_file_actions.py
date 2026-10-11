"""Tests for desktop folder actions and their inline reply budget."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mindroom.desktop.file_actions import execute_file
from mindroom.desktop.filesystem import DesktopFilesystem, DesktopFilesystemError
from mindroom.desktop.protocol import DesktopProtocolError
from mindroom.desktop.reply_fitting import fits_inline
from tests.desktop_helpers import (
    _LONGEST_SESSION_ID,
    _MAX_PROTOCOL_IDENTIFIER_LENGTH,
    APP_ID,
    _command,
    _longest_request_id,
    _root_id,
)
from tests.desktop_helpers import selected_root as selected_root  # noqa: PLC0414


@pytest.mark.asyncio
async def test_folder_actions_list_selected_folders_and_directories_and_read_from_an_offset(
    selected_root: Path,
) -> None:
    """Folders and their entries are listed, and a file read starts at the requested byte offset."""
    files = DesktopFilesystem((selected_root,))
    root_id = _root_id(files)

    folders = await execute_file(files, _command("list_folders"))
    assert folders["folders"] == [{"id": root_id, "name": "selected", "path": str(selected_root)}]
    listing = await execute_file(files, _command("list_directory", parameters={"root_id": root_id}))
    assert listing["entries"] == [
        {"name": "docs", "type": "directory"},
        {"name": "link", "type": "symlink"},
        {"name": "note.txt", "type": "file"},
    ]
    assert listing["truncated"] is False
    docs = await execute_file(files, _command("list_directory", parameters={"root_id": root_id, "path": "docs"}))
    assert docs["entries"] == [{"name": "readme.md", "type": "file"}]
    read = await execute_file(
        files,
        _command("read_file", parameters={"root_id": root_id, "path": "note.txt", "offset": 8}),
    )
    assert read["text"] == "text"
    assert read["eof"] is True
    files.close()


@pytest.mark.asyncio
async def test_folder_actions_require_a_filesystem() -> None:
    """Without selected folders there is nothing to read."""
    with pytest.raises(DesktopProtocolError, match=r"^Local file access is disabled\.$"):
        await execute_file(None, _command("list_folders"))


@pytest.mark.asyncio
async def test_list_directory_reply_is_bounded_by_the_real_enveloped_response(tmp_path: Path) -> None:
    """The trimmed listing must fit the enveloped reply with its widest metrics, not just the bare entries dict."""
    root = tmp_path / "root"
    root.mkdir()
    names = sorted(("字" * 60 + f"{number:03}") for number in range(200))
    for name in names:
        (root / name).write_text("x")
    files = DesktopFilesystem((root,))
    command = _command(
        "list_directory",
        request_id="r" * _MAX_PROTOCOL_IDENTIFIER_LENGTH,
        session_id="s" * _MAX_PROTOCOL_IDENTIFIER_LENGTH,
        parameters={"root_id": _root_id(files)},
    )
    result = await execute_file(files, command)
    assert fits_inline(command, result)
    entries = result["entries"]
    assert isinstance(entries, list)
    assert result["truncated"] is True
    assert 0 < len(entries) < 200
    # The kept prefix stays in the existing deterministic (sorted) order.
    assert [entry["name"] for entry in entries] == names[: len(entries)]
    files.close()


@pytest.mark.asyncio
async def test_list_folders_reply_is_bounded_by_the_real_enveloped_response(tmp_path: Path) -> None:
    """Many long root paths must fit the enveloped reply with its widest metrics, not just the bare folders dict."""
    base = tmp_path / ("a" * 200) / ("b" * 200)
    base.mkdir(parents=True)
    roots = []
    for number in range(80):
        leaf = base / f"root-{number:03}-{'c' * 200}"
        leaf.mkdir()
        roots.append(leaf)
    files = DesktopFilesystem(tuple(roots))
    command = _command(
        "list_folders",
        request_id="r" * _MAX_PROTOCOL_IDENTIFIER_LENGTH,
        session_id="s" * _MAX_PROTOCOL_IDENTIFIER_LENGTH,
    )
    result = await execute_file(files, command)
    assert fits_inline(command, result)
    folders = result["folders"]
    assert isinstance(folders, list)
    assert result["truncated"] is True
    assert 0 < len(folders) < len(roots)
    files.close()


@pytest.mark.asyncio
async def test_read_file_pages_fit_inline_and_rebuild_the_file(tmp_path: Path) -> None:
    """Escape-heavy text arrives in pages within the budget, and reading on from next_offset rebuilds it exactly."""
    root = (tmp_path / "selected").resolve()
    root.mkdir()
    content = ("é" * 20_000 + "€" * 5_000 + "\U0001f600" * 3_000 + '"\\\t\n' * 2_000).encode()
    (root / "notes.txt").write_bytes(content)
    files = DesktopFilesystem((root,))
    root_id = _root_id(files)
    received, offset, reads = b"", 0, 0
    while not received or offset < len(content):
        reads += 1
        command = _command(
            "read_file",
            request_id=_longest_request_id(f"read-{reads}-"),
            session_id=_LONGEST_SESSION_ID,
            sequence=reads,
            parameters={"root_id": root_id, "path": "notes.txt", "offset": offset},
        )
        result = await execute_file(files, command)
        assert fits_inline(command, result)
        shown = str(result["text"]).encode()
        assert shown
        assert (result["offset"], result["next_offset"]) == (offset, offset + len(shown))
        assert result["eof"] is (offset + len(shown) == len(content))
        assert result["truncated"] is not result["eof"]
        received, offset = received + shown, offset + len(shown)
    assert received == content
    assert reads > len(content) // 16_384 + 1
    files.close()


@pytest.mark.asyncio
async def test_long_local_paths_reach_folder_reads(tmp_path: Path) -> None:
    """Paths inside selected folders are not limited to identifier length."""
    nested = Path(*["d" * 60] * 5)
    root = (tmp_path / "selected").resolve()
    (root / nested).mkdir(parents=True)
    (root / nested / "note.txt").write_text("deep", encoding="utf-8")
    files = DesktopFilesystem((root,))
    read = _command("read_file", parameters={"root_id": _root_id(files), "path": str(nested / "note.txt")})
    assert (await execute_file(files, read))["text"] == "deep"
    files.close()


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
    selected_root: Path,
    action: str,
    parameters: dict[str, object],
) -> None:
    """Parent paths, absolute paths, unknown roots, and links never return outside bytes."""
    files = DesktopFilesystem((selected_root,))
    with pytest.raises(DesktopFilesystemError) as raised:
        await execute_file(files, _command(action, parameters={"root_id": _root_id(files), **parameters}))
    assert "outside secret" not in str(raised.value)
    files.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "parameters", "error"),
    [
        ("list_folders", {"root_id": "x"}, "Unexpected desktop parameters: root_id."),
        ("list_directory", {"offset": 0}, "Unexpected desktop parameters: offset."),
        ("read_file", {"path": "note.txt", "app": APP_ID}, "Unexpected desktop parameters: app."),
        ("read_file", {"path": "note.txt", "offset": "8"}, "Desktop parameter offset must be an integer."),
    ],
)
async def test_folder_actions_reject_unrelated_or_malformed_parameters(
    selected_root: Path,
    action: str,
    parameters: dict[str, object],
    error: str,
) -> None:
    """Each folder action accepts only its own strictly typed parameters."""
    files = DesktopFilesystem((selected_root,))
    if action != "list_folders":
        parameters = {"root_id": _root_id(files), **parameters}
    with pytest.raises(DesktopProtocolError, match=f"^{re.escape(error)}$"):
        await execute_file(files, _command(action, parameters=parameters))
    files.close()
