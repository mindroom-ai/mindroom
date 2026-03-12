"""Tests for the local multi-instance deploy helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

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


def test_running_matrix_peer_names_excludes_current_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only other running Matrix instances should be flagged for manual restarts."""
    instances = {
        "alpha": _instance("alpha", matrix_type=deploy.MatrixType.TUWUNEL, data_root=tmp_path),
        "beta": _instance("beta", matrix_type=deploy.MatrixType.SYNAPSE, data_root=tmp_path),
        "gamma": _instance("gamma", matrix_type=None, data_root=tmp_path),
    }
    monkeypatch.setattr(
        deploy,
        "get_actual_status",
        lambda name: {
            "alpha": (False, True),
            "beta": (False, True),
            "gamma": (False, False),
        }[name],
    )

    assert deploy._running_matrix_peer_names(instances, exclude_name="alpha") == ["beta"]


def test_stop_uses_project_down_without_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stopping should still work even if the env file is already gone."""
    registry = deploy.Registry(
        instances={
            "alpha": _instance("alpha", matrix_type=deploy.MatrixType.TUWUNEL, data_root=tmp_path),
        },
    )
    monkeypatch.setattr(deploy, "load_registry", lambda: registry)
    monkeypatch.setattr(deploy, "save_registry", lambda _registry: None)

    commands: list[str] = []

    def _run(cmd: str, **_kwargs: object) -> SimpleNamespace:
        commands.append(cmd)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(deploy.subprocess, "run", _run)

    deploy.stop("alpha")

    assert commands == ["docker compose -p alpha down"]
    assert registry.instances["alpha"].status == deploy.InstanceStatus.STOPPED


def test_remove_instance_preserves_state_when_teardown_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed teardown must not orphan containers by deleting local instance state."""
    env_dir = tmp_path / "envs"
    env_dir.mkdir()
    monkeypatch.setattr(deploy, "ENV_DIR", env_dir)

    instance = _instance("alpha", matrix_type=deploy.MatrixType.SYNAPSE, data_root=tmp_path)
    data_dir = Path(instance.data_dir)
    data_dir.mkdir(parents=True)
    env_file = env_dir / "alpha.env"
    env_file.write_text("INSTANCE_NAME=alpha\n")

    registry = deploy.Registry(
        instances={"alpha": instance},
        allocated_ports=deploy.AllocatedPorts(mindroom=[instance.mindroom_port], matrix=[instance.matrix_port or 8448]),
    )

    def _run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stderr="boom")

    monkeypatch.setattr(deploy.subprocess, "run", _run)

    with pytest.raises(deploy.typer.Exit):
        deploy._remove_instance("alpha", registry, deploy.console)

    assert "alpha" in registry.instances
    assert data_dir.exists()
    assert env_file.exists()
