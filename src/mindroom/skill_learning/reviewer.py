"""One skill review: Hermes' fork of the agent's final request, or a digest replay when the request cannot be forked.

Like Hermes' default review, the fork replays the finished response's final request on the same model with its
request settings unchanged and appends the review prompt, so the review sees the conversation verbatim. Its tools are
the definitions the run's functions declare, without the wait budget a request offered for calls that could become
jobs, which the review's own functions do not accept; the provider serves the conversation from its prompt cache only
when the request offered no such budget. Only the skill tools execute; every other tool keeps its definition and
answers that it is not available. When the review uses another model, or the response left no request to fork, the
review replays the stored conversation as a digest instead, like Hermes' routed review.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import validate_call

from mindroom.agent_loop.model_loop import EMPTY_REQUEST, EndRun, LoopHooks, SendRequest, run_model_loop
from mindroom.agent_loop.output_limit import output_limit_answers
from mindroom.agent_loop.run import own_model_messages
from mindroom.agent_loop.spec import RunContext
from mindroom.agent_loop.tools import PreparedTools
from mindroom.approval.tool_approval import JOB_APPROVAL_TYPE
from mindroom.custom_tools.skill_manage import SkillManageTools
from mindroom.helper_usage import HelperUsageOwner, record_helper_usage
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord
from mindroom.llm.usage import RunMetrics
from mindroom.logging_config import get_logger
from mindroom.model_usage import context_input_tokens_from_counts
from mindroom.providers.registry import build_model_provider
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.skill_learning.tools import SkillCatalog, SkillTools, load_skill_catalog
from mindroom.skill_learning.transcript import conversation_messages, render_transcript
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.tool_function import ToolFunction

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from mindroom.agent_loop.model_loop import LoopState, RequestDecision
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import ModelProvider, ModelRequest, ToolSpec
    from mindroom.llm.usage import MessageMetrics
    from mindroom.skill_learning.capture import CapturedRequest
    from mindroom.skill_learning.tools import ReviewProgress
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

# Hermes caps one review fork at 16 iterations and 75% of the review model's context window, at most 600k input
# tokens across all of its requests, falling back to 120k when the window is unknown.
_REVIEW_TOOL_CALL_LIMIT = 16
_INPUT_CONTEXT_FRACTION = 0.75
_MAX_INPUT_TOKENS = 600_000
_FALLBACK_INPUT_TOKENS = 120_000
_CHARS_PER_TOKEN = 4
# Each request of a review resends the conversation, whether a fork's full request or a digest replay's transcript,
# and fork compaction is absent, so the conversation may use a quarter of the budget to leave room for more requests.
_CONVERSATION_BUDGET_SHARE = 4
# The skill tools a review runs, as the review prompt describes them, in the order it lists them.
_SKILL_TOOL_LINES = {
    "skill_manage": 'skill_manage(action, name, ...): action "create" (full SKILL.md in content), "patch" '
    '(old_string/new_string, optionally file_path), "edit" (full SKILL.md replacement in content), "write_file" '
    '(file_path and file_content), or "remove_file" (file_path).',
    "get_skill_instructions": "get_skill_instructions(skill_name): load a skill's full SKILL.md, its owner, and its "
    "support files.",
    "get_skill_reference": "get_skill_reference(skill_name, reference_path): load one support file under references/, "
    "named as get_skill_instructions lists it or by its file name.",
    "get_skill_script": "get_skill_script(skill_name, script_path): load one support file under scripts/, named the "
    "same way; scripts never run in a review.",
}


@dataclass(frozen=True)
class _ReviewRequest:
    """One review's model and first messages, the tool definitions it offers, and the settings of its requests.

    ``tools`` holds the functions that answer the model's calls, which may be fewer than ``definitions``.
    """

    model: ModelProvider
    model_name: str
    messages: list[Message]
    definitions: tuple[ToolSpec, ...]
    tools: PreparedTools
    settings: ModelRequest
    forked: bool


def _review_input_budget_tokens(config: Config, model_name: str) -> int:
    """Return the aggregate input budget of one review."""
    context_window = config.models[model_name].context_window
    if context_window is None:
        return _FALLBACK_INPUT_TOKENS
    return min(_MAX_INPUT_TOKENS, int(context_window * _INPUT_CONTEXT_FRACTION))


def _tool_awaits_decision(definition: ToolSpec) -> bool:
    """Return whether a call of this tool waits for a decision: it pauses its run, asks as its job, or runs elsewhere."""
    return bool(
        definition.requires_confirmation
        or definition.external_execution
        or definition.approval_type == JOB_APPROVAL_TYPE,
    )


def _review_tools(definitions: Sequence[ToolSpec], tools: SkillTools) -> tuple[PreparedTools, list[str]]:
    """Answer the calls of every tool the request defines, but run only the skill tools, as the review's.

    Like Hermes' denial message, every other tool answers with the skill tools the review can use. A tool whose call
    waits for a decision gets no function, so the loop answers that it does not exist or is not available instead of
    pausing the review. Returns the functions and the skill tools that run, in the order the review prompt lists them.
    """
    # The functions run the review's own methods, which validate their arguments as the agent's coercing functions
    # would, such as refusing an action outside the enum.
    entrypoints: dict[str, Callable[..., Any]] = {
        "skill_manage": validate_call(tools.skill_manage),
        "get_skill_instructions": validate_call(tools.get_skill_instructions),
        "get_skill_reference": validate_call(tools.get_skill_reference),
        "get_skill_script": validate_call(tools.get_skill_script),
    }
    answered = {definition.name: definition for definition in definitions if not _tool_awaits_decision(definition)}
    runnable = [name for name in _SKILL_TOOL_LINES if name in answered]

    async def deny(**_arguments: object) -> str:
        verb = "runs" if len(runnable) == 1 else "run"
        return f"This tool is not available during a skill review; only {_listing(runnable)} {verb} here."

    functions = {
        name: ToolFunction(
            name=name,
            description=definition.description,
            parameters=definition.parameters,
            entrypoint=entrypoints.get(name, deny),
        )
        for name, definition in answered.items()
    }
    return PreparedTools(functions=functions, instructions=()), runnable


def _listing(names: Sequence[str]) -> str:
    return " and ".join(names) if len(names) < 3 else ", ".join(names[:-1]) + f", and {names[-1]}"


def _review_prompt(config: Config, catalog: SkillCatalog, runnable: Sequence[str]) -> str:
    owners = "\n".join(
        f"- {entry.name} ({entry.owner}): {entry.description}"
        for entry in sorted(catalog.entries.values(), key=lambda item: item.name)
    )
    tool_lines = "\n".join(f"- {_SKILL_TOOL_LINES[name]}" for name in runnable)
    return (
        f"{config.get_prompt('SKILL_REVIEW_PROMPT')}\nTools:\n{tool_lines}\n\n"
        f"Skills and their owners:\n{owners or '(none yet)'}\n\n"
        f"You can only call {_listing(runnable)} in this review; every other tool answers that it is not available, "
        "so do not call one."
    )


def _fork(
    config: Config,
    agent_name: str,
    captured: CapturedRequest | None,
    tools: SkillTools,
    catalog: SkillCatalog,
) -> _ReviewRequest | None:
    """Return the fork of the response's final request, or None when the review cannot reuse it."""
    if captured is None or config.agents[agent_name].skill_learning.model not in {None, captured.model_name}:
        return None
    request = captured.request
    final = request.messages[-1] if request.messages else None
    # A loop that stopped after a tool call, or that its request cap ended before the model answered the results,
    # left no final answer to continue.
    if final is None or final.role != "assistant" or final.tool_calls or not final.content:
        return None
    sent = _context_tokens(config, captured.model, captured.model_name, final.metrics)
    if sent * _CONVERSATION_BUDGET_SHARE > _review_input_budget_tokens(config, captured.model_name):
        return None
    functions, runnable = _review_tools(request.tools, tools)
    if "skill_manage" not in runnable:
        return None
    return _ReviewRequest(
        model=captured.model,
        model_name=captured.model_name,
        messages=[*request.messages, Message(role="user", content=_review_prompt(config, catalog, runnable))],
        definitions=tuple(request.tools),
        tools=functions,
        settings=_review_settings(request),
        forked=True,
    )


