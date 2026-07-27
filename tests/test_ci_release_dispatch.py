"""Tests for serialized, idempotent CalVer release creation and publisher dispatch."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
CALVER_WORKFLOW = WORKFLOW_DIR / "calver-auto-release.yml"
BUILD_MINDROOM_WORKFLOW = WORKFLOW_DIR / "build-mindroom.yml"

OUR_SHA = "1111111111111111111111111111111111111111"
TAG_OBJECT_SHA = "2222222222222222222222222222222222222222"
OTHER_SHA = "3333333333333333333333333333333333333333"


def _load_workflow(path: Path) -> dict[str, Any]:
    """Parse a workflow file, normalizing the YAML 1.1 ``on`` -> ``True`` key."""
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    if True in workflow:
        workflow["on"] = workflow.pop(True)
    return workflow


def _steps(workflow: dict[str, Any], job: str) -> list[dict[str, Any]]:
    """Return the steps of one job."""
    return workflow["jobs"][job]["steps"]


def _step(workflow: dict[str, Any], job: str, step_id: str) -> dict[str, Any]:
    """Return the single step with ``step_id`` in ``job``."""
    matches = [step for step in _steps(workflow, job) if step.get("id") == step_id]
    assert len(matches) == 1, f"expected exactly one step with id {step_id!r}, got {len(matches)}"
    return matches[0]


@pytest.fixture(scope="module")
def calver_workflow() -> dict[str, Any]:
    """Return the parsed CalVer release workflow."""
    return _load_workflow(CALVER_WORKFLOW)


@pytest.fixture(scope="module")
def build_mindroom_workflow() -> dict[str, Any]:
    """Return the parsed MindRoom image build workflow."""
    return _load_workflow(BUILD_MINDROOM_WORKFLOW)


def _run_bash(script: str, env: dict[str, str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run a workflow ``run:`` script with a stubbed ``gh`` on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "gh"
    stub.write_text(
        """#!/usr/bin/env bash
