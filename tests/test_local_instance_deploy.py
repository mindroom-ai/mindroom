"""Tests for the local multi-instance deploy helper."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml
from rich.console import Console

from tests.conftest import normalize_console_output

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


def test_auth_url_preserves_nested_subdomains(tmp_path: Path) -> None:
    """Authelia URLs should match the compose route for nested subdomains."""
    instance = _instance("alpha", matrix_type=None, data_root=tmp_path)
    instance.domain = "foo.bar.example.com"

    assert deploy._auth_url(instance) == "https://auth-foo.bar.example.com"


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

    text = normalize_console_output(console.export_text())
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


def test_restart_only_matrix_recreates_matrix_services_without_project_down(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matrix-only restart should not tear down the full instance project."""
    env_dir = tmp_path / "envs"
    env_dir.mkdir()
    monkeypatch.setattr(deploy, "ENV_DIR", env_dir)

    instance = _instance("alpha", matrix_type=deploy.MatrixType.TUWUNEL, data_root=tmp_path)
    env_file = env_dir / "alpha.env"
    env_file.write_text("INSTANCE_NAME=alpha\n")
    registry = deploy.Registry(instances={"alpha": instance})

    commands: list[str] = []
    monkeypatch.setattr(deploy, "save_registry", lambda _registry: None)
    monkeypatch.setattr(deploy, "_sync_matrix_host_overrides", lambda _instances: None)
    monkeypatch.setattr(deploy, "_ensure_instance_env_file_reference", lambda _env_file: None)
    monkeypatch.setattr(deploy, "_ensure_external_network", lambda _name: False)
    monkeypatch.setattr(deploy, "_traefik_proxy_names", lambda _name: [])
    monkeypatch.setattr(deploy, "_load_traefik_settings", lambda _env_file: deploy.TraefikSettings())
    monkeypatch.setattr(deploy, "_print_running_instance_access", lambda *_args, **_kwargs: None)

    def _run(cmd: str, **_kwargs: object) -> SimpleNamespace:
        commands.append(cmd)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(deploy.subprocess, "run", _run)

    deploy._restart_instance("alpha", instance, registry, only_matrix=True, no_build=True)

    assert len(commands) == 1
    assert "docker compose -p alpha down" not in commands[0]
    assert "up -d --force-recreate tuwunel wellknown" in commands[0]
    assert registry.instances["alpha"].status == deploy.InstanceStatus.PARTIAL


def test_compose_loads_shared_env_before_instance_env() -> None:
    """Per-instance env values should override the shared repo defaults."""
    compose = yaml.safe_load(Path("local/instances/deploy/docker-compose.yml").read_text())

    assert compose["services"]["mindroom"]["env_file"] == [
        {"path": "../../../.env", "required": False},
        {"path": "${INSTANCE_ENV_FILE}", "required": True},
    ]


def test_sandbox_runner_does_not_mount_mindroom_storage() -> None:
    """The static runner must not receive the MindRoom storage tree."""
    compose = yaml.safe_load(Path("local/instances/deploy/docker-compose.yml").read_text())
    runner = compose["services"]["sandbox-runner"]

    assert all("${DATA_DIR}/mindroom_data" not in volume for volume in runner["volumes"])
    assert "${DATA_DIR}/mindroom_data:/app/shared/.mindroom" not in runner["volumes"]
    assert "MINDROOM_SANDBOX_SHARED_STORAGE_ROOT=/app/shared" not in runner["environment"]


def test_compose_builds_from_repo_root() -> None:
    """Local compose builds must use the repo root so Dockerfile copies resolve."""
    compose = yaml.safe_load(Path("local/instances/deploy/docker-compose.yml").read_text())

    assert compose["services"]["mindroom"]["build"] == {
        "context": "../../..",
        "dockerfile": "local/instances/deploy/Dockerfile.mindroom",
    }
    assert compose["services"]["sandbox-runner"]["build"] == {
        "context": "../../..",
        "dockerfile": "local/instances/deploy/Dockerfile.mindroom",
    }


