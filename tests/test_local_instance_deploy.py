"""Tests for the local multi-instance deploy helper."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml
from rich.console import Console

from tests.conftest import normalize_console_output

_REAL_SUBPROCESS_RUN = subprocess.run
_SANDBOX_SERVICES = {"sandbox-runner", "sandbox-relay"}
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
    (env_dir / "alpha.env").write_text(f"INSTANCE_NAME=alpha\nDATA_DIR={instance.data_dir}\n")
    users_file = Path(instance.data_dir) / "authelia" / "users_database.yml"
    users_file.parent.mkdir(parents=True)
    users_file.write_text((deploy.SCRIPT_DIR / "templates" / "authelia" / "users_database.yml").read_text())
    console = Console(record=True, width=240)
    commands: list[str] = []

    def _run(cmd: str, **_kwargs: object) -> SimpleNamespace:
        commands.append(cmd)
        if cmd.endswith(" config --format json --no-env-resolution"):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "services": {
                            "authelia": {
                                "volumes": [
                                    {
                                        "type": "bind",
                                        "source": str(users_file.parent).replace("$", "$$"),
                                        "target": "/config",
                                        "bind": {"create_host_path": True},
                                    },
                                ],
                            },
                        },
                    },
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(deploy, "console", console)
    monkeypatch.setattr(deploy, "ENV_DIR", env_dir)
    monkeypatch.setattr(deploy, "REGISTRY_FILE", tmp_path / "instances.json")
    monkeypatch.setattr(deploy, "load_registry", lambda: registry)
    monkeypatch.setattr(deploy, "REPO_ROOT", tmp_path / "source")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "home"))
    monkeypatch.setattr(deploy.subprocess, "run", _run)
    return instance, users_file, commands, console


def _require_docker_compose() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is required for interpolation coverage")
    version = _REAL_SUBPROCESS_RUN(
        ["docker", "compose", "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if version.returncode:
        pytest.skip("Docker Compose is required for interpolation coverage")


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


def _launched_services(commands: list[str]) -> list[str]:
    """Read the selected services from the single captured Compose launch."""
    launches = [cmd.split(" up -d", 1)[1].split() for cmd in commands if " up -d" in cmd]
    assert len(launches) == 1
    services = launches[0]
    if services and services[0] == "--force-recreate":
        services = services[1:]
    return services


@pytest.mark.parametrize("command", ["start", "restart", "restart_all"])
@pytest.mark.parametrize(
    "example_state",
    [
        "unchanged",
        "renamed",
        "enabled_by_default",
        "literal_block",
        "internal_crlf",
        "base64_tail_bits",
        "zero_padded_parameters",
        "reordered_duplicate_parameters",
        "version_segment_parameters",
        "missing_version",
        "zero_time",
        "missing_time",
        "ignored_key_length",
        "ldap_crypt",
        "ldap_argon2",
        "ldap_both",
        "binary",
        "binary_ldap",
    ],
)
def test_authelia_launch_rejects_enabled_public_credentials(
    authelia_launch: tuple[deploy.Instance, Path, list[str], Console],
    command: str,
    example_state: str,
) -> None:
    """Reject the public hash before launch effects even after account edits."""
    instance, users_file, commands, console = authelia_launch
    database = yaml.safe_load(users_file.read_text(encoding="utf-8"))
    public_hash = database["users"]["admin"]["password"]
    _, variant, version, parameters, salt, digest = public_hash.split("$")
    prefix = f"${variant}${version}${parameters}$"
    crlf_salt = "\r\n".join(salt)
    crlf_digest = "\r\n".join(digest)
    equivalent_hashes = {
        "internal_crlf": f"{prefix}{crlf_salt}${crlf_digest}\r\n",
        "base64_tail_bits": f"{prefix}{salt[:-1]}R${digest[:-1]}p",
        "zero_padded_parameters": f"${variant}$v=0019$m={'0' * 5000}1024,t=01,p=08${salt}${digest}",
        "reordered_duplicate_parameters": f"${variant}$v=19$m=1,t=3,p=1,p=8,m=1024,t=1${salt}${digest}",
        "version_segment_parameters": f"${variant}$v=19,m=1024,t=1,p=8$m=1,t=3,p=1${salt}${digest}",
        "missing_version": f"${variant}$m=1024$t=1,p=8${salt}${digest}",
        "zero_time": f"${variant}$v=19$m=1024,t=0,p=8${salt}${digest}",
        "missing_time": f"${variant}$v=19$m=1024,p=8${salt}${digest}",
        "ignored_key_length": f"${variant}$v=19$m=1024,t=1,p=8,k=4294967295${salt}${digest}",
        "ldap_crypt": "{CRYPT}" + public_hash,
        "ldap_argon2": "{ARGON2}" + public_hash,
        "ldap_both": "{CRYPT}{ARGON2}" + public_hash,
    }
    if example_state == "renamed":
        database["users"]["operator"] = database["users"].pop("admin")
    elif example_state == "enabled_by_default":
        del database["users"]["admin"]["disabled"]
    elif example_state in {"binary", "binary_ldap"}:
        encoded_hash = "{CRYPT}" + public_hash + "\n" if example_state == "binary_ldap" else public_hash
        database["users"]["admin"]["password"] = encoded_hash.encode("utf-8")
    elif example_state in equivalent_hashes:
        database["users"]["admin"]["password"] = equivalent_hashes[example_state]
    if example_state == "literal_block":
        users_file.write_text(f"users:\n  admin:\n    password: |\n      {public_hash}\n", encoding="utf-8")
    else:
        users_file.write_text(yaml.safe_dump(database), encoding="utf-8")
    before = users_file.read_bytes()
    env_before = (deploy.ENV_DIR / "alpha.env").read_bytes()

    with pytest.raises(deploy.typer.Exit) as exc:
        _launch_authelia(command, use_registry=True)

    assert exc.value.exit_code == 1
    assert commands
    assert all(cmd.endswith(" config --format json --no-env-resolution") for cmd in commands)
    assert users_file.read_bytes() == before
    assert (deploy.ENV_DIR / "alpha.env").read_bytes() == env_before
    assert instance.status == deploy.InstanceStatus.RUNNING
    text = normalize_console_output(console.export_text())
    assert str(users_file) in text
    assert "public example" in text.lower()
    assert "password hash" in text.lower()
    assert "local/instances/deploy/README.md" in text
    assert str(database["users"][next(iter(database["users"]))]["password"]) not in text
    assert public_hash not in text


@pytest.mark.parametrize("command", ["start", "restart", "restart_all"])
def test_authelia_launch_rejects_colliding_yaml_usernames(
    authelia_launch: tuple[deploy.Instance, Path, list[str], Console],
    command: str,
) -> None:
    """Ambiguous YAML account keys must not hide an enabled public password."""
    instance, users_file, commands, console = authelia_launch
    public_hash = yaml.safe_load(users_file.read_text())["users"]["admin"]["password"]
    source = (
        f'users:\n  on:\n    disabled: false\n    password: "{public_hash}"\n'
        f'  yes:\n    disabled: true\n    password: "{public_hash}"\n'
    )
    users_file.write_text(source)
    env_before = (deploy.ENV_DIR / "alpha.env").read_bytes()

    with pytest.raises(deploy.typer.Exit) as exc:
        _launch_authelia(command, use_registry=True)

    assert exc.value.exit_code == 1
    assert commands
    assert all(cmd.endswith(" config --format json --no-env-resolution") for cmd in commands)
    assert users_file.read_text() == source
    assert (deploy.ENV_DIR / "alpha.env").read_bytes() == env_before
    assert instance.status == deploy.InstanceStatus.RUNNING
    assert "Invalid Authelia users database" in normalize_console_output(console.export_text())
    assert public_hash not in console.export_text()


@pytest.mark.parametrize("command", ["start", "restart", "restart_all"])
@pytest.mark.parametrize("field", ["password", "disabled"])
def test_authelia_launch_rejects_binary_account_field_keys(
    authelia_launch: tuple[deploy.Instance, Path, list[str], Console],
    command: str,
    field: str,
) -> None:
    """Go decodes binary struct keys as strings; ambiguous Python keys must fail closed."""
    _instance, users_file, commands, console = authelia_launch
    database = yaml.safe_load(users_file.read_text(encoding="utf-8"))
    user = database["users"]["admin"]
    user[field.encode("utf-8")] = user.pop(field)
    users_file.write_text(yaml.safe_dump(database), encoding="utf-8")
    before = users_file.read_bytes()

    with pytest.raises(deploy.typer.Exit) as exc:
        _launch_authelia(command)

    assert exc.value.exit_code == 1
    assert all(cmd.endswith(" config --format json --no-env-resolution") for cmd in commands)
    assert users_file.read_bytes() == before
    assert "Invalid Authelia users database" in normalize_console_output(console.export_text())


@pytest.mark.parametrize("command", ["start", "restart", "restart_all"])
@pytest.mark.parametrize("example_state", ["replaced", "removed", "disabled", "disabled_encoded"])
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
            if example_state == "disabled_encoded":
                database["users"]["admin"]["password"] = "{CRYPT}" + database["users"]["admin"]["password"] + "\n"
    users_file.write_text(yaml.safe_dump(database))
    before = users_file.read_bytes()

    _launch_authelia(command)

    assert users_file.read_bytes() == before
    assert instance.status == deploy.InstanceStatus.RUNNING
    services = _launched_services(commands)
    assert services.count("sandbox-runner") <= 1
    assert [service for service in services if service not in _SANDBOX_SERVICES] == [
        "mindroom",
        "tuwunel",
        "wellknown",
        "authelia",
    ]


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
    assert _launched_services(commands) == ["tuwunel", "wellknown"]
    assert not any(" config --format json" in cmd for cmd in commands)


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
    services = _launched_services(commands)
    assert services.count("sandbox-runner") <= 1
    assert [service for service in services if service not in _SANDBOX_SERVICES] == ["mindroom", "tuwunel", "wellknown"]
    assert not any(" config --format json" in cmd for cmd in commands)


@pytest.mark.parametrize("command", ["start", "restart", "restart_all"])
@pytest.mark.parametrize(
    "contents",
    [None, "users: [", "users: []", "users:\n  admin: null\n", b"users:\n  private-marker: \xff\n"],
)
def test_authelia_launch_rejects_unreadable_user_database(
    authelia_launch: tuple[deploy.Instance, Path, list[str], Console],
    command: str,
    contents: str | bytes | None,
) -> None:
    """An unreadable account database cannot bypass the launch check."""
    _instance, users_file, commands, console = authelia_launch
    if contents is None:
        users_file.unlink()
    elif isinstance(contents, bytes):
        users_file.write_bytes(contents)
    else:
        users_file.write_text(contents, encoding="utf-8")

    with pytest.raises(deploy.typer.Exit) as exc:
        _launch_authelia(command)

    assert exc.value.exit_code == 1
    assert commands
    assert all(cmd.endswith(" config --format json --no-env-resolution") for cmd in commands)
    text = normalize_console_output(console.export_text())
    assert str(users_file) in text
    assert "local/instances/deploy/README.md" in text
    assert "private-marker" not in text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (1, "argon2i"),
        (3, "m=1025,t=1,p=8"),
        (3, "m=1024,t=2,p=8"),
        (3, "m=1024,t=1,p=9"),
        (4, "MDEyMzQ1Njc4OWFiY2RlZg"),
        (5, "e7rBJC02ad64LZ63hb15DFQ2CrfzMkABVvrIFNI6aZ8"),
    ],
    ids=["variant", "memory", "time", "parallelism", "salt", "digest"],
)
def test_authelia_launch_allows_distinct_argon2_inputs(
    authelia_launch: tuple[deploy.Instance, Path, list[str], Console],
    field: int,
    value: str,
) -> None:
    """Public-hash detection must compare all effective Argon2 inputs."""
    _instance, users_file, commands, _console = authelia_launch
    database = yaml.safe_load(users_file.read_text(encoding="utf-8"))
    parts = database["users"]["admin"]["password"].split("$")
    parts[field] = value
    database["users"]["admin"]["password"] = "$".join(parts)
    users_file.write_text(yaml.safe_dump(database), encoding="utf-8")
    before = users_file.read_bytes()

    _launch_authelia("start")

    assert "authelia" in _launched_services(commands)
    assert users_file.read_bytes() == before


@pytest.mark.skipif(sys.platform == "win32", reason="Requires a POSIX ASCII C locale")
def test_authelia_account_check_reads_utf8_under_ascii_locale(
    authelia_launch: tuple[deploy.Instance, Path, list[str], Console],
    tmp_path: Path,
) -> None:
    """UTF-8 account files and the shipped template must not depend on locale."""
    _instance, users_file, _commands, _console = authelia_launch
    database = yaml.safe_load(users_file.read_text(encoding="utf-8"))
    database["users"]["admin"].update(disabled=True, displayname="Zoë")
    users_file.write_text(yaml.safe_dump(database, allow_unicode=True), encoding="utf-8")
    template = tmp_path / "templates" / "authelia" / "users_database.yml"
    template.parent.mkdir(parents=True)
    template.write_text(yaml.safe_dump(database, allow_unicode=True), encoding="utf-8")
    before = users_file.read_bytes()
    code = """
