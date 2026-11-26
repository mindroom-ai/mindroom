"""Native reasoning scratchpad toolkit with function-scoped instructions."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from textwrap import dedent
from typing import TYPE_CHECKING, Any, Optional

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit, tool
from mindroom.tool_system.invocation import current_tool_call

if TYPE_CHECKING:
    from collections.abc import Collection

logger = get_logger(__name__)

_DEFAULT_RUN_ID = "default"
_FINAL_NEXT_ACTIONS = frozenset({"final", "final_answer", "finalize"})
_REQUIRED_DEFAULT_INSTRUCTION_FUNCTIONS = frozenset({"think", "analyze"})


@dataclass(frozen=True, slots=True)
class _ReasoningStep:
    """One recorded think or analyze step stored in session state."""

    title: str
    reasoning: str
    action: str | None = None
    result: str | None = None
    next_action: str = "continue"
    confidence: float = 0.8

    def to_json(self) -> str:
        """Serialize the step as JSON for session-state persistence."""
        return json.dumps(
            {
                "title": self.title,
                "action": self.action,
                "result": self.result,
                "reasoning": self.reasoning,
                "next_action": self.next_action,
                "confidence": self.confidence,
            },
        )


def _normalize_next_action(next_action: str) -> str:
    """Map caller-provided next_action strings onto continue, validate, or final_answer."""
    normalized = next_action.lower()
    if normalized == "validate":
        return "validate"
    if normalized in _FINAL_NEXT_ACTIONS:
        return "final_answer"
    return "continue"


def _format_steps(raw_steps: list[str]) -> str:
    """Format recorded JSON reasoning steps into the scratchpad transcript."""
    formatted: list[str] = []
    for index, raw_step in enumerate(raw_steps, 1):
        step = json.loads(raw_step)
        formatted.append(
            f"Step {index}:\n"
            f"Title: {step.get('title')}\n"
            f"Reasoning: {step.get('reasoning')}\n"
            f"Action: {step.get('action')}\n"
            f"Confidence: {step.get('confidence')}",
        )
    return "\n\n".join(formatted)


class ReasoningTools(Toolkit):
    """Step-by-step reasoning scratchpad providing think and analyze tools."""

    name = "reasoning_tools"

    DEFAULT_INSTRUCTIONS = dedent(
        """\
        You have access to the `think` and `analyze` tools to work through problems step-by-step and structure your thought process. You must ALWAYS `think` before making tool calls or generating a response.

        1. **Think** (scratchpad):
            - Purpose: Use the `think` tool as a scratchpad to break down complex problems, outline steps, and decide on immediate actions within your reasoning flow. Use this to structure your internal monologue.
            - Usage: Call `think` before making tool calls or generating a response. Explain your reasoning and specify the intended action (e.g., "make a tool call", "perform calculation", "ask clarifying question").

        2. **Analyze** (evaluation):
            - Purpose: Evaluate the result of a think step or a set of tool calls. Assess if the result is expected, sufficient, or requires further investigation.
            - Usage: Call `analyze` after a set of tool calls. Determine the `next_action` based on your analysis: `continue` (more reasoning needed), `validate` (seek external confirmation/validation if possible), or `final_answer` (ready to conclude).
            - Explain your reasoning highlighting whether the result is correct/sufficient.

        ## IMPORTANT GUIDELINES
        - **Always Think First:** You MUST use the `think` tool before making tool calls or generating a response.
        - **Iterate to Solve:** Use the `think` and `analyze` tools iteratively to build a clear reasoning path. The typical flow is `Think` -> [`Tool Calls` if needed] -> [`Analyze` if needed] -> ... -> `final_answer`. Repeat this cycle until you reach a satisfactory conclusion.
        - **Make multiple tool calls in parallel:** After a `think` step, you can make multiple tool calls in parallel.
        - **Keep Thoughts Internal:** The reasoning steps (thoughts and analyses) are for your internal process only. Do not share them directly with the user.
        - **Conclude Clearly:** When your analysis determines the `next_action` is `final_answer`, provide a concise and accurate final answer to the user.""",
    )

    FEW_SHOT_EXAMPLES = dedent(
        """
        Below are examples demonstrating how to use the `think` and `analyze` tools.

        ### Examples

        **Example 1: Simple Fact Retrieval**

        *User Request:* How many continents are there on Earth?

        *Agent's Internal Process:*

        ```tool_call
        think(
          title="Understand Request",
          thought="The user wants to know the standard number of continents on Earth. This is a common piece of knowledge.",
          action="Recall or verify the number of continents.",
          confidence=0.95
        )
        ```
        *--(Agent internally recalls the fact)--*
        ```tool_call
        analyze(
          title="Evaluate Fact",
          result="Standard geographical models list 7 continents: Africa, Antarctica, Asia, Australia, Europe, North America, South America.",
          analysis="The recalled information directly answers the user's question accurately.",
          next_action="final_answer",
          confidence=1.0
        )
        ```

        *Agent's Final Answer to User:*
        There are 7 continents on Earth: Africa, Antarctica, Asia, Australia, Europe, North America, and South America.

        **Example 2: Multi-Step Information Gathering**

        *User Request:* What is the capital of France and its current population?

        *Agent's Internal Process:*

        ```tool_call
        think(
          title="Plan Information Retrieval",
          thought="The user needs two pieces of information: the capital of France and its current population. I should use external tools (like search) to find the most up-to-date and accurate information.",
          action="First, search for the capital of France.",
          confidence=0.95
        )
        ```

        *Perform multiple tool calls in parallel*
        *--(Tool call 1: search(query="capital of France"))--*
        *--(Tool call 2: search(query="population of Paris current"))--*
        *--(Tool Result 1: "Paris")--*
        *--(Tool Result 2: "Approximately 2.1 million (city proper, estimate for early 2024)")--*

        ```tool_call
        analyze(
          title="Analyze Capital Search Result",
          result="The search result indicates Paris is the capital of France.",
          analysis="This provides the first piece of requested information. Now I need to find the population of Paris.",
          next_action="continue",
          confidence=1.0
        )
        ```
        ```tool_call
        analyze(
          title="Analyze Population Search Result",
          result="The search provided an estimated population figure for Paris.",
          analysis="I now have both the capital and its estimated population. I can provide the final answer.",
          next_action="final_answer",
          confidence=0.9
        )
        ```

        *Agent's Final Answer to User:*
        The capital of France is Paris. Its estimated population (city proper) is approximately 2.1 million as of early 2024.""",
    )

    def __init__(
        self,
        enable_think: bool = True,
        enable_analyze: bool = True,
        all: bool = False,  # noqa: A002
        instructions: str | None = None,
        add_few_shot: bool = False,
        few_shot_examples: str | None = None,
    ) -> None:
        default_parts = ["<reasoning_instructions>", self.DEFAULT_INSTRUCTIONS]
        if add_few_shot:
            default_parts.append(few_shot_examples if few_shot_examples is not None else self.FEW_SHOT_EXAMPLES)
        default_parts.append("</reasoning_instructions>\n")
        self._default_instructions = "\n".join(default_parts)
        self._custom_instructions = instructions
        self._fallback_session_state: dict[str, Any] = {}
        self._lock = threading.Lock()

        super().__init__(name="reasoning_tools")
        if not (all or enable_think):
            self.functions.pop("think", None)
        if not (all or enable_analyze):
            self.functions.pop("analyze", None)

    @property
    def instructions(self) -> str:  # ty: ignore[incompatible-property-override]  # Toolkit writes it only when passed one
        """Return reasoning instructions scoped to the toolkit's currently registered functions."""
        return self.instructions_for(self.functions)

    def instructions_for(self, function_names: Collection[str]) -> str:
        """Return custom instructions for any offered function, or default instructions when both think and analyze are offered."""
        offered = set(function_names)
        if not offered:
            return ""
        if self._custom_instructions is not None:
            return self._custom_instructions
        if not offered >= _REQUIRED_DEFAULT_INSTRUCTION_FUNCTIONS:
            return ""
        return self._default_instructions

    def _record_step(self, step: _ReasoningStep) -> str:
        """Append one reasoning step to the active run's session state and return the formatted transcript."""
        call = current_tool_call()
        if call is not None:
            session_state = call.context.session_state
            run_id = call.context.run_id
        else:
            session_state = self._fallback_session_state
            run_id = _DEFAULT_RUN_ID

        with self._lock:
            steps_by_run = session_state.get("reasoning_steps")
            if not isinstance(steps_by_run, dict):
                steps_by_run = {}
                session_state["reasoning_steps"] = steps_by_run
            run_steps = steps_by_run.get(run_id)
            if not isinstance(run_steps, list):
                run_steps = []
                steps_by_run[run_id] = run_steps
            run_steps.append(step.to_json())
            snapshot = list(run_steps)
        return _format_steps(snapshot)

    @tool
    def think(
        self,
        title: str,
        thought: str,
        action: Optional[str] = None,  # noqa: UP045 - Optional keeps null out of the tool schema.
        confidence: float = 0.8,
    ) -> str:
        """Use this tool as a scratchpad to reason about the question and work through it step-by-step.

        This tool will help you break down complex problems into logical steps and track the reasoning process.
        You can call it as many times as needed. These internal thoughts are never revealed to the user.

        Args:
            title: A concise title for this step
            thought: Your detailed thought for this step
            action: What you'll do based on this thought
            confidence: How confident you are about this thought (0.0 to 1.0)

        Returns:
            A list of previous thoughts and the new thought

        """
        logger.debug("Recording reasoning thought", title=title)
        step = _ReasoningStep(
            title=title,
            reasoning=thought,
            action=action,
            next_action="continue",
            confidence=confidence,
        )
        return self._record_step(step)

    @tool
    def analyze(
        self,
        title: str,
        result: str,
        analysis: str,
        next_action: str = "continue",
        confidence: float = 0.8,
    ) -> str:
        """Use this tool to analyze results from a reasoning step and determine next actions.

        Args:
            title: A concise title for this analysis step
            result: The outcome of the previous action
            analysis: Your analysis of the results
            next_action: What to do next ("continue", "validate", or "final_answer")
            confidence: How confident you are in this analysis (0.0 to 1.0)

        Returns:
            A list of previous thoughts and the new analysis

        """
        logger.debug("Recording reasoning analysis", title=title)
        step = _ReasoningStep(
            title=title,
            result=result,
            reasoning=analysis,
            next_action=_normalize_next_action(next_action),
            confidence=confidence,
        )
        return self._record_step(step)
