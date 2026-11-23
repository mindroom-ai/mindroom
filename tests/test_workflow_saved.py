"""Tests for saved workflows: file storage, AST parsing, snapshotting, toolkit, SDK run(), and !workflows."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from mindroom.workflow_runs.saved import (
    SavedWorkflow,
    SavedWorkflowExistsError,
    list_saved,
    read_saved_source,
    save_source,
)


def test_list_saved_parses_without_running_the_file(tmp_path: Path) -> None:
    """Listing saved workflows reads docstrings and literal ARGS via AST without importing or executing code."""
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "dangerous.py").write_text(
        """\"\"\"Audit a branch before merging.

More details on line two.
\"\"\"
raise SystemExit(99)

ARGS = {"base": "Branch to compare against", "strict": "Fail on warnings"}

def main(args):
    return args
""",
        encoding="utf-8",
    )
    (workflows / "bare.py").write_text(
        "ARGS = compute_at_runtime()\ndef main(args):\n    return 1\n",
        encoding="utf-8",
    )
    (workflows / "README.md").write_text("ignored", encoding="utf-8")
    (workflows / "__init__.py").write_text("# ignored invalid name", encoding="utf-8")

    listed = list_saved(tmp_path)

    assert listed == [
        SavedWorkflow(name="bare", description="", args={}, path=workflows / "bare.py"),
        SavedWorkflow(
            name="dangerous",
            description="Audit a branch before merging.",
            args={"base": "Branch to compare against", "strict": "Fail on warnings"},
            path=workflows / "dangerous.py",
        ),
    ]


def test_save_refuses_to_replace_a_different_file_and_accepts_an_identical_one(tmp_path: Path) -> None:
    """Saving adds a missing docstring, accepts identical content, and refuses to overwrite different content."""
    source = "def main(args):\n    return 1\n"

    written = save_source(tmp_path, "review-branch", source, description="Review a branch.")
    assert written == tmp_path / "workflows" / "review-branch.py"
    saved_bytes = read_saved_source(tmp_path, "review-branch")
    assert saved_bytes == b"""\"\"\"Review a branch.\"\"\"\n\ndef main(args):\n    return 1\n"""

    # Identical effective content is accepted idempotently.
    assert save_source(tmp_path, "review-branch", source, description="Review a branch.") == written

    # Existing docstring is preserved rather than duplicated.
    with_doc = """\"\"\"Keep me.\"\"\"\n\ndef main(args):\n    return 2\n"""
    kept = save_source(tmp_path, "has-doc", with_doc, description="Ignored description.")
    assert read_saved_source(tmp_path, "has-doc") == with_doc.encode("utf-8")
    assert kept == tmp_path / "workflows" / "has-doc.py"

    # Different content raises the exact plan error message and leaves the file untouched.
    with pytest.raises(
        SavedWorkflowExistsError,
        match=r"^workflows/review-branch\.py already exists with different content\. Pick another name or edit the file\.$",
    ):
        save_source(tmp_path, "review-branch", "def main(args):\n    return 99\n", description="Review a branch.")
    assert read_saved_source(tmp_path, "review-branch") == saved_bytes


def test_two_simultaneous_saves_leave_one_winner(tmp_path: Path) -> None:
    """Two racing saves with different content produce one written file and one SavedWorkflowExistsError."""
    barrier = threading.Barrier(2)
    results: list[Path | BaseException] = []
    lock = threading.Lock()

    def worker(body: str) -> None:
        barrier.wait()
        try:
            outcome: Path | BaseException = save_source(tmp_path, "race", body, description=None)
        except BaseException as exc:
            outcome = exc
        with lock:
            results.append(outcome)

    first = "def main(args):\n    return 1\n"
    second = "def main(args):\n    return 2\n"
    threads = [threading.Thread(target=worker, args=(first,)), threading.Thread(target=worker, args=(second,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    paths = [item for item in results if isinstance(item, Path)]
    errors = [item for item in results if isinstance(item, SavedWorkflowExistsError)]
    assert len(paths) == 1
    assert len(errors) == 1
    assert read_saved_source(tmp_path, "race").decode("utf-8") in {first, second}


@pytest.mark.parametrize("bad_name", ["../x", "Has Space", "", "-lead", "_lead", "a" * 65, "sub/dir", "ok\n"])
def test_bad_names_and_symlinks_are_refused(tmp_path: Path, bad_name: str) -> None:
    """Invalid names are refused by both read and save."""
    with pytest.raises(ValueError, match="workflow name"):
        read_saved_source(tmp_path, bad_name)
    with pytest.raises(ValueError, match="workflow name"):
        save_source(tmp_path, bad_name, "def main(args):\n    return 1\n", description=None)


def test_symlinks_are_refused(tmp_path: Path) -> None:
    """A symlinked workflows directory or symlinked workflow file is refused without following the link."""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.py"
    secret.write_text("def main(args):\n    return 1\n", encoding="utf-8")

    # Symlinked workflows directory.
    ws_dir_link = tmp_path / "ws_dir"
    ws_dir_link.mkdir()
    (ws_dir_link / "workflows").symlink_to(outside)
    symlink_error = r"Too many levels of symbolic links|Not a directory"
    with pytest.raises(OSError, match=symlink_error):
        read_saved_source(ws_dir_link, "secret")
    with pytest.raises(OSError, match=symlink_error):
        save_source(ws_dir_link, "other", "def main(args):\n    return 2\n", description=None)
    with pytest.raises(OSError, match=symlink_error):
        list_saved(ws_dir_link)

    # Symlinked file inside a real workflows directory.
    ws_file_link = tmp_path / "ws_file"
    (ws_file_link / "workflows").mkdir(parents=True)
    (ws_file_link / "workflows" / "secret.py").symlink_to(secret)
    with pytest.raises(OSError, match=symlink_error):
        read_saved_source(ws_file_link, "secret")
    with pytest.raises(OSError, match=symlink_error):
        save_source(ws_file_link, "secret", "def main(args):\n    return 2\n", description=None)
    assert list_saved(ws_file_link) == []
