"""The team reply snapshot releases before version 3 stored, which keyed member slots by Agno's url-safe member id."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mindroom.tool_system.events import ToolTraceEntry


def _url_safe_member_id(config_name: str) -> str:
    """Return Agno's ``url_safe_string`` of a member's config name, the id a version-2 snapshot keys the member by.

    Spaces, camelCase boundaries, and underscores become dashes, punctuation other than dots goes, dash runs collapse.
    """
    kebab = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", config_name.replace(" ", "-")).lower().replace("_", "-")
    return re.sub(r"-+", "-", re.sub(r"[^\w\-.]", "", kebab))


def _is_v2_member(member: object) -> bool:
    """Return whether one stored version-2 member slot names its id and config name as text."""
    if not isinstance(member, dict):
        return False
    fields = cast("dict[str, object]", member)
    return isinstance(fields.get("id"), str) and isinstance(fields.get("config_name"), str)


# LEGACY_COMPAT: Team reply snapshots of version 2, keyed by Agno's url-safe member id.
# Legacy format: a reply record's ``team_state`` of ``{"kind": "team_stream", "version": 2}`` whose ``members`` each
# hold an ``id``, Agno's ``url_safe_string`` of the member's ``config_name`` ("Code_Review" is "code-review"), beside
# that ``config_name``, and whose ``separate_next_scopes`` name a member slot as ``agent:<id>`` and the consensus as
# ``team``; the tool trace stored with it names each member call's slot by that ``<id>`` too (the trace's own
# ``LEGACY_COMPAT`` rule in ``tool_system/events.py`` reads its ``scope_key``).
# Last legacy release: v2026.10.677, whose ``TeamStreamPresentation.to_state`` (``teams/presentation.py``) wrote it, as
# the Agno runtime did before it; replacement: the next release, whose version-3 snapshot keys each member by its
# config name alone and lists the slots a hidden tool call separates as ``separate_next_slots``, a member's config
# name or null for the consensus.
# Handling: restoring the snapshot reads it as the version 3 it stands for, its ids, separators, and trace members
# mapped to config names, so a team reply paused before the upgrade resumes in place with its member slots, pending
# calls, and paragraph breaks. A snapshot whose ids are not the url-safe ids of their config names, or whose
# separators or trace name a slot it does not hold, is refused, as v2026.10.674 refused it.
# Coverage, on the reply records v2026.10.674 stored (tests/fixtures/legacy_team_presentation/):
#   tests/teams/test_legacy_team_presentation.py::test_a_v2_reply_record_restores_with_its_members_keyed_by_config_name,
#   tests/teams/test_legacy_team_presentation.py::test_a_team_reply_paused_by_v2026_10_674_resumes_in_place,
#   tests/teams/test_legacy_team_presentation.py::test_a_v2_snapshot_keys_each_member_by_the_url_safe_id_of_its_config_name,
#   tests/teams/test_legacy_team_presentation.py::test_a_malformed_v2_snapshot_is_refused
def upgrade_v2_team_snapshot(
    state: Mapping[str, object],
    tool_trace: Sequence[ToolTraceEntry],
) -> tuple[dict[str, object], list[ToolTraceEntry]] | None:
    """Return a version-2 snapshot as version 3 and its tool trace with each member named by config name.

    None when the snapshot is not one a version-2 writer stored: its member ids are the url-safe ids of their config
    names, each held once, and its separators and trace name only its own slots.
    """
    stored_members = state.get("members")
    if not isinstance(stored_members, list) or not all(_is_v2_member(member) for member in stored_members):
        return None
    members = cast("list[dict[str, object]]", stored_members)
    config_names = {cast("str", member["id"]): cast("str", member["config_name"]) for member in members}
    if len(config_names) != len(members) or any(
        _url_safe_member_id(config_name) != member_id for member_id, config_name in config_names.items()
    ):
        return None
    slots: dict[str, str | None] = {
        "team": None,
        **{f"agent:{member_id}": name for member_id, name in config_names.items()},
    }
    separators = state.get("separate_next_scopes", [])
    if (
        not isinstance(separators, list)
        or not all(isinstance(scope, str) and scope in slots for scope in separators)
        or not all(entry.member_id is None or entry.member_id in config_names for entry in tool_trace)
    ):
        return None
    upgraded: dict[str, object] = {
        "kind": "team_stream",
        "version": 3,
        "members": [
            {"id": member["config_name"], "display_name": member.get("display_name"), "content": member.get("content")}
            for member in members
        ],
        "consensus": state.get("consensus"),
    }
    if separators:
        upgraded["separate_next_slots"] = [slots[scope] for scope in cast("list[str]", separators)]
    trace = [
        entry if entry.member_id is None else replace(entry, member_id=config_names[entry.member_id])
        for entry in tool_trace
    ]
    return upgraded, trace


__all__ = ["upgrade_v2_team_snapshot"]
