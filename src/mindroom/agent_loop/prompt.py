# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Render an agent's system prompt from named sections, as a stable part and a turn part.

The stable part holds what an agent's build fixes, so it stays byte for byte the same across the build's turns and
providers can cache it as the request's prefix. The turn part holds the date and session context and the blocks a turn
appends. The request carries the two parts as separate system messages, the stable one first, and each wire sends
them as its API takes system text (``system_text`` for wires that take one text).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.llm.messages import Message

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

# ``PromptSpec.markdown`` decides whether the prompt states the Markdown output format (``PromptSpec.markdown_format``).
MARKDOWN_OUTPUT_FORMAT = "Your replies are shown as Markdown, so format them with Markdown where it helps the reader."
MATRIX_MARKDOWN_OUTPUT_FORMAT = (
    f"{MARKDOWN_OUTPUT_FORMAT}\n"
    "- Single newlines break lines; supports Mermaid fences (```mermaid), GitHub callouts "
    "(`> [!NOTE]`, `TIP`, `IMPORTANT`, `WARNING`, `CAUTION`), footnotes (`[^1]` / `[^1]: ...`), "
    "`<details><summary>...</summary>...</details>` (blank lines around inner Markdown), and "
    "`<span data-mx-spoiler>...</span>` spoilers (not `||spoiler||`).\n"
    "- Math: `$inline$` (no spaces immediately inside `$`) and `$$display$$` on its own line (not `\\(...\\)`/`\\[...\\]`).\n"
    "- Link rooms/messages unbackticked (no bare `!room_id`/`$event_id`): `<https://matrix.to/#/!room_id>`, "
    "`[label](https://matrix.to/#/!room_id/$event_id)`, or `<https://matrix.to/#/!room_id/$event_id>` (optional "
    "`?via=server`).\n"
    "- External image URLs (`![](https://...)`) are not rendered; link to external URLs instead."
)
MATRIX_TOOL_FOLDING_FORMAT = (
    "- MindRoom Chat may collapse earlier text and tool calls up through your last tool call once your reply finishes; "
    "write a self-contained final answer after your last tool call."
)


def matrix_markdown_output_format(*, has_tools: bool) -> str:
    """Return Matrix Markdown formatting rules, adding the tool-folding rule only when tools are offered."""
    if not has_tools:
        return MATRIX_MARKDOWN_OUTPUT_FORMAT
    return f"{MATRIX_MARKDOWN_OUTPUT_FORMAT}\n{MATRIX_TOOL_FOLDING_FORMAT}"


@dataclass(frozen=True, slots=True, kw_only=True)
class PromptSpec:
    """Every input of one agent's system prompt, in the order its sections render.

    ``persona`` is an authored prompt that leads the stable part in place of the agent's identity.
    ``role`` fills ``<your_role>``: an agent's identity, runtime facts, preloaded files, and authored role (only the
    runtime facts under a persona), or a team's identity.
    ``markdown`` states ``markdown_format`` right after the role (``MATRIX_MARKDOWN_OUTPUT_FORMAT`` for Matrix turns,
    ``MARKDOWN_OUTPUT_FORMAT`` outside Matrix): an agent and a team leader set ``markdown`` from the ``markdown``
    setting, and a voice call, realtime or cascaded, turns it off (``matrix_rtc.call_tools._spoken``), since a call
    speaks its replies.
    ``tool_instructions`` holds the toolkit and function instructions in the order the tools are offered.
    ``turn_context`` is the turn part: the date and session context, plus the blocks a turn appends
    (``build.prompt_for_turn``).
    """

    persona: str | None = None
    role: str | None
    instructions: tuple[str, ...]
    markdown: bool = False
    markdown_format: str = MARKDOWN_OUTPUT_FORMAT
    tool_instructions: tuple[str, ...] = ()
    skills_block: str | None = None
    knowledge_block: str | None = None
    turn_context: str | None = None


def render_instructions(instructions: Sequence[str]) -> str:
    """Render an instruction list as one system-prompt section.

    A single instruction stays verbatim; single-line instructions become ``- `` bullets; a multi-line instruction
    stays a block, because its own bullets and paragraphs would otherwise nest under a bullet it does not belong to.
    Each one is stripped, so blocks stay one blank line apart whatever whitespace they end with.
    """
    if len(instructions) == 1:
        return instructions[0]
    separator = "\n\n" if any(_is_block(instruction) for instruction in instructions) else "\n"
    return separator.join(
        instruction.strip() if _is_block(instruction) else f"- {instruction.strip()}" for instruction in instructions
    )


def _is_block(instruction: str) -> bool:
    # A trailing newline does not make a one-line instruction a block.
    return "\n" in instruction.strip()


def join_sections(sections: Iterable[str | None]) -> str:
    """Join prompt sections with one blank line between them, each stripped; an empty section adds nothing."""
    return "\n\n".join(text for section in sections if section and (text := section.strip()))


def render_system_messages(spec: PromptSpec) -> tuple[Message, ...]:
    """Return the system messages for ``spec``: the stable part, then the turn part; an empty part sends none."""
    stable = join_sections(
        (
            spec.persona,
            f"<your_role>\n{spec.role.strip()}\n</your_role>" if spec.role and spec.role.strip() else None,
            spec.markdown_format if spec.markdown else None,
            render_instructions(spec.instructions) if spec.instructions else None,
            *spec.tool_instructions,
            spec.skills_block,
            spec.knowledge_block,
        ),
    )
    turn = join_sections((spec.turn_context,))
    return tuple(Message(role="system", content=text) for text in (stable, turn) if text)


__all__ = [
    "MARKDOWN_OUTPUT_FORMAT",
    "MATRIX_MARKDOWN_OUTPUT_FORMAT",
    "MATRIX_TOOL_FOLDING_FORMAT",
    "PromptSpec",
    "join_sections",
    "matrix_markdown_output_format",
    "render_instructions",
    "render_system_messages",
]
