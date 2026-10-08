"""The dreaming built-in: what it reviews, how it validates a proposal, and when it applies one."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch
from urllib.parse import quote

import pytest

from mindroom.automations.dreaming import check_dreaming
from mindroom.automations.steps import Ask, AutomationContext, Done
from mindroom.automations.threads import automations_tracking_root, record_automation_thread
from mindroom.config.agent import AgentConfig
from mindroom.config.automations import DreamingAutomation
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.runtime_resolution import resolve_agent_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

TODAY = datetime.now(UTC).date()
YESTERDAY = f"memory/{TODAY - timedelta(days=1)}.md"
TODAY_NOTE = f"memory/{TODAY}.md"
PROJECTS = "\n".join(f"- Project {index} is owned by person {index}." for index in range(20)) + "\n"
EXPORT = "thread_exports/room/thread.yaml"


class _Workspace:
    """An agent with file memory, its config, and helpers to play the dream and review runs."""

    def __init__(self, tmp_path: Path, **agent_fields: object) -> None:
        self.tmp_path = tmp_path
        self.agent_fields = agent_fields
        self.config, self.paths, self.root = self._build()

    def _build(self) -> tuple[Config, RuntimePaths, Path]:
        agent = AgentConfig(
            display_name="Mind",
            memory_backend="file",
            automations=[DreamingAutomation()],
            **self.agent_fields,
        )
        config = Config(agents={"mind": agent}, router=RouterConfig(model="default"))
        paths = resolve_runtime_paths(config_path=self.tmp_path / "config.yaml", storage_path=self.tmp_path)
        root = resolve_agent_runtime("mind", config, paths, None, create=True).file_memory_root
        assert root is not None
        return config, paths, root

    def write(self, path: str, text: str, *, age: timedelta | None = None) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        if age is not None:
            moment = (datetime.now(UTC) - age).timestamp()
            os.utime(target, (moment, moment))

    def read(self, path: str) -> str:
        return (self.root / path).read_text(encoding="utf-8")

    def check(self) -> Ask | None:
        (entry,) = self.config.resolve_entity("mind").automations
        return check_dreaming(
            AutomationContext(
                agent_name="mind",
                config=self.config,
                runtime_paths=self.paths,
                entry=entry,
                options={},
                settings={},
                workspace=self.root,
                state_dir=automations_tracking_root(self.paths) / "mind",
            ),
        )

    def run_dir(self) -> Path:
        runs = sorted((self.root / ".mindroom/dreaming/runs").iterdir())
        return runs[-1]

    def agenda(self) -> str:
        return (self.run_dir() / "agenda.md").read_text(encoding="utf-8")

    def stage(self, path: str, text: str | None) -> None:
        target = self.run_dir() / "staging" / path
        if text is None:
            target.unlink()
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def dream(
        self,
        ask: Ask,
        *,
        report: str = "Handled every item.\nDREAM: DONE\n",
        timed_out: bool = False,
    ) -> Ask | Done:
        (self.run_dir() / "report.md").write_text(report, encoding="utf-8")
        assert ask.then is not None
        return ask.then(self.config, "$dream", timed_out)

    def review(self, ask: Ask | Done, verdict: str | None, *, timed_out: bool = False) -> Done:
        assert isinstance(ask, Ask)
        assert ask.new_thread
        if verdict is not None:
            (self.run_dir() / "verdict.md").write_text(f"Checked every source.\n{verdict}\n", encoding="utf-8")
        assert ask.then is not None
        with patch("mindroom.automations.dreaming.refresh_agent_memory_search") as refresh:
            done = ask.then(self.config, "$verify", timed_out)
            assert isinstance(done, Done)
            # The runner calls this on the event loop.
            assert not refresh.called
            if done.on_loop is not None:
                done.on_loop()
        self.refreshed = refresh.called
        return done

    def state(self) -> dict[str, object]:
        path = self.tmp_path / "tracking" / "automations" / "mind" / "dreaming.json"
        if not path.exists():
            return {"reviewed": {}, "attempted": {}, "pending_run": None, "latest_run": None}
        return json.loads(path.read_text(encoding="utf-8"))


def _workspace(tmp_path: Path, **agent_fields: object) -> _Workspace:
    workspace = _Workspace(tmp_path, **agent_fields)
    workspace.write("memory/projects.md", PROJECTS, age=timedelta(days=30))
    return workspace


def _started(workspace: _Workspace) -> Ask:
    ask = workspace.check()
    assert ask is not None
    return ask


# Check


def test_a_new_agent_with_only_old_files_asks_for_nothing(tmp_path: Path) -> None:
    """Enabling the automation starts from recent history: week-old inputs are seeded as handled."""
    workspace = _workspace(tmp_path)
    workspace.write("memory/2026-01-01.md", "- Old note.\n", age=timedelta(days=30))
    workspace.write(EXPORT, "messages: []\n", age=timedelta(days=30))

    assert workspace.check() is None
    assert set(workspace.state()["reviewed"]) == {"memory/2026-01-01.md", EXPORT}

    workspace.write(EXPORT, "messages: [hello]\n")
    assert workspace.check() is not None


def test_a_freshly_exported_old_conversation_counts_by_its_last_message(tmp_path: Path) -> None:
    """Turning on thread exports writes every old thread today; the room index's last message time decides its age."""
    workspace = _workspace(tmp_path)
    old = int((datetime.now(UTC) - timedelta(days=90)).timestamp() * 1000)
    recent = int(datetime.now(UTC).timestamp() * 1000)
    workspace.write("thread_exports/room/old.yaml", "messages: [long ago]\n")
    workspace.write("thread_exports/room/new.yaml", "messages: [today]\n")
    workspace.write(
        "thread_exports/room/index.json",
        json.dumps(
            {"threads": [{"file": "new.yaml", "last_timestamp": recent}, {"file": "old.yaml", "last_timestamp": old}]},
        ),
    )

    _started(workspace)

    agenda = workspace.agenda()
    assert "- `thread_exports/room/new.yaml`" in agenda
    assert "old.yaml" not in agenda
    assert "thread_exports/room/old.yaml" in workspace.state()["reviewed"]


