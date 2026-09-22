"""Configuration bundle installation preserves complete, validated trees."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import TextIO

import pytest

from mindroom.config.main import CONFIG_LOAD_USER_ERROR_TYPES, load_config
from mindroom.config_bundle import install_config_bundle
from mindroom.constants import resolve_runtime_paths
from mindroom.file_watcher import changed_watched_paths, paths_mtime_snapshot


def _bundle(path: Path, prompt: str = "First prompt") -> Path:
    path.mkdir()
    (path / "prompts").mkdir()
    (path / "config.yaml").write_text("agents: !include agents.yaml\n")
    (path / "agents.yaml").write_text(
        "helper:\n  display_name: Helper\n  instructions:\n    - !include_text prompts/helper.md\n",
    )
    (path / "prompts/helper.md").write_text(prompt)
    (path / ".env").write_text("MATRIX_SERVER_NAME=example.org\n")
    return path


def _snapshot(path: Path) -> dict[str, bytes]:
    return {str(item.relative_to(path)): item.read_bytes() for item in path.rglob("*") if item.is_file()}


def test_install_complete_tree_and_native_receipt(tmp_path: Path) -> None:
    """The installed tree must contain nested sources and load with its own environment."""
    source = _bundle(tmp_path / "source")
    target = tmp_path / "active"
    receipt = install_config_bundle(source, target, process_env={})
    runtime = resolve_runtime_paths(config_path=target / "config.yaml", process_env={})
    loaded = load_config(runtime)
    assert receipt.status == "installed"
    assert receipt.fingerprint == loaded.source_fingerprint
    assert loaded.agents["helper"].instructions == ["First prompt"]
    assert runtime.env_value("MATRIX_SERVER_NAME") == "example.org"
    assert _snapshot(source).items() <= _snapshot(target).items()


def test_unchanged_bundle_keeps_active_and_previous(tmp_path: Path) -> None:
    """Retries must not rotate away the previous revision or touch active inode/mtime."""
    source = _bundle(tmp_path / "source")
    target = tmp_path / "active"
    install_config_bundle(source, target, process_env={})
    (source / "prompts/helper.md").write_text("Second prompt")
    install_config_bundle(source, target, process_env={})
    previous = _snapshot(tmp_path / "active.previous")
    stat = target.stat()
    assert install_config_bundle(source, target, process_env={}).status == "unchanged"
    assert target.stat() == stat
    assert _snapshot(tmp_path / "active.previous") == previous


@pytest.mark.parametrize("failure", ["nested_include", "validation", "copy", "publish", "metadata"])
def test_failed_candidate_preserves_active_and_previous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """No failed candidate may destroy either usable revision."""
    source = _bundle(tmp_path / "source")
    target = tmp_path / "active"
    install_config_bundle(source, target, process_env={})
    (source / "prompts/helper.md").write_text("Second prompt")
    install_config_bundle(source, target, process_env={})
    active = _snapshot(target)
    previous = _snapshot(tmp_path / "active.previous")
    (source / "prompts/helper.md").write_text("Third prompt")
    if failure == "nested_include":
        (source / "agents.yaml").write_text("helper: !include missing.yaml\n")
    elif failure == "validation":
        (source / "agents.yaml").write_text("helper: {unknown_setting: true}\n")
    elif failure == "copy":

        def fail_copy(*_args: object, **_kwargs: object) -> None:
            msg = "copy failed"
            raise OSError(msg)

        monkeypatch.setattr(shutil, "copytree", fail_copy)
    elif failure == "metadata":
        write_text = Path.write_text

        def fail_metadata(path: Path, data: str, *args: object, **kwargs: object) -> int:
            if path.name == ".mindroom-bundle.json":
                msg = "metadata failed"
                raise OSError(msg)
            return write_text(path, data, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", fail_metadata)
    else:
        rename = Path.rename

        def fail_publish(path: Path, destination: Path) -> Path:
            if destination == target and "stage" in path.name:
                msg = "publish failed"
                raise OSError(msg)
            return rename(path, destination)

        monkeypatch.setattr(Path, "rename", fail_publish)
    with pytest.raises(CONFIG_LOAD_USER_ERROR_TYPES):
        install_config_bundle(source, target, process_env={})
    assert _snapshot(target) == active
    assert _snapshot(tmp_path / "active.previous") == previous


def test_initialize_only_preserves_authored_edits_and_changed_source(tmp_path: Path) -> None:
    """Bootstrap restart must keep edits even when source changes or disappears."""
    source = _bundle(tmp_path / "source")
    target = tmp_path / "active"
    install_config_bundle(source, target, initialize_only=True, process_env={})
    (target / "prompts/helper.md").write_text("Authored prompt")
    (source / "config.yaml").write_text("invalid: [")
    before = _snapshot(target)
    assert install_config_bundle(source, target, initialize_only=True, process_env={}).status == "initialized"
    assert _snapshot(target) == before
    assert not (tmp_path / "active.previous").exists()


def test_changed_bundle_requires_force_after_authored_edits(tmp_path: Path) -> None:
    """Authored files must survive a new revision unless replacement is explicit."""
    source = _bundle(tmp_path / "source")
    target = tmp_path / "active"
    install_config_bundle(source, target, process_env={})
    (target / "prompts/helper.md").write_text("Authored prompt")
    (source / "prompts/helper.md").write_text("New revision")
    with pytest.raises(ValueError, match="--force"):
        install_config_bundle(source, target, process_env={})
    assert (target / "prompts/helper.md").read_text() == "Authored prompt"
    install_config_bundle(source, target, force=True, process_env={})
    assert (target / "prompts/helper.md").read_text() == "New revision"
    assert (tmp_path / "active.previous/prompts/helper.md").read_text() == "Authored prompt"


@pytest.mark.parametrize("config", ["../config.yaml", "/config.yaml"])
def test_config_must_stay_inside_bundle(tmp_path: Path, config: str) -> None:
    """Config selection must never validate a file outside the candidate tree."""
    with pytest.raises(ValueError, match="relative"):
        install_config_bundle(_bundle(tmp_path / "source"), tmp_path / "active", config=Path(config))


def test_reject_source_symlinks_without_touching_active(tmp_path: Path) -> None:
    """Copying a bundle must not follow outside links or change their targets."""
    source = _bundle(tmp_path / "source")
    outside = tmp_path / "outside"
    outside.write_text("Keep")
    (source / "link").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        install_config_bundle(source, tmp_path / "active")
    assert outside.read_text() == "Keep"
    assert not (tmp_path / "active").exists()


@pytest.mark.parametrize("phase", ["publish", "rotate_previous"])
def test_interrupted_activation_recovers_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """A process exit between renames leaves complete trees recoverable on retry."""
    source = _bundle(tmp_path / "source")
    target = tmp_path / "active"
    install_config_bundle(source, target, process_env={})
    (source / "prompts/helper.md").write_text("Second prompt")
    install_config_bundle(source, target, process_env={})
    (source / "prompts/helper.md").write_text("Third prompt")
    rename = Path.rename

    def interrupt(path: Path, destination: Path) -> Path:
        if (phase == "publish" and destination == target) or (
            phase == "rotate_previous" and destination == tmp_path / "active.previous"
        ):
            msg = "interrupted"
            raise SystemExit(msg)
        return rename(path, destination)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", interrupt)
        with pytest.raises(SystemExit):
            install_config_bundle(source, target, process_env={})
    install_config_bundle(source, target, process_env={})
    assert (target / "prompts/helper.md").read_text() == "Third prompt"
    assert (tmp_path / "active.previous/prompts/helper.md").read_text() == "Second prompt"


def test_env_edits_require_force(tmp_path: Path) -> None:
    """Environment files participate in authored drift protection too."""
    source = _bundle(tmp_path / "source")
    target = tmp_path / "active"
    install_config_bundle(source, target, process_env={})
    (target / ".env").write_text("MATRIX_SERVER_NAME=authored.example.org\n")
    with pytest.raises(ValueError, match="--force"):
        install_config_bundle(source, target, process_env={})
    assert "authored.example.org" in (target / ".env").read_text()


def test_activation_notifies_existing_mtime_watcher_even_with_reproducible_timestamps(tmp_path: Path) -> None:
    """Included file changes must reload even when artifact timestamps are all preserved."""
    source = _bundle(tmp_path / "source")
    target = tmp_path / "active"
    install_config_bundle(source, target, process_env={})
    paths = [target / "config.yaml", target / "agents.yaml", target / "prompts/helper.md"]
    before = paths_mtime_snapshot(paths)
    (source / "prompts/helper.md").write_text("Changed include")
    for path in paths:
        os.utime(source / path.relative_to(target), ns=(before[path], before[path]))
    install_config_bundle(source, target, process_env={})
    assert target / "config.yaml" in changed_watched_paths(before, paths_mtime_snapshot(paths))


def test_previous_tree_is_a_supported_validated_rollback_source(tmp_path: Path) -> None:
    """Rollback reuses installation and retains the rejected revision for inspection."""
    source = _bundle(tmp_path / "source")
    target = tmp_path / "active"
    first = install_config_bundle(source, target, process_env={})
    (source / "prompts/helper.md").write_text("Rejected revision")
    install_config_bundle(source, target, process_env={})
    rollback = install_config_bundle(tmp_path / "active.previous", target, process_env={})
    assert rollback.fingerprint == first.fingerprint
    assert (target / "prompts/helper.md").read_text() == "First prompt"
    assert (tmp_path / "active.previous/prompts/helper.md").read_text() == "Rejected revision"


def test_reserved_metadata_cannot_be_selected_as_config(tmp_path: Path) -> None:
    """Installer metadata must never replace the very file validated as config."""
    source = _bundle(tmp_path / "source")
    (source / ".mindroom-bundle.json").write_text('{"agents": {}}')
    with pytest.raises(ValueError, match="reserved"):
        install_config_bundle(source, tmp_path / "active", config=Path(".mindroom-bundle.json"), process_env={})
    assert not (tmp_path / "active").exists()


@pytest.mark.parametrize("changed_file", ["prompts/helper.md", ".env"])
def test_pinned_rollback_retry_cannot_toggle_revisions(tmp_path: Path, changed_file: str) -> None:
    """The whole-tree guard rejects retry after rollback rotates YAML or environment revisions."""
    source = _bundle(tmp_path / "source")
    target = tmp_path / "active"
    first = install_config_bundle(source, target, process_env={})
    (source / changed_file).write_text("REVISION=rejected\n")
    second = install_config_bundle(source, target, process_env={})
    if changed_file == ".env":
        assert first.fingerprint == second.fingerprint
    assert first.digest != second.digest
    previous = tmp_path / "active.previous"
    install_config_bundle(previous, target, expected_digest=first.digest, process_env={})
    active_snapshot = _snapshot(target)
    previous_snapshot = _snapshot(previous)
    with pytest.raises(ValueError, match="digest"):
        install_config_bundle(previous, target, expected_digest=first.digest, process_env={})
    assert _snapshot(target) == active_snapshot
    assert _snapshot(previous) == previous_snapshot


@pytest.mark.parametrize("name", ["active", "active.previous", ".active.pending", ".active.retired"])
@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_recovery_paths_reject_files_and_symlinks(tmp_path: Path, name: str, kind: str) -> None:
    """Reserved siblings must never redirect recovery outside the managed directories."""
    source = _bundle(tmp_path / "source")
    outside = _bundle(tmp_path / "outside")
    before = _snapshot(outside)
    path = tmp_path / name
    if kind == "file":
        path.write_text("Keep")
    else:
        path.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="real directories"):
        install_config_bundle(source, tmp_path / "active", process_env={})
    assert _snapshot(outside) == before
    assert path.is_symlink() if kind == "symlink" else path.read_text() == "Keep"


@pytest.mark.parametrize("phase", ["retire", "retain"])
def test_rotation_rename_failure_recovers_without_losing_previous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """Post-publication rename errors retain the new active and recover rollback on retry."""
    source = _bundle(tmp_path / "source")
    target = tmp_path / "active"
    install_config_bundle(source, target, process_env={})
    (source / "prompts/helper.md").write_text("Second prompt")
    install_config_bundle(source, target, process_env={})
    (source / "prompts/helper.md").write_text("Third prompt")
    rename = Path.rename
    failing_destination = tmp_path / (".active.retired" if phase == "retire" else "active.previous")

    def fail_rotation(path: Path, destination: Path) -> Path:
        if destination == failing_destination:
            msg = "rotation failed"
            raise OSError(msg)
        return rename(path, destination)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", fail_rotation)
        receipt = install_config_bundle(source, target, process_env={})
        assert receipt.status == "installed"
        assert receipt.recovery_pending
    assert (target / "prompts/helper.md").read_text() == "Third prompt"
    assert (tmp_path / ".active.pending/prompts/helper.md").read_text() == "Second prompt"
    assert install_config_bundle(source, target, process_env={}).status == "unchanged"
    assert (tmp_path / "active.previous/prompts/helper.md").read_text() == "Second prompt"


@pytest.mark.parametrize("name", [".active.pending", ".active.retired"])
def test_unowned_recovery_directories_are_untouched(tmp_path: Path, name: str) -> None:
    """An unrelated directory with a reserved name must never be adopted or removed."""
    source = _bundle(tmp_path / "source")
    recovery = _bundle(tmp_path / name)
    before = _snapshot(recovery)
    with pytest.raises(ValueError, match="recovery"):
        install_config_bundle(source, tmp_path / "active", process_env={})
    assert _snapshot(recovery) == before
    assert not (tmp_path / "active").exists()


def test_retired_cleanup_failure_keeps_success_receipt_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup error after activation must not lose its receipt or block the next revision."""
    source = _bundle(tmp_path / "source")
    target = tmp_path / "active"
    install_config_bundle(source, target, process_env={})
    (source / "prompts/helper.md").write_text("Second prompt")
    install_config_bundle(source, target, process_env={})
    (source / "prompts/helper.md").write_text("Third prompt")
    rmtree = shutil.rmtree

    def fail_cleanup(path: Path, **kwargs: object) -> None:
        if path.name == ".active.retired":
            msg = "cleanup failed"
            raise OSError(msg)
        rmtree(path, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "rmtree", fail_cleanup)
        receipt = install_config_bundle(source, target, process_env={})
    assert receipt.status == "installed"
    assert receipt.recovery_pending
    assert (target / "prompts/helper.md").read_text() == "Third prompt"
    assert (tmp_path / "active.previous/prompts/helper.md").read_text() == "Second prompt"
    assert install_config_bundle(source, target, process_env={}).status == "unchanged"
    assert not (tmp_path / ".active.retired").exists()
    (source / "prompts/helper.md").write_text("Fourth prompt")
    assert install_config_bundle(source, target, process_env={}).status == "installed"