import importlib.util
import locale
from pathlib import Path
import sys

assert sys.flags.utf8_mode == 0
assert locale.getencoding().lower() in {"ascii", "ansi_x3.4-1968", "us-ascii"}
spec = importlib.util.spec_from_file_location("deploy_encoding_test", sys.argv[1])
deploy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = deploy
spec.loader.exec_module(deploy)
users_file = Path(sys.argv[2])
deploy.SCRIPT_DIR = Path(sys.argv[3])
deploy._resolve_authelia_users_file = lambda _instance: users_file
instance = deploy.Instance(name="alpha", mindroom_port=8765, data_dir=str(users_file.parent), domain="localhost")
deploy._require_authelia_account_setup(instance)
"""
    result = _REAL_SUBPROCESS_RUN(
        [sys.executable, "-X", "utf8=0", "-c", code, str(_SCRIPT_PATH.resolve()), str(users_file), str(tmp_path)],
        env={**os.environ, "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0"},
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert users_file.read_bytes() == before


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


@pytest.mark.parametrize("account_state", ["public_example", "missing", "malformed"])
def test_rejected_authelia_start_preserves_existing_instance_data(
    authelia_launch: tuple[deploy.Instance, Path, list[str], Console],
    account_state: str,
) -> None:
    """Account rejection must precede real setup writes and Matrix database removal."""
    instance, users_file, commands, _console = authelia_launch
    data_dir = Path(instance.data_dir)
    matrix_dir = data_dir / "tuwunel"
    matrix_dir.mkdir()
    (matrix_dir / "database-marker").write_bytes(b"existing Matrix data")
    env_file = deploy.ENV_DIR / "alpha.env"
    env_file.write_text("INSTANCE_NAME=alpha\nMATRIX_SERVER_NAME=m-previous.localhost\n")
    env_before = env_file.read_bytes()

    # Let the real setup helpers discover only synthetic config and credentials.
    deploy.REPO_ROOT.mkdir()
    source_config = deploy.REPO_ROOT / "config.yaml"
    source_config.write_text("agents: {}\n")
    source_credentials = Path.home() / ".mindroom" / "credentials"
    source_credentials.mkdir(parents=True)
    credential_file = source_credentials / "test-provider.json"
    credential_file.write_text('{"api_key": "synthetic-test-value"}\n')
    if account_state == "missing":
        users_file.unlink()
    elif account_state == "malformed":
        users_file.write_text("users: [")
    before = {path.relative_to(data_dir): path.read_bytes() if path.is_file() else None for path in data_dir.rglob("*")}

    with pytest.raises(deploy.typer.Exit) as exc:
        _launch_authelia("start", use_registry=True)

    assert exc.value.exit_code == 1
    assert {
        path.relative_to(data_dir): path.read_bytes() if path.is_file() else None for path in data_dir.rglob("*")
    } == before
    assert env_file.read_bytes() == env_before
    assert source_config.read_text() == "agents: {}\n"
    assert credential_file.read_text() == '{"api_key": "synthetic-test-value"}\n'
    assert not deploy.REGISTRY_FILE.exists()
    assert instance.status == deploy.InstanceStatus.RUNNING
    assert commands
    assert all(cmd.endswith(" config --format json --no-env-resolution") for cmd in commands)


def test_matrix_only_authelia_start_keeps_real_setup_and_existing_data(
    authelia_launch: tuple[deploy.Instance, Path, list[str], Console],
) -> None:
    """The account-check exemption must still prepare storage without clearing matching Matrix data."""
    instance, users_file, commands, _console = authelia_launch
    users_file.unlink()
    data_dir = Path(instance.data_dir)
    matrix_dir = data_dir / "tuwunel"
    matrix_dir.mkdir()
    marker = matrix_dir / "database-marker"
    marker.write_bytes(b"existing Matrix data")
    (deploy.ENV_DIR / "alpha.env").write_text("INSTANCE_NAME=alpha\nMATRIX_SERVER_NAME=m-alpha.localhost\n")

    _launch_authelia("start", only_matrix=True)

    assert marker.read_bytes() == b"existing Matrix data"
    assert (data_dir / "config").is_dir()
    assert (data_dir / "mindroom_data" / "tracking").is_dir()
    assert not users_file.exists()
    assert instance.status == deploy.InstanceStatus.PARTIAL
    assert any(" up -d" in command for command in commands)


@pytest.mark.parametrize("command", ["start", "restart", "restart_all"])
@pytest.mark.parametrize("case", ["env_public", "shell_public", "shell_configured", "env_configured"])
def test_authelia_launch_checks_compose_selected_database(  # noqa: PLR0915
    authelia_launch: tuple[deploy.Instance, Path, list[str], Console],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    case: str,
) -> None:
    """Validate the mounted database with real Compose interpolation before launch effects."""
    _require_docker_compose()

    instance, registry_users, commands, console = authelia_launch
    public_text = registry_users.read_text()
    configured = yaml.safe_load(public_text)
    configured["users"]["admin"]["disabled"] = True
    configured_text = yaml.safe_dump(configured)
    registry_users.write_text(configured_text)
    public_root = tmp_path / "public $literal $$double"
    configured_root = tmp_path / "configured $literal $$double"
    for root, text in [(public_root, public_text), (configured_root, configured_text)]:
        users_file = root / "authelia" / "users_database.yml"
        users_file.parent.mkdir(parents=True)
        users_file.write_text(text)

    env_root = public_root if case in {"env_public", "shell_configured"} else configured_root
    (deploy.ENV_DIR / "alpha.env").write_text(
        f"INSTANCE_NAME=alpha\nINSTANCE_DOMAIN=alpha.localhost\n"
        f"DATA_DIR='{env_root}'\nMATRIX_SERVER_NAME=m-previous.localhost\n",
    )
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("INSTANCE_ENV_FILE", raising=False)
    if case == "shell_public":
        monkeypatch.setenv("DATA_DIR", str(public_root))
    elif case == "shell_configured":
        monkeypatch.setenv("DATA_DIR", str(configured_root))
    elif case == "env_configured":
        registry_users.write_text(public_text)
    # Older Compose validates socket path length even for daemon-free config commands.
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent-mindroom-test.sock")

    # Rejected starts must not reach real setup or clear the old Matrix database.
    matrix_dir = Path(instance.data_dir) / "tuwunel"
    matrix_dir.mkdir()
    (matrix_dir / "database-marker").write_text("existing Matrix data\n")
    deploy.REPO_ROOT.mkdir()
    (deploy.REPO_ROOT / "config.yaml").write_text("agents: {}\n")
    source_credentials = Path.home() / ".mindroom" / "credentials"
    source_credentials.mkdir(parents=True)
    (source_credentials / "synthetic.json").write_text('{"api_key": "synthetic-test-value"}\n')
    fake_run = deploy.subprocess.run

    def _run(cmd: str, **kwargs: object) -> subprocess.CompletedProcess[str] | SimpleNamespace:
        if cmd.endswith(" config --format json --no-env-resolution"):
            commands.append(cmd)
            result = _REAL_SUBPROCESS_RUN(cmd, **kwargs)
            assert result.returncode == 0, result.stderr
            return result
        return fake_run(cmd, **kwargs)

    monkeypatch.setattr(deploy.subprocess, "run", _run)
    before = {path.relative_to(tmp_path): path.read_bytes() if path.is_file() else None for path in tmp_path.rglob("*")}
    if case in {"env_public", "shell_public"}:
        with pytest.raises(deploy.typer.Exit) as exc:
            _launch_authelia(command, use_registry=True)
        assert exc.value.exit_code == 1
        assert commands
        assert all(cmd.endswith(" config --format json --no-env-resolution") for cmd in commands)
        assert {
            path.relative_to(tmp_path): path.read_bytes() if path.is_file() else None for path in tmp_path.rglob("*")
        } == before
        assert instance.status == deploy.InstanceStatus.RUNNING
        text = normalize_console_output(console.export_text())
        assert str(public_root / "authelia" / "users_database.yml") in text
        assert "public example" in text
    else:
        _launch_authelia(command)
        assert "authelia" in _launched_services(commands)
    assert (public_root / "authelia" / "users_database.yml").read_text() == public_text
    assert (configured_root / "authelia" / "users_database.yml").read_text() == configured_text


@pytest.mark.parametrize("failure", ["command", "json", "missing_mount", "named_volume", "relative_source"])
def test_authelia_launch_rejects_unresolved_compose_database(
    authelia_launch: tuple[deploy.Instance, Path, list[str], Console],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """An unresolved mount must not fall back to a configured registry database."""
    instance, users_file, commands, console = authelia_launch
    configured = yaml.safe_load(users_file.read_text())
    configured["users"]["admin"]["disabled"] = True
    users_file.write_text(yaml.safe_dump(configured))
    mount = {"type": "bind", "source": str(users_file.parent), "target": "/config"}
    if failure == "missing_mount":
        mount["target"] = "/other"
    elif failure == "named_volume":
        mount["type"] = "volume"
    elif failure == "relative_source":
        mount["source"] = "relative/authelia"
    stdout = json.dumps({"services": {"authelia": {"volumes": [mount]}}})
    if failure == "json":
        stdout = "synthetic-credential-in-invalid-output"

    def _run(cmd: str, **_kwargs: object) -> SimpleNamespace:
        commands.append(cmd)
        return SimpleNamespace(
            returncode=1 if failure == "command" else 0,
            stdout=stdout,
            stderr="synthetic-credential-in-error-output",
        )

    monkeypatch.setattr(deploy.subprocess, "run", _run)
    before = {path.relative_to(tmp_path): path.read_bytes() if path.is_file() else None for path in tmp_path.rglob("*")}
    with pytest.raises(deploy.typer.Exit) as exc:
        _launch_authelia("start", use_registry=True)
    assert exc.value.exit_code == 1
    assert commands
    assert all(cmd.endswith(" config --format json --no-env-resolution") for cmd in commands)
    assert {
        path.relative_to(tmp_path): path.read_bytes() if path.is_file() else None for path in tmp_path.rglob("*")
    } == before
    assert instance.status == deploy.InstanceStatus.RUNNING
    assert "synthetic-credential" not in console.export_text()


@pytest.mark.parametrize("matrix_type", [None, deploy.MatrixType.TUWUNEL, deploy.MatrixType.SYNAPSE])
@pytest.mark.parametrize("auth_type", [None, deploy.AuthType.AUTHELIA])
def test_full_stack_starts_its_configured_sandbox_runner(
    tmp_path: Path,
    matrix_type: deploy.MatrixType | None,
    auth_type: deploy.AuthType | None,
) -> None:
    """Fresh full stacks select the worker endpoint configured for their execution tools."""
    instance = _instance("alpha", matrix_type=matrix_type, data_root=tmp_path)
    instance.auth_type = auth_type
    selected = set(deploy._get_services_to_start(instance).split())
    compose = yaml.safe_load(Path("local/instances/deploy/docker-compose.yml").read_text())
    proxy_url = next(
        value
        for value in compose["services"]["mindroom"]["environment"]
        if value.startswith("MINDROOM_SANDBOX_PROXY_URL=")
    )
    assert proxy_url == "MINDROOM_SANDBOX_PROXY_URL=http://sandbox-relay:8766"
    assert compose["services"].keys() >= _SANDBOX_SERVICES
    assert {"mindroom", *_SANDBOX_SERVICES} <= selected


@pytest.mark.parametrize("matrix_type", [deploy.MatrixType.TUWUNEL, deploy.MatrixType.SYNAPSE])
def test_matrix_only_start_excludes_runtime_and_sandbox(
    tmp_path: Path,
    matrix_type: deploy.MatrixType,
) -> None:
    """Starting only the homeserver must not start either execution runtime."""
    instance = _instance("alpha", matrix_type=matrix_type, data_root=tmp_path)
    instance.auth_type = deploy.AuthType.AUTHELIA
    selected = set(deploy._get_services_to_start(instance, only_matrix=True).split())
    assert matrix_type.value in selected
    assert selected.isdisjoint({"mindroom", *_SANDBOX_SERVICES, "authelia"})


def test_sandbox_runner_waits_for_workspace_ownership() -> None:
    """The configured non-root runner must wait for isolated volume initialization."""
    compose = yaml.safe_load(Path("local/instances/deploy/docker-compose.yml").read_text())
    services = compose["services"]
    runner = services["sandbox-runner"]

    assert runner.get("depends_on", {}).get("sandbox-workspace-init") == {
        "condition": "service_completed_successfully",
    }
    initializer = services["sandbox-workspace-init"]
    assert initializer["user"] == "0:0"
    assert initializer["command"] == ["chown", "-R", "${UID:-1000}:${GID:-1000}", "/app/workspace"]
    assert runner["user"] == "${UID:-1000}:${GID:-1000}"
    assert initializer["volumes"] == runner["volumes"] == ["sandbox-workspace:/app/workspace"]
    assert initializer["image"] == runner["image"]
    assert initializer["build"] == runner["build"]
    assert initializer["restart"] == "no"
    assert initializer["network_mode"] == "none"
    assert "env_file" not in initializer


@pytest.mark.parametrize("force_recreate", [False, True], ids=["start", "restart"])
def test_failed_sandbox_initialization_preserves_instance_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    force_recreate: bool,
) -> None:
    """A failed Compose initialization must not publish the instance as running."""
    env_dir = tmp_path / "envs"
    env_dir.mkdir()
    (env_dir / "alpha.env").write_text("INSTANCE_NAME=alpha\n")
    registry_file = tmp_path / "instances.json"
    monkeypatch.setattr(deploy, "ENV_DIR", env_dir)
    monkeypatch.setattr(deploy, "REGISTRY_FILE", registry_file)
    instance = _instance("alpha", matrix_type=None, data_root=tmp_path)
    instance.status = deploy.InstanceStatus.STOPPED
    registry = deploy.Registry(instances={"alpha": instance})
    deploy.save_registry(registry)
    original_registry = registry_file.read_bytes()
    commands: list[str] = []

    def _run(cmd: str, **_kwargs: object) -> SimpleNamespace:
        commands.append(cmd)
        if cmd == "docker network inspect mynetwork" or cmd.startswith("docker ps --filter network=mynetwork "):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        assert " up -d" in cmd
        assert cmd.endswith(" mindroom sandbox-runner sandbox-relay")
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr='service "sandbox-workspace-init" did not complete successfully: exit 1',
        )

    monkeypatch.setattr(deploy.subprocess, "run", _run)

    with pytest.raises(deploy.typer.Exit) as exc_info:
        deploy._bring_up_instance(
            "alpha",
            instance,
            registry,
            only_matrix=False,
            use_registry=False,
            registry_url=deploy.DEFAULT_REGISTRY,
            no_build=True,
            status_message="Starting instance...",
            success_verb="started",
            force_recreate=force_recreate,
        )

    assert exc_info.value.exit_code == 1
    assert len(commands) == 3
    assert (" --force-recreate " in commands[-1]) is force_recreate
    assert instance.status == deploy.InstanceStatus.STOPPED
    assert registry_file.read_bytes() == original_registry


_COMPOSE_FILES = sorted(Path("local/instances/deploy").glob("docker-compose*.yml"))


def _write_older_env_file(instance: deploy.Instance) -> Path:
    """Write the env file a create from before generated per-instance secrets produced."""
    env_file = deploy.ENV_DIR / f"{instance.name}.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"INSTANCE_ENV_FILE={env_file}",
        f"INSTANCE_NAME={instance.name}",
        f"MINDROOM_PORT={instance.mindroom_port}",
        f"DATA_DIR={instance.data_dir}",
        f"INSTANCE_DOMAIN={instance.domain}",
    ]
    if instance.matrix_type is not None:
        lines += [f"MATRIX_PORT={instance.matrix_port}", f"MATRIX_SERVER_NAME=m-{instance.domain}"]
    if instance.matrix_type == deploy.MatrixType.SYNAPSE:
        lines.append("POSTGRES_PASSWORD=synapse_password")
    env_file.write_text("\n".join(lines) + "\n")
    return env_file


@pytest.mark.parametrize(
    ("matrix_type", "auth_type"),
    [
        (None, None),
        (deploy.MatrixType.TUWUNEL, None),
        (deploy.MatrixType.SYNAPSE, None),
        (None, deploy.AuthType.AUTHELIA),
        (deploy.MatrixType.SYNAPSE, deploy.AuthType.AUTHELIA),
    ],
)
@pytest.mark.parametrize("env_generation", ["current", "older"])
def test_rendered_compose_isolates_sandbox_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    matrix_type: deploy.MatrixType | None,
    auth_type: deploy.AuthType | None,
    env_generation: str,
) -> None:
    """Every Compose variant renders for current and older env files and reaches the runner only via the relay."""
    _require_docker_compose()
    monkeypatch.setattr(deploy, "ENV_DIR", tmp_path / "envs")
    for name in ["DATA_DIR", "INSTANCE_ENV_FILE", *deploy.RUNTIME_SECRET_NAMES, *deploy.SYNAPSE_SECRET_NAMES]:
        monkeypatch.delenv(name, raising=False)
    # Older Compose validates socket path length even for daemon-free config commands.
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent-mindroom-test.sock")
    instance = _instance("alpha", matrix_type=matrix_type, data_root=tmp_path)
    instance.auth_type = auth_type
    if env_generation == "current":
        deploy._create_environment_file(instance, "alpha", matrix_type)
        env_file = deploy.ENV_DIR / "alpha.env"
    else:
        env_file = _write_older_env_file(instance)
    env_values = deploy._read_env_values(env_file)

    cmd = f"{deploy._get_docker_compose_files(instance)} -p alpha config --format json --no-env-resolution"
    result = _REAL_SUBPROCESS_RUN(cmd, shell=True, capture_output=True, text=True, check=False)  # noqa: S604

    assert result.returncode == 0, result.stderr
    model = json.loads(result.stdout)
    services = model["services"]
    assert set(services["sandbox-runner"]["networks"]) == {"sandbox-network"}
    assert set(services["sandbox-relay"]["networks"]) == {"mindroom-network", "sandbox-network"}
    assert services["sandbox-relay"]["command"][-2:] == ["sandbox-runner", "8766"]
    assert services["sandbox-relay"]["sysctls"] == {"net.ipv4.ip_forward": "0"}
    for name, service in services.items():
        if name not in _SANDBOX_SERVICES:
            assert "sandbox-network" not in service.get("networks", {}), name
    # Compose reuses an unchanged network, so existing instances and their attached bridges keep it.
    assert model["networks"]["mindroom-network"] == {"name": "alpha_mindroom-network", "driver": "bridge", "ipam": {}}
    mindroom_env = services["mindroom"]["environment"]
    assert mindroom_env["MINDROOM_SANDBOX_PROXY_URL"] == "http://sandbox-relay:8766"
    proxy_token = env_values.get("MINDROOM_SANDBOX_PROXY_TOKEN", "")
    assert mindroom_env["MINDROOM_SANDBOX_PROXY_TOKEN"] == proxy_token
    assert services["sandbox-runner"]["environment"]["MINDROOM_SANDBOX_PROXY_TOKEN"] == proxy_token
    assert mindroom_env["MINDROOM_API_KEY"] == env_values.get("MINDROOM_API_KEY", "")
    assert (len(proxy_token) == 64) is (env_generation == "current")
    if matrix_type == deploy.MatrixType.SYNAPSE:
        redis_password = env_values.get("REDIS_PASSWORD", "")
        assert (len(redis_password) == 64) is (env_generation == "current")
        assert services["redis"]["command"] == ["redis-server", "--requirepass", redis_password]
        assert services["redis"]["environment"] == {"REDIS_PASSWORD": redis_password}
        assert services["postgres"]["environment"]["POSTGRES_PASSWORD"] == env_values["POSTGRES_PASSWORD"]


def test_deploy_files_ship_no_shared_secret_defaults() -> None:
    """Instances must never fall back to credentials that every deployment shares."""
    for path in [*_COMPOSE_FILES, Path("local/instances/deploy/templates/synapse/homeserver.yaml.j2")]:
        text = path.read_text()
        assert "synapse_password" not in text, path
        assert "sandbox-secret" not in text, path


def test_create_generates_unique_synapse_instance_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each instance gets its own dashboard key and datastore passwords, rendered into Synapse."""
    monkeypatch.setattr(deploy, "ENV_DIR", tmp_path / "envs")
    monkeypatch.setattr(deploy, "ENV_TEMPLATE", tmp_path / "missing.env.template")
    secrets_by_instance = {}
    for name in ["alpha", "beta"]:
        instance = _instance(name, matrix_type=deploy.MatrixType.SYNAPSE, data_root=tmp_path)
        deploy._create_environment_file(instance, name, deploy.MatrixType.SYNAPSE)
        deploy._setup_synapse_config(instance)
        values = deploy._read_env_values(tmp_path / "envs" / f"{name}.env")
        generated = {key: values[key] for key in (*deploy.RUNTIME_SECRET_NAMES, *deploy.SYNAPSE_SECRET_NAMES)}
        assert all(len(value) == 64 and set(value) <= set("0123456789abcdef") for value in generated.values())
        assert len(set(generated.values())) == len(generated)
        homeserver = yaml.safe_load((Path(instance.data_dir) / "synapse" / "homeserver.yaml").read_text())
        assert homeserver["database"]["args"]["password"] == generated["POSTGRES_PASSWORD"]
        assert homeserver["redis"]["password"] == generated["REDIS_PASSWORD"]
        secrets_by_instance[name] = generated

    assert set(secrets_by_instance["alpha"].values()).isdisjoint(secrets_by_instance["beta"].values())


