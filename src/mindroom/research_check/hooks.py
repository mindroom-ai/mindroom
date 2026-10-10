"""Judge each finished reply with its tool calls, and have the agent verify specific claims no lookup supported."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from mindroom.config.judgment import JudgmentConfig, LLMJudgmentConfig
from mindroom.hooks import EVENT_MESSAGE_AFTER_RESPONSE, AfterResponseContext, hook
from mindroom.judgment.evaluator import create_judgment_evaluator
from mindroom.judgment.state import JudgmentMessage, JudgmentQuestion, build_judgment_request
from mindroom.redaction import redact_sensitive_text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.tool_system.events import ToolTraceEntry

_MAX_TOOL_ENTRIES = 30
_MAX_PREVIEW_CHARS = 300
_CHECKED_RESPONSE_KINDS = ("ai", "team")

_RESEARCH_CHECK_QUESTION = JudgmentQuestion(
    id="research_check",
    instructions=(
        "Decide whether the assistant's final reply needs a follow-up that verifies it. "
        "The assistant message that lists tool calls shows every lookup made for this reply."
    ),
    when_true=(
        "The reply recommends or asserts specific real-world things a person could act on, such as named places, "
        "businesses, products, events, prices, opening hours, availability, or other current facts, "
        "and the listed tool calls did not look them up or their results do not support them."
    ),
    when_false=(
        "The reply makes no such specific claims, the listed tool calls looked up and support its specific claims, "
        "the user supplied the facts, or the claims are stable common knowledge."
    ),
)

_FOLLOW_UP = (
    "Research check: your previous reply recommends or states specific things that no lookup for that reply verified. "
    "Check each one now with your search or browsing tools, correct or withdraw anything that does not hold up, "
    "and name the sources you checked. If you cannot look them up, say which ones remain unverified."
)


class ResearchCheckSettings(BaseModel):
    """The plugin's `settings` in config.yaml."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    judgment: JudgmentConfig
    instructions: str = ""
    agents: tuple[str, ...] | None = None


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_PREVIEW_CHARS else f"{text[:_MAX_PREVIEW_CHARS]}…"


def _tool_line(entry: ToolTraceEntry) -> str:
    line = f"- {entry.tool_name}"
    if entry.args_preview:
        line += f" with {_clip(entry.args_preview)}"
    if entry.result_preview:
        line += f"; result: {_clip(entry.result_preview)}"
    # Tool arguments are not essential evidence, so redact them instead of refusing the whole request.
    return redact_sensitive_text(line)


def _research_check_messages(
    body: str,
    tool_trace: Sequence[ToolTraceEntry],
    reply: str,
) -> tuple[JudgmentMessage, ...]:
    """Show the judge the request, every lookup made for the reply, and the reply itself."""
    lines = [_tool_line(entry) for entry in tool_trace[:_MAX_TOOL_ENTRIES]]
    if len(tool_trace) > _MAX_TOOL_ENTRIES:
        lines.append(f"- and {len(tool_trace) - _MAX_TOOL_ENTRIES} more")
    tools = "\n".join(["Tool calls made for this reply:", *lines]) if lines else "Tool calls made for this reply: none"
    return (
        JudgmentMessage("user", body),
        JudgmentMessage("assistant", tools),
        JudgmentMessage("assistant", reply),
    )


@hook(EVENT_MESSAGE_AFTER_RESPONSE, timeout_ms=35_000)
async def check_research(ctx: AfterResponseContext) -> None:
    """Send one verification follow-up when the judge finds unresearched claims in a reply to a person."""
    result = ctx.result
    envelope = result.envelope
    # Hook-sourced turns include this plugin's own follow-ups, so each person's turn is checked at most once.
    if result.response_kind not in _CHECKED_RESPONSE_KINDS or envelope.hook_source is not None:
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
        f"@{envelope.agent_name} {_FOLLOW_UP}",
        thread_id=envelope.target.resolved_thread_id,
        trigger_dispatch=True,
    )
