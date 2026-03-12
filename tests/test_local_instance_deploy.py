"""Tests for the local multi-instance deploy helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

_SCRIPT_PATH = Path("local/instances/deploy/deploy.py")
_MODULE_SPEC = importlib.util.spec_from_file_location("mindroom_local_instance_deploy", _SCRIPT_PATH)
assert _MODULE_SPEC is not None
assert _MODULE_SPEC.loader is not None
deploy = importlib.util.module_from_spec(_MODULE_SPEC)
_MODULE_SPEC.loader.exec_module(deploy)


def _instance(
    name: str,
    *,
    matrix_type: deploy.MatrixType | None,
    data_root: Path,
) -> deploy.Instance:
    matrix_port = 8448 if matrix_type is not None else None
    return deploy.Instance(
        name=name,
        mindroom_port=8765,
        matrix_port=matrix_port,
        data_dir=str(data_root / name),
        domain=f"{name}.localhost",
        matrix_type=matrix_type,
    )


def test_sync_matrix_host_overrides_writes_peer_domains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each Matrix instance should get a compose override for the other Matrix domains."""
    env_dir = tmp_path / "envs"
    env_dir.mkdir()
    monkeypatch.setattr(deploy, "ENV_DIR", env_dir)

    instances = {
        "alpha": _instance("alpha", matrix_type=deploy.MatrixType.TUWUNEL, data_root=tmp_path),
        "beta": _instance("beta", matrix_type=deploy.MatrixType.SYNAPSE, data_root=tmp_path),
        "gamma": _instance("gamma", matrix_type=None, data_root=tmp_path),
    }

    deploy._sync_matrix_host_overrides(instances)

    alpha_override = (env_dir / "alpha.matrix-hosts.yml").read_text()
    beta_override = (env_dir / "beta.matrix-hosts.yml").read_text()

    assert '"m-beta.localhost:host-gateway"' in alpha_override
    assert "m-alpha.localhost" not in alpha_override
    assert '"m-alpha.localhost:host-gateway"' in beta_override
    assert "m-beta.localhost" not in beta_override
    assert not (env_dir / "gamma.matrix-hosts.yml").exists()


def test_refresh_running_matrix_services_recreates_running_peers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Starting one Matrix instance should refresh already-running peers with the new host mappings."""
    env_dir = tmp_path / "envs"
    env_dir.mkdir()
    monkeypatch.setattr(deploy, "ENV_DIR", env_dir)
    monkeypatch.setattr(deploy, "REPO_ROOT", tmp_path)

    instances = {
        "alpha": _instance("alpha", matrix_type=deploy.MatrixType.TUWUNEL, data_root=tmp_path),
        "beta": _instance("beta", matrix_type=deploy.MatrixType.SYNAPSE, data_root=tmp_path),
    }
    for name in instances:
        (env_dir / f"{name}.env").write_text(f"INSTANCE_NAME={name}\n")

    deploy._sync_matrix_host_overrides(instances)
    monkeypatch.setattr(deploy, "get_actual_status", lambda name: (False, name == "beta"))

    commands: list[str] = []

    def _run(cmd: str, **_kwargs: object) -> SimpleNamespace:
        commands.append(cmd)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(deploy.subprocess, "run", _run)

    deploy._refresh_running_matrix_services_for_host_updates(
        instances,
        updated_instance_name="alpha",
    )

    assert len(commands) == 1
    assert "beta.matrix-hosts.yml" in commands[0]
    assert "up -d --no-deps --force-recreate synapse" in commands[0]
