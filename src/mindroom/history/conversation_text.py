"""What people and agents said in stored turns, as text a background model call reads.

Memory extraction, skill reviews, and the knowledge graph each send a stored conversation to a model. This module owns
the decisions they share: which turns and messages are conversation, who said each message, and how text is clipped and
redacted, so a prompt MindRoom wrote is never read as someone's words and every reader redacts the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mindroom.automations.steps import is_automation_hook_source
from mindroom.constants import HOOK_SOURCE_RUN_METADATA_KEY, TURN_INTENT_RUN_METADATA_KEY
from mindroom.history.interrupted_replay import is_interrupted_replay_run
from mindroom.history.run_visibility import is_model_history_visible_run
from mindroom.prompt_message_tags import parse_msg_tags
from mindroom.redaction import REDACTION_FAILED, redact_sensitive_text
from mindroom.turn_origin import TurnIntent

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.models.message import Message
    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput

# The marker redaction itself shortens text with.
_TRUNCATED = "... [truncated]"


@dataclass(frozen=True)
class Utterance:
    """One message of a stored turn: who said it and what, before redaction or clipping."""

    # `said` is a message in a prompt, `reply` the run's agent, `runtime` a prompt MindRoom wrote, `tool_result` a tool's.
    kind: Literal["said", "reply", "runtime", "tool_result"]
    # The Matrix ID behind a `said` message; None for the agent, MindRoom, or a tool.
    sender: str | None
    # Send this only through `redact_conversation_text`.
    unredacted: str
    # The stored message, for its tool calls and tool name.
    message: Message


def redact_conversation_text(text: str, *, max_chars: int | None = None) -> str:
    """Return ``text`` with credential-like values removed, best effort, in at most ``max_chars`` characters.

    The whole text is redacted at once, so a label and its value on separate lines are still matched, and with
    ``max_chars`` only a little more than what is kept is read. When that refuses an ambiguous line, only the lines
    around it are dropped instead of the whole text: a line that cannot be redacted on its own, with the rest of a
    quoted value it opens, and a line holding the value of a secret-named key that ends the line before it.
    """
    redacted = redact_sensitive_text(text, max_length=max_chars)
    if redacted != REDACTION_FAILED:
        return redacted
    kept = _redact_line_by_line(text if max_chars is None else text[:max_chars])
    if max_chars is None or (len(text) <= max_chars and len(kept) <= max_chars):
        return kept
    return f"{kept[: max_chars - len(_TRUNCATED)]}{_TRUNCATED}"


def _redact_line_by_line(text: str) -> str:
    lines = text.splitlines()
    kept: list[str] = []
    # The line before, read together with each line, so a label still masks a value on the next line.
    previous = ""
    hidden_through = -1
    for index, line in enumerate(lines):
        if index <= hidden_through:
            continue
        paired = redact_sensitive_text(f"{previous}\n{line}")
        if paired != REDACTION_FAILED:
            kept.append(paired.partition("\n")[2])
            previous = line
        elif redact_sensitive_text(line) == REDACTION_FAILED:
            # A quote this line leaves open may close on a later line, and everything up to there is its value.
            if _has_quote(line):
                hidden_through = next(
                    (later for later in range(index + 1, len(lines)) if _has_quote(lines[later])),
                    index,
                )
            previous = ""
        else:
            # The line is the value of a key the line before left open; it still labels the line after it.
            previous = line
    return "\n".join(kept)


def _has_quote(line: str) -> bool:
    return '"' in line or "'" in line


def one_line(text: str) -> str:
    """Join a text's lines and spaces into single spaces, so a line break never starts another speaker's line."""
    return " ".join(text.split())


def run_requester(run: RunOutput | TeamRunOutput) -> str | None:
    """Return the person a stored turn answered."""
    requester = (run.metadata or {}).get("requester_id") or run.user_id
    return requester if isinstance(requester, str) else None


def utterances(run: RunOutput | TeamRunOutput, *, agent_work: bool = False) -> list[Utterance]:
    """Return what was said in one stored turn, oldest first.

    Only turns the agent's own history shows count, never copies of earlier turns. A prompt MindRoom wrote is nobody's
    words: an automation's turn is left out entirely, and a background job follow-up keeps only its reply. A stopped
    turn's answer is a rendered summary that can quote tool previews, so it is left out too. ``agent_work`` keeps all of
    these, plus tool calls and results, for a reviewer of how the agent worked.
    """
    if not is_model_history_visible_run(run):
        return []
    metadata = run.metadata or {}
    automation = is_automation_hook_source(metadata.get(HOOK_SOURCE_RUN_METADATA_KEY))
    if automation and not agent_work:
        return []
    runtime_prompt = automation or metadata.get(TURN_INTENT_RUN_METADATA_KEY) == TurnIntent.JOB_FOLLOW_UP
    keeps_reply = agent_work or not is_interrupted_replay_run(run)
    requester = run_requester(run) or "unknown"
    said: list[Utterance] = []
    for message in run.messages or []:
        content = message.content if isinstance(message.content, str) else ""
        if message.from_history:
            continue
        if message.role == "user" and runtime_prompt:
            if agent_work and content.strip():
                said.append(Utterance("runtime", None, content, message))
        elif message.role == "user":
            said.extend(
                Utterance("said", sender, body, message)
                for sender, body in parse_msg_tags(content, default_sender=requester)
                if body.strip()
            )
        elif message.role == "assistant" and keeps_reply and (content.strip() or (agent_work and message.tool_calls)):
            said.append(Utterance("reply", None, content, message))
        elif message.role == "tool" and agent_work:
            said.append(Utterance("tool_result", None, message.get_content_string(), message))
    return said


def conversation_lines(
    run: RunOutput | TeamRunOutput,
    *,
    agent_name: str,
    agent_for_sender: Callable[[str], str | None],
    max_line_chars: int,
) -> list[str]:
    """Return one ``<speaker>: <text>`` line per message of a stored turn, each text redacted to ``max_line_chars``.

    ``agent_for_sender`` names the configured agent behind a Matrix ID, so agents' messages are not labeled as people's.
    """
    return [
        f"{_speaker(utterance.sender, agent_name, agent_for_sender)}: {text}"
        for utterance in utterances(run)
        if (text := one_line(redact_conversation_text(utterance.unredacted.strip(), max_chars=max_line_chars)))
    ]


def _speaker(sender: str | None, agent_name: str, agent_for_sender: Callable[[str], str | None]) -> str:
    """Label a sender as an agent or a person with no colon, since Graphiti reads a speaker up to the first colon."""
    if sender is None:
        return f"agent {agent_name}"
    if (agent := agent_for_sender(sender)) is not None:
        return f"agent {agent}"
    user, separator, server = sender.partition(":")
    return f"person {user} ({server})" if separator else f"person {sender}"
