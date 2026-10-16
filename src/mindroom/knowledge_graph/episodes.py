"""Turn stored runs into graph episodes.

Episodes come only from what was said and what tools returned, never from text the model writes for the graph.
Each episode has one kind, because Graphiti records provenance per episode: ``conversation`` holds what people and the
agent said, labeled by speaker, and ``tool_results`` holds what the tools the model chose returned.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from mindroom.automations.exploration_policy import EXPLORATION_HOOK_SOURCE
from mindroom.automations.steps import is_automation_hook_source
from mindroom.constants import HOOK_SOURCE_RUN_METADATA_KEY
from mindroom.knowledge_graph.store import EpisodeInput
from mindroom.redaction import redact_sensitive_data, redact_sensitive_text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agno.models.response import ToolExecution
    from agno.run.agent import RunOutput

    from mindroom.config.knowledge_graph import KnowledgeGraphIngestConfig

type _ThreadKind = Literal["conversation", "exploration", "automation"]
type _RunKey = tuple[int, str]

# The knowledge_graph tool's functions; their results came from the graph, so they are never evidence for it.
KNOWLEDGE_GRAPH_FUNCTIONS = frozenset({"search_graph", "graph_entity"})
_MSG_TAG = re.compile(r"<msg\b([^>]*)><!\[CDATA\[(.*?)\]\]></msg>", re.DOTALL)
_FROM_ATTR = re.compile(r"""\bfrom=(["'])(.*?)\1""")
_CONTAINER_TAG = re.compile(r"</?(?:messages|queued_messages|conversation)>")
_MAX_LISTED_CALLS = 20


@dataclass(frozen=True)
class _RenderedEpisode:
    """One episode and the last run it covers."""

    episode: EpisodeInput
    last_run: _RunKey


def run_key(run: RunOutput) -> _RunKey:
    """Return a run's position in its session."""
    return (run.created_at or 0, run.run_id or "")


def classify_thread(runs: Sequence[RunOutput], automation_thread_ids: set[str]) -> _ThreadKind:
    """Return what a thread is, from its root run's trusted hook source or the threads automations recorded."""
    if not runs:
        return "conversation"
    root = min(runs, key=run_key)
    metadata = root.metadata or {}
    hook_source = metadata.get(HOOK_SOURCE_RUN_METADATA_KEY)
    if hook_source == EXPLORATION_HOOK_SOURCE:
        return "exploration"
    if isinstance(hook_source, str) and is_automation_hook_source(hook_source):
        return "automation"
    # LEGACY_COMPAT: Automation runs stored without their hook source.
    # Legacy format: Run metadata had no `hook_source` key, so a stored automation run looks like a conversation.
    # Last legacy release: v2026.10.306; replacement: the next release stores the trusted hook source on every run.
    # Handling: A thread whose root was recorded in `tracking/automations/threads.json` counts as an automation thread.
    # Coverage: tests/test_knowledge_graph_episodes.py::test_classify_thread_uses_the_root_hook_source_then_automation_threads.
    if metadata.get("thread_id") in automation_thread_ids:
        return "automation"
    return "conversation"


def render_episodes(
    runs: Sequence[RunOutput],
    *,
    thread_kind: _ThreadKind,
    episode_kind: Literal["conversation", "tool_results"],
    agent_name: str,
    settings: KnowledgeGraphIngestConfig,
) -> list[_RenderedEpisode]:
    """Render one kind of episode from runs, oldest first, at most ``max_episode_chars`` each, split at run boundaries."""
    if thread_kind == "automation" or (episode_kind == "conversation" and thread_kind == "exploration"):
        return []
    rendered: list[_RenderedEpisode] = []
    lines: list[str] = []
    calls: list[str] = []
    last: RunOutput | None = None

    def flush() -> None:
        if lines and last is not None:
            rendered.append(_RenderedEpisode(_episode(episode_kind, lines, last, settings, calls=calls), run_key(last)))
        lines.clear()
        calls.clear()

    for run in sorted(runs, key=run_key):
        if episode_kind == "conversation":
            run_lines, run_calls = _conversation_lines(run, agent_name, settings), []
        else:
            executions = [
                execution for execution in run.tools or [] if execution.tool_name not in KNOWLEDGE_GRAPH_FUNCTIONS
            ]
            run_lines = [_tool_line(execution, settings) for execution in executions]
            run_calls = [f"{execution.tool_name} {_digest(execution)}" for execution in executions]
        if lines and _size(lines) + _size(run_lines) > settings.max_episode_chars:
            flush()
        lines.extend(run_lines)
        calls.extend(run_calls)
        last = run
    flush()
    return rendered


def _episode(
    kind: Literal["conversation", "tool_results"],
    lines: list[str],
    last_run: RunOutput,
    settings: KnowledgeGraphIngestConfig,
    *,
    calls: Sequence[str] = (),
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


def _conversation_lines(run: RunOutput, agent_name: str, settings: KnowledgeGraphIngestConfig) -> list[str]:
    requester = (run.metadata or {}).get("requester_id") or run.user_id or "unknown"
    lines: list[str] = []
    for message in run.messages or []:
        if message.from_history or not isinstance(message.content, str) or not message.content.strip():
            continue
        if message.role == "user":
            lines.extend(
                f"person {sender}: {_clean(body, settings.max_episode_chars)}"
                for sender, body in _prompt_messages(message.content, str(requester))
            )
        elif message.role == "assistant":
            lines.append(f"agent {agent_name}: {_clean(message.content, settings.max_episode_chars)}")
    return lines


def _prompt_messages(content: str, requester: str) -> list[tuple[str, str]]:
    """Return each ``<msg>`` tag's sender and body, or the whole prompt as the requester's."""
    tagged = [
        (sender.group(2) if (sender := _FROM_ATTR.search(attrs)) else requester, body.replace("]]]]><![CDATA[>", "]]>"))
        for attrs, body in _MSG_TAG.findall(content)
    ]
    if tagged:
        return [(html.unescape(sender), body) for sender, body in tagged]
    return [(requester, _CONTAINER_TAG.sub("", content))]


def _tool_line(execution: ToolExecution, settings: KnowledgeGraphIngestConfig) -> str:
    outcome = "error" if execution.tool_call_error else "result"
    output = _clean(str(execution.result or ""), settings.max_tool_result_chars)
    return f"call {execution.tool_name}({tool_arguments(execution)}) {outcome}: {output}"


def tool_arguments(execution: ToolExecution) -> str:
    """Return a tool call's arguments as canonical JSON with credential-like values redacted."""
    redacted = redact_sensitive_data(execution.tool_args or {}, max_string_length=500)
    return json.dumps(redacted, sort_keys=True, default=str)


def _digest(execution: ToolExecution) -> str:
    return hashlib.sha256(tool_arguments(execution).encode()).hexdigest()[:12]


def _clean(text: str, limit: int) -> str:
    # Redaction scans a little past the limit, so a credential cut at the edge is still caught, and keeps its input
    # under the redaction size limit.
    return redact_sensitive_text(text.strip(), max_length=limit)


def _size(lines: Sequence[str]) -> int:
    return sum(len(line) + 1 for line in lines)