def test_ensure_env_secrets_fills_only_empty_values(tmp_path: Path) -> None:
    """Existing secrets are preserved while empty template placeholders are replaced once."""
    env_file = tmp_path / "alpha.env"
    env_file.write_text(
        "MINDROOM_API_KEY=\nexport MINDROOM_SANDBOX_PROXY_TOKEN='exported-token'\nPOSTGRES_PASSWORD=existing",
    )
    names = ("MINDROOM_API_KEY", "MINDROOM_SANDBOX_PROXY_TOKEN", "POSTGRES_PASSWORD")

    assert deploy._ensure_env_secrets(env_file, names) == ["MINDROOM_API_KEY"]
    written = env_file.read_text()
    values = deploy._read_env_values(env_file)

    assert written.startswith(
        "MINDROOM_API_KEY=\nexport MINDROOM_SANDBOX_PROXY_TOKEN='exported-token'\nPOSTGRES_PASSWORD=existing\n",
    )
    assert len(values["MINDROOM_API_KEY"]) == 64
    assert values["MINDROOM_SANDBOX_PROXY_TOKEN"] == "exported-token"  # noqa: S105
    assert values["POSTGRES_PASSWORD"] == "existing"  # noqa: S105
    assert deploy._ensure_env_secrets(env_file, names) == []
    assert env_file.read_text() == written


