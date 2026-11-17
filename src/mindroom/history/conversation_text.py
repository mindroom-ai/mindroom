"""What people and agents said in stored turns, for background model calls and compaction hooks.

Skill reviews, the knowledge graph, and file-memory auto-flush each send stored turns to a model, and compaction hands
the turns it folds to plugins. This module owns the decisions they share: which turns and messages are conversation and
who said each message, so a prompt MindRoom wrote is never read as someone's words. Each reader renders and redacts the
text itself. A turn's own preparation marks the notes it adds to a thread's context (``thread_context_note``) and
the agent's own thread messages its input carries (``own_thread_message``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from mindroom.agent_loop.tools import is_media_follow_up
from mindroom.automations.steps import is_automation_hook_source
from mindroom.constants import HOOK_SOURCE_RUN_METADATA_KEY, TURN_INTENT_RUN_METADATA_KEY
from mindroom.history.interrupted_replay import is_interrupted_replay_run
from mindroom.history.run_visibility import is_model_history_visible_run
from mindroom.history.summary_message import is_compaction_summary
from mindroom.hooks.enrichment import strip_enrichment_blocks
from mindroom.llm.messages import Message, is_queued_notice_message
from mindroom.prompt_message_tags import parse_msg_tags
from mindroom.turn_origin import TurnIntent

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.llm.runs import AnyRunRecord

# The ``provider_data`` key marking a note MindRoom adds to a thread's context: that earlier messages were omitted, that
# the agent's previous reply is still being delivered, or that reply's unfinished text. The note is stored with its turn.
_THREAD_CONTEXT_NOTE_KEY = "mindroom_thread_context_note"
# The ``provider_data`` key marking a thread message the agent itself sent, which a turn's input carries because its
# scope stores no run that holds it.
_OWN_THREAD_MESSAGE_KEY = "mindroom_own_thread_message"


@dataclass(frozen=True)
class Utterance:
    """One message of a stored turn: who said it and what, before redaction or clipping."""

    # `said` is a message in a prompt, `reply` the run's agent, `runtime` a prompt MindRoom wrote, `tool_result` a tool's.
    # A `reply` names who spoke, not which model answered: an earlier message of the agent that the turn's input carried
    # (``own_thread_message``), which the turn stores because its scope had not, is a `reply` too.
    kind: Literal["said", "reply", "runtime", "tool_result"]
    # The Matrix ID behind a `said` message; None for the agent, MindRoom, or a tool.
    sender: str | None
    # The display name a `said` message carried, as the sender had it then; None when it carried none.
    display_name: str | None
    # Send this only through redaction.
    unredacted: str
    # The stored message, for its tool calls, its tool name, and whether its tool call failed.
    message: Message


def thread_context_note(content: str) -> Message:
    """Return a user message holding a note MindRoom adds to a thread's context, marked so no reader quotes it."""
    return Message(role="user", content=content, provider_data={_THREAD_CONTEXT_NOTE_KEY: True})


def _is_thread_context_note(message: Message) -> bool:
    return (message.provider_data or {}).get(_THREAD_CONTEXT_NOTE_KEY) is True


def own_thread_message(content: str, provider_data: Mapping[str, object] | None = None) -> Message:
    """Return a user message holding ``content``, the ``<msg>`` tag of a thread message the agent itself sent.

    The model reads its own earlier message as a thread message marked ``sent_by_you``, never as an assistant turn:
    providers merge consecutive assistant turns, so one would read as the end of the answer before it, and the model
    then ends its answers the same way. A reader of stored turns still hears the agent (``spoken_message``).
    """
    return Message(role="user", content=content, provider_data={**(provider_data or {}), _OWN_THREAD_MESSAGE_KEY: True})


def is_own_thread_message(message: Message) -> bool:
    """Return whether ``own_thread_message`` built ``message``, which is then no prompt of its turn."""
    return (message.provider_data or {}).get(_OWN_THREAD_MESSAGE_KEY) is True and isinstance(message.content, str)