def test_thread_exports_turned_on_later_start_from_recent_history_too(tmp_path: Path) -> None:
    """An old conversation exported after the automation's first run counts as handled, like on the first run."""
    workspace = _workspace(tmp_path)
    workspace.write("memory/2026-01-01.md", "- Old note.\n", age=timedelta(days=30))
    assert workspace.check() is None
    assert "memory/2026-01-01.md" in workspace.state()["reviewed"]
    old = int((datetime.now(UTC) - timedelta(days=200)).timestamp() * 1000)
    workspace.write("thread_exports/room/old.yaml", "messages: [long ago]\n")
    workspace.write(
        "thread_exports/room/index.json",
        json.dumps({"threads": [{"file": "old.yaml", "last_timestamp": old}]}),
    )

    assert workspace.check() is None
    assert "thread_exports/room/old.yaml" in workspace.state()["reviewed"]


def test_recent_conversations_and_daily_notes_are_on_the_agenda_and_today_is_not(tmp_path: Path) -> None:
    """New exports and past daily notes are reviewed; today's note is still being written, so it is neither input nor staged."""
    workspace = _workspace(tmp_path, context_files=["memory/profile.md"])
    workspace.write(EXPORT, "messages: [hello]\n")
    workspace.write(YESTERDAY, "- Sam moved to Utrecht.\n")
    workspace.write(TODAY_NOTE, "- Fresh note.\n")
    workspace.write("memory/profile.md", "Context file.\n")

    ask = _started(workspace)

    assert ask.new_thread
    assert ask.text.startswith("🌙 Dreaming: reconcile your memory")
    assert "(changed inputs this run: 2)" in ask.text
    agenda = workspace.agenda()
    assert f"- `{EXPORT}`" in agenda
    assert f"- `{YESTERDAY}`" in agenda
    assert TODAY_NOTE not in agenda
    staged = sorted(
        path.relative_to(workspace.run_dir() / "staging").as_posix()
        for path in (workspace.run_dir() / "staging").rglob("*")
        if path.is_file()
    )
    assert staged == sorted([YESTERDAY, "memory/projects.md"])


