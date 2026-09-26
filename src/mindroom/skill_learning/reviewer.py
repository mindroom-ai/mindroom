"""One skill review: Hermes' fork of the agent's final request, or a digest replay when the request cannot be forked.

Like Hermes' default review, the fork replays the finished response's final request on the same model with its tools
unchanged and appends the review prompt, so the provider serves the conversation from its prompt cache and the review
sees it verbatim. Only the skill tools execute; every other tool keeps its definition and answers that it is not
available. When the review uses another model, or the response left no request to fork, the review replays the stored
conversation as a digest instead, like Hermes' routed review.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agno.compression.manager import CompressionManager
from agno.metrics import BaseMetrics, RunMetrics
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.tools.function import Function
from pydantic import validate_call

from mindroom import model_loading
from mindroom.agent_storage import create_session_storage, load_agent_session
from mindroom.background_tasks import run_blocking_until_complete, run_coroutine_until_complete
from mindroom.claude_prompt_cache import aclose_anthropic_async_client, prewarm_anthropic_async_client
from mindroom.custom_tools.skill_manage import SkillManageTools
from mindroom.helper_usage import HelperUsageOwner, record_helper_usage
from mindroom.logging_config import get_logger
from mindroom.model_usage import context_input_tokens_from_counts
from mindroom.skill_learning.tools import SkillCatalog, SkillTools, load_skill_catalog
from mindroom.skill_learning.transcript import conversation_messages, render_transcript
from mindroom.tool_call_budget import install_model_call_cap, request_gate

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from pathlib import Path

    from agno.models.base import Model
    from pydantic import BaseModel

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
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
# The skill tools a review runs, as the review prompt describes them.
_SKILL_TOOL_LINES = {
    "get_skill_instructions": "get_skill_instructions(skill_name): load a skill's full SKILL.md, its owner, and its "
    "support files.",
    "get_skill_reference": "get_skill_reference(skill_name, reference_path): load one support file under references/, "
    "named as get_skill_instructions lists it or by its file name.",
    "get_skill_script": "get_skill_script(skill_name, script_path): load one support file under scripts/, named the "
    "same way; scripts never run in a review.",
    "skill_manage": 'skill_manage(action, name, ...): action "create" (full SKILL.md in content), "patch" '
    '(old_string/new_string, optionally file_path), "edit" (full SKILL.md replacement in content), "write_file" '
    '(file_path and file_content), or "remove_file" (file_path).',
}


@dataclass(frozen=True)
class _ReviewRequest:
    """The model, messages, and tools of one review's first request."""

    model: Model
    model_name: str
    messages: list[Message]
    tools: list[Function | dict[str, Any]]
    forked: bool
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | type[BaseModel] | None = None
    compressed_tool_results: bool = False


# AGNO_COMPAT: One compression manager both sends compressed tool results and compresses more of them.
# Reason: Agno's response loop sends tool results' compressed text only when a compression manager is passed, and then
# also compresses before every request; the fork must resend what its response compressed without compressing more.
# Upstream issue: tracking gap; no Agno issue or PR separates sending compressed results from compressing new ones.
# Upstream PR: none identified.
# Remove when: Agno can send existing compressed tool results without compressing further; the fork's rule of never
# compressing during a review remains MindRoom policy.
# Coverage: tests/test_skill_learning.py::test_a_fork_keeps_compressed_results_and_compresses_nothing_more.
@dataclass
class _SendCompressedResults(CompressionManager):
    """Send the tool results a response compressed as it sent them, and compress nothing more in the review.

    Like Hermes deferring fork compaction, compressing during the review would rewrite the cached conversation and
    shorten the skill files the review just loaded before it patches them.
    """

    async def ashould_compress(self, *_args: object, **_kwargs: object) -> bool:
        return False


def _review_input_budget_tokens(config: Config, model_name: str) -> int:
    """Return the aggregate input budget of one review."""
    context_window = config.models[model_name].context_window
    if context_window is None:
        return _FALLBACK_INPUT_TOKENS
    return min(_MAX_INPUT_TOKENS, int(context_window * _INPUT_CONTEXT_FRACTION))


