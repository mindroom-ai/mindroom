"""Project Matrix thread history into the text every judge reads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.constants import ATTACHMENT_IDS_KEY, ORIGINAL_SENDER_KEY
from mindroom.entity_resolution import current_internal_sender_ids
from mindroom.judgment.state import JudgmentMessage
from mindroom.matrix.sidecar_content import holds_unresolved_sidecar
from mindroom.matrix.thread_diagnostics import is_thread_history_degraded
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.requester_identity import mindroom_user_id
from mindroom.tool_system.events import body_with_tool_tags, message_tool_trace

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage


def message_judgment_text(message: ResolvedVisibleMessage) -> str | None:
    """Return one message as judges read it: tool calls as tags, earlier media as a placeholder."""
    if holds_unresolved_sidecar(message.content):
        # A long-text preview is not the whole message.
        return None
    msgtype = message.content.get("msgtype", "m.text")
    # A tuple compares without hashing, so a malformed list or dict msgtype cannot raise.
    if msgtype in ("m.text", "m.notice"):
        if not message.body.strip():
            return None
        trace = message_tool_trace(message.content)
        body = message.body if trace is None else body_with_tool_tags(message.body, trace)
        return f"{body}\n[with attachments]" if message.content.get(ATTACHMENT_IDS_KEY) else body
    kind = (msgtype.removeprefix("m.") if isinstance(msgtype, str) else "") or "media"
    filename = message.content.get("filename")
    name = filename if isinstance(filename, str) and filename.strip() else message.body
    label = f"[{kind}: {name}]" if name.strip() else f"[{kind}]"
    # With its own file name, a media event's body is a caption the sender wrote.
    return f"{label}\n{message.body}" if name != message.body and message.body.strip() else label


def conversation_context_before_sources(
    history: Sequence[ResolvedVisibleMessage],
    *,
    source_event_ids: tuple[str, ...],
    thread_id: str | None,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[JudgmentMessage, ...] | None:
    """Keep complete public text before this turn's first source, never future queued input.

    Preserve the conversation rather than guessing which old request is still active; the gate
    clips long messages and keeps the newest that fit. Missing or partial history cannot
    authorize continued tool use.
    """
    if is_thread_history_degraded(history) or (
        isinstance(history, ThreadHistoryResult) and not history.is_full_history
    ):
        return None
    if not history:
        # Only a proven new thread establishes that no earlier task context exists.
        return () if thread_id in source_event_ids else None
    internal_senders = current_internal_sender_ids(config, runtime_paths)
    agent_senders = internal_senders - {mindroom_user_id(config, runtime_paths)}
    context: list[JudgmentMessage] = []
    for message in history:
        if message.event_id in source_event_ids:
            return tuple(context)
        text = message_judgment_text(message)
        if text is None:
            return None
        role = "user"
        if message.sender in internal_senders:
            # A relay speaks for its original author: a person, MindRoom's internal user (a requester), or an agent.
            author = message.content.get(ORIGINAL_SENDER_KEY)
            if not author or (isinstance(author, str) and author in agent_senders):
                role = "assistant"
        context.append(JudgmentMessage(role, text))
    return None