def test_the_cap_leaves_the_rest_due_for_the_next_run(tmp_path: Path) -> None:
    """At most 40 inputs per run, oldest first; the others come back next time."""
    workspace = _workspace(tmp_path)
    for index in range(45):
        workspace.write(f"thread_exports/room/t{index:02}.yaml", "messages: []\n", age=timedelta(days=6, minutes=index))

    ask = _started(workspace)
    agenda = workspace.agenda()
    assert agenda.count("- `thread_exports/") == 40
    assert "`thread_exports/room/t44.yaml`" in agenda
    assert "`thread_exports/room/t00.yaml`" not in agenda
    assert "5 more changed inputs wait for later runs." in agenda

    done = workspace.dream(ask)
    assert isinstance(done, Done)
    assert done.notice == "Dreaming changed nothing (inputs reviewed: 40)."
    assert done.resolve == ("$dream",)
    _started(workspace)
    assert workspace.agenda().count("- `thread_exports/") == 5


def test_a_rejected_capped_run_returns_only_for_inputs_no_agenda_has_listed(tmp_path: Path) -> None:
    """The input the cap left out starts the next run and leads its agenda; after that, the same evidence waits."""
    workspace = _workspace(tmp_path)
    for index in range(41):
        workspace.write(f"thread_exports/room/t{index:02}.yaml", "messages: []\n", age=timedelta(days=6, minutes=index))
    ask = _started(workspace)
    assert "`thread_exports/room/t00.yaml`" not in workspace.agenda()
    workspace.stage("memory/projects.md", PROJECTS + "- New fact.\n")
    workspace.review(workspace.dream(ask), "VERDICT: REJECT — wrong source")

    ask = _started(workspace)
    assert "`thread_exports/room/t00.yaml`" in workspace.agenda()
    workspace.stage("memory/projects.md", PROJECTS + "- New fact.\n")
    workspace.review(workspace.dream(ask), "VERDICT: REJECT — still wrong")

    assert workspace.check() is None


def test_a_run_a_restart_cut_short_waits_for_new_evidence(tmp_path: Path) -> None:
    """The agenda counts as attempted once posted, so a chain the runner lost does not start again on the same inputs."""
    workspace = _workspace(tmp_path)
    workspace.write(EXPORT, "messages: [hello]\n")
    _started(workspace)

    assert workspace.check() is None
    workspace.write(YESTERDAY, "- A new note.\n")
    _started(workspace)
    assert f"- `{EXPORT}`" in workspace.agenda()


def test_an_input_a_failed_run_listed_is_not_seeded_while_the_agent_is_idle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seeding only skips history no agenda listed, so a failed run's conversation is still reviewed after a quiet week."""
    workspace = _workspace(tmp_path)
    workspace.write(EXPORT, "messages: [hello]\n", age=timedelta(days=2))
    ask = _started(workspace)
    workspace.dream(ask, report="Ran out of time halfway.\n")
    monkeypatch.setattr("mindroom.automations.dreaming._SEED_AGE", timedelta(days=1))

    assert workspace.check() is None
    workspace.write(YESTERDAY, "- Back from a quiet week.\n")

    _started(workspace)
    assert f"- `{EXPORT}`" in workspace.agenda()


def test_an_entry_that_vanishes_during_the_scan_is_skipped(tmp_path: Path) -> None:
    """Another writer's temporary file can disappear between listing and reading without failing the check."""
    workspace = _workspace(tmp_path)
    workspace.write(EXPORT, "messages: [hello]\n")
    workspace.write("thread_exports/room/.thread.yaml.tmp", "partial\n")
    real_scandir = os.scandir

    class _Vanished:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self.name = entry.name
            self._entry = entry

        def stat(self, *, follow_symlinks: bool) -> os.stat_result:
            if self.name.endswith(".tmp"):
                raise FileNotFoundError(self.name)
            return self._entry.stat(follow_symlinks=follow_symlinks)

    class _Listing:
        def __init__(self, fd: int) -> None:
            self._listing = real_scandir(fd)

        def __enter__(self) -> list[_Vanished]:
            return [_Vanished(entry) for entry in self._listing.__enter__()]

        def __exit__(self, *exc: object) -> None:
            self._listing.__exit__(*exc)

    with patch("mindroom.automations.dreaming.os.scandir", side_effect=_Listing):
        ask = _started(workspace)

    assert f"- `{EXPORT}`" in workspace.agenda()
    assert (workspace.run_dir() / "staging" / "memory" / "projects.md").read_text(encoding="utf-8") == PROJECTS
    assert ask.new_thread


