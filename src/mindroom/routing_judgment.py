"""Optional JEV selection over the router's already eligible responders."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.agent_descriptions import describe_agent
from mindroom.judgment.evaluator import create_choice_evaluator
from mindroom.judgment.state import ChoiceQuestion, JudgmentMessage, build_judgment_request

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage


@dataclass(frozen=True, slots=True)
class ResponderSelection:
    """An accepted selection, including an explicit no-fit outcome."""

    entity_name: str | None


async def judge_responder(
    message: str,
    candidates: list[str],
    config: Config,
    runtime_paths: RuntimePaths,
    thread_context: Sequence[ResolvedVisibleMessage] | None,
) -> ResponderSelection | None:
    """Return None to use ordinary routing, or an accepted candidate/no-fit choice."""
    settings = config.router.judgment
    if settings is None or not 2 <= len(candidates) <= 253:
        return None
    choices = {f"candidate_{index}": name for index, name in enumerate(candidates)}
    question = ChoiceQuestion(
        id="responder_selection",
        instructions=(
            "Which eligible agent or team is best suited to handle the current request? "
            "Use recent conversation only to interpret the current request. "
            "Prefer one responder capable of handling the whole request, including its delegation capabilities."
        ),
        options=(
            *((key, describe_agent(name, config)) for key, name in choices.items()),
            ("no_fit", "None of the eligible responders can usefully handle this request."),
            ("multiple", "The request needs multiple independent responders; no single candidate can handle it."),
        ),
    )
    aliases: dict[str, str] = {}
    messages: list[JudgmentMessage] = []
    for item in (thread_context or ())[-3:]:
        sender = aliases.setdefault(item.sender, f"speaker_{len(aliases) + 1}")
        messages.append(JudgmentMessage("user", f"Recent conversation ({sender}):\n{item.body}"))
    messages.append(JudgmentMessage("user", f"Current request:\n{message}"))
    request = build_judgment_request(question, tuple(messages), instructions="Select only a supplied option.")
    evaluate = create_choice_evaluator(
        settings,
        runtime_paths,
        owner=f"{runtime_paths.storage_root}:router",
        question_id=question.id,
    )
    if evaluate is None:
        return None
    result = await evaluate(request)
    if result.failure is not None or result.decision is None or result.decision.option == "multiple":
        return None
    if result.decision.option == "no_fit":
        return ResponderSelection(None)
    name = choices.get(result.decision.option)
    return ResponderSelection(name) if name is not None else None
