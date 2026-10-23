# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""The team reply as the user reads it: the header, one slot per member, the consensus, and the tool trace.

``TeamStreamPresentation`` is the structured document a streamed or collected team run renders from and the durable
``team_stream`` snapshot an approval continuation restores. Member slots are keyed by ``url_safe_member_id`` of the
member's config name, the id Agno derived for its members, which stored snapshots and reply records carry.
``publish_team_presentation`` shows a resumed run's document live in the reply it continues. ``format_team_response``
renders a finished run from its records instead, as Agno's blocking team reply did.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from mindroom.agent_loop.run import own_answers
from mindroom.delegation.state import member_slot_id
from mindroom.llm.events import ContentDelta, ToolCompleted, ToolStarted
from mindroom.matrix.message_builder import opens_with_markdown_block
from mindroom.teams.runtime import MemberEvent
from mindroom.tool_system.events import (
    StreamingToolTracker,
    StructuredStreamChunk,
    ToolTraceEntry,
    append_stream_text,
    complete_pending_tool_block,
    is_visible_tool_marker_line,
    remap_visible_tool_marker_indices,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mindroom.llm.runs import AnyRunRecord, TeamRunRecord, ToolExecution
    from mindroom.streaming import ProgressPublisher
    from mindroom.teams.runtime import TeamRunEvent


def url_safe_member_id(input_string: str) -> str:
    """Return Agno's ``url_safe_string`` of a member's config name, the member id of stored reply snapshots."""
    # Replace spaces with dashes
    safe_string = input_string.replace(" ", "-")

    # Convert camelCase to kebab-case
    safe_string = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", safe_string).lower()

    # Convert snake_case to kebab-case
    safe_string = safe_string.replace("_", "-")

    # Remove special characters, keeping alphanumeric, dashes, and dots
    safe_string = re.sub(r"[^\w\-.]", "", safe_string)

    # Ensure no consecutive dashes
    safe_string = re.sub(r"-+", "-", safe_string)

    return safe_string  # noqa: RET504 - kept as Agno's ``url_safe_string`` reads, statement for statement.


_TEAM_HEADER_PREFIX = "🤝 **Team Response** ("
_TEAM_HEADER_END = "):\n\n"
_NO_CONSENSUS_NOTE = "\n*No team consensus - showing individual responses only*"
NO_TEAM_RESPONSE = "No team response generated."


def format_team_header(agent_names: list[str]) -> str:
    """Format the team response header.

    Args:
        agent_names: List of agent names in the team

    Returns:
        Formatted header string

    """
    return f"{_TEAM_HEADER_PREFIX}{', '.join(agent_names)}{_TEAM_HEADER_END}"


def strip_team_display(text: str) -> str:
    """Remove the display-only header and no-consensus note from a visible team reply."""
    if text.startswith(_TEAM_HEADER_PREFIX):
        _header, separator, body = text.partition(_TEAM_HEADER_END)
        text = body if separator else text
    return text.removesuffix(_NO_CONSENSUS_NOTE).rstrip()


def _format_member_contribution(agent_name: str, content: str) -> str:
    """Return one member's contribution: its bold name, then its content."""
    first_line = content.lstrip().splitlines()[0] if content.strip() else ""
    # Content that opens with a tool marker or a block such as a table needs its own paragraph to render.
    own_paragraph = is_visible_tool_marker_line(first_line) or opens_with_markdown_block(content)
    separator = "\n\n" if own_paragraph else " "
    return f"**{agent_name}**:{separator}{content}"


def _with_consensus(contributions: list[str], consensus: str) -> list[str]:
    """Return the contributions, then the consensus, or the no-consensus note when only members spoke."""
    if consensus.strip():
        return [*contributions, "\n**Team Consensus**:", consensus]
    return [*contributions, _NO_CONSENSUS_NOTE] if contributions else contributions


def render_team_member_slots(
    member_ids: Sequence[str],
    *,
    display_names_by_id: Mapping[str, str],
    per_member: Mapping[str, str],
    consensus: str,
) -> list[str]:
    """Render stable identity slots using labels only as presentation attributes; each slot's text is stripped."""
    contributions = [
        _format_member_contribution(display_names_by_id[member_id], per_member[member_id].strip())
        for member_id in member_ids
        if per_member[member_id].strip()
    ]
    return _with_consensus(contributions, consensus.strip())


def finished_run_text(record: AnyRunRecord) -> str:
    """Return a finished run's text: what it shows (``handover_content``), or else its own answers (``own_answers``).

    A failed run shows why it failed, credentials redacted, and a run without content never shows an earlier reply
    of the thread that its input carried.
    """
    content = record.handover_content
    if content:
        return str(content)
    return "\n\n".join(own_answers(record))


def format_team_response(record: TeamRunRecord) -> str:
    """Return a finished team run's text as Agno's blocking team reply rendered it, without the header.

    Each member run that ended (``ended_member_runs``, in the order they ended) contributes its ``finished_run_text``,
    so a member delegated twice contributes twice and a member whose run failed shows the reason its run recorded,
    credentials redacted, even when it wrote text first. What the leader's run hands over (``handover_content``) is
    the consensus, so a leader that failed shows that reason too. A run in which nobody wrote anything renders as
    empty text.
    """
    contributions = [
        _format_member_contribution(member.agent_name or "Team Member", text)
        for member in record.ended_member_runs
        if (text := finished_run_text(member)).strip()
    ]
    return "\n\n".join(_with_consensus(contributions, str(record.handover_content or "")))


def team_response_text(record: TeamRunRecord) -> str:
    """Return a finished team run's reply without the header: its runs' text, else the leader's, else the no-reply note."""
    return format_team_response(record) or finished_run_text(record) or NO_TEAM_RESPONSE


def _restore_hidden_team_separators(
    state: Mapping[str, object],
    *,
    valid_tool_scopes: set[str],
    show_tool_calls: bool,
    per_member: Mapping[str, str],
    consensus: str,
) -> set[str]:
    """Restore validated hidden-tool boundaries from a durable team snapshot."""
    stored = state.get("separate_next_scopes", [])
    if (
        not isinstance(stored, list)
        or any(not isinstance(scope, str) or scope not in valid_tool_scopes for scope in stored)
        or len(stored) != len(set(stored))
        or (show_tool_calls and stored)
    ):
        msg = "Team continuation presentation snapshot is invalid"
        raise RuntimeError(msg)
    scopes = cast("list[str]", stored)
    if any(not (consensus if scope == "team" else per_member[scope.removeprefix("agent:")]) for scope in scopes):
        msg = "Team continuation presentation snapshot is invalid"
        raise RuntimeError(msg)
    return set(scopes)


@dataclass
class TeamStreamPresentation:
    """Structured team document that survives an approval continuation."""

    member_ids: list[str]
    config_names_by_id: dict[str, str]
    display_names_by_id: dict[str, str]
    show_tool_calls: bool
    per_member: dict[str, str]
    consensus: str = ""
    tool_trace: list[ToolTraceEntry] = field(default_factory=list)
    tool_tracker: StreamingToolTracker = field(default_factory=StreamingToolTracker, init=False)
    separate_next_scopes: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        """Restore pending tool identity from the durable trace."""
        self.tool_tracker.restore_pending(self.tool_trace)
        self.separate_next_scopes = {
            pending.scope_key
            for pending in self.tool_tracker.pending_tools
            if pending.scope_key is not None and self._slot_text(pending.scope_key)
        }

    @property
    def display_names(self) -> list[str]:
        """Return immutable rendering labels in frozen member order."""
        return [self.display_names_by_id[member_id] for member_id in self.member_ids]

    @classmethod
    def new(
        cls,
        config_names: Sequence[str],
        display_names: Sequence[str],
        *,
        show_tool_calls: bool,
    ) -> TeamStreamPresentation:
        """Create an empty presentation for a new team run."""
        frozen_config_names = list(config_names)
        ids = [url_safe_member_id(config_name) for config_name in frozen_config_names]
        names = list(display_names)
        if (
            len(ids) != len(names)
            or len(ids) != len(frozen_config_names)
            or len(ids) != len(set(ids))
            or any(not member_id for member_id in ids)
        ):
            msg = "Team presentation member identities are invalid"
            raise RuntimeError(msg)
        return cls(
            member_ids=ids,
            config_names_by_id=dict(zip(ids, frozen_config_names, strict=True)),
            display_names_by_id=dict(zip(ids, names, strict=True)),
            show_tool_calls=show_tool_calls,
            per_member=dict.fromkeys(ids, ""),
        )

    @classmethod
    def restore(
        cls,
        *,
        config_names: Sequence[str],
        show_tool_calls: bool,
        state: Mapping[str, object] | None,
        tool_trace: Sequence[ToolTraceEntry],
    ) -> TeamStreamPresentation:
        """Restore a durable structured snapshot without interpreting its rendered Markdown."""
        if state is None or state.get("kind") != "team_stream" or state.get("version") != 2:
            msg = "Team continuation presentation snapshot is invalid"
            raise RuntimeError(msg)
        stored_members = state.get("members")
        if not isinstance(stored_members, list):
            msg = "Team continuation presentation snapshot is invalid"
            raise RuntimeError(msg)  # noqa: TRY004
        restored_ids: list[str] = []
        config_names_by_id: dict[str, str] = {}
        display_names_by_id: dict[str, str] = {}
        per_member: dict[str, str] = {}
        for member in stored_members:
            if not isinstance(member, dict):
                msg = "Team continuation presentation snapshot is invalid"
                raise RuntimeError(msg)  # noqa: TRY004
            stored_member = cast("dict[str, object]", member)
            member_id = stored_member.get("id")
            config_name = stored_member.get("config_name")
            display_name = stored_member.get("display_name")
            content = stored_member.get("content")
            if (
                not isinstance(member_id, str)
                or not member_id
                or member_id in per_member
                or not isinstance(config_name, str)
                or not config_name
                or not isinstance(display_name, str)
                or not isinstance(content, str)
            ):
                msg = "Team continuation presentation snapshot is invalid"
                raise RuntimeError(msg)
            restored_ids.append(member_id)
            config_names_by_id[member_id] = config_name
            display_names_by_id[member_id] = display_name
            per_member[member_id] = content
        expected_config_names = list(config_names)
        expected_ids = [url_safe_member_id(config_name) for config_name in expected_config_names]
        if (
            restored_ids != expected_ids
            or len(expected_ids) != len(set(expected_ids))
            or [config_names_by_id[member_id] for member_id in restored_ids] != expected_config_names
        ):
            msg = "Team continuation presentation snapshot is invalid"
            raise RuntimeError(msg)
        valid_tool_scopes = {"team", *(f"agent:{member_id}" for member_id in restored_ids)}
        if any(entry.scope_key not in valid_tool_scopes for entry in tool_trace):
            msg = "Team continuation durable tool scope is not a frozen presentation slot"
            raise RuntimeError(msg)
        consensus = state.get("consensus")
        if not isinstance(consensus, str):
            msg = "Team continuation presentation snapshot is invalid"
            raise RuntimeError(msg)  # noqa: TRY004
        restored_separators = _restore_hidden_team_separators(
            state,
            valid_tool_scopes=valid_tool_scopes,
            show_tool_calls=show_tool_calls,
            per_member=per_member,
            consensus=consensus,
        )
        restored = cls(
            member_ids=restored_ids,
            config_names_by_id=config_names_by_id,
            display_names_by_id=display_names_by_id,
            show_tool_calls=show_tool_calls,
            per_member=per_member,
            consensus=consensus,
            tool_trace=list(deepcopy(tool_trace)),
        )
        restored.separate_next_scopes.update(restored_separators)
        return restored

    def to_state(self) -> dict[str, object]:
        """Serialize only the structure needed to reproduce the current body."""
        state: dict[str, object] = {
            "kind": "team_stream",
            "version": 2,
            "members": [
                {
                    "id": member_id,
                    "config_name": self.config_names_by_id[member_id],
                    "display_name": self.display_names_by_id[member_id],
                    "content": self.per_member[member_id],
                }
                for member_id in self.member_ids
            ],
            "consensus": self.consensus,
        }
        if not self.show_tool_calls and self.separate_next_scopes:
            document_scopes = [*(f"agent:{member_id}" for member_id in self.member_ids), "team"]
            state["separate_next_scopes"] = [scope for scope in document_scopes if scope in self.separate_next_scopes]
        return state

    def resolve_member_id(self, agent_id: str | None, reported_name: str | None) -> str:
        """Resolve an event or a member run only through its stable member identity, by ``member_slot_id``.

        A run record's member identity is optional, and a run that names no member has no slot.
        """
        return member_slot_id(self.config_names_by_id, agent_id, reported_name)

    def append_member(self, member_id: str, content: object | None, *, separate: bool = False) -> None:
        """Append a member content delta to the slot of ``member_id``, which ``resolve_member_id`` returned.

        ``separate`` starts the content in a paragraph of its own when the slot already holds text.
        """
        scope_key = f"agent:{member_id}"
        self.per_member[member_id] = append_stream_text(
            self.per_member[member_id],
            content,
            separate=separate or scope_key in self.separate_next_scopes,
        )
        if content:
            self.separate_next_scopes.discard(scope_key)

    def append_consensus(self, content: object | None, *, separate: bool = False) -> None:
        """Append a coordinator content delta to the consensus slot; ``separate`` as for ``append_member``."""
        self.consensus = append_stream_text(
            self.consensus,
            content,
            separate=separate or "team" in self.separate_next_scopes,
        )
        if content:
            self.separate_next_scopes.discard("team")

    def _append_tool_marker(self, scope_key: str, marker: str) -> None:
        if scope_key == "team":
            self.consensus += marker
            return
        member_id = scope_key.removeprefix("agent:")
        self.per_member[member_id] += marker

    def _slot_text(self, scope_key: str) -> str:
        if scope_key == "team":
            return self.consensus
        return self.per_member.get(scope_key.removeprefix("agent:"), "")

    def _set_slot_text(self, scope_key: str, text: str) -> None:
        if scope_key == "team":
            self.consensus = text
            return
        self.per_member[scope_key.removeprefix("agent:")] = text

    def _align_tool_trace_with_document(self) -> None:
        """Keep trace slots and visible indices in rendered document order."""
        scope_order = {
            **{f"agent:{member_id}": index for index, member_id in enumerate(self.member_ids)},
            "team": len(self.member_ids),
        }
        old_indices = {id(trace_entry): index for index, trace_entry in enumerate(self.tool_trace, start=1)}
        self.tool_trace.sort(key=lambda trace_entry: scope_order[trace_entry.scope_key or "team"])
        index_map = {
            old_indices[id(trace_entry)]: new_index for new_index, trace_entry in enumerate(self.tool_trace, start=1)
        }
        if self.show_tool_calls:
            self.per_member = {
                member_id: remap_visible_tool_marker_indices(member_text, index_map)
                for member_id, member_text in self.per_member.items()
            }
            self.consensus = remap_visible_tool_marker_indices(self.consensus, index_map)
        self.tool_tracker.sync_visible_indices(self.tool_trace)

    def start_tool(self, scope_key: str, tool: ToolExecution | None) -> None:
        """Record a tool start in its exact member or coordinator slot."""
        tool_index = len(self.tool_trace) + 1
        marker, trace_entry = self.tool_tracker.start(tool, scope_key=scope_key, tool_index=tool_index)
        if trace_entry is None:
            return
        trace_entry.scope_key = scope_key
        self.tool_trace.append(trace_entry)
        if self.show_tool_calls:
            self._append_tool_marker(scope_key, marker)
        elif self._slot_text(scope_key):
            self.separate_next_scopes.add(scope_key)
        self._align_tool_trace_with_document()

    def complete_tool(self, scope_key: str, tool: ToolExecution | None) -> None:
        """Complete only the pending slot identified by this tool event."""
        completion = self.tool_tracker.complete(tool, scope_key=scope_key)
        if completion is None:
            return
        tool_name, result, pending_tool, completed_trace = completion
        if pending_tool is None or pending_tool.visible_tool_index is None or completed_trace is None:
            return
        completed_trace.scope_key = pending_tool.scope_key or scope_key
        if self.show_tool_calls:
            updated_text, _ = complete_pending_tool_block(
                self._slot_text(pending_tool.scope_key),
                tool_name,
                result,
                tool_index=pending_tool.visible_tool_index,
            )
            self._set_slot_text(pending_tool.scope_key, updated_text)
        self.tool_tracker.update_visible_trace_entry(self.tool_trace, pending_tool, completed_trace)

    def start_member_tool(self, member_id: str, tool: ToolExecution | None) -> None:
        """Record a tool start in the slot of ``member_id``, which ``resolve_member_id`` returned."""
        self.start_tool(f"agent:{member_id}", tool)

    def complete_member_tool(self, member_id: str, tool: ToolExecution | None) -> None:
        """Record a tool completion in the slot of ``member_id``, which ``resolve_member_id`` returned."""
        self.complete_tool(f"agent:{member_id}", tool)

    def render_body(self) -> str:
        """Render the current structured document."""
        parts = render_team_member_slots(
            self.member_ids,
            display_names_by_id=self.display_names_by_id,
            per_member=self.per_member,
            consensus=self.consensus,
        )
        return format_team_header(self.display_names) + "\n\n".join(parts) if parts else ""


def apply_team_event(presentation: TeamStreamPresentation, event: TeamRunEvent) -> None:
    """Apply one team run event to the document: member events to the member's slot, the leader's to the consensus."""
    if isinstance(event, MemberEvent):
        member_id = presentation.resolve_member_id(event.member_id, event.member_name)
        inner = event.event
        if isinstance(inner, ContentDelta):
            presentation.append_member(member_id, inner.text)
        elif isinstance(inner, ToolStarted):
            presentation.start_member_tool(member_id, inner.tool)
        elif isinstance(inner, ToolCompleted):
            presentation.complete_member_tool(member_id, inner.tool)
    elif isinstance(event, ContentDelta):
        presentation.append_consensus(event.text)
    elif isinstance(event, ToolStarted):
        presentation.start_tool("team", event.tool)
    elif isinstance(event, ToolCompleted):
        presentation.complete_tool("team", event.tool)


async def publish_team_presentation(
    progress: ProgressPublisher | None,
    presentation: TeamStreamPresentation,
) -> None:
    """Show the team continuation's current document in the reply it resumes."""
    if progress is not None:
        await progress(
            StructuredStreamChunk(content=presentation.render_body(), tool_trace=presentation.tool_trace),
        )


__all__ = [
    "NO_TEAM_RESPONSE",
    "TeamStreamPresentation",
    "apply_team_event",
    "finished_run_text",
    "format_team_header",
    "format_team_response",
    "publish_team_presentation",
    "render_team_member_slots",
    "strip_team_display",
    "team_response_text",
    "url_safe_member_id",
]