def _review_tools(
    schemas: Iterable[Function | dict[str, Any]],
    tools: SkillTools,
) -> tuple[list[Function | dict[str, Any]], list[str], bool]:
    """Keep every tool definition of the agent's request, but run only the skill tools, as the review's.

    Each copy keeps its definition's fields, so the request's tools stay byte-identical. Like Hermes' denial message,
    every other tool answers with the skill tools the review can use. A tool that needs approval or external execution
    stays a plain definition, which Agno answers with "The requested tool does not exist or is not available." instead of
    pausing the review. Returns the tools, the skill tools that run, and whether a skill tool needs approval.
    """
    # The copies skip Agno's entrypoint processing to keep their schemas, so the skill tools validate their own
    # arguments as Agno would, such as an action outside the enum or a string "false" for replace_all.
    entrypoints: dict[str, Callable[..., Any]] = {
        name: validate_call(entrypoint)
        for name, entrypoint in (
            ("get_skill_instructions", tools.get_skill_instructions),
            ("get_skill_reference", tools.get_skill_reference),
            ("get_skill_script", tools.get_skill_script),
            ("skill_manage", tools.skill_manage),
        )
    }
    skill_tools = [tool for tool in schemas if isinstance(tool, Function) and tool.name in entrypoints]
    runnable = [tool.name for tool in skill_tools if not (tool.requires_confirmation or tool.external_execution)]

    async def deny(**_arguments: object) -> str:
        verb = "runs" if len(runnable) == 1 else "run"
        return f"This tool is not available during a skill review; only {_listing(runnable)} {verb} here."

    review_tools: list[Function | dict[str, Any]] = []
    for tool in schemas:
        if not isinstance(tool, Function):
            review_tools.append(tool)
        elif tool.requires_confirmation or tool.external_execution:
            review_tools.append({"type": "function", "function": tool.to_dict()})
        else:
            entrypoint = entrypoints.get(tool.name, deny)
            review_tools.append(Function(**tool.to_dict(), entrypoint=entrypoint, skip_entrypoint_processing=True))
    return review_tools, runnable, len(runnable) < len(skill_tools)


def _listing(names: Sequence[str]) -> str:
    return ", ".join(names[:-1]) + f", and {names[-1]}" if len(names) > 1 else "".join(names)


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
    final = captured.messages[-1] if captured.messages else None
    # A loop that stopped after a tool call, or whose last request was refused, left no final answer to continue.
    if final is None or final.role != "assistant" or final.tool_calls or not final.content:
        return None
    sent = _context_tokens(config, captured.model, captured.model_name, final.metrics) if final.metrics else 0
    if sent * _CONVERSATION_BUDGET_SHARE > _review_input_budget_tokens(config, captured.model_name):
        return None
    review_tools, runnable, needs_approval = _review_tools(captured.tools, tools)
    # A review cannot give approval, and a patch without its read tool is always refused, so all skill tools must run;
    # a request made before the agent had any skill offered no reader for the skills the library holds now.
    if needs_approval or "skill_manage" not in runnable:
        return None
    if catalog.entries and "get_skill_instructions" not in runnable:
        return None
    return _ReviewRequest(
        model=captured.model,
        model_name=captured.model_name,
        messages=[*captured.messages, Message(role="user", content=_review_prompt(config, catalog, runnable))],
        tools=review_tools,
        forked=True,
        tool_choice=captured.tool_choice,
        response_format=captured.response_format,
        compressed_tool_results=captured.compressed_tool_results,
    )