echo "$@" >>"$GH_CALLS"
case "$*" in
  *git/ref/tags/*) echo "$STUB_TAG_OBJECT" ;;
  *git/tags/*) echo "$STUB_TAG_COMMIT" ;;
  *compare/*) echo "$STUB_COMPARE_STATUS" ;;
  "workflow run "*)
    for failing in $STUB_FAILING_WORKFLOWS; do
      [[ "$*" == *"$failing"* ]] && exit 1
    done
    ;;
  *) echo "unexpected gh invocation: $*" >&2; exit 64 ;;
esac
""",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    calls = tmp_path / "gh-calls.txt"
    calls.write_text("", encoding="utf-8")
    outputs = tmp_path / "github-output.txt"
    outputs.write_text("", encoding="utf-8")
    return subprocess.run(
        ["bash", "-e", "-c", script],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "GH_CALLS": str(calls),
            "GITHUB_OUTPUT": str(outputs),
            "STUB_TAG_OBJECT": "",
            "STUB_TAG_COMMIT": "",
            "STUB_COMPARE_STATUS": "",
            "STUB_FAILING_WORKFLOWS": "",
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _claim_script(calver_workflow: dict[str, Any]) -> str:
    """Return the tag-claim step's shell script."""
    return _step(calver_workflow, "release", "claim_release_tag")["run"]


def _claim_env(**stub: str) -> dict[str, str]:
    """Return the environment the tag-claim step runs with."""
    return {
        "GH_TOKEN": "token",
        "GITHUB_REPOSITORY": "mindroom-ai/mindroom",
        "GITHUB_SHA": OUR_SHA,
        "RELEASE_REF": "v2026.7.269",
        "STUB_TAG_OBJECT": f"tag {TAG_OBJECT_SHA}",
        **stub,
    }


def test_calver_release_creation_is_serialized(calver_workflow: dict[str, Any]) -> None:
    """Concurrent runs must queue, since each derives its tag from the tags it can see."""
    concurrency = calver_workflow["concurrency"]
    assert concurrency["group"]
    assert "github.run_id" not in concurrency["group"], "a per-run group serializes nothing"
    assert concurrency["cancel-in-progress"] is False


def test_publisher_dispatch_requires_owning_the_release_tag(calver_workflow: dict[str, Any]) -> None:
    """Only the run whose tag claim succeeded may dispatch publishers."""
    dispatch = _step(calver_workflow, "release", "dispatch_publishers")
    assert dispatch["if"] == "steps.claim_release_tag.outputs.dispatch == 'true'"


def test_tag_claim_dispatches_when_the_tag_points_at_this_run(
    calver_workflow: dict[str, Any],
    tmp_path: Path,
) -> None:
    """The run that created the tag owns the publish set for it."""
    result = _run_bash(
        _claim_script(calver_workflow),
        _claim_env(STUB_TAG_COMMIT=OUR_SHA),
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert "dispatch=true" in (tmp_path / "github-output.txt").read_text(encoding="utf-8")


def test_tag_claim_skips_dispatch_when_another_run_already_released_this_commit(
    calver_workflow: dict[str, Any],
    tmp_path: Path,
) -> None:
    """A run that lost the tag race to a release containing its commit must not double-publish."""
    result = _run_bash(
        _claim_script(calver_workflow),
        _claim_env(STUB_TAG_COMMIT=OTHER_SHA, STUB_COMPARE_STATUS="behind"),
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    outputs = (tmp_path / "github-output.txt").read_text(encoding="utf-8")
    assert "dispatch=false" in outputs
    assert "dispatch=true" not in outputs


def test_tag_claim_fails_when_the_tag_does_not_contain_this_commit(
    calver_workflow: dict[str, Any],
    tmp_path: Path,
) -> None:
    """A tag that omits the merged commit is a lost release, not a silent no-op."""
    result = _run_bash(
        _claim_script(calver_workflow),
        _claim_env(STUB_TAG_COMMIT=OTHER_SHA, STUB_COMPARE_STATUS="diverged"),
        tmp_path,
    )

    assert result.returncode != 0
    assert OUR_SHA in result.stderr
    assert "dispatch=true" not in (tmp_path / "github-output.txt").read_text(encoding="utf-8")


def test_tag_claim_dereferences_annotated_tags(
    calver_workflow: dict[str, Any],
    tmp_path: Path,
) -> None:
    """CalVer tags are annotated, so the ref object is a tag object, not the commit."""
    result = _run_bash(
        _claim_script(calver_workflow),
        _claim_env(STUB_TAG_COMMIT=OUR_SHA),
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "gh-calls.txt").read_text(encoding="utf-8")
    assert f"git/tags/{TAG_OBJECT_SHA}" in calls


def _release_publishers() -> set[str]:
    """Return every workflow that publishes a release when handed a ``release_ref``."""
    publishers = {
        path.name
        for path in sorted(WORKFLOW_DIR.glob("*.yml"))
        if "release_ref" in (_load_workflow(path)["on"].get("workflow_dispatch") or {}).get("inputs", {})
    }
    assert publishers, "expected at least one release publisher workflow"
    return publishers


def test_dispatch_covers_every_release_ref_publisher_exactly_once(
    calver_workflow: dict[str, Any],
    tmp_path: Path,
) -> None:
    """Every workflow taking a ``release_ref`` input is dispatched once per release."""
    publishers = _release_publishers()
    dispatch = _step(calver_workflow, "release", "dispatch_publishers")
    result = _run_bash(
        dispatch["run"],
        {
            "GH_TOKEN": "token",
            "PUBLISH_WORKFLOW_REF": "main",
            "RELEASE_REF": "v2026.7.269",
        },
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    dispatched = re.findall(r"workflow run (\S+\.yml)", (tmp_path / "gh-calls.txt").read_text(encoding="utf-8"))
    assert sorted(dispatched) == sorted(publishers)
    assert len(dispatched) == len(set(dispatched))
    assert all("--field release_ref=v2026.7.269" in line for line in _dispatch_lines(tmp_path))


def _dispatch_lines(tmp_path: Path) -> list[str]:
    """Return the recorded ``gh workflow run`` invocations."""
    calls = (tmp_path / "gh-calls.txt").read_text(encoding="utf-8").splitlines()
    return [line for line in calls if line.startswith("workflow run ")]


def test_dispatch_attempts_all_publishers_then_fails(
    calver_workflow: dict[str, Any],
    tmp_path: Path,
) -> None:
    """One failing dispatch must not silently drop the remaining publishers."""
    dispatch = _step(calver_workflow, "release", "dispatch_publishers")
    result = _run_bash(
        dispatch["run"],
        {
            "GH_TOKEN": "token",
            "PUBLISH_WORKFLOW_REF": "main",
            "RELEASE_REF": "v2026.7.269",
            "STUB_FAILING_WORKFLOWS": "build-platform.yml",
        },
        tmp_path,
    )

    assert result.returncode != 0
    assert "build-platform.yml" in result.stderr
    assert len(_dispatch_lines(tmp_path)) == len(_release_publishers())


def test_build_mindroom_does_not_run_on_main_pushes(build_mindroom_workflow: dict[str, Any]) -> None:
    """Release images come from the CalVer dispatch, so main pushes must not rebuild them."""
    triggers = build_mindroom_workflow["on"]
    assert "push" not in triggers
    assert set(triggers) == {"pull_request", "workflow_dispatch"}


def test_build_mindroom_still_validates_pull_requests(build_mindroom_workflow: dict[str, Any]) -> None:
    """PRs must keep building both Dockerfiles without publishing them."""
    pull_request = build_mindroom_workflow["on"]["pull_request"]
    assert pull_request["branches"] == ["main"]
    assert "local/instances/deploy/Dockerfile.*" in pull_request["paths"]

    build_step = next(
        step for step in _steps(build_mindroom_workflow, "build") if step["name"] == "Build and push image"
    )
    assert build_step["with"]["push"] == "${{ github.event_name == 'workflow_dispatch' }}"


def test_build_mindroom_publishes_release_images_on_dispatch(
    build_mindroom_workflow: dict[str, Any],
) -> None:
    """Manifest creation stays tied to release dispatches."""
    manifest = build_mindroom_workflow["jobs"]["manifest"]
    assert manifest["if"] == "github.event_name == 'workflow_dispatch'"
    assert manifest["needs"] == "build"