def test_threads_any_automation_started_are_never_read_back(tmp_path: Path) -> None:
    """Threads the runner recorded for any built-in, such as a prompt_curation thread, are never read as conversations."""
    workspace = _workspace(tmp_path)
    workspace.write(EXPORT, "messages: [hello]\n")
    ask = _started(workspace)
    workspace.dream(ask)
    record_automation_thread(workspace.paths, "$curation:example.test")
    export = f"thread_exports/room/{quote('$curation:example.test', safe='')}.yaml"
    workspace.write(export, "messages: [maintenance]\n")

    assert workspace.check() is None


def test_an_agent_without_a_workspace_yet_is_skipped_quietly(tmp_path: Path) -> None:
    """A workspace appears with the agent's first turn; until then there is nothing to reconcile and no warning."""
    workspace = _Workspace(tmp_path)
    workspace.root.rmdir()

    assert workspace.check() is None


def test_old_runs_are_pruned_but_unapplied_proposals_are_kept(tmp_path: Path) -> None:
    """The newest 30 run directories stay for undo, plus every proposal the next run still carries."""
    workspace = _workspace(tmp_path)
    state_path = tmp_path / "tracking/automations/mind/dreaming.json"
    state_path.parent.mkdir(parents=True)
    state = {**workspace.state(), "pending_run": "20260101T000000000000Z"}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    runs = workspace.root / ".mindroom/dreaming/runs"
    for index in range(35):
        (runs / f"20260101T0000{index:02}000000Z" / "staging").mkdir(parents=True)
    workspace.write(EXPORT, "messages: [hello]\n")

    _started(workspace)

    names = sorted(path.name for path in runs.iterdir())
    # The pending run, the newest 30 older ones, and the run that just started.
    assert names[0] == "20260101T000000000000Z"
    assert names[1] == "20260101T000005000000Z"
    assert len(names) == 32


# Validate


@pytest.mark.parametrize(
    ("report", "timed_out"),
    [("Handled every item.\nDREAM: DONE\n", True), ("Ran out of time halfway.\n", False)],
)
def test_an_unfinished_dream_is_incomplete_and_keeps_its_inputs_due(
    tmp_path: Path,
    report: str,
    timed_out: bool,
) -> None:
    """Without the completion line, or after the fallback, nothing is acknowledged, and new evidence brings the inputs back."""
    workspace = _workspace(tmp_path)
    workspace.write(EXPORT, "messages: [hello]\n")
    ask = _started(workspace)
    workspace.stage("memory/projects.md", PROJECTS + "- New fact.\n")

    done = workspace.dream(ask, report=report, timed_out=timed_out)

    assert isinstance(done, Done)
    assert done.notice is not None
    assert done.notice.startswith("⚠️ Dreaming stopped:")
    assert not (workspace.run_dir() / "staging").exists()
    assert EXPORT not in workspace.state()["reviewed"]
    assert workspace.check() is None
    workspace.write(YESTERDAY, "- Another note.\n")
    _started(workspace)
    assert f"- `{EXPORT}`" in workspace.agenda()


