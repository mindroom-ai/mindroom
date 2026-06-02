"""Tests for GitHub release workflow metadata PR handling."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


@pytest.fixture(scope="module")
def release_workflow() -> str:
    """Return the release workflow text."""
    return RELEASE_WORKFLOW.read_text(encoding="utf-8")


def test_release_metadata_pr_reuses_open_metadata_pr(release_workflow: str) -> None:
    """Repeated release metadata updates should update an open metadata PR in place."""
    assert "gh pr list" in release_workflow
    assert "--state open" in release_workflow
    assert """--search '"Update MindRoom release metadata" in:title'""" in release_workflow
    assert 'startswith("Update MindRoom release metadata")' in release_workflow
    assert 'RELEASE_METADATA_BRANCH="${EXISTING_RELEASE_METADATA_PR#* }"' in release_workflow
    assert 'gh pr edit "$EXISTING_RELEASE_METADATA_PR_NUMBER"' in release_workflow


def test_release_metadata_fallback_branch_is_not_tag_specific(release_workflow: str) -> None:
    """New metadata PRs should use one reusable branch instead of one branch per release tag."""
    assert 'RELEASE_METADATA_BRANCH="release-metadata/mindroom"' in release_workflow
    assert 'RELEASE_METADATA_BRANCH="release-metadata/${TAG_NAME}"' not in release_workflow


def test_release_metadata_push_uses_explicit_force_with_lease(release_workflow: str) -> None:
    """Metadata branch updates should compare against the fetched branch state."""
    assert (
        'REMOTE_RELEASE_METADATA_SHA=$(git rev-parse --verify --quiet "refs/remotes/origin/${RELEASE_METADATA_BRANCH}")'
        in release_workflow
    )
    assert (
        '--force-with-lease="refs/heads/${RELEASE_METADATA_BRANCH}:${REMOTE_RELEASE_METADATA_SHA}"' in release_workflow
    )
    assert '--force-with-lease="refs/heads/${RELEASE_METADATA_BRANCH}:"' in release_workflow
    assert 'git push "${FORCE_WITH_LEASE[@]}"' in release_workflow
