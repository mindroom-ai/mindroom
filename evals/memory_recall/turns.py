"""Extract the turns that carried an automatic memory recall from `debug.log_llm_requests` logs.

A turn's tool loop logs one request per model call, each repeating the turn's memory block, so a turn is kept once:
the first request carrying a given block in a given session.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from mindroom.token_counting.families import load_o200k

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Iterator, Sequence
    from pathlib import Path

_BLOCK_MARKER = "Automatically extracted"
_CURRENT_MESSAGE = "Current message:"
_TRANSIENT_OPEN = "<mindroom_transient_context>"
_TRANSIENT_CLOSE = "</mindroom_transient_context>"
_MESSAGE_CONTEXT = "<mindroom_message_context>"
# The line MEMORY_CONTEXT_PROMPT_TEMPLATE puts before the hits, for any memory label ("agent file", "agent", ...).
_HITS_HEADER = re.compile(r"^Previous .+ memories that might be related:\n", re.MULTILINE)


@dataclass(frozen=True)
class Turn:
    """One user turn that received an automatic memory recall."""

    ts: datetime
    agent: str
    session_id: str
    query: str
    injected_hits: tuple[str, ...]
    injected_tokens: int


def extract_turns(log_paths: Sequence[Path], agents: Collection[str]) -> list[Turn]:
    """Return the distinct recall turns of `agents` in `log_paths`, in log order."""
    seen: set[tuple[str, str]] = set()
    turns: list[Turn] = []
    for record in _records(log_paths, agents):
        messages = _messages(record)
        block_index = _block_index(messages)
        if block_index is None:
            continue
        block = _text(messages[block_index].get("content"))
        session_id = record.get("session_id") or record.get("thread_id") or ""
        if (session_id, block) in seen:
            continue
        seen.add((session_id, block))
        memory_start = max(block.find(_TRANSIENT_OPEN), 0)
        memory = _memory_part(block[memory_start:])
        turns.append(
            Turn(
                ts=datetime.fromisoformat(record["timestamp"]),
                agent=record["agent_id"],
                session_id=session_id,
                query=_query(record, messages, block_index, block[:memory_start]),
                injected_hits=_hits(memory),
                injected_tokens=len(load_o200k().encode_ordinary(memory)),
            ),
        )
    return turns


def write_turns(turns: Iterable[Turn], path: Path) -> None:
    """Write `turns` to `path` as JSONL."""
    with path.open("w") as out:
        for turn in turns:
            row = asdict(turn)
            row["ts"] = turn.ts.isoformat()
            out.write(json.dumps(row) + "\n")


def read_turns(path: Path) -> list[Turn]:
    """Read the turns `write_turns` wrote to `path`."""
    turns = []
    with path.open() as rows:
        for line in rows:
            row = json.loads(line)
            row["ts"] = datetime.fromisoformat(row["ts"])
            row["injected_hits"] = tuple(row["injected_hits"])
            turns.append(Turn(**row))
    return turns


def _records(log_paths: Sequence[Path], agents: Collection[str]) -> Iterator[dict[str, Any]]:
    """Yield the logged requests of `agents`; `grep -F` drops other agents' lines first, as logs reach gigabytes a day."""
    if not agents:
        return
    patterns = [arg for agent in agents for arg in ("-e", f'"agent_id": "{agent}"')]
    env = {**os.environ, "LC_ALL": "C"}
    for path in log_paths:
        with subprocess.Popen(["grep", "-F", *patterns, str(path)], stdout=subprocess.PIPE, env=env) as grep:
            assert grep.stdout is not None
            for line in grep.stdout:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and record.get("agent_id") in agents:
                    yield record
        # grep exits 1 when nothing matched; 2 is an error such as a missing file.
        if grep.returncode > 1:
            raise subprocess.CalledProcessError(grep.returncode, grep.args)


def _messages(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the request's messages: `body.messages` in the current format, top-level `messages` in the old one."""
    body = record.get("body")
    messages = body.get("messages") if isinstance(body, dict) else record.get("messages")
    return [message for message in messages or [] if isinstance(message, dict)]


def _block_index(messages: list[dict[str, Any]]) -> int | None:
    """Return the index of the last user message carrying the memory block."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") == "user" and _BLOCK_MARKER in _text(message.get("content")):
            return index
    return None


def _text(content: object) -> str:
    """Return a message's text: the string itself, or its `text` parts joined (tool results are not text parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _memory_part(block: str) -> str:
    """Cut the memory block before the message context, or before its closing tag when it has none."""
    for end_marker in (_MESSAGE_CONTEXT, _TRANSIENT_CLOSE):
        end = block.find(end_marker)
        if end != -1:
            return block[:end]
    return block


def _hits(memory: str) -> tuple[str, ...]:
    """Split the memory part into its `- ` list items, each without the bullet."""
    header = _HITS_HEADER.search(memory)
    listing = memory[header.end() :] if header else memory
    return tuple(hit.strip() for hit in re.split(r"\n- ", "\n" + listing.strip()) if hit.strip())


def _query(record: dict[str, Any], messages: list[dict[str, Any]], block_index: int, block_preface: str) -> str:
    """Return the user's current message: the text after `Current message:`, else the logged turn prompt.

    That line sits in the block's own message before the block, or in the user message just before it.
    """
    user_text = block_preface
    if not user_text.strip():
        for message in reversed(messages[:block_index]):
            if message.get("role") == "assistant":
                break
            if message.get("role") == "user":
                user_text = _text(message.get("content"))
                break
    if _CURRENT_MESSAGE in user_text:
        return user_text.split(_CURRENT_MESSAGE, 1)[1].strip()
    return (record.get("current_turn_prompt") or "").strip()
