"""Run the runtime chart's rendered content-bundle init scripts against real directory trees."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from tests.test_helm_instance_worker_isolation import _init_container, _render_chart, _resource, _values_files

if TYPE_CHECKING:
    from collections.abc import Callable

RUNTIME_CHART = Path("cluster/k8s/runtime")
_IMAGE = "registry.example.com/team/bundle@sha256:" + "1" * 64
# An mtime no copy produces: target files still carrying it after a sync were not rewritten.
_UNTOUCHED_NS = 1_000_000_000 * 1_000_000_000
# Names that break unquoted or option-like path handling.
_ODD_NAMES = ("with space [x] *.md", "-leading-dash", "back\\slash", " padded ", "it's")

type Snapshot = dict[str, tuple[str, int, bytes | str | None]]
type Shell = tuple[str, dict[str, str] | None]


@pytest.fixture(scope="module")
def bundle_inits(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Any]]:
    """Render one default bundle and one overwrite: false bundle, keyed by bundle name."""
    values = {
        "contentBundles": [
            {"name": "sync", "image": _IMAGE},
            {"name": "keep", "image": _IMAGE, "overwrite": False},
        ],
    }
    docs = _render_chart(
        RUNTIME_CHART,
        release_name="mindroom-runtime",
        values_files=_values_files(tmp_path_factory.mktemp("values"), values),
    )
    deployment = _resource(docs, "Deployment", "mindroom-runtime")
    return {name: _init_container(deployment, f"content-bundle-{name}") for name in ("sync", "keep")}


@pytest.fixture(params=["sh", "busybox"])
def shell(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Shell:
    """Return the shell and environment to run the script with: the system sh, or BusyBox for every command."""
    if request.param == "sh":
        if not sys.platform.startswith("linux"):
            pytest.skip("bundle images run a Linux userland with GNU or BusyBox tools")
        return "sh", None
    busybox = shutil.which("busybox")
    if busybox is None:
        pytest.skip("busybox is not installed")
    bin_dir = tmp_path_factory.mktemp("busybox-bin")
    applets = subprocess.run([busybox, "--list"], check=True, capture_output=True, text=True).stdout.split()
    for applet in applets:
        (bin_dir / applet).symlink_to(busybox)
    return str(bin_dir / "sh"), {"PATH": str(bin_dir)}


def _run(init: dict[str, Any], shell: Shell, source: Path, target: Path) -> subprocess.CompletedProcess[str]:
    """Run the rendered init command with the source and target paths swapped for local trees."""
    executable, env = shell
    script, name, *_paths = init["args"]
    completed = subprocess.run(
        [executable, *init["command"][1:], script, name, str(source), str(target)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    return completed


def _snapshot(root: Path) -> Snapshot:
    """Map root (as ".") and every entry below it to its type, permission bits, and content or link target."""
    entries: Snapshot = {}
    for path in (root, *root.rglob("*")):
        info = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISLNK(info.st_mode):
            entries[relative] = ("link", 0, str(path.readlink()))
        elif stat.S_ISDIR(info.st_mode):
            entries[relative] = ("dir", stat.S_IMODE(info.st_mode), None)
        else:
            entries[relative] = ("file", stat.S_IMODE(info.st_mode), path.read_bytes())
    return entries


def _make_source(root: Path) -> Path:
    """Create a small bundle with nested directories, an executable, a symlink, and awkward names."""
    tools = root / "plugins" / "tools"
    tools.mkdir(parents=True)
    (tools / "plugin.py").write_text("VERSION = 1\n")
    (tools / "helper.py").write_text("HELPER = True\n")
    (root / "scripts").mkdir()
    seed = root / "scripts" / "seed.sh"
    seed.write_text("#!/bin/sh\necho seeded\n")
    seed.chmod(0o755)
    (root / "config.yaml").write_text("agents: {}\n")
    (root / "current").symlink_to("plugins/tools")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("guide\n")
    for name in _ODD_NAMES:
        (root / "docs" / name).write_text(f"{name}\n")
    return root


def _file_ids(root: Path) -> dict[str, tuple[int, int]]:
    """Map every regular file below root to its inode and mtime."""
    return {
        path.relative_to(root).as_posix(): (path.lstat().st_ino, path.lstat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def _mark_untouched(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            os.utime(path, ns=(_UNTOUCHED_NS, _UNTOUCHED_NS))


def _change_content(source: Path) -> None:
    (source / "config.yaml").write_text("agents: []\n")  # Same size, so only the content comparison sees it.


def _change_mode(source: Path) -> None:
    (source / "scripts" / "seed.sh").chmod(0o700)


def _delete_file(source: Path) -> None:
    (source / "plugins" / "tools" / "helper.py").unlink()


def _delete_directory(source: Path) -> None:
    shutil.rmtree(source / "plugins")
    (source / "current").unlink()


def _file_to_directory(source: Path) -> None:
    (source / "config.yaml").unlink()
    (source / "config.yaml").mkdir()
    (source / "config.yaml" / "main.yaml").write_text("agents: {}\n")


def _directory_to_file(source: Path) -> None:
    shutil.rmtree(source / "scripts")
    (source / "scripts").write_text("not a directory any more\n")


def _retarget_symlink(source: Path) -> None:
    (source / "current").unlink()
    (source / "current").symlink_to("docs")


def _swap_symlink_and_file(source: Path) -> None:
    (source / "current").unlink()
    (source / "current").write_text("now a file\n")
    (source / "docs" / "guide.md").unlink()
    (source / "docs" / "guide.md").symlink_to("../config.yaml")


def _change_directory_mode(source: Path) -> None:
    (source / "docs").chmod(0o700)
    source.chmod(0o750)


@pytest.mark.parametrize(
    ("change", "changed"),
    [
        (_change_content, {"config.yaml"}),
        (_change_mode, {"scripts/seed.sh"}),
        (_delete_file, set()),
        (_delete_directory, set()),
        (_file_to_directory, {"config.yaml/main.yaml"}),
        (_directory_to_file, {"scripts"}),
        (_retarget_symlink, set()),
        (_swap_symlink_and_file, {"current"}),
        (_change_directory_mode, set()),
    ],
    ids=lambda value: value.__name__.strip("_") if callable(value) else "",
)
def test_sync_applies_source_changes_and_leaves_unchanged_files_untouched(
    bundle_inits: dict[str, dict[str, Any]],
    shell: Shell,
    tmp_path: Path,
    change: Callable[[Path], None],
    changed: set[str],
) -> None:
    """A resync makes the target equal the source again and rewrites only files the change touched."""
    source = _make_source(tmp_path / "bundle")
    target = tmp_path / "storage" / "content-bundles" / "sync"
    _run(bundle_inits["sync"], shell, source, target)
    assert _snapshot(target) == _snapshot(source)
    _mark_untouched(target)
    before = _file_ids(target)

    change(source)
    _run(bundle_inits["sync"], shell, source, target)

    assert _snapshot(target) == _snapshot(source)
    after = _file_ids(target)
    rewritten = {name for name, ids in after.items() if before.get(name) != ids or ids[1] != _UNTOUCHED_NS}
    assert rewritten == changed


@pytest.mark.parametrize("existing", ["missing", "empty"])
def test_sync_copies_the_whole_bundle_onto_a_new_target(
    bundle_inits: dict[str, dict[str, Any]],
    shell: Shell,
    tmp_path: Path,
    existing: str,
) -> None:
    """A first start creates the target, including missing parents, as an exact copy that keeps source mtimes."""
    source = _make_source(tmp_path / "bundle")
    target = tmp_path / "storage" / "content-bundles" / "sync"
    if existing == "empty":
        target.mkdir(parents=True)

    _run(bundle_inits["sync"], shell, source, target)

    assert _snapshot(target) == _snapshot(source)
    # BusyBox cp -a keeps whole seconds only.
    assert {name: ids[1] // 10**9 for name, ids in _file_ids(target).items()} == {
        name: ids[1] // 10**9 for name, ids in _file_ids(source).items()
    }


def test_sync_removes_and_repairs_entries_written_into_the_target(
    bundle_inits: dict[str, dict[str, Any]],
    shell: Shell,
    tmp_path: Path,
) -> None:
    """Files, links, and mode changes made in the target since the last start are undone, as rm -rf would."""
    source = _make_source(tmp_path / "bundle")
    target = tmp_path / "storage" / "sync"
    outside = tmp_path / "outside"
    outside.mkdir()
    _run(bundle_inits["sync"], shell, source, target)
    (target / "plugins" / "tools" / "__pycache__").mkdir()
    (target / "plugins" / "tools" / "__pycache__" / "plugin.pyc").write_bytes(b"\0")
    (target / "escape").symlink_to(outside)
    (target / "docs" / "guide.md").write_text("edited\n")
    (target / "scripts" / "seed.sh").chmod(0o777)
    (target / "docs").chmod(0o2775)
    target.chmod(0o777)

    _run(bundle_inits["sync"], shell, source, target)

    assert _snapshot(target) == _snapshot(source)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_sync_replaces_a_target_path_that_is_not_a_directory(
    bundle_inits: dict[str, dict[str, Any]],
    shell: Shell,
    tmp_path: Path,
    kind: str,
) -> None:
    """A symlink or file at the target path is replaced by a directory instead of being written through."""
    source = _make_source(tmp_path / "bundle")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = tmp_path / "sync"
    if kind == "symlink":
        target.symlink_to(elsewhere)
    else:
        target.write_text("stale\n")

    _run(bundle_inits["sync"], shell, source, target)

    assert not target.is_symlink()
    assert _snapshot(target) == _snapshot(source)
    assert list(elsewhere.iterdir()) == []


def test_sync_falls_back_to_a_full_copy_for_names_with_newlines(
    bundle_inits: dict[str, dict[str, Any]],
    shell: Shell,
    tmp_path: Path,
) -> None:
    """Line-based listings cannot carry a newline in a name, so such trees are replaced wholesale."""
    source = _make_source(tmp_path / "bundle")
    (source / "docs" / "new\nline.md").write_text("new\n")
    target = tmp_path / "sync"
    target.mkdir()
    (target / "old\nline.md").write_text("stale\n")

    completed = _run(bundle_inits["sync"], shell, source, target)

    assert "a name contains a newline" in completed.stderr
    assert _snapshot(target) == _snapshot(source)


def test_overwrite_false_copies_over_the_target_without_removing_anything(
    bundle_inits: dict[str, dict[str, Any]],
    shell: Shell,
    tmp_path: Path,
) -> None:
    """With overwrite: false, bundle files replace their target copies and target-only files stay."""
    source = _make_source(tmp_path / "bundle")
    target = tmp_path / "keep"
    _run(bundle_inits["keep"], shell, source, target)
    notes = target / "local-notes.md"
    notes.write_text("kept\n")
    first = _snapshot(target)
    _change_content(source)
    _delete_file(source)

    _run(bundle_inits["keep"], shell, source, target)

    kept = {name: first[name] for name in ("local-notes.md", "plugins/tools/helper.py")}
    assert _snapshot(target) == {**_snapshot(source), **kept}


def test_bundle_paths_reach_the_script_as_arguments(tmp_path: Path) -> None:
    """Source and target paths are passed as arguments, so shell syntax in values is never evaluated."""
    target = "/app/agent_data/content-bundles/odd $(touch x) `id` dir"
    values = {"contentBundles": [{"name": "odd", "image": _IMAGE, "sourcePath": "/bundle/it's", "targetPath": target}]}
    docs = _render_chart(RUNTIME_CHART, release_name="mindroom-runtime", values_files=_values_files(tmp_path, values))
    init = _init_container(_resource(docs, "Deployment", "mindroom-runtime"), "content-bundle-odd")

    assert init["command"] == ["sh", "-ec"]
    assert init["args"][1:] == ["content-bundle-odd", "/bundle/it's", target]
    assert "touch" not in init["args"][0]
