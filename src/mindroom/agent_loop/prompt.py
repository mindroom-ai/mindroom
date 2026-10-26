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


@dataclass(frozen=True, slots=True, kw_only=True)
class PromptSpec:
    """Every input of one agent's system prompt, in the order its sections render.

    ``persona`` is an authored prompt that leads the stable part in place of the agent's identity.
    ``role`` fills ``<your_role>``: an agent's identity, runtime facts, preloaded files, and authored role (only the
    runtime facts under a persona), or a helper's task.
    ``markdown`` adds the markdown line; callers with an output schema leave it False.
    ``tool_instructions`` holds the toolkit and function instructions in the order the tools are offered, and
    ``output_format_prompt`` the JSON prompt a structured-output request needs (``structured.output_format_prompt``).
    ``turn_context`` is the turn part: the date and session context, plus the blocks a turn appends
    (``build.prompt_for_turn``).
    """

    persona: str | None = None
    role: str | None
    instructions: tuple[str, ...]
    markdown: bool = False
    tool_instructions: tuple[str, ...] = ()
    skills_block: str | None = None
    knowledge_block: str | None = None
    output_format_prompt: str | None = None
    turn_context: str | None = None


def render_instructions(instructions: Sequence[str]) -> str:
    """Render an instruction list as one system-prompt section.

    A single instruction stays verbatim; single-line instructions become ``- `` bullets; a multi-line instruction
    stays a block, because its own bullets and paragraphs would otherwise nest under a bullet it does not belong to.
    """
    if len(instructions) == 1:
        return instructions[0]
    separator = "\n\n" if any(_is_block(instruction) for instruction in instructions) else "\n"
    return separator.join(
        instruction if _is_block(instruction) else f"- {instruction.strip()}" for instruction in instructions
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
            render_instructions(spec.instructions) if spec.instructions else None,
            "<additional_information>\n- Use markdown to format your answers.\n</additional_information>"
            if spec.markdown
            else None,
            *spec.tool_instructions,
            spec.skills_block,
            spec.knowledge_block,
            spec.output_format_prompt,
        ),
    )
    turn = join_sections((spec.turn_context,))
    return tuple(Message(role="system", content=text) for text in (stable, turn) if text)


__all__ = [
    "PromptSpec",
    "join_sections",
    "render_instructions",
    "render_system_messages",
]