def _review_settings(request: ModelRequest) -> ModelRequest:
    """Return the settings every request of a review sends: the given request's, asking for whole replies.

    The review reads only finished replies, so each of its requests asks for one non-streaming response.
    """
    return replace(request, messages=(), tools=(), stream=False)


def _agent_skill_definitions(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    skills_root: Path,
    catalog: SkillCatalog,
) -> tuple[ToolSpec, ...]:
    """Return the agent's own skill tool definitions for a review that has no request of the agent to fork.

    They are sorted by name, as every request of an agent offers its tools.
    """
    toolkits = [
        *([catalog.skills] if catalog.skills is not None else []),
        SkillManageTools(agent_name, config, runtime_paths, skills_root),
    ]
    definitions = [function.definition() for toolkit in toolkits for function in adapt_toolkit(toolkit).functions]
    return tuple(sorted(definitions, key=lambda definition: definition.name))


def _replay_model_name(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    identity: ToolExecutionIdentity | None,
    captured: CapturedRequest | None,
) -> str:
    """Return the review model, which defaults to the model the reviewed response used in its room and thread."""
    if (model_name := config.agents[agent_name].skill_learning.model) is not None:
        return model_name
    if captured is not None:
        return captured.model_name
    return config.resolve_runtime_model(
        entity_name=agent_name,
        active_model_name=None,
        room_id=identity.room_id if identity is not None else None,
        thread_id=identity.resolved_thread_id if identity is not None else None,
        runtime_paths=runtime_paths,
    ).model_name


