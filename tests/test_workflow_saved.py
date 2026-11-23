"""Tests for saved workflows: file storage, AST parsing, snapshotting, toolkit, SDK run(), and !workflows."""

from __future__ import annotations

import base64
import hashlib
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

import mindroom.workflow as workflow_sdk
from mindroom.script_runs.models import ScriptRunState
from mindroom.workflow_runs.saved import (
    SavedWorkflow,
    SavedWorkflowExistsError,
    list_saved,
    read_saved_source,
    save_source,
)
from tests.workflow_helpers import (
    FakeWorkflowGateway,
    WorkflowStack,
    completed,
    sdk_env,
    tool_args,
    workflow_stack,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from types import ModuleType


@pytest_asyncio.fixture
async def stack(tmp_path: Path) -> AsyncIterator[WorkflowStack]:
    """Engine harness with a workspace ready for saved workflows."""
    with workflow_stack(tmp_path) as harness:
        yield harness
        await harness.engine.close()


@pytest.fixture
def gateway(tmp_path: Path) -> Iterator[FakeWorkflowGateway]:
    """Fake workflow gateway for pure SDK tests."""
    with FakeWorkflowGateway(tmp_path / "capability") as fake:
        yield fake


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch, gateway: FakeWorkflowGateway) -> ModuleType:
    """SDK module wired to the fake gateway."""
    sdk_env(monkeypatch, gateway)
    return workflow_sdk


@pytest.fixture
def in_main(sdk: ModuleType) -> Iterator[None]:
    """Run the test as if inside `main(args)`."""
    sdk._local.lane = sdk._Lane("")
    yield
    sdk._local.lane = None


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


@pytest.mark.usefixtures("in_main")
def test_sdk_run_calls_main_in_a_child_lane(gateway: FakeWorkflowGateway, sdk: ModuleType) -> None:
    """`run(name, args)` submits a group node and executes the saved workflow in lane `/0:run`."""
    helper_source = (
        b"from mindroom.workflow import tool\n"
        b"def main(args):\n"
        b"    first = tool('files', 'read', path=args['p'])\n"
        b"    second = tool('files', 'stat', path=args['p'])\n"
        b"    return f'{first}:{second}'\n"
    )
    gateway.sources["helper"] = helper_source
    gateway.respond = lambda req: completed(
        f"{req['args']['function']}({req['args']['arguments']['path']})" if req["kind"] == "tool" else None,
    )

    result = sdk.run("helper", {"p": "a.txt"})
    after = sdk.tool("files", "read", path="b.txt")

    assert result == "read(a.txt):stat(a.txt)"
    assert after == "read(b.txt)"
    assert [(node["key"], node["kind"]) for node in gateway.nodes] == [
        ("@0", "group"),
        ("/0:run@0", "tool"),
        ("/0:run@1", "tool"),
        ("@1", "tool"),
    ]
    assert gateway.nodes[0]["args"] == {
        "op": "run",
        "name": "helper",
        "source_sha256": hashlib.sha256(helper_source).hexdigest(),
    }
    assert gateway.nodes[0]["meta"]["members"] == ["/0:run"]


@pytest.mark.asyncio
async def test_run_executes_the_bytes_snapshotted_for_the_attempt(stack: WorkflowStack) -> None:
    """Changing the saved file on disk mid-attempt still returns the attempt's first snapshotted bytes."""
    workspace = stack.context.agent_workspace_root(create=True)
    v1 = b"def main(args):\n    return 'v1'\n"
    v2 = b"def main(args):\n    return 'v2'\n"
    save_source(workspace, "sub", v1)
    await stack.start(source="def main(args):\n    return 1\n")

    async with stack.http() as client:
        first = await client.get("sources/sub")
        assert first.status_code == 200
        # Overwrite on disk between two run("sub") calls of the same attempt.
        (workspace / "workflows" / "sub.py").write_bytes(v2)
        second = await client.get("sources/sub")
        missing = await client.get("sources/missing")

    assert second.status_code == 200
    assert missing.status_code == 404
    expected_sha = hashlib.sha256(v1).hexdigest()
    assert first.json()["sha256"] == expected_sha
    assert second.json()["sha256"] == expected_sha
    assert base64.b64decode(first.json()["source_b64"]) == v1
    assert base64.b64decode(second.json()["source_b64"]) == v1

    group_args = {"op": "run", "name": "sub", "source_sha256": expected_sha}
    receipt = await stack.submit("@0", "group", group_args, {"members": ["/0:run"]})
    assert receipt.cached is False
    stored = stack.store.get_node(stack.run_id, "@0")
    assert stored is not None
    assert stored.request["source_sha256"] == expected_sha


@pytest.mark.asyncio
async def test_edited_saved_file_reruns_its_group_on_resume(stack: WorkflowStack) -> None:
    """Editing a saved workflow file changes its snapshot hash on resume and reruns the group and what follows."""
    workspace = stack.context.agent_workspace_root(create=True)
    v1 = b"def main(args):\n    return 'v1'\n"
    v2 = b"def main(args):\n    return 'v2'\n"
    save_source(workspace, "sub", v1)
    await stack.start(source="def main(args):\n    return 1\n")

    async with stack.http() as client:
        snap1 = (await client.get("sources/sub")).json()
    await stack.submit(
        "@0",
        "group",
        {"op": "run", "name": "sub", "source_sha256": snap1["sha256"]},
        {"members": ["/0:run"]},
    )
    await stack.result("/0:run@0", "tool", tool_args("counter", "append", line="inside"))
    await stack.result("@1", "tool", tool_args("counter", "append", line="after"))
    await stack.finish_attempt(result="done")
    await stack.end_attempt(ScriptRunState.EXITED)

    # Resume without editing: group, inner call, and after call all hit cache.
    await stack.resume(stack.run_id)
    async with stack.http() as client:
        snap_same = (await client.get("sources/sub")).json()
    same_group = await stack.submit(
        "@0",
        "group",
        {"op": "run", "name": "sub", "source_sha256": snap_same["sha256"]},
        {"members": ["/0:run"]},
    )
    same_inner = await stack.submit("/0:run@0", "tool", tool_args("counter", "append", line="inside"))
    same_after = await stack.submit("@1", "tool", tool_args("counter", "append", line="after"))
    assert (same_group.cached, same_inner.cached, same_after.cached) == (True, True, True)
    await stack.finish_attempt(result="done")
    await stack.end_attempt(ScriptRunState.EXITED)

    # Edit the saved workflow file and resume: new hash -> group reruns and what follows reruns.
    (workspace / "workflows" / "sub.py").write_bytes(v2)
    await stack.resume(stack.run_id)
    async with stack.http() as client:
        snap2 = (await client.get("sources/sub")).json()
    assert snap2["sha256"] == hashlib.sha256(v2).hexdigest()
    assert snap2["sha256"] != snap1["sha256"]

    edited_group = await stack.submit(
        "@0",
        "group",
        {"op": "run", "name": "sub", "source_sha256": snap2["sha256"]},
        {"members": ["/0:run"]},
    )
    edited_after = await stack.submit("@1", "tool", tool_args("counter", "append", line="after"))
    assert (edited_group.cached, edited_after.cached) == (False, False)
    group_node = stack.store.get_node(stack.run_id, "@0")
    after_node = stack.store.get_node(stack.run_id, "@1")
    assert group_node is not None
    assert group_node.generation == 2
    assert after_node is not None
    assert after_node.generation == 2
