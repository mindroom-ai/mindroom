"""Tests for GitHub release workflow metadata PR handling."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
README = ROOT / "README.md"
MACOS_APP_DOC = ROOT / "docs" / "installation" / "macos-app.md"
MACOS_BUILD_SCRIPT = ROOT / "macos" / "build-macos-app.sh"


@pytest.fixture(scope="module")
def release_workflow() -> str:
    """Return the release workflow text."""
    return RELEASE_WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def macos_build_script() -> str:
    """Return the macOS app build script text."""
    return MACOS_BUILD_SCRIPT.read_text(encoding="utf-8")


def test_release_builds_apple_silicon_macos_app(release_workflow: str, macos_build_script: str) -> None:
    """Published app binaries target Apple silicon only, so no Intel runtime or helper is ever built."""
    assert "NOTARIZE=1 macos/build-macos-app.sh --dmg" in release_workflow
    assert '--product "$APP_NAME" --arch arm64)' in macos_build_script
    assert "x86_64" not in release_workflow
    assert "x86_64" not in macos_build_script


def test_macos_app_publishes_after_matching_pypi_release(release_workflow: str) -> None:
    """The app installs `mindroom==<app version>`, so it must not ship before that release is on PyPI."""
    macos_job = release_workflow.split("  build_macos_app:\n", 1)[1].split("\n  update_homebrew_tap:", 1)[0]
    assert "\n    needs: deploy\n" in macos_job
    # Retrying a release whose wheel is already on PyPI must still publish the app.
    assert "skip-existing: true" in release_workflow
    assert "APP_VERSION: ${{ inputs.release_ref }}" in macos_job


@pytest.mark.parametrize("include_matching_pr", [False, True])
def test_release_metadata_pr_reuses_open_metadata_pr(release_workflow: str, include_matching_pr: bool) -> None:
    """Only the fixed branch in this repository can be selected, despite newer matching titles."""
    assert "gh pr list" in release_workflow
    assert "--state open" in release_workflow
    query_line = next(line for line in release_workflow.splitlines() if "--jq 'map(select(" in line)
    query = shlex.split(query_line)[1]
    prs = [
        {"number": 12, "headRefName": "unrelated", "isCrossRepository": False, "updatedAt": "2026-09-29"},
        {
            "number": 13,
            "headRefName": "release-metadata/mindroom",
            "isCrossRepository": True,
            "updatedAt": "2026-09-30",
        },
    ]
    if include_matching_pr:
        prs.append(
            {
                "number": 11,
                "headRefName": "release-metadata/mindroom",
                "isCrossRepository": False,
                "updatedAt": "2026-09-28",
            },
        )
    for pr in prs:
        pr["title"] = "Update MindRoom release metadata for v2026.9.1"
    result = subprocess.run(["jq", "-r", query], input=json.dumps(prs), text=True, capture_output=True, check=True)
    assert result.stdout.strip() == ("11" if include_matching_pr else "")
    assignments = [
        line.strip() for line in release_workflow.splitlines() if line.strip().startswith("RELEASE_METADATA_BRANCH=")
    ]
    assert assignments == ['RELEASE_METADATA_BRANCH="release-metadata/mindroom"']
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
    assert "uses: actions/create-github-app-token@v3" in release_workflow
    assert "app-id: ${{ vars.RELEASE_BOT_APP_ID }}" in release_workflow
    assert "private-key: ${{ secrets.RELEASE_BOT_PRIVATE_KEY }}" in release_workflow
    assert "owner: mindroom-ai" in release_workflow
    assert "repositories: homebrew-tap" in release_workflow
    assert "GH_TOKEN: ${{ steps.release-bot.outputs.token }}" in release_workflow
    assert "HOMEBREW_TAP_DISPATCH_TOKEN" not in release_workflow
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