@pytest.mark.parametrize("change", ["replace", "edit"])
def test_recovery_rejects_changed_owned_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """A transaction cannot authorize mutation of changed recovery contents."""
    source = _bundle(tmp_path / "source")
    target = tmp_path / "active"
    install_config_bundle(source, target, process_env={})
    (source / "prompts/helper.md").write_text("Second prompt")
    rename = Path.rename

    def interrupt(path: Path, destination: Path) -> Path:
        if destination == target:
            raise SystemExit
        return rename(path, destination)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", interrupt)
        with pytest.raises(SystemExit):
            install_config_bundle(source, target, process_env={})
    pending = tmp_path / ".active.pending"
    if change == "replace":
        pending.rename(tmp_path / "saved")
        _bundle(pending, "Unrelated tree")
    else:
        (pending / ".env").write_text("RECOVERY_EDIT=keep\n")
    before = _snapshot(pending)
    with pytest.raises(ValueError, match="Unowned"):
        install_config_bundle(source, target, process_env={})
    assert _snapshot(pending) == before
    assert not target.exists()


@pytest.mark.parametrize("metadata", ["invalid", "[]", '{"digest": "wrong"}'])
def test_invalid_metadata_cannot_authorize_replacement(tmp_path: Path, metadata: str) -> None:
    """Corrupt drift baselines must fail closed without changing active files."""
    source = _bundle(tmp_path / "source")
    target = tmp_path / "active"
    install_config_bundle(source, target, process_env={})
    (target / ".mindroom-bundle.json").write_text(metadata)
    before = _snapshot(target)
    (source / "prompts/helper.md").write_text("Second prompt")
    with pytest.raises(ValueError, match="--force"):
        install_config_bundle(source, target, process_env={})
    assert _snapshot(target) == before


