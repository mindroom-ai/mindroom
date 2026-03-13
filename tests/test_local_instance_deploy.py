"""Tests for the local multi-instance deploy helper."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

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


def test_traefik_proxy_names_only_returns_traefik_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proxy detection should ignore app containers that merely carry Traefik labels."""

    def _run(cmd: str, **_kwargs: object) -> SimpleNamespace:
        assert "docker ps --filter network=mynetwork" in cmd
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "traefik:v3.1\ttraefik-main\n"
                "ghcr.io/mindroom-ai/mindroom-synapse:develop\talpha-synapse\n"
                "ghcr.io/mindroom-ai/deploy-mindroom:latest\talpha-mindroom\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(deploy.subprocess, "run", _run)

    assert deploy._traefik_proxy_names("mynetwork") == ["traefik-main"]


def test_load_traefik_settings_reads_env_overrides(tmp_path: Path) -> None:
    """Per-instance env files should override Traefik label defaults."""
    env_file = tmp_path / "alpha.env"
    env_file.write_text(
        "TRAEFIK_WEB_ENTRYPOINT=public-web\nTRAEFIK_MATRIX_ENTRYPOINT=federation\nTRAEFIK_CERTRESOLVER=letsencrypt\n",
    )

    assert deploy._load_traefik_settings(env_file) == deploy.TraefikSettings(
        web_entrypoint="public-web",
        matrix_entrypoint="federation",
        certresolver="letsencrypt",
    )


def test_print_running_instance_access_warns_without_traefik(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start output should explain when only localhost ports are currently usable."""
    instance = _instance("alpha", matrix_type=deploy.MatrixType.TUWUNEL, data_root=tmp_path)
    console = Console(record=True)
    monkeypatch.setattr(deploy, "console", console)

    deploy._print_running_instance_access(
        instance,
        only_matrix=False,
        traefik_proxies=[],
        traefik_settings=deploy.TraefikSettings(),
    )

    text = console.export_text()
    assert "MindRoom local:" in text
    assert "Matrix local:" in text
    assert "No Traefik container detected" in text
    assert "domain-based federation" in text
    assert "web=websecure" in text
    assert "matrix=matrix-fed" in text
    assert "resolver=porkbun" in text


def test_print_running_instance_access_keeps_domain_routes_conditional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detected Traefik proxies should not be reported as sufficient on their own."""
    instance = _instance("alpha", matrix_type=deploy.MatrixType.TUWUNEL, data_root=tmp_path)
    console = Console(record=True)
    monkeypatch.setattr(deploy, "console", console)

    deploy._print_running_instance_access(
        instance,
        only_matrix=False,
        traefik_proxies=["traefik-main"],
        traefik_settings=deploy.TraefikSettings(
            web_entrypoint="public-web",
            matrix_entrypoint="federation",
            certresolver="letsencrypt",
        ),
    )

    text = console.export_text()
    assert "Traefik detected:" in text
    assert "only work" in text
    assert "after the proxy matches this instance's entrypoint and certresolver names" in text
    assert "Configured MindRoom domain:" in text
    assert "Configured Matrix domain:" in text
    assert "web=public-web" in text
    assert "matrix=federation" in text
    assert "resolver=letsencrypt" in text


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


def test_remove_instance_repairs_container_owned_data_before_deleting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removal should recover from root-owned bind-mount files created by containers."""
    env_dir = tmp_path / "envs"
    env_dir.mkdir()
    monkeypatch.setattr(deploy, "ENV_DIR", env_dir)

    instance = _instance("alpha", matrix_type=deploy.MatrixType.TUWUNEL, data_root=tmp_path)
    data_dir = Path(instance.data_dir)
    data_dir.mkdir(parents=True)
    env_file = env_dir / "alpha.env"
    env_file.write_text("INSTANCE_NAME=alpha\n")

    registry = deploy.Registry(
        instances={"alpha": instance},
        allocated_ports=deploy.AllocatedPorts(mindroom=[instance.mindroom_port], matrix=[instance.matrix_port or 8448]),
    )

    commands: list[str] = []

    def _run(cmd: str, **_kwargs: object) -> SimpleNamespace:
        commands.append(cmd)
        if "docker ps -a --filter" in cmd:
            return SimpleNamespace(returncode=0, stdout="ghcr.io/mindroom-ai/mindroom-tuwunel:latest\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    permission_denied = PermissionError("permission denied")
    rmtree_calls = 0

    def _rmtree(path: Path) -> None:
        nonlocal rmtree_calls
        rmtree_calls += 1
        assert path == data_dir
        if rmtree_calls == 1:
            raise permission_denied

    monkeypatch.setattr(deploy.subprocess, "run", _run)
    monkeypatch.setattr(deploy.shutil, "rmtree", _rmtree)

    deploy._remove_instance("alpha", registry, deploy.console)

    assert rmtree_calls == 2
    assert any(cmd.startswith("docker run --rm ") for cmd in commands)
    assert "alpha" not in registry.instances
    assert not env_file.exists()
    assert registry.allocated_ports.mindroom == []
    assert registry.allocated_ports.matrix == []


def test_remove_all_persists_progress_when_later_instance_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batch removal should keep the registry file aligned with completed deletions."""
    reg_file = tmp_path / "instances.json"
    monkeypatch.setattr(deploy, "REGISTRY_FILE", reg_file)

    alpha = _instance("alpha", matrix_type=deploy.MatrixType.TUWUNEL, data_root=tmp_path)
    beta = _instance("beta", matrix_type=deploy.MatrixType.SYNAPSE, data_root=tmp_path)
    beta.mindroom_port = 8766
    beta.matrix_port = 8449
    registry = deploy.Registry(
        instances={"alpha": alpha, "beta": beta},
        allocated_ports=deploy.AllocatedPorts(
            mindroom=[alpha.mindroom_port, beta.mindroom_port],
            matrix=[alpha.matrix_port or 8448, beta.matrix_port or 8449],
        ),
    )
    deploy.save_registry(registry)
    monkeypatch.setattr(deploy, "load_registry", lambda: registry)

    def _remove_instance(name: str, registry: deploy.Registry, _console: Console) -> None:
        if name == "alpha":
            del registry.instances[name]
            registry.allocated_ports.mindroom.remove(alpha.mindroom_port)
            registry.allocated_ports.matrix.remove(alpha.matrix_port or 8448)
            return
        raise deploy.typer.Exit(1)

    monkeypatch.setattr(deploy, "_remove_instance", _remove_instance)

    with pytest.raises(deploy.typer.Exit):
        deploy.remove(all=True, force=True)

    saved_registry = json.loads(reg_file.read_text())
    assert sorted(saved_registry["instances"]) == ["beta"]


def test_get_actual_status_does_not_count_wellknown_as_matrix_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """The .well-known sidecar alone should not count as a live Matrix stack."""

    def _run(cmd: str, **_kwargs: object) -> SimpleNamespace:
        assert "docker ps --filter" in cmd
        return SimpleNamespace(returncode=0, stdout="wellknown\n", stderr="")

    monkeypatch.setattr(deploy.subprocess, "run", _run)

    assert deploy.get_actual_status("alpha") == (False, False)


def test_get_actual_status_requires_matrix_runtime_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """Database sidecars alone should not count as a running Matrix server."""

    def _run(cmd: str, **_kwargs: object) -> SimpleNamespace:
        assert "docker ps --filter" in cmd
        return SimpleNamespace(returncode=0, stdout="postgres\nredis\n", stderr="")

    monkeypatch.setattr(deploy.subprocess, "run", _run)

    assert deploy.get_actual_status("alpha") == (False, False)
