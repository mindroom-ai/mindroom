"""Tests for GitHub release workflow metadata PR handling."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def test_release_metadata_pr_reuses_open_metadata_pr() -> None:
    """Repeated release metadata updates should update an open metadata PR in place."""
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "gh pr list" in workflow
    assert "--state open" in workflow
    assert 'startswith("Update MindRoom release metadata")' in workflow
    assert 'RELEASE_METADATA_BRANCH="${EXISTING_RELEASE_METADATA_PR#* }"' in workflow
    assert 'gh pr edit "$EXISTING_RELEASE_METADATA_PR_NUMBER"' in workflow


def test_release_metadata_fallback_branch_is_not_tag_specific() -> None:
    """New metadata PRs should use one reusable branch instead of one branch per release tag."""
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert 'RELEASE_METADATA_BRANCH="release-metadata/mindroom"' in workflow
    assert 'RELEASE_METADATA_BRANCH="release-metadata/${TAG_NAME}"' not in workflow