@pytest.mark.parametrize("suffix", ["lock", "transaction"])
def test_control_files_cannot_be_symlinks(tmp_path: Path, suffix: str) -> None:
    """Installer bookkeeping must not lock or overwrite files reached through links."""
    source = _bundle(tmp_path / "source")
    outside = tmp_path / "outside"
    outside.write_text("Keep")
    (tmp_path / f".active.{suffix}").symlink_to(outside)
    with pytest.raises(ValueError, match="real file"):
        install_config_bundle(source, tmp_path / "active", process_env={})
    assert outside.read_text() == "Keep"


@pytest.mark.parametrize("initialized", [False, True])
def test_unowned_previous_directory_cannot_be_rotated_or_deleted(tmp_path: Path, initialized: bool) -> None:
    """A preexisting or replaced previous tree is not owned merely because its path matches."""
    source = _bundle(tmp_path / "source")
    target = tmp_path / "active"
    if initialized:
        install_config_bundle(source, target, process_env={})
    previous = _bundle(tmp_path / "active.previous", "Keep unrelated files")
    before = _snapshot(previous)
    active = _snapshot(target)
    (source / "prompts/helper.md").write_text("New revision")
    with pytest.raises(ValueError, match="Unowned"):
        install_config_bundle(source, target, process_env={})
    assert _snapshot(previous) == before
    assert _snapshot(target) == active


