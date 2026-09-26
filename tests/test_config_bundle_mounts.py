"""Real mount-boundary regression cases run only inside a fresh mount namespace."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mindroom.config_bundle import install_config_bundle


def _enter_mount_namespace(nodeid: str) -> bool:
    if os.environ.get("MINDROOM_BUNDLE_MOUNT_TEST_CHILD") == "1":
        return False
    if not shutil.which("unshare") or not shutil.which("mount") or not shutil.which("umount"):
        pytest.skip("mount namespace tools unavailable")
    command = ["unshare", "--user", "--map-root-user", "--mount"]
    if subprocess.run([*command, "true"], capture_output=True, check=False).returncode:
        pytest.skip("unprivileged mount namespace unavailable")
    # Clearing addopts also drops its `-p no:tach`, and Tach's plugin spends seconds scanning the tree.
    pytest_args = [nodeid, "-q", "-n", "0", "--no-cov", "-o", "addopts=", "-p", "no:tach"]
    result = subprocess.run(
        [*command, sys.executable, "-m", "pytest", *pytest_args],
        env={**os.environ, "MINDROOM_BUNDLE_MOUNT_TEST_CHILD": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return True


@pytest.mark.parametrize("force", [False, True])
def test_nested_bind_mount_rejected_before_rotation(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    force: bool,
) -> None:
    """A same-filesystem bind mount never becomes managed data, even with force."""
    if _enter_mount_namespace(request.node.nodeid):
        return
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("agents: {}\n")
    (source / "mounted").mkdir()
    (source / "mounted/keep.txt").write_text("External data")
    target = tmp_path / "active"
    install_config_bundle(source, target, process_env={})
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.txt").write_text("External data")
    subprocess.run(["mount", "--bind", str(external), str(target / "mounted")], check=True)
    try:
        (source / "config.yaml").write_text("agents: {}\n# next\n")
        with pytest.raises(ValueError, match="mount"):
            install_config_bundle(source, target, force=force, process_env={})
        assert (target / "config.yaml").read_text() == "agents: {}\n"
        assert not (tmp_path / "active.previous").exists()
        assert (external / "keep.txt").read_text() == "External data"
    finally:
        for root in (target, tmp_path / ".active.pending", tmp_path / "active.previous", tmp_path / ".active.retired"):
            if (root / "mounted").exists():
                result = subprocess.run(["umount", str(root / "mounted")], capture_output=True, check=False)
                if result.returncode == 0:
                    break
    assert install_config_bundle(source, target, process_env={}).status == "installed"


def test_recovery_rejects_new_mount_in_owned_retired_tree(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matching bytes in recovery never authorize deleting a later mounted volume."""
    if _enter_mount_namespace(request.node.nodeid):
        return
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("agents: {}\n# first\n")
    (source / "mounted").mkdir()
    (source / "mounted/keep.txt").write_text("External data")
    target = tmp_path / "active"
    install_config_bundle(source, target, process_env={})
    (source / "config.yaml").write_text("agents: {}\n# second\n")
    install_config_bundle(source, target, process_env={})
    (source / "config.yaml").write_text("agents: {}\n# third\n")
    pending = tmp_path / ".active.pending"
    previous = tmp_path / "active.previous"
    retired = tmp_path / ".active.retired"
    rename = Path.rename

    def interrupt(path: Path, destination: Path) -> Path:
        if path == pending and destination == previous:
            raise SystemExit
        return rename(path, destination)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", interrupt)
        with pytest.raises(SystemExit):
            install_config_bundle(source, target, process_env={})
    assert retired.exists()
    assert pending.exists()
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.txt").write_text("External data")
    subprocess.run(["mount", "--bind", str(external), str(retired / "mounted")], check=True)
    try:
        with pytest.raises(ValueError, match="mount"):
            install_config_bundle(source, target, process_env={})
        assert (external / "keep.txt").read_text() == "External data"
        assert pending.exists()
        assert retired.exists()
        assert (target / "config.yaml").read_text() == "agents: {}\n# third\n"
    finally:
        subprocess.run(["umount", str(retired / "mounted")], check=True)
    assert install_config_bundle(source, target, process_env={}).status == "unchanged"
    assert (previous / "config.yaml").read_text() == "agents: {}\n# second\n"
    assert not retired.exists()