def _agent_skill_schemas(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    skills_root: Path,
    catalog: SkillCatalog,
) -> list[Function]:
    """Return the agent's own skill tool definitions for a review that has no request of the agent to fork."""
    functions = [
        *(catalog.skills.get_tools() if catalog.skills is not None else []),
        *SkillManageTools(agent_name, config, runtime_paths, skills_root).get_async_functions().values(),
    ]
    schemas = [function.model_copy() for function in functions]
    for schema in schemas:
        schema.process_entrypoint()
    return schemas


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
        load_agent_session,
        agent_name,
        config,
        runtime_paths,
        session_id,
        execution_identity=identity,
    )
    if session is None:
        return None
    transcript = await asyncio.to_thread(
        render_transcript,
        conversation_messages(session),
        summary=session.summary.summary if session.summary is not None else None,
        budget_chars=_review_input_budget_tokens(config, model_name) * _CHARS_PER_TOKEN // _CONVERSATION_BUDGET_SHARE,
    )
    schemas = await asyncio.to_thread(_agent_skill_schemas, config, runtime_paths, agent_name, skills_root, catalog)
    review_tools, runnable, _needs_approval = _review_tools(schemas, tools)
    model = model_loading.get_model_instance(config, runtime_paths, model_name, execution_identity=identity)
    install_model_call_cap(model, entity_name=agent_name)
    evidence = (
        "The conversation to review is supplied inside <conversation> tags as a transcript of the stored turns.\n"
        f"<conversation>\n{transcript}\n</conversation>"
    )
    return _ReviewRequest(
        model=model,
        model_name=model_name,
        messages=[Message(role="user", content=f"{evidence}\n\n{_review_prompt(config, catalog, runnable)}")],
        tools=review_tools,
        forked=False,
    )


def _context_tokens(config: Config, model: Model, model_name: str, metrics: BaseMetrics) -> int:
    """Return the input that requests reported, counting prompt-cache reads and writes."""
    return (
        context_input_tokens_from_counts(
            input_tokens=metrics.input_tokens,
            cache_read_tokens=metrics.cache_read_tokens,
            cache_write_tokens=metrics.cache_write_tokens,
            provider=model.provider,
            configured_provider=config.models[model_name].provider,
            model_id=model.id,
        )
        or 0
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
    budget_tokens = _review_input_budget_tokens(config, review.model_name)
    invocation_id = uuid4().hex
    # Agno adds the usage of each of the review's requests to the run's metrics.
    metrics = RunMetrics()
    run = RunOutput(
        run_id=invocation_id,
        session_id=session_id,
        model=review.model.id,
        model_provider=review.model.provider,
        metrics=metrics,
    )

    def allow_request() -> bool:
        # Like Hermes, the review ends before its next request once the input it sent reached the budget.
        if (spent := _context_tokens(config, review.model, review.model_name, metrics)) < budget_tokens:
            return True
        logger.info("Skill review reached its input budget", budget_tokens=budget_tokens, spent_tokens=spent)
        return False

    messages = list(review.messages)
    try:
        # The response closed its Claude client; opening one does blocking credential and TLS work, and a client
        # opened while a new response cancels the review must still be closed.
        await run_blocking_until_complete(prewarm_anthropic_async_client, review.model)
        with request_gate(review.model, allow_request):
            await review.model.aresponse(
                messages=messages,
                tools=review.tools,
                tool_choice=review.tool_choice,
                tool_call_limit=_REVIEW_TOOL_CALL_LIMIT,
                response_format=review.response_format,
                run_response=run,
                compression_manager=_SendCompressedResults() if review.compressed_tool_results else None,
            )
    finally:
        await run_coroutine_until_complete(aclose_anthropic_async_client(review.model))
        # Only the review's own replies, whose metrics give the usage report its requests.
        run.messages = messages[len(review.messages) :]
        if run.messages:
            try:
                await _record_usage(run, invocation_id, config, runtime_paths, agent_name, session_id, identity)
            except Exception:
                # Usage is bookkeeping; its failure must never turn a stop or a finished review into a failure.
                logger.exception("Could not record skill review usage", agent=agent_name)
    logger.info(
        "Skill review model run finished",
        agent=agent_name,
        forked=review.forked,
        model_requests=sum(1 for message in run.messages if message.role == "assistant"),
        input_tokens=_context_tokens(config, review.model, review.model_name, metrics),
        cache_read_tokens=metrics.cache_read_tokens,
    )


async def _record_usage(
    run: RunOutput,
    invocation_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    session_id: str,
    identity: ToolExecutionIdentity | None,
) -> None:
    """Count the review's usage against the conversation it reviewed, also when a timeout or a stop ended it."""
    await record_helper_usage(
        run,
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
