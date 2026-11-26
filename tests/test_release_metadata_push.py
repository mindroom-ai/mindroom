"""Behavioral and unit tests for release-metadata/mindroom push race handling."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
CHECK_APPCAST_VERSION = ROOT / ".github" / "scripts" / "check_appcast_version.py"
NORMALIZE_APPCAST = ROOT / ".github" / "scripts" / "normalize_appcast.py"


@pytest.fixture(scope="module")
def release_workflow() -> str:
    """Return the release workflow text."""
    return RELEASE_WORKFLOW.read_text(encoding="utf-8")


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
    outputs: dict[str, str]
    remote_appcast: str
    remote_tip: str | None
    race_sha: str | None
    push_attempts: int


def _parse_github_output(path: Path) -> dict[str, str]:
    outputs: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            outputs[key] = value
    return outputs


def _write_metadata_stubs(
    tmp_path: Path,
    *,
    bash: str,
    real_git: str,
    comp: Path,
    existing_pr: int | None,
    race_mode: str | None,
    fail_all_pushes: bool,
    fail_retry_main_fetch: bool,
) -> tuple[Path, Path, Path, Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh_calls = tmp_path / "gh-calls.txt"
    pr_body = tmp_path / "pr-body.txt"
    push_log = tmp_path / "pushes.txt"
    github_output = tmp_path / "github-output.txt"
    for log_file in (gh_calls, pr_body, push_log, github_output):
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

    fetch_hook = (
        f'if [[ "$1" == "fetch" && "$3" == "main" && -f "{marker}" ]]; then '
        f'echo "simulated main fetch failure" >&2; exit 1; fi\n'
        if fail_retry_main_fetch
        else ""
    )
    git_script = (
        f"#!{bash}\n"
        f"{fetch_hook}"
        f'if [[ "$1" == "push" ]]; then echo push >> "{push_log}"; {push_hook}; fi\n'
        f'exec "{real_git}" "$@"\n'
    )
    for name, content in (("gh", gh_script), ("git", git_script), ("sleep", f"#!{bash}\nexit 0\n")):
        stub = bin_dir / name
        stub.write_text(content, encoding="utf-8")
        stub.chmod(0o755)
    return bin_dir, gh_calls, pr_body, push_log, github_output


def _run_metadata_step(
    tmp_path: Path,
    release_workflow: str,
    *,
    tag_name: str,
    local_versions: tuple[str, ...],
    main_versions: tuple[str, ...] = ("2026.11.620",),
    remote_branch_versions: tuple[str, ...] | None = ("2026.11.620", "2026.11.619"),
    advance_main_versions: tuple[str, ...] | None = None,
    race_versions: tuple[str, ...] | None = None,
    race_mode: str | None = None,
    fail_all_pushes: bool = False,
    fail_retry_main_fetch: bool = False,
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
    (work / "macos" / "appcast.xml").write_text(_appcast_xml(*main_versions), encoding="utf-8")
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
    bin_dir, gh_calls, pr_body, push_log, github_output = _write_metadata_stubs(
        tmp_path,
        bash=bash,
        real_git=real_git,
        comp=comp,
        existing_pr=existing_pr,
        race_mode=race_mode,
        fail_all_pushes=fail_all_pushes,
        fail_retry_main_fetch=fail_retry_main_fetch,
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
            "GITHUB_OUTPUT": str(github_output),
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
        outputs=_parse_github_output(github_output),
        remote_appcast=appcast.stdout if appcast and appcast.returncode == 0 else "",
        remote_tip=remote_tip,
        race_sha=race_sha,
        push_attempts=len(push_log.read_text(encoding="utf-8").splitlines()),
    )


def test_build_macos_app_wires_superseded_output_to_homebrew_tap(release_workflow: str) -> None:
    """Superseded releases must skip dispatching a stale tag to mindroom-ai/homebrew-tap."""
    jobs = yaml.safe_load(release_workflow)["jobs"]
    build_macos_app = jobs["build_macos_app"]
    metadata_step = next(
        step for step in build_macos_app["steps"] if step.get("name") == "Update Sparkle appcast metadata"
    )
    homebrew_job = jobs["update_homebrew_tap"]

    assert metadata_step["id"] == "appcast-metadata"
    assert build_macos_app["outputs"]["superseded"] == "${{ steps.appcast-metadata.outputs.superseded }}"
    assert homebrew_job["needs"] == "build_macos_app"
    assert homebrew_job["if"] == "needs.build_macos_app.outputs.superseded != 'true'"


def test_appcast_metadata_race_newer_winner_skips_downgrade(tmp_path: Path, release_workflow: str) -> None:
    """When a newer release wins the push race, the older run retries, detects it, and exits 0 as superseded."""
    res = _run_metadata_step(
        tmp_path,
        release_workflow,
        tag_name="v2026.11.621",
        local_versions=("2026.11.621", "2026.11.620"),
        race_versions=("2026.11.622", "2026.11.620"),
        race_mode="before",
    )
    assert res.proc.returncode == 0, res.proc.stderr
    assert res.outputs == {"superseded": "true"}
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
    assert res.outputs == {"superseded": "false"}
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
    assert res.outputs == {"superseded": "false"}
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
    assert res.outputs == {"superseded": "false"}
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


def test_appcast_metadata_retry_fetch_main_failure_fails_closed(tmp_path: Path, release_workflow: str) -> None:
    """If fetching main fails during retry, the loop must not push without re-checking main."""
    res = _run_metadata_step(
        tmp_path,
        release_workflow,
        tag_name="v2026.11.622",
        local_versions=("2026.11.622", "2026.11.620"),
        race_versions=("2026.11.621", "2026.11.620"),
        race_mode="before",
        fail_retry_main_fetch=True,
    )
    assert res.proc.returncode != 0
    assert res.push_attempts == 1
    assert not any(call.startswith(("pr edit", "pr create")) for call in res.gh_calls)


def test_appcast_metadata_skips_when_main_already_newer(tmp_path: Path, release_workflow: str) -> None:
    """An older release skips cleanly as superseded when main already contains a strictly newer appcast release."""
    res = _run_metadata_step(
        tmp_path,
        release_workflow,
        tag_name="v2026.11.621",
        local_versions=("2026.11.621", "2026.11.620"),
        advance_main_versions=("2026.11.622", "2026.11.620"),
    )
    assert res.proc.returncode == 0, res.proc.stderr
    assert res.outputs == {"superseded": "true"}
    assert res.push_attempts == 0
    assert "2026.11.621" not in res.remote_appcast
    assert not any(call.startswith(("pr edit", "pr create")) for call in res.gh_calls)


def test_appcast_metadata_no_diff_when_main_already_newer_marks_superseded(
    tmp_path: Path,
    release_workflow: str,
) -> None:
    """A no-diff run whose checked-out main already has a newer release still emits superseded=true."""
    res = _run_metadata_step(
        tmp_path,
        release_workflow,
        tag_name="v2026.11.621",
        main_versions=("2026.11.622", "2026.11.620"),
        local_versions=("2026.11.622", "2026.11.620"),
    )
    assert res.proc.returncode == 0, res.proc.stderr
    assert res.outputs == {"superseded": "true"}
    assert res.push_attempts == 0
    assert not any(call.startswith(("pr edit", "pr create")) for call in res.gh_calls)


def test_appcast_metadata_no_diff_same_tag_marks_not_superseded(tmp_path: Path, release_workflow: str) -> None:
    """An up-to-date same-tag rerun with no diff emits superseded=false so Homebrew dispatch still runs."""
    res = _run_metadata_step(
        tmp_path,
        release_workflow,
        tag_name="v2026.11.622",
        main_versions=("2026.11.622", "2026.11.620"),
        local_versions=("2026.11.622", "2026.11.620"),
    )
    assert res.proc.returncode == 0, res.proc.stderr
    assert res.outputs == {"superseded": "false"}
    assert res.push_attempts == 0
    assert not any(call.startswith(("pr edit", "pr create")) for call in res.gh_calls)


def test_appcast_metadata_no_diff_when_remote_branch_newer_marks_superseded(
    tmp_path: Path,
    release_workflow: str,
) -> None:
    """A no-diff rerun still marks superseded when release-metadata/mindroom already has a newer release."""
    res = _run_metadata_step(
        tmp_path,
        release_workflow,
        tag_name="v2026.11.621",
        main_versions=("2026.11.621", "2026.11.620"),
        local_versions=("2026.11.621", "2026.11.620"),
        remote_branch_versions=("2026.11.622", "2026.11.620"),
    )
    assert res.proc.returncode == 0, res.proc.stderr
    assert res.outputs == {"superseded": "true"}
    assert res.push_attempts == 0
    assert "2026.11.622" in res.remote_appcast
    assert not any(call.startswith(("pr edit", "pr create")) for call in res.gh_calls)


def test_appcast_metadata_skips_pr_update_when_branch_moves_after_push(tmp_path: Path, release_workflow: str) -> None:
    """If another run advances the branch after push succeeds, this run skips editing the PR and marks superseded."""
    res = _run_metadata_step(
        tmp_path,
        release_workflow,
        tag_name="v2026.11.621",
        local_versions=("2026.11.621", "2026.11.620"),
        race_versions=("2026.11.622", "2026.11.620"),
        race_mode="after",
    )
    assert res.proc.returncode == 0, res.proc.stderr
    assert res.outputs == {"superseded": "true"}
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