def test_partial_transaction_write_preserves_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A partial journal write must not poison installer state before any tree moves."""
    source = _bundle(tmp_path / "source")
    target = tmp_path / "active"
    install_config_bundle(source, target, process_env={})
    before = _snapshot(target)
    (source / "prompts/helper.md").write_text("Second prompt")

    def fail_dump(_value: object, stream: TextIO) -> None:
        stream.write('{"pending":')
        msg = "journal write failed"
        raise OSError(msg)

    with monkeypatch.context() as patch:
        patch.setattr(json, "dump", fail_dump)
        with pytest.raises(OSError, match="journal write failed"):
            install_config_bundle(source, target, process_env={})
    assert _snapshot(target) == before
    assert install_config_bundle(source, target, process_env={}).status == "installed"
    assert (tmp_path / "active.previous/prompts/helper.md").read_text() == "First prompt"


def test_malformed_transaction_values_fail_closed(tmp_path: Path) -> None:
    """A record with valid keys but invalid digest types cannot authorize recovery."""
    source = _bundle(tmp_path / "source")
    journal = tmp_path / ".active.transaction"
    content = '{"pending": [], "retired": null}'
    journal.write_text(content)
    with pytest.raises(ValueError, match="Invalid bundle recovery"):
        install_config_bundle(source, tmp_path / "active", process_env={})
    assert journal.read_text() == content
    assert not (tmp_path / "active").exists()


def test_final_journal_write_failure_returns_success_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completed tree rotation remains retryable when persisting idle ownership fails."""
    source = _bundle(tmp_path / "source")
    target = tmp_path / "active"
    install_config_bundle(source, target, process_env={})
    (source / "prompts/helper.md").write_text("Second prompt")
    dump = json.dump

    def fail_final_write(value: dict[str, object], stream: TextIO) -> None:
        if "previous" in value:
            stream.write('{"previous":')
            msg = "final journal write failed"
            raise OSError(msg)
        dump(value, stream)

    with monkeypatch.context() as patch:
        patch.setattr(json, "dump", fail_final_write)
        receipt = install_config_bundle(source, target, process_env={})
    assert receipt.status == "installed"
    assert receipt.recovery_pending
    assert (target / "prompts/helper.md").read_text() == "Second prompt"
    assert install_config_bundle(source, target, process_env={}).status == "unchanged"
    (source / "prompts/helper.md").write_text("Third prompt")
    assert install_config_bundle(source, target, process_env={}).status == "installed"
    assert (tmp_path / "active.previous/prompts/helper.md").read_text() == "Second prompt"