def test_a_valid_proposal_is_saved_as_a_patch_and_reviewed_in_a_thread_of_its_own(tmp_path: Path) -> None:
    """Moves, dedupes, and annotations become one reversible patch that a fresh run reviews."""
    workspace = _workspace(tmp_path)
    workspace.write(YESTERDAY, "- Sam moved to Utrecht.\n- Prefers tea.\n")
    workspace.write("memory/people.md", "- Prefers tea.\n", age=timedelta(days=30))
    ask = _started(workspace)
    workspace.stage(YESTERDAY, "- Sam moved to Utrecht. (moved to memory/people.md)\n")
    workspace.stage("memory/people.md", "- Prefers tea.\n- Sam moved to Utrecht.\n")

    review = workspace.dream(ask)

    assert isinstance(review, Ask)
    assert review.new_thread
    assert review.text.startswith("🔍 Dreaming review:")
    patch_text = (workspace.run_dir() / "proposal.patch").read_text(encoding="utf-8")
    assert f"--- a/{YESTERDAY}" in patch_text
    assert "+- Sam moved to Utrecht." in patch_text


@pytest.mark.parametrize("path", [TODAY_NOTE, "memory/profile.md", "memory/notes.txt", "MEMORY.md"])
def test_staged_files_outside_what_the_run_may_change_are_findings(tmp_path: Path, path: str) -> None:
    """Today's note, context files, non-Markdown files, and anything outside memory/ stop the run unapplied."""
    workspace = _workspace(tmp_path, context_files=["memory/profile.md"])
    workspace.write(EXPORT, "messages: [hello]\n")
    ask = _started(workspace)
    if path == "MEMORY.md":
        (workspace.run_dir() / "staging" / path).write_text("# Memory\n", encoding="utf-8")
    else:
        workspace.stage(path, "New text.\n")

    done = workspace.dream(ask)

    assert isinstance(done, Done)
    assert done.notice.startswith("⚠️ Dreaming stopped:")
    assert path in done.notice
    assert workspace.state()["pending_run"] is None


def test_a_staged_link_is_a_finding_and_never_followed(tmp_path: Path) -> None:
    """Worker code writes staging, so a link there is reported instead of read."""
    workspace = _workspace(tmp_path)
    workspace.write(EXPORT, "messages: [hello]\n")
    workspace.write("secret.md", "outside memory\n")
    ask = _started(workspace)
    (workspace.run_dir() / "staging" / "memory" / "link.md").symlink_to(workspace.root / "secret.md")

    done = workspace.dream(ask)

    assert isinstance(done, Done)
    assert "memory/link.md cannot be staged" in done.notice


def test_a_run_that_changes_nothing_acknowledges_its_inputs_unless_memory_moved(tmp_path: Path) -> None:
    """No change records progress, but only when memory still matches what the run saw."""
    workspace = _workspace(tmp_path)
    workspace.write(EXPORT, "messages: [hello]\n")
    ask = _started(workspace)
    workspace.write("memory/projects.md", PROJECTS + "- Written by another conversation.\n")

    done = workspace.dream(ask)

    assert isinstance(done, Done)
    assert done.notice == "⚠️ Memory changed during the run, so a later run reviews these inputs again."
    assert EXPORT not in workspace.state()["reviewed"]
    assert workspace.state()["pending_run"] is None


# Apply


def test_an_approved_proposal_writes_only_what_changed_and_resolves_both_threads(tmp_path: Path) -> None:
    """Changed, created, and deleted files are applied; untouched files keep their bytes and times."""
    workspace = _workspace(tmp_path)
    workspace.write("memory/stale.md", "- Duplicate of project 1.\n", age=timedelta(days=30))
    workspace.write("memory/untouched.md", "- Keep me.\n", age=timedelta(days=30))
    workspace.write(YESTERDAY, "- Project 3 now belongs to person 7.\n")
    untouched_mtime = (workspace.root / "memory/untouched.md").stat().st_mtime_ns
    ask = _started(workspace)
    corrected = PROJECTS.replace(
        "- Project 3 is owned by person 3.",
        f"- Project 3 is owned by person 7 (source: {YESTERDAY}).",
    )
    workspace.stage("memory/projects.md", corrected)
    workspace.stage("memory/people/person-7.md", "- Owns project 3.\n")
    workspace.stage("memory/stale.md", None)
    review = workspace.dream(ask)

    done = workspace.review(review, "VERDICT: APPROVE")

    assert done.notice == "✅ Dreaming applied the reviewed proposal (files written: 2, removed: 1)."
    assert done.resolve == ("$dream", "$verify")
    assert workspace.read("memory/projects.md") == corrected
    assert workspace.read("memory/people/person-7.md") == "- Owns project 3.\n"
    assert not (workspace.root / "memory/stale.md").exists()
    assert (workspace.root / "memory/untouched.md").stat().st_mtime_ns == untouched_mtime
    assert workspace.refreshed
    assert not (workspace.run_dir() / "staging").exists()
    assert (workspace.run_dir() / "proposal.patch").exists()
    assert workspace.check() is None


