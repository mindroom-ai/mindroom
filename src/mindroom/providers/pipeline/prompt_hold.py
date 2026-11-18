"""Keep sending a conversation's system prompt unchanged while its prompt cache can be reused.

The system prompt leads every request, so a changed one makes the provider read the whole conversation again. A
request that names its conversation's hold (``ModelRequest.prompt_hold``) to a route that takes a system prompt
update (``Capabilities.system_updates``) is therefore sent the system prompt the conversation last sent, and a change
to its stable first system message, such as an edited ``AGENTS.md``, as a system prompt update: a system message
after the newest user message, which every later request sends again in the same place.

One rule decides it, here and nowhere else: a request is sent the held prompt and its updates only if the file was
written for the same model and tools, was used within the cache lifetime, holds the request's other system messages,
and every anchor it stores (the digest of the first message, and of the messages before each update) matches the
request's messages. Any other request is sent exactly as it was built, and the hold starts over from it. So a request
whose conversation a compaction or a history limit rewrote sends its own prompt, and one that replays the same
messages, such as a retry or a resumed run, sends the same held prompt and updates.

Only a conversation's own turns write its file. A run that only reads it (``ModelRequest.prompt_hold_pin``), such
as a skill review's fork or an approval resumed after its turn, is sent what the file held at its first request
when it continues that as it is and its newest answer records a request sent the same, and its own requests
otherwise: every request of the run is sent the same prompt, the one the run it continues was sent.

The stage records on each answer the identity of its request as it was sent, so the usage a provider reported for
it sizes only a later request sent the same prompt and updates: the file may hold another by then.

The file lies in the agent's state root, so a restart keeps it and one requester's private prompt stays out of
another's; its modification time is the last request of one of the conversation's own turns.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from contextlib import suppress
from dataclasses import asdict, replace
from difflib import SequenceMatcher
from functools import partial
from typing import TYPE_CHECKING, Any

from mindroom.constants import PROMPT_CACHE_IDLE_SECONDS
from mindroom.file_locks import advisory_file_lock
from mindroom.llm.messages import (
    PROMPT_UPDATE_KEY,
    REWRITTEN_ATTEMPT_KEY,
    Message,
    is_queued_notice_message,
    stable_serialize,
)
from mindroom.llm.provider import TurnCompleted
from mindroom.llm.request_identity import REQUEST_IDENTITY_KEY, message_digest, request_prefixes
from mindroom.providers.pipeline.wrapped import RequestProjection, WrappedProvider
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.yaml_io import write_text_atomic

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from mindroom.llm.provider import ModelProvider, ModelRequest, StreamEvent

__all__ = ["held_request", "prompt_hold_path", "prompt_hold_stream"]

# What a conversation's file holds; and the messages a request sends with what the file holds once it sent them.
type _State = dict[str, Any]
type _Plan = tuple[Sequence[Message], _State]

# A hold no request continues any more is deleted once it has been idle this long.
_PRUNE_IDLE_SECONDS = 24 * 3600
# A longer update reads better as a new prompt, and so do more updates: every request sends each of them again.
_MAX_UPDATE_CHARS = 8000
_MAX_UPDATES = 8
# The unchanged lines an update quotes around each change.
_CONTEXT_LINES = 2
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
    """Return the update that tells a model sent the ``held`` text that ``fresh`` is in force, or None when none may.

    It states the whole difference, so the newest update alone says what is in force: a unified diff without line
    numbers, whose hunks quote each change with the lines around it and are split by a bare ``@@`` line. Every
    hunk's old side stands exactly once in ``held``, so the update and ``held`` give ``fresh`` and no other text; a
    change that cannot be told so is not told.
    Nor is any change to a prompt that inlines a ``MEMORY.md``, whose every preloaded file follows a heading of its
    path: an update quotes lines and is sent again, so it would keep a memory the user asked to forget.
    """
    if held == fresh:
        return _UPDATE_WITHDRAWN
    old, new = held.split("\n"), fresh.split("\n")
    if any(line.startswith("### ") and line.lower().endswith("/memory.md") for line in (*old, *new)):
        return None
    body: list[str] = []
    for group in SequenceMatcher(None, old, new, autojunk=False).get_grouped_opcodes(_CONTEXT_LINES):
        before = old[group[0][1] : group[-1][2]]
        if sum(old[start : start + len(before)] == before for start in range(len(old) - len(before) + 1)) != 1:
            return None
        body += ["@@"] if body else []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                body += [f" {line}" for line in old[i1:i2]]
            else:
                body += [f"-{line}" for line in old[i1:i2]] + [f"+{line}" for line in new[j1:j2]]
    fence = "`" * max([3, *(len(run) + 1 for line in body for run in re.findall(r"`+", line))])
    text = "\n".join([_UPDATE_HEADER, "Change the system prompt as this diff shows:", f"{fence}diff", *body, fence])
    return text if len(text) <= _MAX_UPDATE_CHARS else None


def _anchors(messages: Sequence[Message], lengths: Sequence[int]) -> list[str | None]:
    """Return, for each of the ascending ``lengths``, the digest of what the first that many ``messages`` send.

    None for a length they do not reach, or one whose messages hold content unknown here (``message_digest``). A
    queued-message notice counts by its text, which the request sends, though the identity of a request leaves it out.
    """
    digest = hashlib.sha256()
    anchors: list[str | None] = []
    done = 0
    for length in lengths:
        while done < min(length, len(messages)):
            message = messages[done]
            piece = message_digest(
                replace(message, provider_data=None) if is_queued_notice_message(message) else message,
            )
            if piece is None:
                break
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


def _read(path: Path, now: float) -> _State | None:
    """Return the state ``path`` holds, or None for a missing or unreadable file or one idle past the cache lifetime."""
    with suppress(OSError, ValueError):
        if now - path.stat().st_mtime < PROMPT_CACHE_IDLE_SECONDS:
            state = json.loads(path.read_text(encoding="utf-8"))
            return state if isinstance(state, dict) else None
    return None


def _continued(state: _State, route: object, system: Sequence[Message], rest: Sequence[Message]) -> _Plan | None:
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
        # It is told after the newest user message, which no earlier update follows, or not at all.
        text = _update_text(held[0], fresh[0]) if len(updates) < _MAX_UPDATES else None
        anchor = _anchors(rest, [len(rest)])[0]
        if text is None or anchor is None or not _may_stand(rest, len(rest)) or len(rest) <= max(lengths, default=0):
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


def _plan(inner: ModelProvider, request: ModelRequest, state: _State | None) -> _Plan | None:
    """Return the plan of ``request`` for a conversation whose file holds ``state``, None to send it as it was built.

    That is a request that holds no prompt: one to a route that takes no update, one that turns its prompt cache off,
    one without a leading system text and a message after it, or a read-only one that does not continue the hold.
    """
    messages = request.messages
    count = next((index for index, message in enumerate(messages) if message.role != "system"), len(messages))
    system, rest = messages[:count], messages[count:]
    texts = [message.content for message in system]
    holds = inner.capabilities.system_updates and request.overrides.prompt_cache
    if not (holds and texts and rest and all(isinstance(text, str) for text in texts)):
        return None
    # What the wire sends of the tools: a deferred one is left out, or loaded by a search.
    tools = [*map(asdict, request.tools), sorted(request.deferred_tools.names), request.deferred_tools.portable]
    route = [inner.identity.id, inner.identity.endpoint, hashlib.sha256(stable_serialize(tools).encode()).hexdigest()]
    if state is not None:
        with suppress(KeyError, TypeError, ValueError, IndexError):
            continued = _continued(state, route, system, rest)
            # A read-only request continues the hold only as it is, without an update it would have to add.
            if continued is not None and (continued[1] is state or request.prompt_hold_pin is None):
                return continued
    if request.prompt_hold_pin is not None:
        return None
    first = _anchors(rest, [1])[0]
    return messages, {"route": route, "system": texts, "current": texts[0], "first": first, "updates": []}


def _identities(inner: ModelProvider, request: ModelRequest, sent: ModelRequest) -> list[str | None]:
    """Return the identity of what precedes each message of ``request`` when ``inner`` is sent it as ``sent``."""
    as_sent = WrappedProvider(inner, prompt_hold_stream, RequestProjection(lambda _inner, _request: sent))
    return request_prefixes(as_sent, request)


def held_request(inner: ModelProvider, request: ModelRequest, *, pins: bool = False) -> ModelRequest:
    """Return ``request`` as ``inner`` is sent it: with the held system prompt and updates, which a sent run ``pins``."""
    pin = request.prompt_hold_pin
    if request.prompt_hold is None:
        return request
    state = pin[0] if pin else _read(request.prompt_hold, time.time())
    plan = _plan(inner, request, state)
    sent = replace(request, messages=request.messages if plan is None else plan[0], prompt_hold=None)
    if pin is not None and not pin:
        # The run it continues may have been sent another prompt than the file holds: only its newest answer tells.
        newest = max((at for at, message in enumerate(request.messages) if message.role == "assistant"), default=-1)
        recorded = (request.messages[newest].provider_data or {}).get(REQUEST_IDENTITY_KEY) if plan else None
        if recorded is None or newest < 0 or recorded != _identities(inner, request, sent)[newest]:
            state, sent = None, replace(request, prompt_hold=None)
        if pins:
            pin.append(state)
    return sent


def _commit(inner: ModelProvider, request: ModelRequest) -> ModelRequest:
    """Return ``request`` as it is sent, storing the hold it continues or starts; requests of one agent take turns."""
    now = time.time()
    path = request.prompt_hold
    assert path is not None
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
            else:
                os.utime(path, (now, now))
    except OSError:
        return sent
    return replace(sent, messages=plan[0])


async def prompt_hold_stream(inner: ModelProvider, request: ModelRequest) -> AsyncIterator[StreamEvent]:
    """Stream ``request`` with its conversation's held system prompt, which only a request that may write it records."""
    sent = request
    if request.prompt_hold is not None:
        send = _commit if request.prompt_hold_pin is None else partial(held_request, pins=True)
        sent = await asyncio.to_thread(send, inner, request)
    events = inner.stream(sent)
    async with closing_async_stream(events):
        async for event in events:
            if sent is not request and isinstance(event, TurnCompleted):
                # The identity of the request as it was sent, which reading the hold again would not surely give.
                identity = (await asyncio.to_thread(_identities, inner, request, sent))[-1]
                answered = event.message.provider_data or {}
                if identity is not None and not answered.get(REWRITTEN_ATTEMPT_KEY):
                    event.message.provider_data = {**answered, REQUEST_IDENTITY_KEY: identity}
            yield event
