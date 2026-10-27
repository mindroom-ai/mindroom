"""Reasoning toolkit whose default instructions show only while both of its functions are offered."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools.reasoning import ReasoningTools as AgnoReasoningTools

if TYPE_CHECKING:
    from collections.abc import Collection

# Agno's default instructions tell the model to call think before every reply and analyze after tool calls.
_DEFAULT_INSTRUCTION_FUNCTIONS = frozenset({"think", "analyze"})


class ReasoningTools(AgnoReasoningTools):
    """Agno's reasoning toolkit, whose default instructions never name a function the agent is not offered."""

    def __init__(
        self,
        *,
        enable_think: bool = True,
        enable_analyze: bool = True,
        all: bool = False,  # noqa: A002 - the name of Agno's parameter and of the tool's config field
        instructions: str | None = None,
        add_few_shot: bool = False,
        few_shot_examples: str | None = None,
    ) -> None:
        super().__init__(
            enable_think=enable_think,
            enable_analyze=enable_analyze,
            all=all,
            instructions=instructions,
            add_few_shot=add_few_shot,
            few_shot_examples=few_shot_examples,
        )
        self._authored_instructions = instructions is not None

    # AGNO_COMPAT: ReasoningTools' default instructions require think and analyze whichever functions it offers.
    # Reason: Agno 3.0.9 builds the same DEFAULT_INSTRUCTIONS, which say to call think before every tool call or
    # reply and analyze after tool calls, when enable_think or enable_analyze leaves a function out, so an agent
    # offered one of them would be told to call a function it does not have.
    # Upstream issue: Tracking gap; a search of Agno's issues for ReasoningTools, enable_think, and enable_analyze
    # found no match.
    # Upstream PR: No matching fix has been verified.
    # Remove when: Agno gives reasoning instructions for the functions an agent is offered, so the filters MindRoom
    # applies after construction cannot leave a named function out; authored `instructions` must still always show.
    # Coverage: tests/agent_loop/test_agent_build.py::test_reasoning_s_default_instructions_show_only_while_both_its_functions_are_offered.
    def instructions_for(self, function_names: Collection[str]) -> str:
        """Return the authored instructions, or the default ones only while ``think`` and ``analyze`` are both offered."""
        if self._authored_instructions or set(function_names) >= _DEFAULT_INSTRUCTION_FUNCTIONS:
            return self.instructions or ""
        return ""