def test_a_proposal_awaiting_review_is_already_pending(tmp_path: Path) -> None:
    """A restart before the review finishes still leaves the proposal for the next run to carry forward."""
    workspace = _workspace(tmp_path)
    workspace.write(EXPORT, "messages: [hello]\n")
    ask = _started(workspace)
    workspace.stage("memory/projects.md", PROJECTS + "- New fact.\n")

    assert isinstance(workspace.dream(ask), Ask)

    assert workspace.state()["pending_run"] == workspace.run_dir().name


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_an_applied_patch_reverses_exactly_with_git_apply(tmp_path: Path) -> None:
    """The kept patch undoes the change, including files without a final newline."""
    workspace = _workspace(tmp_path)
    workspace.write("memory/plain.md", "- First.\n- Last line without newline.", age=timedelta(days=30))
    workspace.write(EXPORT, "messages: [hello]\n")
    ask = _started(workspace)
    workspace.stage("memory/plain.md", "- First.\n- Changed last line without newline.")
    workspace.stage("memory/new.md", "- Created without newline.")
    review = workspace.dream(ask)
    workspace.review(review, "VERDICT: APPROVE")

    patch_path = workspace.run_dir() / "proposal.patch"
    subprocess.run(["git", "apply", "-R", str(patch_path)], cwd=workspace.root, check=True)

    assert workspace.read("memory/plain.md") == "- First.\n- Last line without newline."
    assert not (workspace.root / "memory/new.md").exists()


def test_applied_bytes_are_the_validated_ones_even_when_staging_changes_later(tmp_path: Path) -> None:
    """A run still editing staging after validation cannot change what is applied."""
    workspace = _workspace(tmp_path)
    workspace.write(EXPORT, "messages: [hello]\n")
    ask = _started(workspace)
    workspace.stage("memory/projects.md", PROJECTS + "- Validated fact.\n")
    review = workspace.dream(ask)
    workspace.stage("memory/projects.md", "Rewritten after validation.\n")

    workspace.review(review, "VERDICT: APPROVE")

    assert workspace.read("memory/projects.md") == PROJECTS + "- Validated fact.\n"


def test_a_daily_note_the_run_edited_is_not_due_again(tmp_path: Path) -> None:
    """A reviewed daily note is recorded at the version the run wrote, so its own annotation does not bring it back."""
    workspace = _workspace(tmp_path)
    workspace.write(YESTERDAY, "- Sam moved to Utrecht.\n")
    ask = _started(workspace)
    workspace.stage(YESTERDAY, "- Sam moved to Utrecht. (now in memory/people.md)\n")
    workspace.stage("memory/people.md", "- Sam lives in Utrecht.\n")

    workspace.review(workspace.dream(ask), "VERDICT: APPROVE")

    assert workspace.check() is None


@pytest.mark.parametrize(
    ("done_line", "verdict"),
    [
        ("- DREAM: DONE", "- VERDICT: APPROVE"),
        ("DREAM: DONE.", "VERDICT: APPROVE."),
        ("**DREAM:** DONE", "**VERDICT:** APPROVE"),
        ("> `DREAM: DONE`", "> VERDICT: APPROVE"),
    ],
)
def test_completion_and_verdict_lines_with_markdown_marks_still_count(
    tmp_path: Path,
    done_line: str,
    verdict: str,
) -> None:
    """A copied bullet, bold label, quote, code span, or closing period is read as the model meant it."""
    workspace = _workspace(tmp_path)
    workspace.write(EXPORT, "messages: [hello]\n")
    ask = _started(workspace)
    workspace.stage("memory/projects.md", PROJECTS + "- New fact.\n")

    done = workspace.review(workspace.dream(ask, report=f"Done.\n{done_line}\n"), verdict)

    assert done.notice.startswith("✅ Dreaming applied")


