"""Delegation records started before the primary owned their state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.path_confinement import open_directory_within_root

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class WorkspaceOnlyRecord:
    """A record whose state lived only in its workspace files; its operations leave those files as they were."""


def read_workspace_only_record(child_workspace: Path, scoped_path: str) -> WorkspaceOnlyRecord:
    """Return the marker for a record with no primary state, or raise FileNotFoundError when its workspace directory is missing too."""
    # LEGACY_COMPAT: Delegation records whose working state lived only in their workspace files.
    # Legacy format: A record directory below the child workspace's .mindroom/delegations/ with no state.json in the
    # record's primary-only directory below tracking/; selected when the state file is missing while that workspace
    # directory exists.
    # Last legacy release: v2026.10.101; replacement: v2026.10.102 keeps each record's state below tracking/ and
    # writes run.json, events.jsonl, and transcript.md as exports of it.
    # Handling: Such a record's operations return without reading or writing its workspace files, so a delegation
    # still in flight at the upgrade settles and its record stays as it was; a record found in neither place is missing.
    # Coverage: tests/test_delegation_records.py::test_a_record_started_before_primary_state_is_left_as_it_was.
    with open_directory_within_root(child_workspace, scoped_path):
        return WorkspaceOnlyRecord()
