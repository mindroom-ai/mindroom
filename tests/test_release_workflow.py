"""Tests for GitHub release workflow metadata PR handling."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
CHECK_APPCAST_VERSION = ROOT / ".github" / "scripts" / "check_appcast_version.py"
NORMALIZE_APPCAST = ROOT / ".github" / "scripts" / "normalize_appcast.py"
BUILD_PLATFORM_WORKFLOW = ROOT / ".github" / "workflows" / "build-platform.yml"
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
    jobs = yaml.safe_load(release_workflow)["jobs"]
    macos_job = release_workflow.split("  build_macos_app:\n", 1)[1].split("\n  update_homebrew_tap:", 1)[0]
    assert jobs["build_macos_helper"]["needs"] == "deploy"
    assert jobs["build_macos_app"]["needs"] == "build_macos_helper"
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
    """Release, helper, and image builds pin the version of every package in their build environments."""
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


def test_macos_helper_builds_in_a_job_without_secrets(release_workflow: str) -> None:
    """Python build code for the helper runs on its own runner, so it cannot tamper with the signing job."""
    jobs = yaml.safe_load(release_workflow)["jobs"]
    helper_job = jobs["build_macos_helper"]
    app_steps = jobs["build_macos_app"]["steps"]
    upload = next(
        step for step in helper_job["steps"] if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    )
    download = next(step for step in app_steps if str(step.get("uses", "")).startswith("actions/download-artifact@"))
    app = next(step for step in app_steps if step.get("name") == "Build notarized macOS DMG")

    assert helper_job["permissions"] == {"contents": "read"}
    assert "secrets." not in json.dumps(helper_job)
    assert "github.token" not in json.dumps(helper_job)
    assert any(step.get("run") == "macos/build-desktop-helper.sh" for step in helper_job["steps"])
    assert not any("build-desktop-helper" in step.get("run", "") for step in app_steps)
    assert download["with"]["name"] == upload["with"]["name"]
    assert app["env"]["SKIP_DESKTOP_HELPER_BUILD"] == "1"


def test_release_publishers_restore_no_actions_cache(release_workflow: str) -> None:
    """Any job on main can write the Actions cache, so jobs that ship output or hold release secrets restore none."""
    steps = [step for job in yaml.safe_load(release_workflow)["jobs"].values() for step in job["steps"]]
    setup_uv = [step for step in steps if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")]
    platform_job = yaml.safe_load(BUILD_PLATFORM_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["build-and-push"]
    image_build = next(step for step in platform_job["steps"] if "cache-from" in step.get("with", {}))

    assert setup_uv
    assert all(step.get("with", {}).get("enable-cache") is False for step in setup_uv)
    assert not any(str(step.get("uses", "")).startswith("actions/cache") for step in steps)
    assert image_build["with"]["cache-from"] == (
        "${{ github.event_name == 'pull_request' && format('type=gha,scope={0}', matrix.service.name) || '' }}"
    )


def test_platform_images_keep_separate_build_caches() -> None:
    """Exports to one shared gha scope replace each other, so each platform image reads and writes its own scope."""
    platform_job = yaml.safe_load(BUILD_PLATFORM_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["build-and-push"]
    image_build = next(step for step in platform_job["steps"] if "cache-to" in step.get("with", {}))

    assert "format('type=gha,scope={0}', matrix.service.name)" in image_build["with"]["cache-from"]
    assert image_build["with"]["cache-to"] == "type=gha,mode=max,scope=${{ matrix.service.name }}"


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


def _appcast_xml(*versions: str) -> str:
    items = "".join(
        f"<item><title>{v}</title><sparkle:shortVersionString>{v}</sparkle:shortVersionString></item>" for v in versions
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<rss xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle" version="2.0">\n'
        f"<channel>{items}</channel>\n</rss>\n"
    )


@dataclass(frozen=True)
class _MetadataStepResult:
    proc: subprocess.CompletedProcess[str]
    gh_calls: list[str]
    pr_body: str
    remote_appcast: str
    remote_tip: str | None
    race_sha: str | None
    push_attempts: int


def _write_metadata_stubs(
    tmp_path: Path,
    *,
    bash: str,
    real_git: str,
    comp: Path,
    existing_pr: int | None,
    race_mode: str | None,
    fail_all_pushes: bool,
) -> tuple[Path, Path, Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh_calls, pr_body, push_log = tmp_path / "gh-calls.txt", tmp_path / "pr-body.txt", tmp_path / "pushes.txt"
    for log_file in (gh_calls, pr_body, push_log):
        log_file.write_text("", encoding="utf-8")

    prs = (
        [{"number": existing_pr, "headRefName": "release-metadata/mindroom", "isCrossRepository": False}]
        if existing_pr is not None
        else []
    )
    gh_script = (
        f'#!{bash}\necho "$@" >> "{gh_calls}"\n'
        f'if [[ "$1 $2" == "pr list" ]]; then echo {shlex.quote(json.dumps(prs))}\n'
        f'elif [[ "$1 $2" == "pr edit" || "$1 $2" == "pr create" ]]; then\n'
        f'  while [[ $# -gt 0 ]]; do [[ "$1" == "--body-file" ]] && cp "$2" "{pr_body}"; shift; done\n'
        f'else echo "unexpected gh invocation: $*" >&2; exit 64; fi\n'
    )
    race_push = f'"{real_git}" -C "{comp}" push --force origin race-branch:release-metadata/mindroom >/dev/null 2>&1'
    marker = tmp_path / "raced"
    if fail_all_pushes:
        push_hook = 'echo "simulated push failure" >&2; exit 1'
    elif race_mode == "before":
        push_hook = f'if [[ ! -f "{marker}" ]]; then touch "{marker}"; {race_push}; fi; exec "{real_git}" "$@"'
    elif race_mode == "after":
        push_hook = (
            f'"{real_git}" "$@"; rc=$?; '
            f'if [[ "$rc" -eq 0 && ! -f "{marker}" ]]; then touch "{marker}"; {race_push}; fi; exit "$rc"'
        )
    else:
        push_hook = f'exec "{real_git}" "$@"'
    git_script = (
        f'#!{bash}\nif [[ "$1" == "push" ]]; then echo push >> "{push_log}"; {push_hook}; fi\nexec "{real_git}" "$@"\n'
    )
    for name, content in (("gh", gh_script), ("git", git_script), ("sleep", f"#!{bash}\nexit 0\n")):
        stub = bin_dir / name
        stub.write_text(content, encoding="utf-8")
        stub.chmod(0o755)
    return bin_dir, gh_calls, pr_body, push_log


def _run_metadata_step(
    tmp_path: Path,
    release_workflow: str,
    *,
    tag_name: str,
    local_versions: tuple[str, ...],
    remote_branch_versions: tuple[str, ...] | None = ("2026.11.620", "2026.11.619"),
    advance_main_versions: tuple[str, ...] | None = None,
    race_versions: tuple[str, ...] | None = None,
    race_mode: str | None = None,
    fail_all_pushes: bool = False,
    existing_pr: int | None = 3587,
) -> _MetadataStepResult:
    """Execute the real ``Update Sparkle appcast metadata`` step against a local bare repo."""
    steps = yaml.safe_load(release_workflow)["jobs"]["build_macos_app"]["steps"]
    script = next(step["run"] for step in steps if step.get("name") == "Update Sparkle appcast metadata")
    bash, real_git = shutil.which("bash"), shutil.which("git")
    assert bash is not None
    assert real_git is not None

    bare, work, comp = tmp_path / "remote.git", tmp_path / "work", tmp_path / "comp"
    git_env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }

    def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run([real_git, *args], cwd=cwd, env=git_env, check=check, text=True, capture_output=True)

    git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    git(tmp_path, "clone", str(bare), str(work))
    (work / ".github" / "scripts").mkdir(parents=True)
    for helper in (NORMALIZE_APPCAST, CHECK_APPCAST_VERSION):
        shutil.copy(helper, work / ".github" / "scripts" / helper.name)
    (work / "macos").mkdir()
    (work / "macos" / "appcast.xml").write_text(_appcast_xml("2026.11.620"), encoding="utf-8")
    git(work, "add", ".")
    git(work, "commit", "-m", "init main")
    git(work, "push", "origin", "main")
    git(tmp_path, "clone", str(bare), str(comp))

    def commit_in_comp(branch: str, versions: tuple[str, ...]) -> str:
        git(comp, "checkout", "-B", branch, "origin/main")
        (comp / "macos" / "appcast.xml").write_text(_appcast_xml(*versions), encoding="utf-8")
        git(comp, "commit", "-am", f"update {branch}")
        return git(comp, "rev-parse", "HEAD").stdout.strip()

    if remote_branch_versions is not None:
        commit_in_comp("seed-remote", remote_branch_versions)
        git(comp, "push", "origin", "HEAD:release-metadata/mindroom")
    if advance_main_versions is not None:
        commit_in_comp("advance-main", advance_main_versions)
        git(comp, "push", "origin", "HEAD:main")
    race_sha = commit_in_comp("race-branch", race_versions) if race_versions else None

    git(work, "checkout", "-B", "release-metadata", "main")
    (work / "macos" / "appcast.xml").write_text(_appcast_xml(*local_versions), encoding="utf-8")
    git(
        work,
        "config",
        f"url.file://{bare}.insteadOf",
        "https://x-access-token:test-token@github.com/mindroom-ai/mindroom.git",
    )
    bin_dir, gh_calls, pr_body, push_log = _write_metadata_stubs(
        tmp_path,
        bash=bash,
        real_git=real_git,
        comp=comp,
        existing_pr=existing_pr,
        race_mode=race_mode,
        fail_all_pushes=fail_all_pushes,
    )
    proc = subprocess.run(
        [bash, "-e", "-c", script],
        cwd=work,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GH_TOKEN": "test-token",
            "GITHUB_REPOSITORY": "mindroom-ai/mindroom",
            "TAG_NAME": tag_name,
        },
        text=True,
        capture_output=True,
        check=False,
    )
    tip = git(bare, "rev-parse", "--verify", "--quiet", "refs/heads/release-metadata/mindroom", check=False)
    remote_tip = tip.stdout.strip() if tip.returncode == 0 else None
    appcast = (
        git(bare, "show", "refs/heads/release-metadata/mindroom:macos/appcast.xml", check=False) if remote_tip else None
    )
    return _MetadataStepResult(
        proc=proc,
        gh_calls=gh_calls.read_text(encoding="utf-8").splitlines(),
        pr_body=pr_body.read_text(encoding="utf-8"),
        remote_appcast=appcast.stdout if appcast and appcast.returncode == 0 else "",
        remote_tip=remote_tip,
        race_sha=race_sha,
        push_attempts=len(push_log.read_text(encoding="utf-8").splitlines()),
    )


def test_appcast_metadata_race_newer_winner_skips_downgrade(tmp_path: Path, release_workflow: str) -> None:
    """When a newer release wins the push race, the older run retries, detects it, and exits 0 without downgrading."""
    res = _run_metadata_step(
        tmp_path,
        release_workflow,
        tag_name="v2026.11.621",
        local_versions=("2026.11.621", "2026.11.620"),
        race_versions=("2026.11.622", "2026.11.620"),
        race_mode="before",
    )
    assert res.proc.returncode == 0, res.proc.stderr
    assert res.remote_tip == res.race_sha
    assert "2026.11.622" in res.remote_appcast
    assert "2026.11.621" not in res.remote_appcast
    assert not any(call.startswith(("pr edit", "pr create")) for call in res.gh_calls)


def test_appcast_metadata_race_older_winner_retries_and_updates_pr(tmp_path: Path, release_workflow: str) -> None:
    """When an older release wins the first push, the newer run refreshes its lease, pushes, and updates the PR."""
    res = _run_metadata_step(
        tmp_path,
        release_workflow,
        tag_name="v2026.11.622",
        local_versions=("2026.11.622", "2026.11.620"),
        race_versions=("2026.11.621", "2026.11.620"),
        race_mode="before",
    )
    assert res.proc.returncode == 0, res.proc.stderr
    assert res.push_attempts == 2
    assert res.remote_tip != res.race_sha
    assert "2026.11.622" in res.remote_appcast
    assert any(
        call.startswith("pr edit 3587 ") and "Update MindRoom release metadata to v2026.11.622" in call
        for call in res.gh_calls
    )
    assert "v2026.11.622" in res.pr_body


def test_appcast_metadata_rerun_same_tag_pushes_and_updates_pr(tmp_path: Path, release_workflow: str) -> None:
    """Rerunning the same release tag pushes over an equal remote version and updates the PR."""
    res = _run_metadata_step(
        tmp_path,
        release_workflow,
        tag_name="v2026.11.622",
        local_versions=("2026.11.622", "2026.11.620"),
        remote_branch_versions=("2026.11.622", "2026.11.619"),
    )
    assert res.proc.returncode == 0, res.proc.stderr
    assert "2026.11.620" in res.remote_appcast
    assert any(call.startswith("pr edit 3587 ") for call in res.gh_calls)


def test_appcast_metadata_fresh_branch_creates_pr(tmp_path: Path, release_workflow: str) -> None:
    """When release-metadata/mindroom does not exist remotely, the step pushes with an empty lease and opens a PR."""
    res = _run_metadata_step(
        tmp_path,
        release_workflow,
        tag_name="v2026.11.622",
        local_versions=("2026.11.622", "2026.11.620"),
        remote_branch_versions=None,
        existing_pr=None,
    )
    assert res.proc.returncode == 0, res.proc.stderr
    assert "2026.11.622" in res.remote_appcast
    assert any(
        call.startswith("pr create ")
        and "--head release-metadata/mindroom" in call
        and "Update MindRoom release metadata to v2026.11.622" in call
        for call in res.gh_calls
    )


def test_appcast_metadata_persistent_push_failure_exits_nonzero(tmp_path: Path, release_workflow: str) -> None:
    """Real push errors still fail loudly once the bounded retry loop is exhausted."""
    res = _run_metadata_step(
        tmp_path,
        release_workflow,
        tag_name="v2026.11.622",
        local_versions=("2026.11.622", "2026.11.620"),
        fail_all_pushes=True,
    )
    assert res.proc.returncode != 0
    assert res.push_attempts == 5
    assert not any(call.startswith(("pr edit", "pr create")) for call in res.gh_calls)


def test_appcast_metadata_skips_when_main_already_newer(tmp_path: Path, release_workflow: str) -> None:
    """An older release skips cleanly when main already contains a strictly newer appcast release."""
    res = _run_metadata_step(
        tmp_path,
        release_workflow,
        tag_name="v2026.11.621",
        local_versions=("2026.11.621", "2026.11.620"),
        advance_main_versions=("2026.11.622", "2026.11.620"),
    )
    assert res.proc.returncode == 0, res.proc.stderr
    assert res.push_attempts == 0
    assert "2026.11.621" not in res.remote_appcast
    assert not any(call.startswith(("pr edit", "pr create")) for call in res.gh_calls)


def test_appcast_metadata_skips_pr_update_when_branch_moves_after_push(tmp_path: Path, release_workflow: str) -> None:
    """If another run advances the branch after push succeeds, this run skips editing the PR."""
    res = _run_metadata_step(
        tmp_path,
        release_workflow,
        tag_name="v2026.11.621",
        local_versions=("2026.11.621", "2026.11.620"),
        race_versions=("2026.11.622", "2026.11.620"),
        race_mode="after",
    )
    assert res.proc.returncode == 0, res.proc.stderr
    assert res.remote_tip == res.race_sha
    assert not any(call.startswith(("pr edit", "pr create")) for call in res.gh_calls)


@pytest.mark.parametrize(
    ("xml_text", "target_tag", "expected"),
    [
        (_appcast_xml("2026.11.100"), "v2026.11.99", "2026.11.100"),
        (_appcast_xml("2026.11.99"), "v2026.11.100", ""),
        (_appcast_xml("2026.11.622"), "v2026.11.622", ""),
        (_appcast_xml("2026.11.622", "2026.11.625", "2026.11.620"), "v2026.11.621", "2026.11.625"),
        (_appcast_xml("dev", "2026.11.622"), "v2026.11.621", "2026.11.622"),
        (_appcast_xml("dev-build"), "v2026.11.621", ""),
        (_appcast_xml("2026.11.622"), "not-a-version", ""),
        ("<not-xml", "v2026.11.621", ""),
        ("", "v2026.11.621", ""),
    ],
)
def test_check_appcast_version(xml_text: str, target_tag: str, expected: str) -> None:
    """The version helper compares numeric CalVer segments and never falsely skips on bad input."""
    proc = subprocess.run(
        [sys.executable, str(CHECK_APPCAST_VERSION), "--newer-than", target_tag],
        input=xml_text,
        text=True,
        capture_output=True,
        check=True,
    )
    assert proc.stdout.strip() == expected