def test_an_applied_file_keeps_its_permissions(tmp_path: Path) -> None:
    """Rewriting a memory file keeps its mode, like the memory tool does, and a created file is readable like a written one."""
    workspace = _workspace(tmp_path)
    (workspace.root / "memory/projects.md").chmod(0o640)
    workspace.write(EXPORT, "messages: [hello]\n")
    ask = _started(workspace)
    workspace.stage("memory/projects.md", PROJECTS + "- New fact.\n")
    workspace.stage("memory/preferences.md", "- New preference.\n")

    workspace.review(workspace.dream(ask), "VERDICT: APPROVE")

    assert (workspace.root / "memory/projects.md").stat().st_mode & 0o777 == 0o640
    assert (workspace.root / "memory/preferences.md").stat().st_mode & 0o777 == 0o644


def test_a_verdict_the_dream_left_behind_never_counts(tmp_path: Path) -> None:
    """Only the review writes the verdict, so an approval written during the dream is removed before the review starts."""
    workspace = _workspace(tmp_path)
    workspace.write(EXPORT, "messages: [hello]\n")
    ask = _started(workspace)
    workspace.stage("memory/projects.md", PROJECTS + "- New fact.\n")
    (workspace.run_dir() / "verdict.md").write_text("VERDICT: APPROVE\n", encoding="utf-8")

    done = workspace.review(workspace.dream(ask), None)

    assert done.notice.startswith(
        "⚠️ Dreaming was not applied: the review did not end with one of the two verdict lines.",
    )
    assert workspace.read("memory/projects.md") == PROJECTS


@pytest.mark.parametrize(
    ("verdict", "timed_out", "reason"),
    [
        ("VERDICT: REJECT — the move drops a measured result.", False, "the move drops a measured result"),
        (None, False, "the review did not end with one of the two verdict lines"),
        ("Looks fine to me.", False, "the review did not end with one of the two verdict lines"),
        ("VERDICT: APPROVE", True, "the review did not finish within an hour"),
        ("VERDICT: APPROVE...", False, "the review did not end with one of the two verdict lines"),
        ("VERDICT: APPROVE\n.", False, "the review did not end with one of the two verdict lines"),
        (
            "VERDICT: APPROVE-WITH-CHANGES - remove the unsupported claim first",
            False,
            "the review did not end with one of the two verdict lines",
        ),
        (
            "VERDICT: APPROVE only after correcting the date",
            False,
            "the review did not end with one of the two verdict lines",
        ),
        (
            "VERDICT: APPROVE-WITH-NOTES — count the moved lines",
            False,
            "the review did not end with one of the two verdict lines",
        ),
    ],
)
def test_anything_but_an_approval_applies_nothing_and_carries_the_proposal(
    tmp_path: Path,
    verdict: str | None,
    timed_out: bool,
    reason: str,
) -> None:
    """A rejection, a missing or unparseable verdict, or a review past the fallback fails closed and waits for new evidence."""
    workspace = _workspace(tmp_path)
    workspace.write(EXPORT, "messages: [hello]\n")
    ask = _started(workspace)
    first_run = workspace.run_dir().name
    workspace.stage("memory/projects.md", PROJECTS + "- New fact.\n")

    done = workspace.review(workspace.dream(ask), verdict, timed_out=timed_out)

    assert done.notice == (f"⚠️ Dreaming was not applied: {reason}. The next run carries the proposal forward.")
    assert done.resolve == ()
    assert workspace.read("memory/projects.md") == PROJECTS
    assert workspace.state()["pending_run"] == first_run
    assert EXPORT not in workspace.state()["reviewed"]
    assert workspace.check() is None


