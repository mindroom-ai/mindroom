"""Native desktop configuration contract tests."""

# ruff: noqa: D103, TC003

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from mindroom.desktop.native_config import (
    NativeConfigError,
    NativeDesktopConfig,
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


def test_native_config_compare_and_swap_rejects_stale_writer(tmp_path: Path) -> None:
    path = native_config_path(tmp_path)
    save_native_config(path, NativeDesktopConfig.from_payload(_payload()), expected_revision=0)
    with pytest.raises(NativeConfigError, match="changed") as caught:
        save_native_config(path, NativeDesktopConfig.from_payload(_payload()), expected_revision=0)
    assert caught.value.code == "revision_conflict"


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
