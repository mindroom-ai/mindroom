# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""The team reply as the user reads it: the header, one slot per member, the consensus, and the tool trace.

``TeamStreamPresentation`` is the structured document a streamed or collected team run renders from and the durable
``team_stream`` snapshot an approval continuation restores. Member slots are keyed by ``url_safe_member_id`` of the
member's config name, the id stored snapshots and reply records carry. ``publish_team_presentation`` shows a resumed
run's document live in the reply it continues. ``format_team_response`` renders a finished run from its records
instead, as a blocking reply shows it, kept as main behaves.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from mindroom.agent_loop.run import own_answers
from mindroom.delegation.state import member_slot_id
from mindroom.llm.events import TextDelta, ToolCompleted, ToolStarted
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

    from mindroom.llm.events import ReportedCall
    from mindroom.llm.runs import AnyRunRecord, TeamRunRecord
    from mindroom.streaming import ProgressPublisher
    from mindroom.teams.runtime import TeamRunEvent


def url_safe_member_id(input_string: str) -> str:
    """Return Agno's ``url_safe_string`` of a member's config name, the member id of stored reply snapshots.

    Spaces, camelCase boundaries, and underscores become dashes, punctuation other than dots goes, dash runs collapse.
    """
    kebab = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", input_string.replace(" ", "-")).lower().replace("_", "-")
    return re.sub(r"-+", "-", re.sub(r"[^\w\-.]", "", kebab))


_TEAM_HEADER_PREFIX = "🤝 **Team Response** ("
_TEAM_HEADER_END = "):\n\n"
_NO_CONSENSUS_NOTE = "\n*No team consensus - showing individual responses only*"
NO_TEAM_RESPONSE = "No team response generated."


def format_team_header(agent_names: list[str]) -> str:
    """Return the team reply's header naming ``agent_names``."""
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
    return record.handover_content or "\n\n".join(own_answers(record))


def format_team_response(record: TeamRunRecord) -> str:
    """Return a finished team run's text as a blocking team reply shows it, without the header.

    Each member run that ended (``ended_member_runs``, in the order they ended) contributes its ``finished_run_text``,
    so a member delegated twice contributes twice and a member whose run failed shows the reason its run recorded,
    credentials redacted, even when it wrote text first. What the leader's run hands over (``handover_content``) is
    the consensus, so a leader that failed shows that reason too. A run in which nobody wrote anything renders as
    empty text.
    """
    # TODO(simplify): A blocking reply lists member runs in the order they ended, one entry per delegation, while a
    # streamed reply shows one slot per member in configured order. Render both from the member slots in configured
    # order. Users, API clients, and replayed history would notice.
    contributions = [
        _format_member_contribution(member.agent_display_name or "Team Member", text)
        for member in record.ended_member_runs
        if (text := finished_run_text(member)).strip()
    ]
    return "\n\n".join(_with_consensus(contributions, record.handover_content or ""))


_INVALID_SNAPSHOT = "Team continuation presentation snapshot is invalid"


