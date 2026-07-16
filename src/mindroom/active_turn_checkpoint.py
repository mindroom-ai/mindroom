"""Bound active provider turns at completed tool-call boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.model_usage import context_input_tokens_from_counts
from mindroom.redaction import redact_sensitive_text
from mindroom.token_budget import effective_input_budget, estimate_compaction_input_tokens, stable_serialize

if TYPE_CHECKING:
    from agno.models.base import Model
    from agno.models.message import Message

    from mindroom.tool_system.events import ToolTraceEntry

ACTIVE_TURN_CHECKPOINT_LIMIT = 4

_MAX_GOAL_CHARS = 6_000
_MAX_PARTIAL_TEXT_CHARS = 4_000
_MAX_TOOL_CONTEXT_CHARS = 16_000
_HOOK_MARKER = "_mindroom_active_turn_checkpoint_hook"


@dataclass(frozen=True)
class ActiveTurnCheckpointTrigger:
    """Context-budget evidence captured at one completed tool boundary."""

    estimated_input_tokens: int
    input_limit_tokens: int
    context_window_tokens: int
    used_actual_input_tokens: bool


@dataclass(frozen=True)
class ActiveTurnCheckpoint:
    """Compact model-facing state replacing one tool-heavy raw run."""

    content: str
    continuation_prompt: str
    trigger: ActiveTurnCheckpointTrigger


@dataclass(frozen=True)
class ActiveTurnCheckpointRecord:
    """Entity-specific persistence input for one checkpointed attempt."""

    session_id: str | None
    run_id: str | None
    checkpoint: ActiveTurnCheckpoint
    run_metadata: dict[str, Any] | None


@dataclass
class ActiveTurnContextGuard:
    """Request-local context guard observed by model events and tool results."""

    context_window_tokens: int
    reserve_tokens: int
    model_max_tokens: int | None
    prepared_context_tokens: int
    configured_provider: str | None = None
    model_id: str | None = None
    latest_actual_input_tokens: int | None = None
    latest_output_tokens: int = 0
    unaccounted_tool_tokens: int = 0
    trigger: ActiveTurnCheckpointTrigger | None = field(default=None, init=False)

    @property
    def input_limit_tokens(self) -> int:
        """Return effective provider-input ceiling after required output headroom."""
        return effective_input_budget(
            self.context_window_tokens,
            configured_reserve_tokens=self.reserve_tokens,
            model_max_tokens=self.model_max_tokens,
        )

    def observe_model_request(
        self,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        provider: str | None = None,
        model_id: str | None = None,
    ) -> None:
        """Record latest provider usage and reset already-accounted tool growth."""
        context_input_tokens = context_input_tokens_from_counts(
            input_tokens=input_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            provider=provider,
            configured_provider=self.configured_provider,
            model_id=model_id or self.model_id,
        )
        if context_input_tokens is not None and context_input_tokens > 0:
            self.latest_actual_input_tokens = context_input_tokens
            self.unaccounted_tool_tokens = 0
        self.latest_output_tokens = output_tokens if isinstance(output_tokens, int) and output_tokens > 0 else 0

    def checkpoint_after_tool_boundary(
        self,
        *,
        messages: list[Message],
        function_call_results: list[Message],
    ) -> bool:
        """Return whether a fully completed tool batch must end this provider run."""
        if self.trigger is not None:
            return True
        boundary_tokens = _estimate_completed_boundary_tokens(messages, function_call_results)
        self.unaccounted_tool_tokens += boundary_tokens
        used_actual = self.latest_actual_input_tokens is not None
        base_tokens = self.prepared_context_tokens
        if self.latest_actual_input_tokens is not None:
            base_tokens = self.latest_actual_input_tokens
        estimated_input_tokens = base_tokens + self.latest_output_tokens + self.unaccounted_tool_tokens
        if estimated_input_tokens < self.input_limit_tokens:
            return False
        self.trigger = ActiveTurnCheckpointTrigger(
            estimated_input_tokens=estimated_input_tokens,
            input_limit_tokens=self.input_limit_tokens,
            context_window_tokens=self.context_window_tokens,
            used_actual_input_tokens=used_actual,
        )
        return True


def build_active_turn_context_guard(
    *,
    context_window_tokens: int | None,
    reserve_tokens: int,
    model_max_tokens: int | None,
    prepared_context_tokens: int | None,
    configured_provider: str | None = None,
    model_id: str | None = None,
) -> ActiveTurnContextGuard | None:
    """Build a guard only when the active model has a usable input budget."""
    if context_window_tokens is None or context_window_tokens <= 0 or prepared_context_tokens is None:
        return None
    guard = ActiveTurnContextGuard(
        context_window_tokens=context_window_tokens,
        reserve_tokens=reserve_tokens,
        model_max_tokens=model_max_tokens,
        prepared_context_tokens=max(0, prepared_context_tokens),
        configured_provider=configured_provider,
        model_id=model_id,
    )
    return guard if guard.input_limit_tokens > 0 else None


def install_active_turn_checkpoint_hook(model: Model, guard: ActiveTurnContextGuard) -> None:
    """Stop Agno normally after a completed tool batch crosses the guard."""
    model_dict = vars(model)
    if model_dict.get(_HOOK_MARKER) is True:
        return
    original_format_function_call_results = model.format_function_call_results
    model_dict[_HOOK_MARKER] = True

    def _format_function_call_results_with_guard(
        messages: list[Message],
        function_call_results: list[Message],
        compress_tool_results: bool = False,
        **kwargs: object,
    ) -> None:
        original_format_function_call_results(
            messages=messages,
            function_call_results=function_call_results,
            compress_tool_results=compress_tool_results,
            **kwargs,
        )
        if not function_call_results:
            return
        _observe_latest_message_usage(guard, messages)
        if guard.checkpoint_after_tool_boundary(
            messages=messages,
            function_call_results=function_call_results,
        ):
            function_call_results[-1].stop_after_tool_call = True

    model_dict["format_function_call_results"] = _format_function_call_results_with_guard


def _observe_latest_message_usage(guard: ActiveTurnContextGuard, messages: list[Message]) -> None:
    """Use provider metrics attached to the latest model response when present."""
    for message in reversed(messages):
        if message.role != "assistant" or message.metrics.input_tokens <= 0:
            continue
        guard.observe_model_request(
            input_tokens=message.metrics.input_tokens,
            output_tokens=message.metrics.output_tokens,
            cache_read_tokens=message.metrics.cache_read_tokens,
            cache_write_tokens=message.metrics.cache_write_tokens,
        )
        return


def build_active_turn_checkpoint(
    *,
    goal: str,
    partial_text: str,
    completed_tools: list[ToolTraceEntry],
    trigger: ActiveTurnCheckpointTrigger,
) -> ActiveTurnCheckpoint:
    """Build bounded durable state for a fresh internal continuation run."""
    bounded_goal = _bounded_redacted_text(goal, _MAX_GOAL_CHARS)
    bounded_partial = _bounded_redacted_text(partial_text, _MAX_PARTIAL_TEXT_CHARS)
    completed_work, key_results = _render_tool_checkpoint_sections(completed_tools)
    partial_section = bounded_partial or "No durable assistant text was produced before the checkpoint."
    content = "\n\n".join(
        (
            "Active-turn continuation checkpoint",
            f"Goal:\n{bounded_goal or '(goal unavailable)'}",
            f"Completed work:\n{completed_work}",
            f"Key results and artifact references:\n{key_results}",
            f"Assistant progress before checkpoint:\n{partial_section}",
            "Pending steps:\n"
            "- Continue unfinished parts of the goal from the saved results.\n"
            "- Do not repeat completed tool calls merely to reconstruct their outputs.\n"
            "- Use a new tool call only when additional work or explicit verification requires it.",
        ),
    )
    continuation_prompt = (
        "[SYSTEM NOTICE - ACTIVE TURN CHECKPOINT COMPLETED]\n"
        "MindRoom saved the completed work below and started a fresh internal run for the same response turn. "
        "Continue the task now without waiting for another user message.\n\n"
        f"{content}"
    )
    return ActiveTurnCheckpoint(content=content, continuation_prompt=continuation_prompt, trigger=trigger)


def _estimate_completed_boundary_tokens(messages: list[Message], function_call_results: list[Message]) -> int:
    """Conservatively estimate assistant tool calls plus their completed results."""
    boundary_message_count = len(function_call_results) + 1
    boundary_messages = messages[-boundary_message_count:]
    serialized = stable_serialize([message.model_dump(exclude_none=True) for message in boundary_messages])
    # Provider message framing and tool-result wrappers vary; fixed overhead
    # keeps the fallback conservative without repeatedly tokenizing full context.
    return estimate_compaction_input_tokens(serialized) + 64 * boundary_message_count


def _bounded_redacted_text(value: str, limit: int) -> str:
    redacted = redact_sensitive_text(value.strip())
    if len(redacted) <= limit:
        return redacted
    return f"{redacted[: limit - 1]}…"


def _quoted_preview(value: str) -> str:
    return stable_serialize(redact_sensitive_text(value))


def _render_tool_checkpoint_sections(completed_tools: list[ToolTraceEntry]) -> tuple[str, str]:
    if not completed_tools:
        return "- No completed tool calls were recorded.", "- No tool result previews were available."

    work_lines = [f"- {len(completed_tools)} tool call(s) completed before this checkpoint."]
    result_lines: list[str] = []
    for index in range(len(completed_tools), 0, -1):
        tool = completed_tools[index - 1]
        work_line = f"- [{index}] `{tool.tool_name}` completed"
        if tool.args_preview:
            work_line += f" with input preview {_quoted_preview(tool.args_preview)}"
        work_line += "."
        result_line = f"- [{index}] `{tool.tool_name}`: "
        result_line += _quoted_preview(tool.result_preview) if tool.result_preview else "no result preview recorded"
        if tool.truncated:
            result_line += " (stored preview truncated)"
        candidate_work_lines = [*work_lines, work_line]
        candidate_result_lines = [*result_lines, result_line]
        if _joined_line_chars(candidate_work_lines, candidate_result_lines) > _MAX_TOOL_CONTEXT_CHARS:
            omitted = index
            while True:
                omission = f"- {omitted} older completed tool call(s) omitted to keep the checkpoint bounded."
                candidate_work_lines = [*work_lines, omission]
                candidate_result_lines = [*result_lines, omission]
                if _joined_line_chars(candidate_work_lines, candidate_result_lines) <= _MAX_TOOL_CONTEXT_CHARS:
                    work_lines = candidate_work_lines
                    result_lines = candidate_result_lines
                    break
                if not result_lines:
                    work_lines = [omission[:_MAX_TOOL_CONTEXT_CHARS]]
                    result_lines = []
                    break
                work_lines.pop()
                result_lines.pop()
                omitted += 1
            break
        work_lines = candidate_work_lines
        result_lines = candidate_result_lines
    return "\n".join(work_lines), "\n".join(result_lines)


def _joined_line_chars(work_lines: list[str], result_lines: list[str]) -> int:
    return len("\n".join(work_lines)) + len("\n".join(result_lines))
