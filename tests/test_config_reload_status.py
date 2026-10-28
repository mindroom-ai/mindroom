"""Reload confirmation must follow runtime application of the exact source tree."""

from __future__ import annotations

import asyncio
import shutil
from functools import partial
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom.config.main import Config, load_config
from mindroom.constants import resolve_runtime_paths
from mindroom.orchestration.config_lifecycle import ConfigReloadLifecycle, load_published_config
from mindroom.response_admission import ResponseAdmissionGate

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _paths(root: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=root / "config.yaml",
        storage_path=root / "data",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": ""},
    )


def test_loaded_fingerprint_covers_includes_and_survives_disk_changes(tmp_path: Path) -> None:
    """A root-only hash or a hash reread after loading would miss an included edit."""
    first = tmp_path / "first"
    first.mkdir()
    (first / "config.yaml").write_text("defaults: !include defaults.yaml\n")
    (first / "defaults.yaml").write_text("enable_streaming: false\n")
    second = tmp_path / "second"
    shutil.copytree(first, second)
    original = load_config(_paths(first))
    copied = load_config(_paths(second))
    assert original.source_fingerprint == copied.source_fingerprint
    assert original.source_fingerprint is not None
    (first / "defaults.yaml").write_text("enable_streaming: true\n")
    edited = load_config(_paths(first))
    assert original.source_fingerprint != edited.source_fingerprint
    assert copied.source_fingerprint == original.source_fingerprint


def _lifecycle(tmp_path: Path) -> ConfigReloadLifecycle:
    runtime_paths = _paths(tmp_path)
    return ConfigReloadLifecycle(
        runtime_paths=runtime_paths,
        is_running=lambda: True,
        current_config=Config,
        agent_bots=dict,
        load_config=partial(load_published_config, runtime_paths, api_enabled=False),
        apply_update_plan=AsyncMock(return_value=True),
        response_admission_gate=ResponseAdmissionGate(),
        before_runtime_replacement=AsyncMock(),
        rebind_published_config_consumers=MagicMock(),
    )


@pytest.mark.asyncio
async def test_confirmation_waits_for_apply_completion(tmp_path: Path) -> None:
    """Loading and publishing a config cannot acknowledge a blocked runtime apply."""
    (tmp_path / "config.yaml").write_text("defaults: {enable_streaming: false}\n")
    lifecycle = _lifecycle(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def apply(_config: object, _plan: object) -> bool:
        entered.set()
        await release.wait()
        return True

    lifecycle.apply_update_plan = apply
    task = asyncio.create_task(lifecycle._apply_queued_config_reload())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert lifecycle.status.status == "pending"
        assert lifecycle.status.fingerprint is not None
        release.set()
        await asyncio.wait_for(task, 2)
        assert lifecycle.status.status == "applied"
        assert lifecycle.status.fingerprint == load_config(_paths(tmp_path)).source_fingerprint
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_comment_only_edit_is_acknowledged(tmp_path: Path) -> None:
    """Equivalent authored config still acknowledges the newly loaded source bytes."""
    path = tmp_path / "config.yaml"
    path.write_text("{}\n")
    lifecycle = _lifecycle(tmp_path)
    current = load_config(lifecycle.runtime_paths)
    lifecycle.current_config = lambda: current
    lifecycle._record_applied(current)
    path.write_text("# reformatted\n{}\n")
    await lifecycle._apply_queued_config_reload()
    assert lifecycle.status.status == "applied"
    assert lifecycle.status.fingerprint != current.source_fingerprint
    # The dashboard's generation advanced with the new bytes, so trigger delivery rebinds to it.
    lifecycle.rebind_published_config_consumers.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["validation", "apply"])
async def test_failure_reports_the_attempted_source(tmp_path: Path, failure: str) -> None:
    """Rejected config and failed runtime application cannot produce a success receipt."""
    path = tmp_path / "config.yaml"
    path.write_text("{}\n")
    lifecycle = _lifecycle(tmp_path)
    current = load_config(lifecycle.runtime_paths)
    lifecycle.current_config = lambda: current
    lifecycle._record_applied(current)
    path.write_text(
        "defaults: {enable_streaming: invalid}\n"
        if failure == "validation"
        else "defaults: {enable_streaming: false}\n",
    )
    lifecycle.apply_update_plan = AsyncMock(side_effect=RuntimeError("private error payload"))
    await lifecycle._apply_queued_config_reload()
    assert lifecycle.status.status == "failed"
    assert lifecycle.status.fingerprint is not None
    assert lifecycle.status.fingerprint != current.source_fingerprint
    assert "private error payload" not in lifecycle.status.model_dump_json()


@pytest.mark.asyncio
async def test_malformed_include_failure_has_no_unproven_fingerprint(tmp_path: Path) -> None:
    """An incomplete parse cannot attribute its failure to a complete source tree."""
    (tmp_path / "config.yaml").write_text("defaults: !include missing.yaml\n")
    lifecycle = _lifecycle(tmp_path)
    await lifecycle._apply_queued_config_reload()
    assert lifecycle.status.status == "failed"
    assert lifecycle.status.fingerprint is None


@pytest.mark.asyncio
async def test_queued_change_preserves_active_failure_fingerprint(tmp_path: Path) -> None:
    """A newer file event cannot erase attribution for an apply already in progress."""
    (tmp_path / "config.yaml").write_text("defaults: {enable_streaming: false}\n")
    lifecycle = _lifecycle(tmp_path)
    expected = load_config(lifecycle.runtime_paths).source_fingerprint

    async def fail_after_new_change(_config: object, _plan: object) -> bool:
        lifecycle.request_reload()
        msg = "apply failed"
        raise RuntimeError(msg)

    lifecycle.apply_update_plan = fail_after_new_change
    task = asyncio.create_task(lifecycle._apply_queued_config_reload())
    lifecycle._reload_task = task
    await task
    assert lifecycle.status.status == "failed"
    assert lifecycle.status.fingerprint == expected
