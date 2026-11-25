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

from mindroom.desktop.command_journal import controller_journal_key
from mindroom.desktop.native_config import (
    NativeConfigError,
    NativeControllerEntry,
    NativeDesktopConfig,
    NativeFilesConfig,
    NativeShellConfig,
    load_native_config,
    native_config_path,
    save_native_config,
)
from mindroom.matrix.device_identity import PinnedMatrixDevice


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


def test_saved_config_that_allows_mindroom_itself_loads_and_saves_without_it(tmp_path: Path) -> None:
    """Earlier releases saved MindRoom's own app IDs; those setups must stay usable and editable, while edits refuse them."""
    path = native_config_path(tmp_path)
    path.parent.mkdir(mode=0o700)
    apps = ["chat.mindroom.menubar", "com.example.Editor", "chat.mindroom.desktophelper"]
    path.write_text(json.dumps(_payload(revision=3, allowed_app_ids=apps)), encoding="utf-8")
    path.chmod(0o600)
    loaded = load_native_config(path)
    assert loaded.allowed_app_ids == ("com.example.Editor",)
    save_native_config(path, loaded, expected_revision=3)
    assert json.loads(path.read_text(encoding="utf-8"))["allowed_app_ids"] == ["com.example.Editor"]
    with pytest.raises(NativeConfigError, match="control MindRoom itself"):
        loaded.with_allowed_apps(["com.example.Editor", "chat.mindroom.menubar"])


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
        {"allowed_app_ids": ["com.example.Editor", "chat.mindroom.menubar"]},
        {"allowed_app_ids": ["chat.mindroom.desktophelper"]},
        {"capture": {"max_screenshot_width": 319, "jpeg_quality": 80}},
        {"capture": {"max_screenshot_width": 1568, "jpeg_quality": 19}},
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


