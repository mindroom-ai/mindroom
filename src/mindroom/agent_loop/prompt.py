# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Render an agent's system message from its prompt inputs.

The layout is part of every provider's prompt-cache prefix, so section order, tags, and blank lines stay as main sends
them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.llm.messages import Message

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.provider import Capabilities

# Claude routes add this line to the system message of every request that offers tools (``model_note_for``).
CLAUDE_TOOLS_NOTE = "Do not reflect on the quality of the returned search results in your response\n\n"


@dataclass(frozen=True, slots=True, kw_only=True)
class PromptSpec:
    """Every input of one agent's system message, in the order the message renders them.

    ``persona`` is an authored prompt that leads the message in place of the agent's identity.
    ``role`` fills ``<your_role>``: an agent's identity, runtime facts, preloaded files, and authored role (only the
    runtime facts under a persona), or a helper's task.
    ``turn_context`` is the date and session context, plus the blocks a turn appends (``build.prompt_for_turn``).
    ``markdown`` adds the markdown line; callers with an output schema leave it False, as main does.
    ``tool_instructions`` holds the toolkit and function instructions in the order the tools are offered.
    ``model_note`` is the text the provider family adds (``model_note_for``), and ``output_format_prompt`` the JSON
    prompt a structured-output request needs (``structured.output_format_prompt``).
    """

    persona: str | None = None
    role: str | None = None
    instructions: tuple[str, ...] = ()
    markdown: bool = False
    tool_instructions: tuple[str, ...] = ()
    turn_context: str | None = None
    skills_block: str | None = None
    knowledge_block: str | None = None
    model_note: str | None = None
    output_format_prompt: str | None = None


def render_instructions(instructions: Sequence[str]) -> str:
    """Render an instruction list as one system-message section.

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


def model_note_for(capabilities: Capabilities, *, offers_tools: bool) -> str | None:
    """Return the text a provider family adds to the system message of a request, given whether it offers tools."""
    # TODO(simplify): every Claude request that offers any tool gets a note about search results, whatever the
    # tools are. Drop the note, which removes this function and the prompts' ``model_note`` inputs.
    # Models would notice: system prompts on Claude routes lose the line.
    return CLAUDE_TOOLS_NOTE if capabilities.claude_family and offers_tools else None


def render_system_message(spec: PromptSpec) -> Message:
    """Return the system message for ``spec``."""
    return Message(role="system", content=(_agent_sections(spec) + _context_sections(spec)).strip())


def _agent_sections(spec: PromptSpec) -> str:
    """Render the agent's own sections: persona, role, instructions, the markdown line, and tool instructions."""
    content = ""
    if spec.persona is not None:
        content += f"{spec.persona}\n"
    if spec.role is not None:
        content += f"\n<your_role>\n{spec.role}\n</your_role>\n\n"
    if spec.instructions:
        content += render_instructions(spec.instructions) + "\n\n"
    if spec.markdown:
        content += "<additional_information>\n- Use markdown to format your answers.\n</additional_information>\n\n"
    return content + "".join(f"{instruction}\n" for instruction in spec.tool_instructions)


def _context_sections(spec: PromptSpec) -> str:
    """Render the per-turn context, skills, knowledge, and provider sections that follow the agent's own."""
    content = ""
    if spec.turn_context is not None:
        content += f"{spec.turn_context}\n"
    if spec.skills_block:
        content += f"\n{spec.skills_block}\n"
    if spec.knowledge_block is not None:
        content += spec.knowledge_block + "\n"
    return content + (spec.model_note or "") + (spec.output_format_prompt or "")


__all__ = [
    "CLAUDE_TOOLS_NOTE",
    "PromptSpec",
    "model_note_for",
    "render_instructions",
    "render_system_message",
]