def _is_snapshot_member(member: object) -> bool:
    """Return whether one stored member slot holds text fields, its id and config name non-empty."""
    if not isinstance(member, dict):
        return False
    fields = cast("dict[str, object]", member)
    keys = ("id", "config_name", "display_name", "content")
    return all(isinstance(fields.get(key), str) for key in keys) and bool(fields["id"]) and bool(fields["config_name"])


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

    @classmethod
    def new(
        cls,
        config_names: Sequence[str],
        display_names: Sequence[str],
        *,
        show_tool_calls: bool,
    ) -> TeamStreamPresentation:
        """Create an empty presentation for a new team run."""
        ids = [url_safe_member_id(config_name) for config_name in config_names]
        if len(ids) != len(display_names) or len(ids) != len(set(ids)) or not all(ids):
            msg = "Team presentation member identities are invalid"
            raise RuntimeError(msg)
        return cls(
            member_ids=ids,
            config_names_by_id=dict(zip(ids, config_names, strict=True)),
            display_names_by_id=dict(zip(ids, display_names, strict=True)),
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
        """Restore a durable structured snapshot without interpreting its rendered Markdown.

        The snapshot must hold the slots of ``config_names`` in order, and every tool call of ``tool_trace`` one of
        them; a hidden tool call separates only slot text written before it.
        """
        if state is None or state.get("kind") != "team_stream" or state.get("version") != 2:
            raise RuntimeError(_INVALID_SNAPSHOT)
        stored_members = state.get("members")
        if not isinstance(stored_members, list) or not all(_is_snapshot_member(member) for member in stored_members):
            raise RuntimeError(_INVALID_SNAPSHOT)
        members = cast("list[dict[str, str]]", stored_members)
        member_ids = [member["id"] for member in members]
        if (
            member_ids != [url_safe_member_id(config_name) for config_name in config_names]
            or len(member_ids) != len(set(member_ids))
            or [member["config_name"] for member in members] != list(config_names)
        ):
            raise RuntimeError(_INVALID_SNAPSHOT)
        valid_tool_scopes = {"team", *(f"agent:{member_id}" for member_id in member_ids)}
        if any(entry.scope_key not in valid_tool_scopes for entry in tool_trace):
            msg = "Team continuation durable tool scope is not a frozen presentation slot"
            raise RuntimeError(msg)
        per_member = {member["id"]: member["content"] for member in members}
        consensus = state.get("consensus")
        separators = state.get("separate_next_scopes", [])
        if (
            not isinstance(consensus, str)
            or not isinstance(separators, list)
            or any(not isinstance(scope, str) or scope not in valid_tool_scopes for scope in separators)
        ):
            raise RuntimeError(_INVALID_SNAPSHOT)
        scopes = cast("list[str]", separators)
        if (
            len(scopes) != len(set(scopes))
            or (show_tool_calls and scopes)
            or any(not (consensus if scope == "team" else per_member[scope.removeprefix("agent:")]) for scope in scopes)
        ):
            raise RuntimeError(_INVALID_SNAPSHOT)
        restored = cls(
            member_ids=member_ids,
            config_names_by_id={member["id"]: member["config_name"] for member in members},
            display_names_by_id={member["id"]: member["display_name"] for member in members},
            show_tool_calls=show_tool_calls,
            per_member=per_member,
            consensus=consensus,
            tool_trace=list(deepcopy(tool_trace)),
        )
        restored.separate_next_scopes.update(scopes)
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
        self._append_text(f"agent:{member_id}", content, separate=separate)

    def append_consensus(self, content: object | None, *, separate: bool = False) -> None:
        """Append a coordinator content delta to the consensus slot; ``separate`` as for ``append_member``."""
        self._append_text("team", content, separate=separate)

    def _append_text(self, scope_key: str, content: object | None, *, separate: bool) -> None:
        """Append to a slot; text after a hidden tool call in that slot starts a paragraph of its own."""
        separate = separate or scope_key in self.separate_next_scopes
        self._set_slot_text(scope_key, append_stream_text(self._slot_text(scope_key), content, separate=separate))
        if content:
            self.separate_next_scopes.discard(scope_key)

    def _slot_text(self, scope_key: str) -> str:
        if scope_key == "team":
            return self.consensus
        return self.per_member[scope_key.removeprefix("agent:")]

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

    def start_tool(self, scope_key: str, call: ReportedCall) -> None:
        """Record a tool start in its exact member or coordinator slot."""
        tool_index = len(self.tool_trace) + 1
        marker, trace_entry = self.tool_tracker.start(call, scope_key=scope_key, tool_index=tool_index)
        if trace_entry is None:
            return
        trace_entry.scope_key = scope_key
        self.tool_trace.append(trace_entry)
        if self.show_tool_calls:
            self._set_slot_text(scope_key, self._slot_text(scope_key) + marker)
        elif self._slot_text(scope_key):
            self.separate_next_scopes.add(scope_key)
        self._align_tool_trace_with_document()

    def complete_tool(self, scope_key: str, call: ReportedCall) -> None:
        """Complete only the pending slot identified by this tool event."""
        tool_name, result, pending_tool, completed_trace = self.tool_tracker.complete(call, scope_key=scope_key)
        if pending_tool is None or pending_tool.visible_tool_index is None:
            return
        completed_trace.scope_key = scope_key
        if self.show_tool_calls:
            updated_text, _ = complete_pending_tool_block(
                self._slot_text(scope_key),
                tool_name,
                result,
                tool_index=pending_tool.visible_tool_index,
            )
            self._set_slot_text(scope_key, updated_text)
        self.tool_tracker.update_visible_trace_entry(self.tool_trace, pending_tool, completed_trace)

    def start_member_tool(self, member_id: str, call: ReportedCall) -> None:
        """Record a tool start in the slot of ``member_id``, which ``resolve_member_id`` returned."""
        self.start_tool(f"agent:{member_id}", call)

    def complete_member_tool(self, member_id: str, call: ReportedCall) -> None:
        """Record a tool completion in the slot of ``member_id``, which ``resolve_member_id`` returned."""
        self.complete_tool(f"agent:{member_id}", call)

    def render_body(self) -> str:
        """Render the current structured document."""
        parts = render_team_member_slots(
            self.member_ids,
            display_names_by_id=self.display_names_by_id,
            per_member=self.per_member,
            consensus=self.consensus,
        )
        header = format_team_header([self.display_names_by_id[member_id] for member_id in self.member_ids])
        return header + "\n\n".join(parts) if parts else ""


def apply_team_event(presentation: TeamStreamPresentation, event: TeamRunEvent) -> None:
    """Apply one team run event to the document: member events to the member's slot, the leader's to the consensus."""
    if isinstance(event, MemberEvent):
        member_id = presentation.resolve_member_id(event.member_id, event.member_name)
        inner = event.event
        if isinstance(inner, TextDelta):
            presentation.append_member(member_id, inner.text)
        elif isinstance(inner, ToolStarted):
            presentation.start_member_tool(member_id, inner.call)
        elif isinstance(inner, ToolCompleted):
            presentation.complete_member_tool(member_id, inner.call)
    elif isinstance(event, TextDelta):
        presentation.append_consensus(event.text)
    elif isinstance(event, ToolStarted):
        presentation.start_tool("team", event.call)
    elif isinstance(event, ToolCompleted):
        presentation.complete_tool("team", event.call)


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
    "url_safe_member_id",
]