def test_copied_volume_preserves_bootstrap_and_installation(tmp_path: Path) -> None:
    """File-level restore keeps ownership even when every directory has a new inode or device."""
    source = _bundle(tmp_path / "source")
    volume = tmp_path / "volume"
    volume.mkdir()
    install_config_bundle(source, volume / "active", process_env={})
    (source / "prompts/helper.md").write_text("Second prompt")
    install_config_bundle(source, volume / "active", process_env={})
    restored = tmp_path / "restored"
    shutil.copytree(volume, restored)
    target = restored / "active"
    assert install_config_bundle(source, target, initialize_only=True, process_env={}).status == "initialized"
    assert install_config_bundle(source, target, process_env={}).status == "unchanged"
    (source / "prompts/helper.md").write_text("Third prompt")
    assert install_config_bundle(source, target, process_env={}).status == "installed"
    assert (restored / "active.previous/prompts/helper.md").read_text() == "Second prompt"


@pytest.mark.parametrize("phase", ["publish", "rotate_previous"])
def test_copied_interrupted_volume_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """Recovery recognizes complete saved trees after remount or file-level restore."""
    source = _bundle(tmp_path / "source")
    volume = tmp_path / "volume"
    volume.mkdir()
    target = volume / "active"
    install_config_bundle(source, target, process_env={})
    (source / "prompts/helper.md").write_text("Second prompt")
    install_config_bundle(source, target, process_env={})
    (source / "prompts/helper.md").write_text("Third prompt")
    rename = Path.rename
    interrupted_destination = target if phase == "publish" else volume / "active.previous"

    def interrupt(path: Path, destination: Path) -> Path:
        if destination == interrupted_destination:
            raise SystemExit
        return rename(path, destination)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", interrupt)
        with pytest.raises(SystemExit):
            install_config_bundle(source, target, process_env={})
    restored = tmp_path / "restored"
    shutil.copytree(volume, restored)
    install_config_bundle(source, restored / "active", process_env={})
    assert (restored / "active/prompts/helper.md").read_text() == "Third prompt"
    assert (restored / "active.previous/prompts/helper.md").read_text() == "Second prompt"