@pytest.mark.parametrize("command", ["start", "restart", "restart_all"])
def test_launch_upgrades_older_synapse_env_without_changing_datastore_passwords(
    authelia_launch: tuple[deploy.Instance, Path, list[str], Console],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """Older instances gain runtime secrets before Compose runs, while their datastores keep their credentials."""
    instance, _users_file, commands, console = authelia_launch
    instance.auth_type = None
    instance.matrix_type = deploy.MatrixType.SYNAPSE
    homeserver = Path(instance.data_dir) / "synapse" / "homeserver.yaml"
    homeserver.parent.mkdir(parents=True)
    homeserver.write_text("database:\n  args:\n    password: synapse_password\n")
    env_file = _write_older_env_file(instance)
    older_env = env_file.read_text()
    env_at_launch: dict[str, str] = {}
    fake_run = deploy.subprocess.run

    def _run(cmd: str, **kwargs: object) -> SimpleNamespace:
        if " up -d" in cmd:
            env_at_launch.update(deploy._read_env_values(env_file))
        return fake_run(cmd, **kwargs)

    monkeypatch.setattr(deploy.subprocess, "run", _run)

    _launch_authelia(command)

    assert env_file.read_text().startswith(older_env)
    assert all(len(env_at_launch[name]) == 64 for name in deploy.RUNTIME_SECRET_NAMES)
    assert env_at_launch["POSTGRES_PASSWORD"] == "synapse_password"  # noqa: S105
    assert "REDIS_PASSWORD" not in env_at_launch
    assert homeserver.read_text() == "database:\n  args:\n    password: synapse_password\n"
    assert set(_launched_services(commands)) >= _SANDBOX_SERVICES
    text = normalize_console_output(console.export_text())
    assert "Added MINDROOM_API_KEY, MINDROOM_SANDBOX_PROXY_TOKEN to" in text
    assert "Dashboard API key: MINDROOM_API_KEY in envs/alpha.env" in text