def test_matrix_compose_files_publish_localhost_ports() -> None:
    """Matrix overlays should publish the allocated host port described by the CLI and docs."""
    tuwunel_compose = yaml.safe_load(Path("local/instances/deploy/docker-compose.tuwunel.yml").read_text())
    synapse_compose = yaml.safe_load(Path("local/instances/deploy/docker-compose.synapse.yml").read_text())

    assert tuwunel_compose["services"]["tuwunel"]["ports"] == ["${MATRIX_PORT:-8448}:6167"]
    assert synapse_compose["services"]["synapse"]["ports"] == ["${MATRIX_PORT:-8448}:8008"]


def test_matrix_compose_files_expose_public_url_to_desktop_pairing() -> None:
    """Local hosted pairing commands should use the Matrix ingress URL."""
    tuwunel_compose = yaml.safe_load(Path("local/instances/deploy/docker-compose.tuwunel.yml").read_text())
    synapse_compose = yaml.safe_load(Path("local/instances/deploy/docker-compose.synapse.yml").read_text())

    assert tuwunel_compose["services"]["mindroom"]["environment"]["MINDROOM_DESKTOP_MATRIX_HOMESERVER"] == (
        "https://m-${INSTANCE_DOMAIN}"
    )
    assert (
        "MINDROOM_DESKTOP_MATRIX_HOMESERVER=https://m-${INSTANCE_DOMAIN}"
        in (synapse_compose["services"]["mindroom"]["environment"])
    )


def test_copy_config_to_instance_uses_repo_root_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Instance config seeding should read the repo-root config file."""
    instance = _instance("alpha", matrix_type=None, data_root=tmp_path)
    target_config = Path(instance.data_dir) / "config" / "config.yaml"
    target_config.parent.mkdir(parents=True)
    repo_root = tmp_path / "repo-root"
    repo_root.mkdir()
    (repo_root / "config.yaml").write_text("models: {}\nrouter:\n  model: default\n", encoding="utf-8")
    monkeypatch.setattr(deploy, "REPO_ROOT", repo_root)

    deploy._copy_config_to_instance(instance)

    assert target_config.read_text(encoding="utf-8") == "models: {}\nrouter:\n  model: default\n"


def test_setup_tuwunel_directory_preserves_matching_server_name(
    tmp_path: Path,
) -> None:
    """Matching MATRIX_SERVER_NAME values must not wipe an existing Tuwunel database."""
    instance = _instance("alpha", matrix_type=deploy.MatrixType.TUWUNEL, data_root=tmp_path)
    tuwunel_dir = Path(instance.data_dir) / "tuwunel"
    tuwunel_dir.mkdir(parents=True)
    marker = tuwunel_dir / "db.sqlite"
    marker.write_text("existing", encoding="utf-8")
    env_file = tmp_path / "alpha.env"
    env_file.write_text("MATRIX_SERVER_NAME=m-alpha.localhost\n", encoding="utf-8")

    deploy._setup_tuwunel_directory(instance, env_file)

    assert marker.exists()


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

    repair_prefix = f"docker run --rm --user 0:0 -v {data_dir}:/target {deploy.PERMISSION_REPAIR_IMAGE} sh -c "
    assert rmtree_calls == 2
    assert any(cmd.startswith(repair_prefix) for cmd in commands)
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


def test_telegram_bridge_compose_renders_configured_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge metadata must be the single image source for generated Compose."""
    monkeypatch.setitem(sys.modules, "matty", ModuleType("matty"))
    bridge_script = Path("local/instances/deploy/bridge.py")
    bridge_spec = importlib.util.spec_from_file_location("mindroom_bridge_manager", bridge_script)
    assert bridge_spec is not None
    assert bridge_spec.loader is not None
    bridge_manager = importlib.util.module_from_spec(bridge_spec)
    bridge_spec.loader.exec_module(bridge_manager)
    monkeypatch.setattr(bridge_manager, "BRIDGES_DIR", bridge_script.parent / "templates" / "bridges")
    bridge = bridge_manager.BridgeConfig(
        bridge_type=bridge_manager.BridgeType.TELEGRAM,
        instance_name="alpha",
        port=29317,
        data_dir=str(tmp_path),
    )
    telegram_template = bridge_manager.BRIDGE_TEMPLATES[bridge_manager.BridgeType.TELEGRAM]
    expected_image = "dock.mau.dev/mautrix/telegram:v0.15.3"

    assert telegram_template["image"] == expected_image

    compose_path = bridge_manager._create_bridge_docker_compose(bridge, telegram_template)
    compose = yaml.safe_load(compose_path.read_text())
    assert compose["services"]["telegram"]["image"] == expected_image

    compose_path = bridge_manager._create_bridge_docker_compose(
        bridge,
        {"image": "registry.example/telegram:compatible"},
    )

    overridden_compose = yaml.safe_load(compose_path.read_text())
    assert overridden_compose["services"]["telegram"]["image"] == "registry.example/telegram:compatible"