@pytest.mark.parametrize("change", ["edit", "replace", "symlink"])
def test_changed_previous_tree_is_never_deleted(tmp_path: Path, change: str) -> None:
    """Prior ownership must not authorize deleting a backup whose content changed."""
    source = _bundle(tmp_path / "source")
    target = tmp_path / "active"
    install_config_bundle(source, target, process_env={})
    (source / "prompts/helper.md").write_text("Second prompt")
    install_config_bundle(source, target, process_env={})
    previous = tmp_path / "active.previous"
    if change == "replace":
        previous.rename(tmp_path / "saved")
        _bundle(previous, "Unrelated tree")
    elif change == "edit":
        (previous / ".env").write_text("BACKUP_EDIT=keep\n")
    else:
        (previous / ".mindroom-bundle.json").unlink()
        (previous / ".mindroom-bundle.json").symlink_to(source / "config.yaml")
    before = _snapshot(previous)
    active = _snapshot(target)
    (source / "prompts/helper.md").write_text("Third prompt")
    with pytest.raises(ValueError, match=r"Unowned|symlink"):
        install_config_bundle(source, target, process_env={})
    assert _snapshot(previous) == before
    assert _snapshot(target) == active


def test_recovery_digest_excludes_installer_metadata(tmp_path: Path) -> None:
    """Reserved regular metadata cannot change content identity or authorize authored content."""
    source = _bundle(tmp_path / "source")
    target = tmp_path / "active"
    install_config_bundle(source, target, process_env={})
    (source / "prompts/helper.md").write_text("Second prompt")
    install_config_bundle(source, target, process_env={})
    previous = tmp_path / "active.previous"
    (previous / ".mindroom-bundle.json").write_text("not a drift baseline")
    (source / "prompts/helper.md").write_text("Third prompt")
    assert install_config_bundle(source, target, process_env={}).status == "installed"
    assert (previous / "prompts/helper.md").read_text() == "Second prompt"


def test_forced_unmanaged_backup_needs_no_marker_mutation(tmp_path: Path) -> None:
    """Force preserves an unmanaged tree exactly and owns its backup through the external journal."""
    source = _bundle(tmp_path / "source", "Installed prompt")
    target = _bundle(tmp_path / "active", "Unmanaged prompt")
    before = _snapshot(target)
    install_config_bundle(source, target, force=True, process_env={})
    previous = tmp_path / "active.previous"
    assert _snapshot(previous) == before
    (source / "prompts/helper.md").write_text("Next prompt")
    assert install_config_bundle(source, target, process_env={}).status == "installed"
    assert (previous / "prompts/helper.md").read_text() == "Installed prompt"