def own_thread_message_text(message: Message) -> str | None:
    """Return what the agent said in a message ``own_thread_message`` built, or None for any other message."""
    if not is_own_thread_message(message):
        return None
    return "\n".join(body for _, _, body in parse_msg_tags(str(message.content), default_sender=""))


def spoken_message(message: Message) -> Message:
    """Return ``message`` as a reader of the conversation hears it: the agent's own thread message as its assistant text."""
    own = own_thread_message_text(message)
    return message if own is None else replace(message, role="assistant", content=own)


def one_line(text: str) -> str:
    """Join a text's lines and spaces into single spaces, so a line break never starts another speaker's line."""
    return " ".join(text.split())


def run_requester(run: AnyRunRecord) -> str | None:
    """Return the person a stored turn answered."""
    requester = (run.metadata or {}).get("requester_id") or run.user_id
    return requester if isinstance(requester, str) else None


def _prompt_utterances(message: Message, content: str, requester: str, *, mindroom_wrote: bool) -> list[Utterance]:
    """Split a user message into its tagged messages, leaving out empty ones."""
    found: list[Utterance] = []
    for sender, display_name, body in parse_msg_tags(content, default_sender=requester):
        if mindroom_wrote:
            utterance = Utterance("runtime", None, None, body, message)
        else:
            # Context a hook attached to the message is MindRoom's, not the sender's words.
            utterance = Utterance("said", sender, display_name, strip_enrichment_blocks(body), message)
        if utterance.unredacted.strip():
            found.append(utterance)
    return found


def utterances(run: AnyRunRecord, *, agent_work: bool = False) -> list[Utterance]:
    """Return what was said in one stored turn, oldest first.

    Only turns the agent's own history shows count. A prompt MindRoom wrote is nobody's words: an automation's turn is
    left out entirely, a background job follow-up keeps only its reply, and a run's own compaction summary and the notes
    a turn's preparation added to its thread context are left out.
    A stopped turn's answer, its last message, is a rendered summary that can quote tool calls with their inputs and
    outputs, so it is left out too; earlier replies its thread context holds stay. ``agent_work`` keeps all of these,
    plus tool calls and results, for a reviewer of how the agent worked. The turn context a run stores
    (``add_to_agent_memory=False``), such as memory results and the approval receipt, is never kept: it is neither
    what was said nor work the agent did.
    """
    if not is_model_history_visible_run(run):
        return []
    metadata = run.metadata or {}
    automation = is_automation_hook_source(metadata.get(HOOK_SOURCE_RUN_METADATA_KEY))
    if automation and not agent_work:
        return []
    runtime_prompt = automation or metadata.get(TURN_INTENT_RUN_METADATA_KEY) == TurnIntent.JOB_FOLLOW_UP
    rendered_answer = run.messages[-1] if run.messages and not agent_work and is_interrupted_replay_run(run) else None
    requester = run_requester(run) or "unknown"
    said: list[Utterance] = []
    for message in run.messages:
        if not message.add_to_agent_memory:
            continue
        content = message.content if isinstance(message.content, str) else ""
        if (own_text := own_thread_message_text(message)) is not None:
            if own_text.strip():
                said.append(Utterance("reply", None, None, own_text, message))
            continue
        mindroom_wrote = (
            runtime_prompt
            or is_queued_notice_message(message)
            or is_compaction_summary(message)
            or is_media_follow_up(message)
            or _is_thread_context_note(message)
        )
        if message.role == "user" and (agent_work or not mindroom_wrote):
            said.extend(_prompt_utterances(message, content, requester, mindroom_wrote=mindroom_wrote))
        elif (
            message.role == "assistant"
            and message is not rendered_answer
            and (content.strip() or (agent_work and message.tool_calls))
        ):
            said.append(Utterance("reply", None, None, content, message))
        elif message.role == "tool" and agent_work:
            said.append(Utterance("tool_result", None, None, message.get_content_string(), message))
    return said
