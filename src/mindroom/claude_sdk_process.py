"""Own one Claude Agent SDK client on a task of its own, for the ``claude_agent`` tool and the ``claude_code`` route.

Both keep a Claude Code process alive past the call that started it, so the task that connects the client must
outlive that call, and something must keep reading the process's output between calls.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from contextvars import Context
from typing import TYPE_CHECKING

from claude_agent_sdk import CLIConnectionError

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeSDKClient, Message

__all__ = ["open_client"]

logger = get_logger(__name__)
_START_CLEANUP_TIMEOUT_SECONDS = 5.0


async def open_client(
    client: ClaudeSDKClient,
    close_requested: asyncio.Event,
    messages: asyncio.Queue[Message | Exception],
) -> asyncio.Task[None]:
    """Connect on a task that owns the client and queues its messages until ``close_requested`` is set.

    The SDK's reader tasks and subprocess copy the connecting context and live as long as the session,
    which outlives the tool call and turn that open it; that turn's contextvars hold its Agent and tools.
    """
    connected = asyncio.get_running_loop().create_future()

    async def own() -> None:
        try:
            await client.connect()
        except Exception as exc:
            # The SDK has already disconnected; the caller raises the failure.
            connected.set_exception(exc)
            return
        except BaseException:
            connected.cancel()
            raise
        queueing = asyncio.create_task(_queue_messages(client, messages), name="claude_session_messages")
        connected.set_result(None)
        try:
            await close_requested.wait()
        finally:
            # What the SDK already holds still reaches ``messages`` while it disconnects.
            try:
                await client.disconnect()
            finally:
                queueing.cancel()

    owner = asyncio.create_task(own(), name="claude_session", context=Context())
    try:
        await asyncio.shield(connected)
    except BaseException:
        owner.cancel()
        await _drain_failed_start(owner)
        if connected.done() and not connected.cancelled():
            connected.exception()
        raise
    return owner


async def _queue_messages(client: ClaudeSDKClient, messages: asyncio.Queue[Message | Exception]) -> None:
    """Move every message Claude Code sends into ``messages``, ending with the error that ends the stream.

    The SDK holds at most 100 unread messages and then stops reading Claude Code's output, which also carries the
    replies to control requests such as ``set_model``; Claude Code keeps sending messages between calls.
    """
    try:
        async for message in client.receive_messages():
            messages.put_nowait(message)
    except Exception as error:
        messages.put_nowait(error)
    else:
        messages.put_nowait(CLIConnectionError("Claude Code stopped sending messages."))


async def _drain_failed_start(owner: asyncio.Task[None]) -> None:
    """Give the SDK a bounded chance to disconnect a start that nothing tracks yet, even when cancelled again.

    The caller re-raises what interrupted the start, so a failing or stuck disconnect is only logged.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _START_CLEANUP_TIMEOUT_SECONDS
    while not owner.done() and (remaining := deadline - loop.time()) > 0:
        with suppress(asyncio.CancelledError):
            await asyncio.wait({owner}, timeout=remaining)
    if not owner.done():
        logger.warning("Claude session cleanup timed out", timeout_seconds=_START_CLEANUP_TIMEOUT_SECONDS)
    elif not owner.cancelled() and (error := owner.exception()) is not None:
        logger.warning("Claude session cleanup failed", error=str(error))