@pytest.fixture
def authelia_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[deploy.Instance, Path, list[str], Console]:
    """Keep launch inputs synthetic and capture all Docker commands."""
    instance = _instance("alpha", matrix_type=deploy.MatrixType.TUWUNEL, data_root=tmp_path)
    instance.auth_type = deploy.AuthType.AUTHELIA
    instance.status = deploy.InstanceStatus.RUNNING
    registry = deploy.Registry(instances={"alpha": instance})
    env_dir = tmp_path / "envs"
    env_dir.mkdir()
    (env_dir / "alpha.env").write_text("INSTANCE_NAME=alpha\n")
    users_file = Path(instance.data_dir) / "authelia" / "users_database.yml"
    users_file.parent.mkdir(parents=True)
    users_file.write_text((deploy.SCRIPT_DIR / "templates" / "authelia" / "users_database.yml").read_text())
    console = Console(record=True, width=240)
    commands: list[str] = []

    def _run(cmd: str, **_kwargs: object) -> SimpleNamespace:
        commands.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(deploy, "console", console)
    monkeypatch.setattr(deploy, "ENV_DIR", env_dir)
    monkeypatch.setattr(deploy, "REGISTRY_FILE", tmp_path / "instances.json")
    monkeypatch.setattr(deploy, "load_registry", lambda: registry)
    monkeypatch.setattr(deploy, "_create_instance_directories", lambda _instance: None)
    monkeypatch.setattr(deploy, "_setup_tuwunel_directory", lambda _instance, _env_file: None)
    monkeypatch.setattr(deploy.subprocess, "run", _run)
    return instance, users_file, commands, console


def _launch_authelia(command: str, *, only_matrix: bool = False, use_registry: bool = False) -> None:
    if command == "start":
        deploy.start(
            "alpha",
            only_matrix=only_matrix,
            use_registry=use_registry,
            registry_url=deploy.DEFAULT_REGISTRY,
            no_build=True,
        )
    else:
        deploy.restart(
            name=None if command == "restart_all" else "alpha",
            all_instances=command == "restart_all",
            only_matrix=only_matrix,
            use_registry=use_registry,
            registry_url=deploy.DEFAULT_REGISTRY,
            no_build=True,
        )


@pytest.mark.parametrize("command", ["start", "restart", "restart_all"])
@pytest.mark.parametrize("example_state", ["unchanged", "renamed", "enabled_by_default"])
def test_authelia_launch_rejects_enabled_public_credentials(
    authelia_launch: tuple[deploy.Instance, Path, list[str], Console],
    command: str,
    example_state: str,
) -> None:
    """Reject the public hash before launch effects even after account edits."""
    instance, users_file, commands, console = authelia_launch
    database = yaml.safe_load(users_file.read_text())
    if example_state == "renamed":
        database["users"]["operator"] = database["users"].pop("admin")
    elif example_state == "enabled_by_default":
        del database["users"]["admin"]["disabled"]
    users_file.write_text(yaml.safe_dump(database))
    before = users_file.read_bytes()
    env_before = (deploy.ENV_DIR / "alpha.env").read_bytes()

    with pytest.raises(deploy.typer.Exit) as exc:
        _launch_authelia(command, use_registry=True)

    assert exc.value.exit_code == 1
    assert commands == []
    assert users_file.read_bytes() == before
    assert (deploy.ENV_DIR / "alpha.env").read_bytes() == env_before
    assert instance.status == deploy.InstanceStatus.RUNNING
    text = normalize_console_output(console.export_text())
    assert str(users_file) in text
    assert "public example" in text.lower()
    assert "password hash" in text.lower()
    assert "local/instances/deploy/README.md" in text
    assert database["users"][next(iter(database["users"]))]["password"] not in text


