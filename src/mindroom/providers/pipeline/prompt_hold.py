"""Keep sending a conversation's system prompt unchanged while its prompt cache can be reused.

The system prompt leads every request, so a changed one makes the provider read the whole conversation again. A
request that names its conversation's hold file (``ModelRequest.prompt_hold``) to a route that takes a system prompt
update (``Capabilities.system_updates``) is therefore sent the system prompt the conversation last sent, and a change
to its stable first system message, such as an edited ``AGENTS.md``, as a system prompt update: a system message
after the newest user message, which every later request sends again in the same place.

One rule decides it, here and nowhere else: a request is sent the held prompt and its updates only if the file was
written for the same model and tools, was used within the cache lifetime, holds the request's other system messages,
and every anchor it stores (the digest of the first message, and of the messages before each update) matches the
request's messages. Any other request is sent exactly as it was built, and the hold starts over from it. So a request
whose conversation a compaction or a history limit rewrote sends its own prompt, and one that replays the same
messages, such as a retry or a resumed run, sends the same held prompt and updates.

The file lies in the agent's state root, so a restart keeps it and one requester's private prompt stays out of
another's; its modification time is the conversation's last request.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from contextlib import suppress
from dataclasses import asdict, replace
from difflib import SequenceMatcher
from itertools import takewhile
from typing import TYPE_CHECKING, Any

from mindroom.constants import PROMPT_CACHE_IDLE_SECONDS
from mindroom.file_locks import advisory_file_lock
from mindroom.llm.messages import PROMPT_UPDATE_KEY, Message, stable_serialize
from mindroom.llm.request_identity import message_digest
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.yaml_io import write_text_atomic

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from mindroom.llm.provider import ModelProvider, ModelRequest, StreamEvent

__all__ = ["held_request", "prompt_hold_path", "prompt_hold_stream"]

# The messages a request sends, and what its conversation's file holds once it sent them.
type _Plan = tuple[Sequence[Message], dict[str, Any]]

# A hold no request continues any more is deleted once it has been idle this long.
_PRUNE_IDLE_SECONDS = 24 * 3600
# A change larger than this, or one more update than this many, reads better as a new prompt.
_MAX_UPDATE_LINES = 200
_MAX_UPDATE_CHARS = 8000
_MAX_UPDATES = 8
_UPDATE_HEADER = (
    "MindRoom system prompt update. Your system prompt changed since this conversation began. This message replaces "
    "every earlier system prompt update in this conversation; where it and the system prompt differ, this message "
    "is in force."
)
_UPDATE_WITHDRAWN = (
    "MindRoom system prompt update. Every earlier system prompt update in this conversation is withdrawn; "
    "the system prompt applies as written."
)


def prompt_hold_path(database: Path, session_id: str) -> Path:
    """Return the hold file of conversation ``session_id``, in the state root of the ``database`` that stores its runs.

    Two requesters' private instances of one agent may share a session id, and never read each other's prompt.
    """
    return database.parent.parent / "prompt_holds" / f"{hashlib.sha256(session_id.encode()).hexdigest()}.json"


def _update_text(held: str, fresh: str) -> str | None:
    """Return the update that tells a model sent the ``held`` text what ``fresh`` says, or None when none may.

    It states the whole difference, so the newest update alone says what is in force. Each changed run of lines is
    named by the inlined file and the heading above it and quotes the lines that no longer hold, which the prompt the
    model was sent still shows. So a change to an inlined ``MEMORY.md`` is never told: a memory the user asked to
    forget would stay in every later request.
    """
    if held == fresh:
        return _UPDATE_WITHDRAWN
    parts = [_UPDATE_HEADER]
    changed = 0
    old, new = held.splitlines(), fresh.splitlines()
    for tag, old_start, old_end, new_start, new_end in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        removed = [line for line in old[old_start:old_end] if line.strip()]
        added = [line for line in new[new_start:new_end] if line.strip()]
        if tag == "equal" or not (removed or added):
            continue
        changed += len(removed) + len(added)
        # The headings above the change, within its own section: nothing after the role stands under the role's.
        above = list(takewhile(lambda line: line != "</your_role>", reversed(old[:old_start])))
        file = next((line for line in above if line.startswith("### /")), None)
        if any(line.startswith("### /") and line.endswith("/MEMORY.md") for line in (file, *removed, *added) if line):
            return None
        heading = next((line for line in above if line.startswith("#")), None)
        where = " > ".join(f'"{line}"' for line in dict.fromkeys((file, heading)) if line) or "the system prompt"
        parts.append(f"In {where}:")
        if removed:
            parts.append("No longer in force (ignore these lines of the system prompt):")
            parts.extend(f"- {line}" for line in removed)
        if added:
            parts.append("In force now:")
            parts.extend(f"+ {line}" for line in added)
    text = "\n".join(parts)
    return None if changed > _MAX_UPDATE_LINES or len(text) > _MAX_UPDATE_CHARS else text


def _anchors(messages: Sequence[Message], lengths: Sequence[int]) -> list[str | None]:
    """Return, for each of the ascending ``lengths``, the digest of what the first that many ``messages`` send.

    None for a length they do not reach, or one whose messages hold content unknown here (``message_digest``).
    """
    digest = hashlib.sha256()
    anchors: list[str | None] = []
    done = 0
    for length in lengths:
        while done < min(length, len(messages)) and (piece := message_digest(messages[done])) is not None:
            digest.update(piece)
            done += 1
        anchors.append(digest.hexdigest() if done == length else None)
    return anchors


def _may_stand(messages: Sequence[Message], position: int) -> bool:
    """Return whether an update may stand before ``messages[position]``: after a user message, before a reply or the end.

    Claude takes a system message nowhere else, and OpenRouter silently sends one placed elsewhere as the user's text.
    """
    return messages[position - 1].role == "user" and (
        position == len(messages) or messages[position].role == "assistant"
    )


def _read(path: Path, now: float) -> dict[str, Any] | None:
    """Return the state ``path`` holds, or None for a missing or unreadable file or one idle past the cache lifetime."""
    with suppress(OSError, ValueError):
        if now - path.stat().st_mtime < PROMPT_CACHE_IDLE_SECONDS:
            state = json.loads(path.read_text(encoding="utf-8"))
            return state if isinstance(state, dict) else None
    return None


def _continued(
    state: dict[str, Any],
    route: list[Any],
    system: Sequence[Message],
    rest: Sequence[Message],
) -> _Plan | None:
    """Return the plan of a request that continues the hold ``state``, or None when the hold must start over."""
    fresh = [message.content for message in system]
    held, updates = state["system"], state["updates"]
    lengths = [length for length, _, _ in updates]
    first, *anchors = _anchors(rest, [1, *lengths])
    if (
        state["route"] != route
        or held[1:] != fresh[1:]
        or first is None
        or first != state["first"]
        or any(
            anchor is None or anchor != stored or not _may_stand(rest, length)
            for anchor, (length, stored, _) in zip(anchors, updates, strict=True)
        )
    ):
        return None
    if fresh[0] != state["current"]:
        # A change the model was not told yet: it is told after the newest user message, or the hold starts over.
        text = _update_text(held[0], fresh[0])
        anchor = _anchors(rest, [len(rest)])[0]
        if (
            text is None
            or anchor is None
            or not _may_stand(rest, len(rest))
            or len(rest) <= max(lengths, default=0)
            or len(updates) >= _MAX_UPDATES
        ):
            return None
        updates = [*updates, [len(rest), anchor, text]]
        state = {**state, "current": fresh[0], "updates": updates}
    sent = [
        message if message.content == text else replace(message, content=text) for message, text in zip(system, held)
    ]
    start = 0
    for length, _, text in updates:
        update = Message(role="user", content=text, add_to_agent_memory=False, provider_data={PROMPT_UPDATE_KEY: True})
        sent += [*rest[start:length], update]
        start = length
    return (*sent, *rest[start:]), state


def _plan(inner: ModelProvider, request: ModelRequest, state: dict[str, Any] | None) -> _Plan | None:
    """Return the plan of ``request`` for a conversation whose file holds ``state``.

    None for a request that holds no prompt: one to a route that takes no update, or without a leading system text
    and a message after it.
    """
    messages = request.messages
    count = next((index for index, message in enumerate(messages) if message.role != "system"), len(messages))
    system, rest = messages[:count], messages[count:]
    texts = [message.content for message in system]
    if not (inner.capabilities.system_updates and texts and rest and all(isinstance(text, str) for text in texts)):
        return None
    tools = hashlib.sha256(stable_serialize([asdict(tool) for tool in request.tools]).encode()).hexdigest()
    route = [inner.identity.id, inner.identity.endpoint, tools]
    if state is not None:
        with suppress(KeyError, TypeError, ValueError, IndexError):
            if (continued := _continued(state, route, system, rest)) is not None:
                return continued
    first = _anchors(rest, [1])[0]
    return messages, {"route": route, "system": texts, "current": texts[0], "first": first, "updates": []}


def held_request(inner: ModelProvider, request: ModelRequest) -> ModelRequest:
    """Return ``request`` as ``inner`` is sent it: with its conversation's held system prompt and updates, if any."""
    if request.prompt_hold is None:
        return request
    plan = _plan(inner, request, _read(request.prompt_hold, time.time()))
    return replace(request, messages=request.messages if plan is None else plan[0], prompt_hold=None)


def _commit(inner: ModelProvider, request: ModelRequest, path: Path) -> ModelRequest:
    """Return ``request`` as it is sent, storing the hold it continues or starts; requests of one agent take turns."""
    now = time.time()
    sent = replace(request, prompt_hold=None)
    try:
        with advisory_file_lock(path.parent / ".lock"):
            for other in path.parent.glob("*.json"):
                if now - other.stat().st_mtime >= _PRUNE_IDLE_SECONDS:
                    other.unlink()
            state = _read(path, now)
            plan = _plan(inner, request, state)
            if plan is None:
                return sent
            if plan[1] != state:
                write_text_atomic(path, json.dumps(plan[1]))
            os.utime(path, (now, now))
    except OSError:
        return sent
    return replace(sent, messages=plan[0])


async def prompt_hold_stream(inner: ModelProvider, request: ModelRequest) -> AsyncIterator[StreamEvent]:
    """Stream ``request`` with its conversation's held system prompt, recording what the conversation now holds."""
    if (path := request.prompt_hold) is not None:
        request = await asyncio.to_thread(_commit, inner, request, path)
    events = inner.stream(request)
    async with closing_async_stream(events):
        async for event in events:
            yield event