async def _replay(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    session_id: str,
    identity: ToolExecutionIdentity | None,
    skills_root: Path,
    catalog: SkillCatalog,
    tools: SkillTools,
    captured: CapturedRequest | None,
) -> _ReviewRequest | None:
    """Return a review that replays the stored conversation as a digest, like Hermes' routed review.

    Returns None when the conversation has no stored session left to review.
    """
    model_name = _replay_model_name(config, runtime_paths, agent_name, identity, captured)
    session = await asyncio.to_thread(
        lambda: create_session_storage(agent_name, config, runtime_paths, execution_identity=identity).load_session(
            session_id,
            "agent",
        ),
    )
    if session is None:
        return None
    messages = await asyncio.to_thread(conversation_messages, session)
    transcript = await asyncio.to_thread(
        render_transcript,
        messages,
        summary=session.summary.text if session.summary is not None else None,
        budget_chars=_review_input_budget_tokens(config, model_name) * _CHARS_PER_TOKEN // _CONVERSATION_BUDGET_SHARE,
    )
    definitions = await asyncio.to_thread(
        _agent_skill_definitions,
        config,
        runtime_paths,
        agent_name,
        skills_root,
        catalog,
    )
    functions, runnable = _review_tools(definitions, tools)
    evidence = (
        "The conversation to review is supplied inside <conversation> tags as a transcript of the stored turns.\n"
        f"<conversation>\n{transcript}\n</conversation>"
    )
    return _ReviewRequest(
        model=build_model_provider(config, runtime_paths, model_name, execution_identity=identity),
        model_name=model_name,
        messages=[Message(role="user", content=f"{evidence}\n\n{_review_prompt(config, catalog, runnable)}")],
        definitions=definitions,
        tools=functions,
        settings=_review_settings(EMPTY_REQUEST),
        forked=False,
    )


def _context_tokens(config: Config, model: ModelProvider, model_name: str, metrics: MessageMetrics | RunMetrics) -> int:
    """Return the input that requests reported, counting prompt-cache reads and writes."""
    identity = model.identity
    return (
        context_input_tokens_from_counts(
            input_tokens=metrics.input_tokens,
            cache_read_tokens=metrics.cache_read_tokens,
            cache_write_tokens=metrics.cache_write_tokens,
            provider=identity.provider,
            configured_provider=config.models[model_name].provider,
            model_id=identity.id,
        )
        or 0
    )


