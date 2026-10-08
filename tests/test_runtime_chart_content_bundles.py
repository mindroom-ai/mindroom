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
# The only commands on PATH when the scripts run: what the sync uses, plus touch for the test seed.
_TOOLS = ("sh", "cp", "mkdir", "rm", "find", "stat", "cmp", "readlink", "chmod", "chown", "id", "touch")

type Snapshot = dict[str, tuple[str, int, bytes | str | None]]


@pytest.fixture(scope="module")
def bundle_inits(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Any]]:
    """Render a default bundle, an overwrite: false bundle, and a seeded bundle, keyed by bundle name."""
    values = {
        "contentBundles": [
            {"name": "sync", "image": _IMAGE},
            {"name": "keep", "image": _IMAGE, "overwrite": False},
            {"name": "seeded", "image": _IMAGE, "seed": {"enabled": True, "command": ["touch", "seed-ran"]}},
        ],
    }
    docs = _render_chart(
        RUNTIME_CHART,
        release_name="mindroom-runtime",
        values_files=_values_files(tmp_path_factory.mktemp("values"), values),
    )
    deployment = _resource(docs, "Deployment", "mindroom-runtime")
    return {name: _init_container(deployment, f"content-bundle-{name}") for name in ("sync", "keep", "seeded")}


@pytest.fixture(params=["gnu", "busybox"])
def tool_dir(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Return a PATH directory holding only _TOOLS, linked to the system's tools or to BusyBox applets."""
    bin_dir = tmp_path_factory.mktemp(f"{request.param}-bin")
    if request.param == "busybox":
        busybox = shutil.which("busybox")
        if busybox is None:
            if os.environ.get("CI"):
                pytest.fail("busybox must be installed in CI, where the pytest workflow installs it")
            pytest.skip("busybox is not installed")
        tools = dict.fromkeys(_TOOLS, busybox)
    else:
        if not sys.platform.startswith("linux"):
            pytest.skip("bundle images run a Linux userland with GNU or BusyBox tools")
        tools = {tool: shutil.which(tool) for tool in _TOOLS}
    for tool, path in tools.items():
        if path is None:
            pytest.skip(f"{tool} is not installed")
        (bin_dir / tool).symlink_to(path)
    return bin_dir


def _run(
    init: dict[str, Any],
    tool_dir: Path,
    source: Path,
    target: Path,
    *,
    cwd: Path | None = None,
    check: bool = True,
    wrapper: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run the rendered init command with the source and target paths swapped for local trees."""
    script, name, *_paths = init["args"]
    completed = subprocess.run(
        [*wrapper, str(tool_dir / "sh"), *init["command"][1:], script, name, str(source), str(target)],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": str(tool_dir)},
        cwd=cwd,
    )
    if check:
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


def _retarget_symlink_by_a_trailing_newline(source: Path) -> None:
    (source / "current").unlink()
    (source / "current").symlink_to("plugins/tools\n")


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
        (_retarget_symlink_by_a_trailing_newline, set()),
        (_swap_symlink_and_file, {"current"}),
        (_change_directory_mode, set()),
    ],
    ids=lambda value: value.__name__.strip("_") if callable(value) else "",
)
def test_sync_applies_source_changes_and_leaves_unchanged_files_untouched(
    bundle_inits: dict[str, dict[str, Any]],
    tool_dir: Path,
    tmp_path: Path,
    change: Callable[[Path], None],
    changed: set[str],
) -> None:
    """A resync makes the target equal the source again and rewrites only files the change touched."""
    source = _make_source(tmp_path / "bundle")
    target = tmp_path / "storage" / "content-bundles" / "sync"
    _run(bundle_inits["sync"], tool_dir, source, target)
    assert _snapshot(target) == _snapshot(source)
    _mark_untouched(target)
    before = _file_ids(target)

    change(source)
    _run(bundle_inits["sync"], tool_dir, source, target)

    assert _snapshot(target) == _snapshot(source)
    after = _file_ids(target)
    rewritten = {name for name, ids in after.items() if before.get(name) != ids or ids[1] != _UNTOUCHED_NS}
    assert rewritten == changed


@pytest.mark.parametrize("existing", ["missing", "empty"])
def test_sync_copies_the_whole_bundle_onto_a_new_target(
    bundle_inits: dict[str, dict[str, Any]],
    tool_dir: Path,
    tmp_path: Path,
    existing: str,
) -> None:
    """A first start creates the target, including missing parents, as an exact copy that keeps source mtimes."""
    source = _make_source(tmp_path / "bundle")
    target = tmp_path / "storage" / "content-bundles" / "sync"
    if existing == "empty":
        target.mkdir(parents=True)

    _run(bundle_inits["sync"], tool_dir, source, target)

    assert _snapshot(target) == _snapshot(source)
    # BusyBox cp -a keeps whole seconds only.
    assert {name: ids[1] // 10**9 for name, ids in _file_ids(target).items()} == {
        name: ids[1] // 10**9 for name, ids in _file_ids(source).items()
    }


def test_sync_removes_and_repairs_entries_written_into_the_target(
    bundle_inits: dict[str, dict[str, Any]],
    tool_dir: Path,
    tmp_path: Path,
) -> None:
    """Files, links, and mode changes made in the target since the last start are undone, as rm -rf would."""
    source = _make_source(tmp_path / "bundle")
    target = tmp_path / "storage" / "sync"
    outside = tmp_path / "outside"
    outside.mkdir()
    _run(bundle_inits["sync"], tool_dir, source, target)
    (target / "plugins" / "tools" / "__pycache__").mkdir()
    (target / "plugins" / "tools" / "__pycache__" / "plugin.pyc").write_bytes(b"\0")
    (target / "escape").symlink_to(outside)
    (target / "docs" / "guide.md").write_text("edited\n")
    (target / "scripts" / "seed.sh").chmod(0o777)
    (target / "docs").chmod(0o2775)
    target.chmod(0o777)

    _run(bundle_inits["sync"], tool_dir, source, target)

    assert _snapshot(target) == _snapshot(source)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_sync_replaces_a_target_path_that_is_not_a_directory(
    bundle_inits: dict[str, dict[str, Any]],
    tool_dir: Path,
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

    _run(bundle_inits["sync"], tool_dir, source, target)

    assert not target.is_symlink()
    assert _snapshot(target) == _snapshot(source)
    assert list(elsewhere.iterdir()) == []


def test_sync_falls_back_to_a_full_copy_for_names_with_newlines(
    bundle_inits: dict[str, dict[str, Any]],
    tool_dir: Path,
    tmp_path: Path,
) -> None:
    """Line-based listings cannot carry a newline in a name, so such trees are replaced wholesale."""
    source = _make_source(tmp_path / "bundle")
    (source / "docs" / "new\nline.md").write_text("new\n")
    target = tmp_path / "sync"
    target.mkdir()
    (target / "old\nline.md").write_text("stale\n")

    completed = _run(bundle_inits["sync"], tool_dir, source, target)

    assert "a name contains a newline" in completed.stderr
    assert _snapshot(target) == _snapshot(source)


@pytest.mark.parametrize("missing", ["find", "cmp"])
def test_sync_falls_back_to_a_full_copy_when_the_image_lacks_a_tool(
    bundle_inits: dict[str, dict[str, Any]],
    tool_dir: Path,
    tmp_path: Path,
    missing: str,
) -> None:
    """Images that only have the tools the old full copy needed keep working."""
    reduced = tmp_path / "reduced-bin"
    reduced.mkdir()
    for tool in tool_dir.iterdir():
        if tool.name != missing:
            (reduced / tool.name).symlink_to(tool)
    source = _make_source(tmp_path / "bundle")
    target = tmp_path / "sync"
    target.mkdir()
    (target / "stale.md").write_text("stale\n")

    completed = _run(bundle_inits["sync"], reduced, source, target)

    assert f"the image has no {missing}" in completed.stderr
    assert _snapshot(target) == _snapshot(source)


@pytest.mark.parametrize("scenario", ["fresh", "update"])
def test_sync_fills_directories_that_end_up_read_only_without_root(
    bundle_inits: dict[str, dict[str, Any]],
    tool_dir: Path,
    tmp_path: Path,
    scenario: str,
    request: pytest.FixtureRequest,
) -> None:
    """Directory modes are applied after the copies, so read-only bundle directories do not block a non-root init."""
    # Let pytest delete the read-only trees afterwards.
    request.addfinalizer(lambda: subprocess.run(["chmod", "-R", "u+w", str(tmp_path)], check=False))
    source = _make_source(tmp_path / "bundle")
    docs = source / "docs"
    target = tmp_path / "sync"
    if scenario == "update":
        _run(bundle_inits["sync"], tool_dir, source, target)
        (docs / "added.md").write_text("added\n")
        (docs / "guide.md").unlink()
    for directory in (source / "scripts", docs, source):
        directory.chmod(0o555)

    _run(bundle_inits["sync"], tool_dir, source, target)
    assert _snapshot(target) == _snapshot(source)

    if scenario == "update":
        # The target directories are read-only now; the next update still adds and removes entries in them.
        source.chmod(0o755)
        docs.chmod(0o755)
        (docs / "later.md").write_text("later\n")
        (docs / "added.md").unlink()
        (source / "scripts").chmod(0o755)
        shutil.rmtree(source / "scripts")
        docs.chmod(0o555)
        source.chmod(0o555)
        _run(bundle_inits["sync"], tool_dir, source, target)
        assert _snapshot(target) == _snapshot(source)


def test_root_without_permission_override_updates_read_only_directories(
    bundle_inits: dict[str, dict[str, Any]],
    tool_dir: Path,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """Root on root-squashed NFS gets no permission override; BusyBox test -w still reports true for it."""
    unshare, setpriv = shutil.which("unshare"), shutil.which("setpriv")
    drop = "--bounding-set=-dac_override,-dac_read_search,-fowner,-chown"
    wrapper = (unshare or "unshare", "-r", setpriv or "setpriv", "--inh-caps=-all", drop)
    if None in (unshare, setpriv) or subprocess.run([*wrapper, "true"], check=False).returncode:
        pytest.skip("needs unshare and setpriv with unprivileged user namespaces")
    request.addfinalizer(lambda: subprocess.run(["chmod", "-R", "u+w", str(tmp_path)], check=False))
    source = _make_source(tmp_path / "bundle")
    docs = source / "docs"
    target = tmp_path / "sync"
    docs.chmod(0o555)
    _run(bundle_inits["sync"], tool_dir, source, target, wrapper=wrapper)

    docs.chmod(0o755)
    (docs / "guide.md").unlink()
    (docs / "added.md").write_text("added\n")
    docs.chmod(0o555)
    _run(bundle_inits["sync"], tool_dir, source, target, wrapper=wrapper)
    assert _snapshot(target) == _snapshot(source)


def test_root_updates_a_writable_parent_it_may_not_chmod(
    bundle_inits: dict[str, dict[str, Any]],
    tool_dir: Path,
    tmp_path: Path,
) -> None:
    """A writable parent owned by someone else refuses chmod on root-squashed NFS; that must not abort the update."""
    unshare = shutil.which("unshare")
    if unshare is None or subprocess.run([unshare, "-r", "true"], check=False).returncode:
        pytest.skip("needs unshare with unprivileged user namespaces")
    real = (tool_dir / "chmod").resolve()
    real_chmod = f'"{real}" chmod' if real.name == "busybox" else f'"{real}"'
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in tool_dir.iterdir():
        (bin_dir / tool.name).symlink_to(tool.resolve())
    (bin_dir / "chmod").unlink()
    (bin_dir / "chmod").write_text(f'#!{bin_dir / "sh"}\n[ "$1" != u+w ] || exit 1\nexec {real_chmod} "$@"\n')
    (bin_dir / "chmod").chmod(0o755)
    source = _make_source(tmp_path / "bundle")
    target = tmp_path / "sync"
    (source / "docs").chmod(0o777)
    _run(bundle_inits["sync"], bin_dir, source, target, wrapper=(unshare, "-r"))

    (source / "docs" / "guide.md").unlink()
    (source / "docs" / "added.md").write_text("added\n")
    _run(bundle_inits["sync"], bin_dir, source, target, wrapper=(unshare, "-r"))
    assert _snapshot(target) == _snapshot(source)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read the unreadable file")
def test_a_failed_copy_fails_the_init_before_the_seed_runs(
    bundle_inits: dict[str, dict[str, Any]],
    tool_dir: Path,
    tmp_path: Path,
) -> None:
    """A copy error stops the init with a nonzero exit, so the pod does not start and the seed does not run."""
    source = _make_source(tmp_path / "bundle")
    target = tmp_path / "seeded"
    marker = tmp_path / "seed-ran"
    _run(bundle_inits["seeded"], tool_dir, source, target, cwd=tmp_path)
    assert marker.exists()
    marker.unlink()
    unreadable = source / "docs" / "unreadable.md"
    unreadable.write_text("secret\n")
    unreadable.chmod(0)

    completed = _run(bundle_inits["seeded"], tool_dir, source, target, cwd=tmp_path, check=False)

    assert completed.returncode != 0
    assert "unreadable.md" in completed.stderr
    assert not marker.exists()


def test_overwrite_false_copies_over_the_target_without_removing_anything(
    bundle_inits: dict[str, dict[str, Any]],
    tool_dir: Path,
    tmp_path: Path,
) -> None:
    """With overwrite: false, bundle files replace their target copies and target-only files stay."""
    source = _make_source(tmp_path / "bundle")
    target = tmp_path / "keep"
    _run(bundle_inits["keep"], tool_dir, source, target)
    notes = target / "local-notes.md"
    notes.write_text("kept\n")
    first = _snapshot(target)
    _change_content(source)
    _delete_file(source)

    _run(bundle_inits["keep"], tool_dir, source, target)

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
