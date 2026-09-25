"""Native desktop configuration contract tests."""

# ruff: noqa: D103

from __future__ import annotations

import json
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mindroom.desktop.native_config import (
    NativeConfigError,
    NativeDesktopConfig,
    NativeFilesConfig,
    NativeShellConfig,
    load_native_config,
    native_config_path,
    save_native_config,
)


def _payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "v": 1,
        "revision": 0,
        "enabled": True,
        "controller": {"user_id": "@controller:example.org", "device_id": "DEVICE", "ed25519": "key"},
        "allowed_requester_ids": ["@person:example.org"],
        "allowed_agent_names": ["assistant"],
        "allowed_app_ids": ["com.example.Editor"],
        "capture": {"max_screenshot_width": 1568, "jpeg_quality": 80},
        "browser": {
            "enabled": False,
            "executable_path": None,
            "user_data_dir": None,
            "timeout_seconds": 90,
        },
    }
    payload.update(updates)
    return payload


def test_native_config_round_trip_and_owner_only_mode(tmp_path: Path) -> None:
    path = native_config_path(tmp_path)
    saved = save_native_config(path, NativeDesktopConfig.from_payload(_payload()), expected_revision=0)
    assert saved.revision == 1
    assert load_native_config(path) == saved
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def _extended(roots: object, *, enabled: object = False, **updates: object) -> dict[str, object]:
    payload = _payload(**updates)
    payload["files"] = {"roots": roots}
    payload["shell"] = {"enabled": enabled}
    return payload


def test_old_config_loads_disabled_local_access_and_saves_complete_extended_payload(tmp_path: Path) -> None:
    path = native_config_path(tmp_path)
    path.parent.mkdir(mode=0o700)
    path.write_text(json.dumps(_payload(revision=4)), encoding="utf-8")
    path.chmod(0o600)
    old = load_native_config(path)
    assert old.files == NativeFilesConfig(roots=())
    assert old.shell == NativeShellConfig(enabled=False)
    saved = save_native_config(path, old, expected_revision=4)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        **_payload(revision=5),
        "files": {"roots": []},
        "shell": {"enabled": False},
    }
    assert load_native_config(path) == saved


def test_extended_config_round_trips_without_checking_saved_root_availability(tmp_path: Path) -> None:
    missing = tmp_path / "removed-after-selection"
    config = NativeDesktopConfig.from_payload(_extended([str(tmp_path), str(missing)], enabled=True))
    assert config.files == NativeFilesConfig(roots=(tmp_path, missing))
    assert config.shell == NativeShellConfig(enabled=True)
    saved = save_native_config(native_config_path(tmp_path), config, expected_revision=0)
    assert saved.to_payload()["files"] == {"roots": [str(tmp_path), str(missing)]}
    assert saved.to_payload()["shell"] == {"enabled": True}
    assert load_native_config(native_config_path(tmp_path)) == saved


@pytest.mark.parametrize(
    "payload",
    [
        {**_payload(), "files": {"roots": []}},
        {**_payload(), "shell": {"enabled": False}},
        {**_extended([]), "files": {"roots": [], "writable": True}},
        {**_extended([]), "shell": {"enabled": False, "auto_approve_seconds": 60}},
        {**_extended([]), "files": []},
        _extended("/selected"),
        _extended(["/selected"] * 2),
        _extended([f"/selected/{index}" for index in range(33)]),
        _extended([7]),
        _extended([""]),
        _extended(["relative"]),
        _extended(["/selected/../other"]),
        _extended(["/selected\x00"]),
        _extended(["/" + "a" * 4096]),
        _extended([], enabled=1),
        _extended([], enabled=None),
    ],
)
def test_native_config_rejects_invalid_local_access(payload: dict[str, object]) -> None:
    with pytest.raises(NativeConfigError) as caught:
        NativeDesktopConfig.from_payload(payload)
    assert caught.value.code == "invalid_request"


def test_local_access_edit_canonicalizes_only_new_roots_and_preserves_other_settings(tmp_path: Path) -> None:
    saved_root = tmp_path / "saved"
    new_root = tmp_path / "new"
    new_root.mkdir()
    link = tmp_path / "link-to-new"
    link.symlink_to(new_root, target_is_directory=True)
    current = NativeDesktopConfig.from_payload(_extended([str(saved_root)], allowed_app_ids=[]))

    updated = current.with_local_access({"roots": [str(saved_root), str(link)]}, {"enabled": True})

    assert updated.files.roots == (saved_root, new_root.resolve())
    assert updated.shell.enabled is True
    assert {**updated.to_payload(), "files": None, "shell": None} == {
        **current.to_payload(),
        "files": None,
        "shell": None,
    }


@pytest.mark.parametrize("new_root", ["missing", "file.txt", "duplicate-link"])
def test_local_access_edit_requires_new_unique_directories(tmp_path: Path, new_root: str) -> None:
    root = (tmp_path / "selected").resolve()
    root.mkdir()
    (tmp_path / "file.txt").write_text("not a folder", encoding="utf-8")
    (tmp_path / "duplicate-link").symlink_to(root, target_is_directory=True)
    current = NativeDesktopConfig.from_payload(_extended([str(root)]))
    with pytest.raises(NativeConfigError) as caught:
        current.with_local_access({"roots": [str(root), str(tmp_path / new_root)]}, {"enabled": False})
    assert caught.value.code == "invalid_request"


def test_native_config_compare_and_swap_rejects_stale_writer(tmp_path: Path) -> None:
    path = native_config_path(tmp_path)
    save_native_config(path, NativeDesktopConfig.from_payload(_payload()), expected_revision=0)
    with pytest.raises(NativeConfigError, match="changed") as caught:
        save_native_config(path, NativeDesktopConfig.from_payload(_payload()), expected_revision=0)
    assert caught.value.code == "revision_conflict"