def _review_hooks(config: Config, review: _ReviewRequest, metrics: RunMetrics) -> LoopHooks:
    """Return the hooks of one review's loop: its input budget, the tool definitions it offers, and its output limit.

    Like an agent's reply on Claude, a review answers the tool calls of a response the output limit cut off instead
    of running them, so a truncated skill_manage call never writes a truncated skill.
    """
    budget_tokens = _review_input_budget_tokens(config, review.model_name)

    async def within_budget(_state: LoopState) -> RequestDecision:
        # Like Hermes, the review ends before its next request once the input it sent reached the budget.
        if (spent := _context_tokens(config, review.model, review.model_name, metrics)) < budget_tokens:
            return SendRequest()
        logger.info("Skill review reached its input budget", budget_tokens=budget_tokens, spent_tokens=spent)
        return EndRun()

    async def offer_definitions(state: LoopState) -> RequestDecision:
        # The loop offers only the tools that answer calls; the review offers every definition of its request.
        state.tools = review.definitions
        return SendRequest()

    return LoopHooks(
        before_request=(within_budget, offer_definitions),
        answer_tool_calls=output_limit_answers(review.model.capabilities),
    )


async def review_conversation(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    session_id: str,
    identity: ToolExecutionIdentity | None,
    skills_root: Path,
    captured: CapturedRequest | None,
    progress: ReviewProgress,
) -> None:
    """Run one review, recording each skill it creates or updates in ``progress`` as the write lands."""
    catalog = await asyncio.to_thread(load_skill_catalog, config, runtime_paths, agent_name, skills_root)
    tools = SkillTools(skills_root, dict(catalog.entries), catalog.reserved_names, progress=progress)
    review = _fork(config, agent_name, captured, tools, catalog) or await _replay(
        config=config,
        runtime_paths=runtime_paths,
        agent_name=agent_name,
        session_id=session_id,
        identity=identity,
        skills_root=skills_root,
        catalog=catalog,
        tools=tools,
        captured=captured,
    )
    if review is None:
        return
    invocation_id = uuid4().hex
    model_identity = review.model.identity
    # The loop adds the usage of each of the review's requests to the record's metrics.
    metrics = RunMetrics()
    record = RunRecord(
        run_id=invocation_id,
        session_id=session_id,
        model=model_identity.id,
        model_provider=model_identity.provider,
        metrics=metrics,
        # A review that reaches its request cap logs it under the reviewed agent.
        agent_id=agent_name,
    )
    messages = list(review.messages)
    try:
        events = run_model_loop(
            review.model,
            messages,
            review.tools,
            record=record,
            run=RunContext(run_id=invocation_id, session_id=session_id),
            hooks=_review_hooks(config, review, metrics),
            tool_call_limit=_REVIEW_TOOL_CALL_LIMIT,
            request_template=review.settings,
        )
        async for _event in events:
            pass
    finally:
        # Only the review's own replies, whose metrics give the usage report its requests. The usage counts against
        # the conversation it reviewed, also when a timeout or a stop ended the review.
        record.messages = messages[len(review.messages) :]
        if record.messages:
            await record_helper_usage(
                record,
                owner=HelperUsageOwner(
                    storage_factory=partial(
                        create_session_storage,
                        agent_name,
                        config,
                        runtime_paths,
                        execution_identity=identity,
                    ),
                    session_id=session_id,
                ),
                invocation_id=invocation_id,
                kind="skill_learning",
                requester_id=identity.requester_id if identity is not None else None,
            )
    logger.info(
        "Skill review model run finished",
        forked=review.forked,
        model_requests=len(own_model_messages(record)),
        input_tokens=_context_tokens(config, review.model, review.model_name, metrics),
        cache_read_tokens=metrics.cache_read_tokens,
    )
