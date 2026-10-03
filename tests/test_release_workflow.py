"""Tests for GitHub release workflow metadata PR handling."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
README = ROOT / "README.md"
MACOS_APP_DOC = ROOT / "docs" / "installation" / "macos-app.md"
MACOS_BUILD_SCRIPT = ROOT / "macos" / "build-macos-app.sh"
PLATFORM_BACKEND = ROOT / "saas-platform" / "platform-backend"
# Root uv.lock packages that publish no wheels, whose setuptools build requirements are pinned in pyproject.toml.
_REVIEWED_SOURCE_BUILDS = {
    "bibtexparser",
    "google-search-results",
    "googlemaps",
    "mouseinfo",
    "pyautogui",
    "pygetwindow",
    "pyrect",
    "pyscreeze",
    "pysher",
    "python3-xlib",
    "pytweening",
    "sgmllib3k",
    "wikipedia",
}


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


# Editable installs add editables to hatchling's build environment; sdist-only dependencies build with setuptools.
@pytest.mark.parametrize(
    ("project", "build_packages"),
    [
        pytest.param(ROOT, {"editables", "setuptools", "wheel"}, id="mindroom"),
        pytest.param(PLATFORM_BACKEND, {"editables"}, id="platform-backend"),
    ],
)
def test_build_environments_install_only_pinned_packages(project: Path, build_packages: set[str]) -> None:
    """Release, helper, and image builds run build code fixed by the commit, not whatever PyPI serves that day."""
    pyproject = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    constraints = pyproject["tool"]["uv"]["build-constraint-dependencies"]
    pins = {name: version for name, _, version in (constraint.partition("==") for constraint in constraints)}
    backend = {
        re.split(r"[^a-z0-9-]", requirement, maxsplit=1)[0] for requirement in pyproject["build-system"]["requires"]
    }
    manifest = tomllib.loads((project / "uv.lock").read_text(encoding="utf-8"))["manifest"]["build-constraints"]

    assert all(re.fullmatch(r"[a-z0-9-]+==[0-9][0-9a-z.]*", constraint) for constraint in constraints), constraints
    assert len(pins) == len(constraints)
    assert backend | build_packages <= pins.keys()
    # `uv sync --locked` reads the build constraints recorded in uv.lock.
    assert {entry["name"]: entry["specifier"] for entry in manifest} == {
        name: f"=={version}" for name, version in pins.items()
    }


@pytest.mark.parametrize(
    ("project", "source_builds"),
    [
        pytest.param(ROOT, _REVIEWED_SOURCE_BUILDS, id="mindroom"),
        pytest.param(PLATFORM_BACKEND, set(), id="platform-backend"),
    ],
)
def test_locked_source_builds_were_reviewed_for_build_pins(project: Path, source_builds: set[str]) -> None:
    """A locked package without wheels builds from source, so its build requirements must join the pins first."""
    lock = tomllib.loads((project / "uv.lock").read_text(encoding="utf-8"))
    source_only = {package["name"] for package in lock["package"] if "sdist" in package and not package.get("wheels")}

    assert source_only <= source_builds


def test_macos_helper_builds_before_signing_secrets_exist(release_workflow: str) -> None:
    """Python build code for the helper runs before the signing keychain and without Apple credentials."""
    steps = yaml.safe_load(release_workflow)["jobs"]["build_macos_app"]["steps"]
    names = [step.get("name") for step in steps]
    helper = steps[names.index("Build desktop helper")]
    app = steps[names.index("Build notarized macOS DMG")]

    assert names.index("Build desktop helper") < names.index("Import Developer ID certificate")
    assert helper["run"] == "macos/build-desktop-helper.sh"
    assert "env" not in helper
    assert app["env"]["SKIP_DESKTOP_HELPER_BUILD"] == "1"


@pytest.mark.parametrize("include_matching_pr", [False, True])
def test_release_metadata_pr_reuses_open_metadata_pr(release_workflow: str, include_matching_pr: bool) -> None:
    """Only the fixed branch in this repository can be selected."""
    assert "gh pr list" in release_workflow
    assert "--state open" in release_workflow
    query_line = next(line for line in release_workflow.splitlines() if "jq -r --arg branch" in line)
    query_command = shlex.split(query_line.replace("$RELEASE_METADATA_BRANCH", "release-metadata/mindroom"))
    prs = [
        {"number": 12, "headRefName": "unrelated", "isCrossRepository": False},
        {
            "number": 13,
            "headRefName": "release-metadata/mindroom",
            "isCrossRepository": True,
        },
    ]
    if include_matching_pr:
        prs.append(
            {
                "number": 11,
                "headRefName": "release-metadata/mindroom",
                "isCrossRepository": False,
            },
        )
    result = subprocess.run(query_command, input=json.dumps(prs), text=True, capture_output=True, check=True)
    assert result.stdout.strip() == ("11" if include_matching_pr else "")
    assignments = [
        line.strip() for line in release_workflow.splitlines() if line.strip().startswith("RELEASE_METADATA_BRANCH=")
    ]
    assert assignments == ['RELEASE_METADATA_BRANCH="release-metadata/mindroom"']
    assert 'gh pr edit "$EXISTING_RELEASE_METADATA_PR"' in release_workflow


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
    assert "uses: actions/create-github-app-token@" in release_workflow
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