@pytest.mark.parametrize("command", ["start", "restart", "restart_all"])
@pytest.mark.parametrize("example_state", ["replaced", "removed", "disabled"])
def test_authelia_launch_preserves_configured_users(
    authelia_launch: tuple[deploy.Instance, Path, list[str], Console],
    command: str,
    example_state: str,
) -> None:
    """Allow explicit setup without changing the operator's user database."""
    instance, users_file, commands, _console = authelia_launch
    database = yaml.safe_load(users_file.read_text())
    configured = {
        "disabled": False,
        "displayname": "Configured User",
        "password": (
            "$argon2id$v=19$m=65536,t=3,p=4$MDEyMzQ1Njc4OWFiY2RlZg$e7rBJC02ad64LZ63hb15DFQ2CrfzMkABVvrIFNI6aZ8"
        ),
        "email": "operator@example.com",
        "groups": ["users"],
    }
    if example_state == "replaced":
        database["users"]["admin"] = configured
    else:
        database["users"]["operator"] = configured
        if example_state == "removed":
            del database["users"]["admin"]
        else:
            database["users"]["admin"]["disabled"] = True
    users_file.write_text(yaml.safe_dump(database))
    before = users_file.read_bytes()

    _launch_authelia(command)

    assert users_file.read_bytes() == before
    assert instance.status == deploy.InstanceStatus.RUNNING
    assert any(" up -d" in cmd and cmd.endswith(" mindroom tuwunel wellknown authelia") for cmd in commands)


@pytest.mark.parametrize("command", ["start", "restart", "restart_all"])
def test_authelia_matrix_only_launch_does_not_require_users(
    authelia_launch: tuple[deploy.Instance, Path, list[str], Console],
    command: str,
) -> None:
    """Matrix-only launches must not inspect the unused authentication database."""
    instance, users_file, commands, _console = authelia_launch
    users_file.unlink()

    _launch_authelia(command, only_matrix=True)

    assert instance.status == deploy.InstanceStatus.PARTIAL
    assert not users_file.exists()
    assert any(" up -d" in cmd and cmd.endswith(" tuwunel wellknown") for cmd in commands)


@pytest.mark.parametrize("command", ["start", "restart", "restart_all"])
def test_launch_without_authelia_does_not_require_users(
    authelia_launch: tuple[deploy.Instance, Path, list[str], Console],
    command: str,
) -> None:
    """Instances without Authelia must not gain an account setup requirement."""
    instance, users_file, commands, _console = authelia_launch
    instance.auth_type = None
    users_file.unlink()

    _launch_authelia(command)

    assert instance.status == deploy.InstanceStatus.RUNNING
    assert not users_file.exists()
    assert any(" up -d" in cmd and cmd.endswith(" mindroom tuwunel wellknown") for cmd in commands)


@pytest.mark.parametrize("contents", [None, "users: [", "users: []", "users:\n  admin: null\n"])
def test_authelia_launch_rejects_unreadable_user_database(
    authelia_launch: tuple[deploy.Instance, Path, list[str], Console],
    contents: str | None,
) -> None:
    """An unreadable account database cannot bypass the launch check."""
    _instance, users_file, commands, console = authelia_launch
    if contents is None:
        users_file.unlink()
    else:
        users_file.write_text(contents)

    with pytest.raises(deploy.typer.Exit) as exc:
        _launch_authelia("start")

    assert exc.value.exit_code == 1
    assert commands == []
    assert str(users_file) in normalize_console_output(console.export_text())


@pytest.mark.parametrize("auth_enabled", [False, True])
def test_print_instance_info_authelia_setup_uses_actual_data_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_enabled: bool,
) -> None:
    """Only Authelia instances should show setup guidance for their own database."""
    instance = _instance("alpha", matrix_type=None, data_root=tmp_path / "custom-data")
    auth_type = deploy.AuthType.AUTHELIA if auth_enabled else None
    console = Console(record=True, width=240)
    monkeypatch.setattr(deploy, "console", console)

    deploy._print_instance_info(instance, None, auth_type)

    text = normalize_console_output(console.export_text())
    users_file = Path(instance.data_dir) / "authelia" / "users_database.yml"
    if auth_enabled:
        assert str(users_file) in text
        assert "Before starting:" in text
        assert "password hash and email" in text
        assert "remove/disable" in text
        assert "local/instances/deploy/README.md" in text
    else:
        assert "Authelia" not in text
        assert str(users_file) not in text
    assert "Default login:" not in text
