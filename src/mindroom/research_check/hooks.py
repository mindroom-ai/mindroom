"""Judge each finished reply with its tool calls, and have the agent verify specific claims no lookup supported."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from mindroom.agents import show_tool_calls_for_agent
from mindroom.config.judgment import JudgmentConfig, LLMJudgmentConfig
from mindroom.hooks import EVENT_MESSAGE_AFTER_RESPONSE, AfterResponseContext, hook
from mindroom.judgment.evaluator import create_judgment_evaluator
from mindroom.judgment.state import JudgmentMessage, JudgmentQuestion, build_judgment_request
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.events import is_visible_tool_marker_line

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.tool_system.events import ToolTraceEntry

# MindRoom already caps tool result previews at 500 characters; keep all of it so the judge sees every hit.
_MAX_PREVIEW_CHARS = 500
# Bytes, like the judgment request limit; leaves room for the person's message and a long reply.
_MAX_TOOL_BYTES = 6_000

_RESEARCH_CHECK_QUESTION = JudgmentQuestion(
    id="research_check",
    instructions=(
        "Decide whether the assistant's final reply needs a follow-up that verifies it. "
        "The assistant message that lists tool calls shows every lookup made for this reply."
    ),
    when_true=(
        "The reply states checkable facts a person would act on about specific real-world places, businesses, products, "
        "or events, such as that they exist, where they are, when they are open or happening, what they cost, or whether "
        "they are available, and the listed tool calls did not look those facts up or their results do not support them."
    ),
    when_false=(
        "The reply makes no such factual claims, the listed tool results support them, the person supplied them, they are "
        "stable common knowledge, or the only unsupported parts are opinions, descriptions of quality, or general suggestions."
    ),
)

_FOLLOW_UP = (
    'Research check on your reply that starts "{opening}": it recommends or states specific things that no lookup '
    "for that reply verified. Check each one now with your search or browsing tools, correct or withdraw anything "
    "that does not hold up, and name the sources you checked. If you cannot look them up, say which ones remain "
    "unverified."
)
# Enough of the reply to name it when newer replies follow it in the conversation.
_OPENING_CHARS = 80


class ResearchCheckSettings(BaseModel):
    """The plugin's `settings` in config.yaml."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    judgment: JudgmentConfig
    instructions: str = ""
    agents: tuple[str, ...] | None = None


def _clip(text: str, limit: int = _MAX_PREVIEW_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit]}…"


def _tool_line(entry: ToolTraceEntry) -> str:
    line = f"- {entry.tool_name}"
    if entry.args_preview:
        line += f" with {_clip(entry.args_preview)}"
    if entry.result_preview:
        line += f"; result: {_clip(entry.result_preview)}"
    return line


def _research_check_messages(
    body: str,
    tool_trace: Sequence[ToolTraceEntry],
    reply: str,
) -> tuple[JudgmentMessage, ...]:
    """Show the judge the request, every lookup made for the reply, and the reply itself."""
    lines: list[str] = []
    budget = _MAX_TOOL_BYTES
    for index, entry in enumerate(tool_trace):
        line = _tool_line(entry)
        budget -= len(line.encode()) + 1
        if budget < 0:
            lines.append(
                f"- and {len(tool_trace) - index} more tool calls not shown here, which may support claims the calls above do not",
            )
            break
        lines.append(line)
    tools = "\n".join(["Tool calls made for this reply:", *lines]) if lines else "Tool calls made for this reply: none"
    # Tool previews are not essential evidence, so redact them instead of refusing the whole request; the joined
    # text is redacted once because some credential patterns span a line break.
    tools = redact_sensitive_text(tools)
    return (
        JudgmentMessage("user", body),
        JudgmentMessage("assistant", tools),
        JudgmentMessage("assistant", reply),
    )


def _reply_opening(reply: str) -> str:
    # Without "@", a mention in the quote cannot tag another agent or person in the follow-up.
    text = " ".join(line for line in reply.splitlines() if not is_visible_tool_marker_line(line)).replace("@", "")
    return _clip(text, _OPENING_CHARS)


@hook(EVENT_MESSAGE_AFTER_RESPONSE, timeout_ms=35_000)
async def check_research(ctx: AfterResponseContext) -> None:
    """Send one verification follow-up when the judge finds unresearched claims in a reply to a person."""
    result = ctx.result
    envelope = result.envelope
    # Only an agent's reply to a person's own request: never this plugin's follow-ups, other agents, automations,
    # schedules, or webhooks. Team replies and hidden tool calls carry no tool trace, so the judge would see no lookups.
    if (
        result.response_kind != "ai"
        or not envelope.origin.may_answer_interactive_prompt
        or not show_tool_calls_for_agent(ctx.config, envelope.agent_name)
    ):
        return
    settings = ResearchCheckSettings.model_validate(ctx.settings)
    if settings.agents is not None and envelope.agent_name not in settings.agents:
        return
    judgment = settings.judgment
    if isinstance(judgment, LLMJudgmentConfig) and judgment.model not in ctx.config.models:
        msg = f"Unknown research_check judgment model: {judgment.model!r}"
        raise ValueError(msg)
    evaluate = create_judgment_evaluator(
        judgment,
        ctx.config,
        ctx.runtime_paths,
        owner=f"{ctx.runtime_paths.storage_root}:{envelope.agent_name}",
        question_id=_RESEARCH_CHECK_QUESTION.id,
    )
    if evaluate is None:
        return
    request = build_judgment_request(
        _RESEARCH_CHECK_QUESTION,
        _research_check_messages(envelope.body, result.tool_trace, result.response_text),
        instructions=settings.instructions,
    )
    if (await evaluate(request)).decision is not True:
        return
    await ctx.send_message(
        envelope.room_id,
        f"@{envelope.agent_name} {_FOLLOW_UP.format(opening=_reply_opening(result.response_text))}",
        thread_id=envelope.target.resolved_thread_id,
        trigger_dispatch=True,
    )