def test_an_unresolved_proposal_stays_pending_across_rejected_successors_until_one_applies(tmp_path: Path) -> None:
    """Job 0 names the oldest and the newest unapplied proposals in every later run until one applies."""
    workspace = _workspace(tmp_path)
    workspace.write(EXPORT, "messages: [hello]\n")
    ask = _started(workspace)
    first_run = workspace.run_dir().name
    workspace.stage("memory/projects.md", PROJECTS + "- First attempt.\n")
    workspace.review(workspace.dream(ask), "VERDICT: REJECT — wrong source")
    workspace.write(YESTERDAY, "- First note.\n")

    ask = _started(workspace)
    second_run = workspace.run_dir().name
    assert f"- `.mindroom/dreaming/runs/{first_run}/`" in workspace.agenda()
    workspace.stage("memory/projects.md", PROJECTS + "- Second attempt.\n")
    workspace.review(workspace.dream(ask), "VERDICT: REJECT — still wrong")
    assert (workspace.state()["pending_run"], workspace.state()["latest_run"]) == (first_run, second_run)
    workspace.write(YESTERDAY, "- First note, then a second.\n")

    ask = _started(workspace)
    agenda = workspace.agenda()
    assert f"- `.mindroom/dreaming/runs/{first_run}/`" in agenda
    assert f"- `.mindroom/dreaming/runs/{second_run}/`" in agenda
    workspace.stage("memory/projects.md", PROJECTS + "- Third attempt.\n")
    workspace.review(workspace.dream(ask), "VERDICT: APPROVE")

    assert (workspace.state()["pending_run"], workspace.state()["latest_run"]) == (None, None)
    assert workspace.check() is None


@pytest.mark.parametrize("change", ["touched", "untouched", "created", "vanished"])
def test_memory_that_changed_during_the_run_blocks_the_whole_proposal(tmp_path: Path, change: str) -> None:
    """Any in-scope change since the run fired, not only to touched files, rejects the batch."""
    workspace = _workspace(tmp_path)
    workspace.write("memory/other.md", "- Other fact.\n", age=timedelta(days=30))
    workspace.write(EXPORT, "messages: [hello]\n")
    ask = _started(workspace)
    workspace.stage("memory/projects.md", PROJECTS + "- New fact.\n")
    workspace.stage("memory/new.md", "- Created by the run.\n")
    review = workspace.dream(ask)
    if change == "touched":
        workspace.write("memory/projects.md", PROJECTS + "- Written meanwhile.\n")
    elif change == "untouched":
        workspace.write("memory/other.md", "- Other fact, edited meanwhile.\n")
    elif change == "created":
        workspace.write("memory/new.md", "- Written meanwhile at the same path.\n")
    else:
        (workspace.root / "memory/other.md").unlink()

    done = workspace.review(review, "VERDICT: APPROVE")

    assert done.notice == (
        "⚠️ Memory changed during the run, so the approved proposal was not applied. The next run carries it forward."
    )
    assert "- New fact." not in workspace.read("memory/projects.md")
    assert workspace.state()["pending_run"] is not None


def test_a_file_that_became_a_context_file_during_the_run_is_not_written(tmp_path: Path) -> None:
    """A config reload that adds a proposed file to context_files blocks the apply, like any memory change."""
    workspace = _workspace(tmp_path)
    workspace.write(EXPORT, "messages: [hello]\n")
    ask = _started(workspace)
    workspace.stage("memory/projects.md", PROJECTS + "- New fact.\n")
    review = workspace.dream(ask)
    workspace.config.agents["mind"].context_files = ["memory/projects.md"]

    done = workspace.review(review, "VERDICT: APPROVE")

    assert done.notice.startswith("⚠️ Memory changed during the run")
    assert workspace.read("memory/projects.md") == PROJECTS


def test_a_linked_memory_directory_fails_the_check_loudly(tmp_path: Path) -> None:
    """A memory/ replaced by a link is refused instead of followed."""
    workspace = _Workspace(tmp_path)
    (workspace.root / "elsewhere").mkdir()
    (workspace.root / "memory").symlink_to(workspace.root / "elsewhere")

    with pytest.raises(OSError):  # noqa: PT011 - the platform names the error
        workspace.check()
