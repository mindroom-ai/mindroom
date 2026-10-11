"""Run desktop folder actions and fit their listings and reads into one reply."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from mindroom.desktop.command_parameters import (
    optional_int_parameter,
    optional_str_parameter,
    reject_unexpected_parameters,
    required_str_parameter,
)
from mindroom.desktop.protocol import DesktopProtocolError
from mindroom.desktop.reply_fitting import leftmost_fitting

if TYPE_CHECKING:
    from mindroom.desktop.filesystem import DesktopFilesystem
    from mindroom.desktop.protocol import DesktopCommand


async def execute_file(files: DesktopFilesystem | None, command: DesktopCommand) -> dict[str, object]:
    """Read through pinned folder descriptors on a worker thread."""
    if files is None:
        msg = "Local file access is disabled."
        raise DesktopProtocolError(msg)
    parameters = command.parameters
    if command.action == "list_folders":
        reject_unexpected_parameters(parameters, allowed=frozenset())
        folders = (await asyncio.to_thread(files.list_folders))["folders"]
        return _fit_listing(
            command,
            cast("list[dict[str, str]]", folders),
            key="folders",
            already_truncated=False,
        )
    if command.action == "list_directory":
        reject_unexpected_parameters(parameters, allowed=frozenset({"root_id", "path"}))
        listing = await asyncio.to_thread(
            files.list_directory,
            required_str_parameter(parameters, "root_id"),
            optional_str_parameter(parameters, "path", default="."),
        )
        return _fit_listing(
            command,
            cast("list[dict[str, str]]", listing["entries"]),
            key="entries",
            already_truncated=bool(listing["truncated"]),
        )
    reject_unexpected_parameters(parameters, allowed=frozenset({"root_id", "path", "offset"}))
    read = await asyncio.to_thread(
        files.read_file,
        required_str_parameter(parameters, "root_id"),
        required_str_parameter(parameters, "path"),
        optional_int_parameter(parameters, "offset") or 0,
    )
    return _fit_file_read(command, read)


def _fit_listing(
    command: DesktopCommand,
    entries: list[dict[str, str]],
    *,
    key: str,
    already_truncated: bool,
) -> dict[str, object]:
    """Keep the fitting prefix of ``entries``, in their existing deterministic order, under the inline budget."""
    total = len(entries)

    def reply(dropped: int) -> dict[str, object]:
        count = total - dropped
        return {key: entries[:count], "truncated": already_truncated or count < total}

    # Dropping later entries never grows the reply, so search for the fewest to drop.
    dropped = leftmost_fitting(command, 0, total, reply)
    return reply(dropped)


def _fit_file_read(command: DesktopCommand, read: dict[str, object]) -> dict[str, object]:
    """Keep the longest prefix of a file read whose escaped reply fits inline; the next read starts after it."""
    text = cast("str", read["text"])
    offset = cast("int", read["offset"])

    def reply(dropped: int) -> dict[str, object]:
        if not dropped:
            return read
        shown = text[: len(text) - dropped]
        return {**read, "text": shown, "next_offset": offset + len(shown.encode()), "eof": False, "truncated": True}

    # Dropping later characters never grows the reply, so search for the fewest to drop.
    dropped = leftmost_fitting(command, 0, len(text), reply)
    return reply(dropped)
