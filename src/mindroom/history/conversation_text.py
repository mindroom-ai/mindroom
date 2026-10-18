"""What people and agents said in stored turns, for background model calls.

Skill reviews, the knowledge graph, and file-memory auto-flush each send stored turns to a model. This module owns the
decisions they share: which turns and messages are conversation and who said each message, so a prompt MindRoom wrote
is never read as someone's words. Each reader renders and redacts the text itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mindroom.automations.steps import is_automation_hook_source
from mindroom.constants import HOOK_SOURCE_RUN_METADATA_KEY, TURN_INTENT_RUN_METADATA_KEY
from mindroom.history.interrupted_replay import is_interrupted_replay_run
from mindroom.history.replay import is_compaction_summary
from mindroom.history.run_visibility import is_model_history_visible_run, is_queued_notice_message
from mindroom.hooks.enrichment import strip_enrichment_blocks
from mindroom.prompt_message_tags import parse_msg_tags
from mindroom.turn_origin import TurnIntent

if TYPE_CHECKING:
    from agno.models.message import Message
    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput


@dataclass(frozen=True)
class Utterance:
    """One message of a stored turn: who said it and what, before redaction or clipping."""

    # `said` is a message in a prompt, `reply` the run's agent, `runtime` a prompt MindRoom wrote, `tool_result` a tool's.
    kind: Literal["said", "reply", "runtime", "tool_result"]
    # The Matrix ID behind a `said` message; None for the agent, MindRoom, or a tool.
    sender: str | None
    # Send this only through redaction.
    unredacted: str
    # The stored message, for its tool calls and tool name.
    message: Message


def one_line(text: str) -> str:
    """Join a text's lines and spaces into single spaces, so a line break never starts another speaker's line."""
    return " ".join(text.split())


def run_requester(run: RunOutput | TeamRunOutput) -> str | None:
    """Return the person a stored turn answered."""
    requester = (run.metadata or {}).get("requester_id") or run.user_id
    return requester if isinstance(requester, str) else None


def _prompt_utterances(message: Message, content: str, requester: str, *, mindroom_wrote: bool) -> list[Utterance]:
    """Split a user message into its tagged messages, leaving out empty ones."""
    found: list[Utterance] = []
    for sender, body in parse_msg_tags(content, default_sender=requester):
        if mindroom_wrote:
            utterance = Utterance("runtime", None, body, message)
        else:
            # Context a hook attached to the message is MindRoom's, not the sender's words.
            utterance = Utterance("said", sender, strip_enrichment_blocks(body), message)
        if utterance.unredacted.strip():
            found.append(utterance)
    return found


def utterances(run: RunOutput | TeamRunOutput, *, agent_work: bool = False) -> list[Utterance]:
    """Return what was said in one stored turn, oldest first.

    Only turns the agent's own history shows count, never copies of earlier turns. A prompt MindRoom wrote is nobody's
    words: an automation's turn is left out entirely, a background job follow-up keeps only its reply, and a run's own
    compaction summary is left out. A stopped turn's answer is a rendered summary that can quote tool previews, so it is
    left out too. ``agent_work`` keeps all of these, plus tool calls and results, for a reviewer of how the agent worked.
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
        mindroom_wrote = runtime_prompt or is_queued_notice_message(message) or is_compaction_summary(message)
        if message.role == "user" and (agent_work or not mindroom_wrote):
            said.extend(_prompt_utterances(message, content, requester, mindroom_wrote=mindroom_wrote))
        elif message.role == "assistant" and keeps_reply and (content.strip() or (agent_work and message.tool_calls)):
            said.append(Utterance("reply", None, content, message))
        elif message.role == "tool" and agent_work:
            said.append(Utterance("tool_result", None, message.get_content_string(), message))
    return said