def _v2_payload(controllers: list[dict[str, object]], **updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "v": 2,
        "revision": 0,
        "enabled": True,
        "controllers": controllers,
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


def _controller_entry_payload(
    user_id: str = "@controller:example.org",
    device_id: str = "DEVICE",
    ed25519: str = "key",
    *,
    requesters: list[str] | None = None,
    agents: list[str] | None = None,
) -> dict[str, object]:
    return {
        "controller": {"user_id": user_id, "device_id": device_id, "ed25519": ed25519},
        "allowed_requester_ids": ["@person:example.org"] if requesters is None else requesters,
        "allowed_agent_names": ["assistant"] if agents is None else agents,
    }


def test_v1_payload_loads_into_single_controller_entry(tmp_path: Path) -> None:
    path = native_config_path(tmp_path)
    path.parent.mkdir(mode=0o700)
    path.write_text(json.dumps(_payload(revision=2)), encoding="utf-8")
    path.chmod(0o600)

    loaded = load_native_config(path)
    expected_entry = NativeControllerEntry(
        controller=PinnedMatrixDevice("@controller:example.org", "DEVICE", "key"),
        allowed_requester_ids=("@person:example.org",),
        allowed_agent_names=("assistant",),
    )
    assert loaded.controllers == (expected_entry,)
    assert loaded.controller == expected_entry.controller
    assert loaded.allowed_requester_ids == expected_entry.allowed_requester_ids
    assert loaded.allowed_agent_names == expected_entry.allowed_agent_names
    assert loaded.to_payload()["v"] == 1

    explicit_v2 = NativeDesktopConfig.from_payload(_v2_payload([_controller_entry_payload()], revision=2))
    assert explicit_v2 == loaded
    assert explicit_v2.to_payload()["v"] == 1


def test_v2_roundtrip_and_upsert_remove_controller(tmp_path: Path) -> None:
    path = native_config_path(tmp_path)
    first = NativeControllerEntry(
        controller=PinnedMatrixDevice("@controller:example.org", "DEVICE", "key"),
        allowed_requester_ids=("@person:example.org",),
        allowed_agent_names=("assistant",),
    )
    second = NativeControllerEntry(
        controller=PinnedMatrixDevice("@other:example.org", "DEVICE_B", "key-b"),
        allowed_requester_ids=("@alice:example.org",),
        allowed_agent_names=("coder",),
    )

    cfg = NativeDesktopConfig.from_payload(_payload())
    multi = cfg.with_upserted_controller(second)
    assert multi.controllers == (first, second)
    assert multi.controller == first.controller
    assert multi.allowed_requester_ids == first.allowed_requester_ids
    assert multi.allowed_agent_names == first.allowed_agent_names
    assert multi.to_payload()["v"] == 2
    assert "controller" not in multi.to_payload()

    saved = save_native_config(path, multi, expected_revision=0)
    assert json.loads(path.read_text(encoding="utf-8"))["v"] == 2
    assert load_native_config(path) == saved

    updated_first = NativeControllerEntry(
        controller=first.controller,
        allowed_requester_ids=("@person:example.org", "@bob:example.org"),
        allowed_agent_names=("assistant", "reviewer"),
    )
    upserted = saved.with_upserted_controller(updated_first)
    assert upserted.controllers == (updated_first, second)

    one_left = upserted.with_removed_controller(controller_journal_key(first.controller))
    assert one_left.enabled is True
    assert one_left.controllers == (second,)
    assert one_left.controller == second.controller
    assert one_left.to_payload()["v"] == 1

    empty = one_left.with_removed_controller(controller_journal_key(second.controller))
    assert empty.enabled is False
    assert empty.controllers == ()
    assert empty.allowed_requester_ids == ()
    assert empty.allowed_agent_names == ()
    assert empty.to_payload()["v"] == 2
    assert empty.to_payload()["controllers"] == []
    with pytest.raises(NativeConfigError) as missing_controller:
        _ = empty.controller
    assert missing_controller.value.code == "configuration_missing"

    saved_empty = save_native_config(path, empty, expected_revision=1)
    assert load_native_config(path) == saved_empty

    with pytest.raises(NativeConfigError) as unknown_remove:
        saved.with_removed_controller("unknown-controller-key")
    assert unknown_remove.value.code == "invalid_request"


@pytest.mark.parametrize(
    "payload",
    [
        _v2_payload([_controller_entry_payload(), _controller_entry_payload()]),
        _v2_payload([_controller_entry_payload(), _controller_entry_payload(ed25519="different-key")]),
        _v2_payload([], enabled=True),
        _v2_payload([_controller_entry_payload()], v=3),
        _v2_payload([_controller_entry_payload()], unknown=True),
        _v2_payload([_controller_entry_payload()], controller={"user_id": "@a:b", "device_id": "D", "ed25519": "k"}),
        _v2_payload([{**_controller_entry_payload(), "extra": True}]),
        _v2_payload([_controller_entry_payload(requesters=[])]),
        _v2_payload([_controller_entry_payload(agents=["dup", "dup"])]),
        _v2_payload([_controller_entry_payload()], files={"roots": []}),
    ],
)
def test_v2_rejects_invalid_and_duplicate_controller_payloads(payload: dict[str, object]) -> None:
    with pytest.raises(NativeConfigError) as caught:
        NativeDesktopConfig.from_payload(payload)
    assert caught.value.code == "invalid_request"


def test_upsert_rejects_mismatched_ed25519_for_same_device() -> None:
    cfg = NativeDesktopConfig.from_payload(_payload())
    conflicting = NativeControllerEntry(
        controller=PinnedMatrixDevice("@controller:example.org", "DEVICE", "other-key"),
        allowed_requester_ids=("@person:example.org",),
        allowed_agent_names=("assistant",),
    )
    with pytest.raises(NativeConfigError) as caught:
        cfg.with_upserted_controller(conflicting)
    assert caught.value.code == "invalid_request"


def test_native_config_accepts_full_jpeg_quality_range() -> None:
    """Saved configs accept jpeg_quality from 20 through 95."""
    for quality in (20, 35, 95):
        cfg = NativeDesktopConfig.from_payload(
            _payload(capture={"max_screenshot_width": 1568, "jpeg_quality": quality}),
        )
        assert cfg.capture.jpeg_quality == quality
