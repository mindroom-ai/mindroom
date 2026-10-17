"""Turn stored runs into graph episodes.

Episodes come only from what was said and what tools returned, never from text the model writes for the graph.
Each episode has one kind, because Graphiti records provenance per episode: ``conversation`` holds what people and the
agent said, labeled by speaker, and ``tool_results`` holds what the tools the model chose returned.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from mindroom.automations.steps import AUTOMATION_HOOK_PREFIX, is_automation_hook_source
from mindroom.constants import HOOK_SOURCE_RUN_METADATA_KEY
from mindroom.history.conversation_text import conversation_lines, redact_conversation_text, run_requester
from mindroom.knowledge_graph.store import EpisodeInput, EpisodeKind
from mindroom.redaction import redact_sensitive_data

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agno.models.response import ToolExecution
    from agno.run.agent import RunOutput

    from mindroom.config.knowledge_graph import KnowledgeGraphIngestConfig

type _ThreadKind = Literal["conversation", "exploration", "delegation", "automation"]
type Progress = tuple[int, str, int]

# The progress of a run that is fully covered, larger than any count of lines a split covers.
RUN_DONE = 1 << 30

# The knowledge_graph tool's functions; their results came from the graph, so they are never evidence for it.
_GRAPH_FUNCTIONS = ("search_graph", "graph_entity")
# Agno's reasoning functions return the model's own thoughts, and subagents, workflow participants, and Claude Code answer
# in another model's prose; a subagent's own tool results come in through its own session. A background job's collected
# result no longer says which tool produced it, so it could be either.
_PROSE_FUNCTIONS = (
    "think",
    "analyze",
    "run_subagent",
    "continue_subagent",
    "run_workflow",
    "get_workflow_run",
    "claude_send",
    "job",
)
_MAX_LISTED_CALLS = 20
# A task delegated by another agent runs in a session of its own, named after the delegation.
_DELEGATION_SESSION_PREFIX = "delegate:"


@dataclass(frozen=True)
class RenderedEpisode:
    """One episode, how far into its conversation it reaches, and whose turns it holds."""

    episode: EpisodeInput
    # `(created_at, run_id, lines)`: how many lines of its newest run it covers, or RUN_DONE once it covers them all.
    progress: Progress
    requester_id: str | None


def is_evidence(execution: ToolExecution) -> bool:
    """Return whether a tool call observed something, rather than returning the graph or a model's prose."""
    if execution.tool_name in (*_GRAPH_FUNCTIONS, *_PROSE_FUNCTIONS):
        return False
    # A shell or `mindroom-agent` call can look the graph up too, and returns its facts as the command's output; the
    # whole arguments are searched, since the rendered ones are clipped.
    arguments = json.dumps(execution.tool_args or {}, default=str)
    return not any(name in arguments for name in _GRAPH_FUNCTIONS)


def run_key(run: RunOutput) -> tuple[int, str]:
    """Return a run's position in its session."""
    return (run.created_at or 0, run.run_id or "")


def classify_thread(runs: Sequence[RunOutput], automation_thread_ids: set[str]) -> _ThreadKind:
    """Return what a thread is, from its session, its root run's trusted hook source, or the threads automations recorded."""
    if not runs:
        return "conversation"
    root = min(runs, key=run_key)
    if (root.session_id or "").startswith(_DELEGATION_SESSION_PREFIX):
        return "delegation"
    metadata = root.metadata or {}
    hook_source = metadata.get(HOOK_SOURCE_RUN_METADATA_KEY)
    if hook_source == f"{AUTOMATION_HOOK_PREFIX}exploration":
        return "exploration"
    if isinstance(hook_source, str) and is_automation_hook_source(hook_source):
        return "automation"
    # LEGACY_COMPAT: Automation runs stored without their hook source.
    # Legacy format: Run metadata had no `hook_source` key, so a stored automation run looks like a conversation.
    # Last legacy release: v2026.10.315; replacement: the next release stores the trusted hook source on every run.
    # Handling: A thread whose root was recorded in `tracking/automations/threads.json` counts as an automation thread.
    # Coverage: tests/test_knowledge_graph_episodes.py::test_classify_thread_uses_the_root_hook_source_then_automation_threads.
    if metadata.get("thread_id") in automation_thread_ids:
        return "automation"
    return "conversation"


