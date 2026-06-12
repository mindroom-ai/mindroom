"""Tests for GitHub release workflow metadata PR handling."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
README = ROOT / "README.md"
MACOS_APP_DOC = ROOT / "docs" / "installation" / "macos-app.md"


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


def test_release_workflow_dispatches_homebrew_tap_update(release_workflow: str) -> None:
    """The main release workflow should notify the dedicated Homebrew tap repo."""
    assert "update_homebrew_tap:" in release_workflow
    assert "needs: build_macos_app" in release_workflow
    assert "HOMEBREW_TAP_DISPATCH_TOKEN" in release_workflow
    assert "Token needs write access to mindroom-ai/homebrew-tap." in release_workflow
    assert "Fine-grained tokens need Contents: write on that repo." in release_workflow
    assert "repos/mindroom-ai/homebrew-tap/dispatches" in release_workflow
    assert "-f event_type=mindroom-release" in release_workflow
    assert '-F "client_payload[tag_name]=$TAG_NAME"' in release_workflow
    assert (
        '-F "client_payload[asset_url]=https://github.com/mindroom-ai/mindroom/releases/download/${TAG_NAME}/MindRoom.dmg"'
        in release_workflow
    )


def test_release_workflow_no_longer_updates_in_repo_cask(release_workflow: str) -> None:
    """The cask should live in mindroom-ai/homebrew-tap, not this source repo."""
    assert "Casks/mindroom.rb" not in release_workflow
    assert "update_mindroom_cask.py" not in release_workflow
    assert "Homebrew cask version and SHA256" not in release_workflow


def test_docs_use_dedicated_homebrew_tap_command() -> None:
    """User-facing install docs should point at the dedicated tap."""
    install_command = "brew install --cask mindroom-ai/tap/mindroom"
    old_command = "brew install --cask mindroom-ai/mindroom/mindroom"

    for path in (README, MACOS_APP_DOC):
        text = path.read_text(encoding="utf-8")
        assert install_command in text
        assert old_command not in text
