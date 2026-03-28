"""Hook-to-Matrix message sender helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

type HookMessageSender = Callable[
    [str, str, str | None, str, dict[str, Any] | None],
    Awaitable[str | None],
]


def resolve_hook_sender_domain(
    client: nio.AsyncClient,
    *,
    sender_domain: str | None = None,
) -> str | None:
    """Return the sender domain for one Matrix client, if enough identity is available."""
    from mindroom.matrix.identity import MatrixID  # noqa: PLC0415

    resolved_sender_domain = sender_domain
    if resolved_sender_domain is None:
        user_id = client.user_id
        if not isinstance(user_id, str):
            return None
        if not user_id.startswith("@") or ":" not in user_id:
            return None
        resolved_sender_domain = MatrixID.parse(user_id).domain
    return resolved_sender_domain


async def send_hook_message(
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    room_id: str,
    body: str,
    thread_id: str | None,
    source_hook: str,
    extra_content: dict[str, Any] | None,
    *,
    sender_domain: str | None = None,
) -> str | None:
    """Send one hook-originated Matrix message."""
    resolved_sender_domain = resolve_hook_sender_domain(client, sender_domain=sender_domain)
    if resolved_sender_domain is None:
        return None

    from mindroom.matrix.client import get_latest_thread_event_id_if_needed, send_message  # noqa: PLC0415
    from mindroom.matrix.mentions import format_message_with_mentions  # noqa: PLC0415

    content_extra = dict(extra_content or {})
    content_extra["com.mindroom.source_kind"] = "hook"
    content_extra["com.mindroom.hook_source"] = source_hook

    latest_thread_event_id = await get_latest_thread_event_id_if_needed(client, room_id, thread_id)
    content = format_message_with_mentions(
        config,
        runtime_paths,
        body,
        sender_domain=resolved_sender_domain,
        thread_event_id=thread_id,
        latest_thread_event_id=latest_thread_event_id,
        extra_content=content_extra,
    )
    return await send_message(client, room_id, content)


def build_hook_message_sender(
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    sender_domain: str | None = None,
) -> HookMessageSender | None:
    """Return a sender bound to one Matrix client, if enough identity is available."""
    resolved_sender_domain = resolve_hook_sender_domain(client, sender_domain=sender_domain)
    if resolved_sender_domain is None:
        return None

    async def _send(
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, Any] | None,
    ) -> str | None:
        return await send_hook_message(
            client,
            config,
            runtime_paths,
            room_id,
            body,
            thread_id,
            source_hook,
            extra_content,
            sender_domain=resolved_sender_domain,
        )

    return _send