def render_episodes(
    runs: Sequence[RunOutput],
    *,
    thread_kind: _ThreadKind,
    episode_kind: EpisodeKind,
    agent_name: str,
    settings: KnowledgeGraphIngestConfig,
    agent_for_sender: Callable[[str], str | None],
) -> list[RenderedEpisode]:
    """Render one kind of episode from runs, oldest first, at most ``max_episode_chars`` each, split at run boundaries.

    ``agent_for_sender`` names the configured agent behind a Matrix ID, so agents' messages are not labeled as people's.
    """
    # Exploration and delegated prompts come from an agent, and their answers restate the tool results.
    if thread_kind == "automation" or (episode_kind == "conversation" and thread_kind in ("exploration", "delegation")):
        return []
    rendered: list[RenderedEpisode] = []
    lines: list[str] = []
    calls: list[str] = []
    newest: RunOutput | None = None
    progress: Progress | None = None

    def flush() -> None:
        if lines and newest is not None and progress is not None:
            episode = _episode(episode_kind, lines, newest, settings, calls)
            rendered.append(RenderedEpisode(episode, progress, run_requester(newest)))
        lines.clear()
        calls.clear()

    cap = settings.max_episode_chars
    for run in sorted(runs, key=run_key):
        items = _episode_items(run, episode_kind, agent_name, settings, agent_for_sender)
        if not items:
            continue
        # Runs stay whole where they fit and share an episode only with the same day and requester, so relative dates
        # resolve against the right day and each episode's cost belongs to one person.
        if (
            lines
            and newest is not None
            and (
                _size(lines) + _size([line for line, _ in items]) > cap
                or _day(run) != _day(newest)
                or run_requester(run) != run_requester(newest)
            )
        ):
            flush()
        for covered, (line, call) in enumerate(items):
            # Only a run larger than the cap is split; each piece records how many of the run's lines it reaches, so
            # pieces cut at another size after a settings change resume where the old ones stopped.
            if lines and _size(lines) + len(line) + 1 > cap:
                progress = (*run_key(run), covered)
                flush()
            lines.append(line)
            if call is not None:
                calls.append(call)
            newest = run
        progress = (*run_key(run), RUN_DONE)
    flush()
    return rendered


def _day(run: RunOutput) -> str:
    return datetime.fromtimestamp(run.created_at or 0, UTC).date().isoformat()


def _episode_items(
    run: RunOutput,
    episode_kind: EpisodeKind,
    agent_name: str,
    settings: KnowledgeGraphIngestConfig,
    agent_for_sender: Callable[[str], str | None],
) -> list[tuple[str, str | None]]:
    """Return a run's lines of one kind, each tool result with the call it lists in the episode description."""
    if episode_kind == "conversation":
        lines = conversation_lines(
            run,
            agent_name=agent_name,
            agent_for_sender=agent_for_sender,
            max_line_chars=settings.max_episode_chars,
        )
        return [(line, None) for line in lines]
    executions = [execution for execution in run.tools or [] if is_evidence(execution)]
    return [
        (_tool_line(execution, settings), f"{execution.tool_name} {_digest(execution)}") for execution in executions
    ]


def _episode(
    kind: EpisodeKind,
    lines: list[str],
    last_run: RunOutput,
    settings: KnowledgeGraphIngestConfig,
    calls: Sequence[str],
) -> EpisodeInput:
    metadata = last_run.metadata or {}
    description = f"{kind}: room {metadata.get('room_id')} thread {metadata.get('thread_id')}"
    if calls:
        listed = ", ".join(calls[:_MAX_LISTED_CALLS])
        more = f" and {len(calls) - _MAX_LISTED_CALLS} more" if len(calls) > _MAX_LISTED_CALLS else ""
        description = f"{description}; calls: {listed}{more}"
    return EpisodeInput(
        kind=kind,
        name=f"{kind} {last_run.session_id} {last_run.run_id}",
        body="\n".join(lines)[: settings.max_episode_chars],
        source_description=description,
        reference_time=datetime.fromtimestamp(last_run.created_at or 0, UTC),
    )


def _tool_line(execution: ToolExecution, settings: KnowledgeGraphIngestConfig) -> str:
    outcome = "error" if execution.tool_call_error else "result"
    output = redact_conversation_text(str(execution.result or "").strip(), max_chars=settings.max_tool_result_chars)
    # Clipped to half a result, so arguments and result together stay within an episode.
    arguments = tool_arguments(execution)[: settings.max_tool_result_chars // 2]
    return f"call {execution.tool_name}({arguments}) {outcome}: {output}"


def tool_arguments(execution: ToolExecution) -> str:
    """Return a tool call's arguments as canonical JSON with credential-like values redacted."""
    redacted = redact_sensitive_data(execution.tool_args or {}, max_string_length=500)
    return json.dumps(redacted, sort_keys=True, default=str)


def _digest(execution: ToolExecution) -> str:
    return hashlib.sha256(tool_arguments(execution).encode()).hexdigest()[:12]


def _size(lines: Sequence[str]) -> int:
    return sum(len(line) + 1 for line in lines)