def test_simultaneous_app_and_terminal_saves_have_one_winner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A concurrent edit cannot silently overwrite another writer with the same revision."""
    from mindroom.desktop import native_config  # noqa: PLC0415

    path = native_config_path(tmp_path)
    original_write = native_config.write_json_file_durable
    ready = threading.Barrier(2)

    def slow_write(*args: object, **kwargs: object) -> None:
        time.sleep(0.05)
        original_write(*args, **kwargs)

    monkeypatch.setattr(native_config, "write_json_file_durable", slow_write)

    def save(app: str) -> str:
        ready.wait(timeout=5)
        try:
            config = NativeDesktopConfig.from_payload(_payload(allowed_app_ids=[app]))
            save_native_config(path, config, expected_revision=0)
        except NativeConfigError as exc:
            return exc.code
        return app

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ["com.example.First", "com.example.Second"]))
    assert results.count("revision_conflict") == 1
    assert load_native_config(path).allowed_app_ids == tuple(
        result for result in results if result != "revision_conflict"
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"unknown": True},
        {"v": True},
        {"allowed_requester_ids": []},
        {"allowed_agent_names": ["same", "same"]},
        {"allowed_app_ids": [""]},
        {"capture": {"max_screenshot_width": 319, "jpeg_quality": 80}},
        {"capture": {"max_screenshot_width": 1568, "jpeg_quality": 96}},
        {
            "browser": {
                "enabled": True,
                "executable_path": "relative",
                "user_data_dir": None,
                "timeout_seconds": 90,
            },
        },
    ],
)
def test_native_config_rejects_invalid_payload(updates: dict[str, object]) -> None:
    with pytest.raises(NativeConfigError):
        NativeDesktopConfig.from_payload(_payload(**updates))


def test_load_native_config_rejects_permissive_mode(tmp_path: Path) -> None:
    path = native_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_payload()), encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(NativeConfigError, match="group or other"):
        load_native_config(path)


@pytest.mark.parametrize("operation", ["lstat", "open"])
def test_load_native_config_reports_filesystem_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    path = native_config_path(tmp_path)
    save_native_config(path, NativeDesktopConfig.from_payload(_payload()), expected_revision=0)

    def denied(*_args: object, **_kwargs: object) -> None:
        raise PermissionError

    with monkeypatch.context() as patch:
        patch.setattr(Path if operation == "lstat" else os, operation, denied)
        with pytest.raises(NativeConfigError, match="could not be read") as caught:
            load_native_config(path)
    assert caught.value.code == "invalid_request"


@pytest.mark.parametrize("content", [b"{", b"\xff"])
def test_owned_malformed_config_can_be_repaired(tmp_path: Path, content: bytes) -> None:
    path = native_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    path.chmod(0o600)
    with pytest.raises(NativeConfigError) as caught:
        load_native_config(path)
    assert caught.value.code == "configuration_repair_required"
    assert caught.value.revision == 0
    saved = save_native_config(path, NativeDesktopConfig.from_payload(_payload()), expected_revision=0)
    assert load_native_config(path) == saved
    assert saved.revision == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_exposed_config_repair_preserves_revision_and_rejects_stale_editor(tmp_path: Path) -> None:
    path = native_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    original = json.dumps(_payload(revision=7))
    path.write_text(original)
    path.chmod(0o644)
    with pytest.raises(NativeConfigError) as caught:
        load_native_config(path)
    assert caught.value.code == "configuration_repair_required"
    assert caught.value.revision == 7
    with pytest.raises(NativeConfigError, match="changed"):
        save_native_config(path, NativeDesktopConfig.from_payload(_payload()), expected_revision=0)
    with pytest.raises(NativeConfigError, match="does not match the edit"):
        save_native_config(path, NativeDesktopConfig.from_payload(_payload()), expected_revision=7)
    assert path.read_text() == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    saved = save_native_config(path, NativeDesktopConfig.from_payload(_payload(revision=7)), expected_revision=7)
    assert saved.revision == 8
    assert load_native_config(path) == saved
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("mode", [0o600, 0o644])
def test_semantically_invalid_config_cannot_be_repaired(tmp_path: Path, mode: int) -> None:
    path = native_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    original = json.dumps(_payload(v=2))
    path.write_text(original)
    path.chmod(mode)
    with pytest.raises(NativeConfigError) as caught:
        save_native_config(path, NativeDesktopConfig.from_payload(_payload()), expected_revision=0)
    assert caught.value.code == "invalid_request"
    assert path.read_text() == original
    assert stat.S_IMODE(path.stat().st_mode) == mode


@pytest.mark.parametrize("kind", ["symlink", "directory", "foreign_owner", "unreadable"])
def test_unsupported_config_path_cannot_be_repaired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    path = native_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    target = tmp_path / "untouched.json"
    target.write_text("{")
    target.chmod(0o600)
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir(mode=0o700)
    else:
        path.write_text("{")
        path.chmod(0o600)
    before = path.lstat()
    with monkeypatch.context() as patch:
        if kind == "foreign_owner":
            patch.setattr(os, "getuid", lambda: before.st_uid + 1)
        if kind == "unreadable":
            original_open = os.open

            def denied(file: Path, flags: int) -> int:
                if file == path:
                    raise PermissionError
                return original_open(file, flags)

            patch.setattr(os, "open", denied)
        with pytest.raises(NativeConfigError) as caught:
            save_native_config(path, NativeDesktopConfig.from_payload(_payload()), expected_revision=0)
    assert caught.value.code == "invalid_request"
    assert path.lstat() == before
    assert target.read_text() == "{"