def test_recovery_rejects_bind_mount_on_retired_root(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mounted recovery root cannot authorize deleting matching external bytes."""
    if _enter_mount_namespace(request.node.nodeid):
        return
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("agents: {}\n# first\n")
    (source / "keep.txt").write_text("External data")
    target = tmp_path / "active"
    install_config_bundle(source, target, process_env={})
    (source / "config.yaml").write_text("agents: {}\n# second\n")
    install_config_bundle(source, target, process_env={})
    (source / "config.yaml").write_text("agents: {}\n# third\n")
    retired = tmp_path / ".active.retired"
    rmtree = shutil.rmtree

    def interrupt_cleanup(path: Path, **kwargs: object) -> None:
        if path == retired:
            msg = "Injected cleanup interruption"
            raise OSError(msg)
        rmtree(path, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "rmtree", interrupt_cleanup)
        assert install_config_bundle(source, target, process_env={}).recovery_pending
    external = tmp_path / "external"
    shutil.copytree(retired, external)
    subprocess.run(["mount", "--bind", str(external), str(retired)], check=True)
    try:
        with pytest.raises(ValueError, match="mount"):
            install_config_bundle(source, target, process_env={})
        assert (external / "config.yaml").read_text() == "agents: {}\n# first\n"
        assert (external / "keep.txt").read_text() == "External data"
        assert (target / "config.yaml").read_text() == "agents: {}\n# third\n"
    finally:
        subprocess.run(["umount", str(retired)], check=True)
    assert install_config_bundle(source, target, process_env={}).status == "unchanged"
    assert (tmp_path / "active.previous/config.yaml").read_text() == "agents: {}\n# second\n"
    assert not retired.exists()


def test_active_root_bind_mount_rejected_before_rotation(tmp_path: Path, request: pytest.FixtureRequest) -> None:
    """Binding complete matching content on the active root never grants ownership."""
    if _enter_mount_namespace(request.node.nodeid):
        return
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("agents: {}\n# first\n")
    target = tmp_path / "active"
    install_config_bundle(source, target, process_env={})
    external = tmp_path / "external"
    shutil.copytree(target, external)
    subprocess.run(["mount", "--bind", str(external), str(target)], check=True)
    try:
        (source / "config.yaml").write_text("agents: {}\n# second\n")
        with pytest.raises(ValueError, match="mount"):
            install_config_bundle(source, target, process_env={})
        assert (external / "config.yaml").read_text() == "agents: {}\n# first\n"
        assert not (tmp_path / "active.previous").exists()
    finally:
        for root in (target, tmp_path / ".active.pending", tmp_path / "active.previous", tmp_path / ".active.retired"):
            if (
                root.exists()
                and subprocess.run(["umount", str(root)], capture_output=True, check=False).returncode == 0
            ):
                break
    assert (target / "config.yaml").read_text() == "agents: {}\n# first\n"
    assert install_config_bundle(source, target, process_env={}).status == "installed"


def test_install_below_mounted_volume_root(tmp_path: Path, request: pytest.FixtureRequest) -> None:
    """A shared-volume mount above the managed tree remains supported."""
    if _enter_mount_namespace(request.node.nodeid):
        return
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("agents: {}\n")
    backing = tmp_path / "backing"
    backing.mkdir()
    volume = tmp_path / "volume"
    volume.mkdir()
    subprocess.run(["mount", "--bind", str(backing), str(volume)], check=True)
    try:
        target = volume / "active"
        assert install_config_bundle(source, target, process_env={}).status == "installed"
        (source / "config.yaml").write_text("agents: {}\n# next\n")
        assert install_config_bundle(source, target, process_env={}).status == "installed"
        assert (backing / "active.previous/config.yaml").read_text() == "agents: {}\n"
    finally:
        subprocess.run(["umount", str(volume)], check=True)


def test_initialize_only_preserves_existing_tree_with_nested_mount(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """Preservation does not take ownership of existing mounted contents."""
    if _enter_mount_namespace(request.node.nodeid):
        return
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("agents: {}\n")
    (source / "mounted").mkdir()
    target = tmp_path / "active"
    install_config_bundle(source, target, process_env={})
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.txt").write_text("External data")
    subprocess.run(["mount", "--bind", str(external), str(target / "mounted")], check=True)
    try:
        (source / "config.yaml").write_text("invalid: [")
        assert install_config_bundle(source, target, initialize_only=True, process_env={}).status == "initialized"
        assert (external / "keep.txt").read_text() == "External data"
        assert (target / "config.yaml").read_text() == "agents: {}\n"
    finally:
        subprocess.run(["umount", str(target / "mounted")], check=True)
