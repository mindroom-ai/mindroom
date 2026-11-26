"""Docker tool configuration and native implementation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolExecutionTarget,
    ToolFileAccess,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import require_non_empty

logger = get_logger(__name__)

_DockerInspectAction = Literal[
    "list_containers",
    "inspect_container",
    "get_container_logs",
    "list_images",
    "inspect_image",
    "list_volumes",
    "inspect_volume",
    "list_networks",
    "inspect_network",
]
_DockerContainerAction = Literal["run", "exec", "start", "stop", "remove"]
_DockerResourceAction = Literal[
    "pull_image",
    "build_image",
    "tag_image",
    "remove_image",
    "create_volume",
    "remove_volume",
    "create_network",
    "remove_network",
    "connect_network",
    "disconnect_network",
]


def _isolate_docker_client(client: Any) -> None:  # noqa: ANN401 - optional docker.DockerClient
    """Pin an isolated empty auth and proxy config on ``client.api`` so host DOCKER_CONFIG never leaks."""
    from docker.auth import AuthConfig
    from docker.utils.proxy import ProxyConfig

    class _IsolatedEmptyAuthConfig(AuthConfig):
        @property
        def is_empty(self) -> bool:
            return False

    api = getattr(client, "api", None)
    if api is not None:
        api._general_configs = {}
        api._proxy_configs = ProxyConfig.from_dict({})
        api._auth_configs = _IsolatedEmptyAuthConfig({"auths": {}})


def _find_docker_socket() -> tuple[str, bool]:
    """Return the first existing local Docker socket path and whether it is a Windows named pipe."""
    home = Path.home()
    unix_candidates = (
        Path("/var/run/docker.sock"),
        home / ".docker" / "run" / "docker.sock",
        home / ".docker" / "desktop" / "docker.sock",
        home / "Library" / "Containers" / "com.docker.docker" / "Data" / "docker.sock",
    )
    for candidate in unix_candidates:
        if candidate.exists():
            return str(candidate), False
    win_pipe = Path(r"\\.\pipe\docker_engine")
    if win_pipe.exists():
        return str(win_pipe), True
    msg = (
        "Docker socket not found. Make sure Docker is installed and running "
        "(start Docker Desktop on macOS, or run 'sudo systemctl start docker' on Linux)."
    )
    raise ValueError(msg)


class _DockerTools(Toolkit):
    """Inspect and manage Docker containers, images, volumes, and networks."""

    def __init__(self, include_tools: list[str] | None = None) -> None:
        self._client: Any | None = None
        all_tools = {
            "docker_inspect": self.docker_inspect,
            "docker_container": self.docker_container,
            "docker_resource": self.docker_resource,
        }
        if include_tools is None:
            selected = list(all_tools.values())
        else:
            unknown = sorted(set(include_tools) - set(all_tools))
            if unknown:
                msg = f"Included tool(s) not present in the toolkit: {', '.join(unknown)}"
                raise ValueError(msg)
            allowed = set(include_tools)
            selected = [fn for name, fn in all_tools.items() if name in allowed]
        super().__init__(name="docker_tools", tools=selected)

    def _get_client(self) -> Any:  # noqa: ANN401 - docker.DockerClient is an optional runtime import
        if self._client is None:
            import docker

            socket_path, is_pipe = _find_docker_socket()
            base_url = f"npipe://{socket_path}" if is_pipe else f"unix://{socket_path}"
            client = docker.DockerClient(base_url=base_url)
            _isolate_docker_client(client)
            client.ping()
            self._client = client
        return self._client

    def _inspect_dispatch(  # noqa: C901, PLR0911
        self,
        action: _DockerInspectAction,
        *,
        target: str | None,
        all_containers: bool,
        tail: int,
    ) -> str:
        client = self._get_client()
        if action == "list_containers":
            rows = []
            for container in client.containers.list(all=all_containers):
                image = container.image
                image_info = (image.tags[0] if image.tags else image.id) if image is not None else None
                rows.append(
                    {
                        "id": container.id,
                        "name": container.name,
                        "image": image_info,
                        "status": container.status,
                        "created": container.attrs.get("Created"),
                        "ports": container.ports,
                        "labels": container.labels,
                    },
                )
            return json.dumps(rows, indent=2)
        if action == "inspect_container":
            return json.dumps(client.containers.get(require_non_empty(target, "target")).attrs, indent=2)
        if action == "get_container_logs":
            logs = client.containers.get(require_non_empty(target, "target")).logs(tail=tail, stream=False)
            return logs.decode("utf-8", errors="replace") if isinstance(logs, bytes) else str(logs or "No logs found")
        if action == "list_images":
            rows = [
                {
                    "id": img.id,
                    "tags": img.tags,
                    "created": img.attrs.get("Created"),
                    "size": img.attrs.get("Size"),
                    "labels": img.labels,
                }
                for img in client.images.list()
            ]
            return json.dumps(rows, indent=2)
        if action == "inspect_image":
            return json.dumps(client.images.get(require_non_empty(target, "target")).attrs, indent=2)
        if action == "list_volumes":
            rows = [
                {
                    "name": vol.name,
                    "driver": vol.attrs.get("Driver"),
                    "mountpoint": vol.attrs.get("Mountpoint"),
                    "created": vol.attrs.get("CreatedAt"),
                    "labels": vol.attrs.get("Labels", {}),
                }
                for vol in client.volumes.list()
            ]
            return json.dumps(rows, indent=2)
        if action == "inspect_volume":
            return json.dumps(client.volumes.get(require_non_empty(target, "target")).attrs, indent=2)
        if action == "list_networks":
            rows = [
                {
                    "id": net.id,
                    "name": net.name,
                    "driver": net.attrs.get("Driver"),
                    "scope": net.attrs.get("Scope"),
                    "created": net.attrs.get("Created"),
                    "internal": net.attrs.get("Internal", False),
                    "containers": list((net.attrs.get("Containers") or {}).keys()),
                }
                for net in client.networks.list()
            ]
            return json.dumps(rows, indent=2)
        if action == "inspect_network":
            return json.dumps(client.networks.get(require_non_empty(target, "target")).attrs, indent=2)
        return f"Unsupported action: {action}"

    def docker_inspect(
        self,
        action: _DockerInspectAction,
        target: str | None = None,
        all_containers: bool = False,
        tail: int = 100,
    ) -> str:
        """Read-only inspection of Docker containers, images, volumes, and networks.

        Actions:
        - `list_containers`: list containers (`all_containers=True` includes stopped).
        - `inspect_container` / `get_container_logs`: inspect or fetch last `tail` log lines of `target`.
        - `list_images` / `inspect_image`: list images or inspect `target`.
        - `list_volumes` / `inspect_volume`: list volumes or inspect `target`.
        - `list_networks` / `inspect_network`: list networks or inspect `target`.
        """
        try:
            result = self._inspect_dispatch(action, target=target, all_containers=all_containers, tail=tail)
        except Exception as exc:
            logger.warning("docker_inspect_failed", action=action, error=str(exc))
            return f"Error in docker_inspect ({action}): {exc}"
        return result

    def _container_dispatch(  # noqa: PLR0911
        self,
        action: _DockerContainerAction,
        *,
        container_id: str | None,
        image: str | None,
        command: str | None,
        name: str | None,
        detach: bool,
        ports: dict[str, str | int] | None,
        volumes: dict[str, dict[str, str]] | None,
        environment: dict[str, str] | None,
        network: str | None,
        timeout: int,
        force: bool,
        remove_volumes: bool,
    ) -> str:
        client = self._get_client()
        if action == "run":
            img = require_non_empty(image, "image")
            fixed_ports = {k: str(v) for k, v in ports.items()} if ports else None
            outcome = client.containers.run(
                image=img,
                command=command,
                name=name,
                detach=detach,
                ports=fixed_ports,
                volumes=volumes,
                environment=environment,
                network=network,
            )
            if isinstance(outcome, bytes):
                return outcome.decode("utf-8", errors="replace")
            return f"Container started with ID: {outcome.id}"
        cid = require_non_empty(container_id, "container_id")
        container = client.containers.get(cid)
        if action == "exec":
            cmd = require_non_empty(command, "command")
            exit_code, output = container.exec_run(cmd)
            text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
            return text if exit_code == 0 else f"Command failed with exit code {exit_code}: {text}"
        if action == "start":
            container.start()
            return f"Container {cid} started successfully"
        if action == "stop":
            container.stop(timeout=timeout)
            return f"Container {cid} stopped successfully"
        if action == "remove":
            container.remove(force=force, v=remove_volumes)
            return f"Container {cid} removed successfully"
        return f"Unsupported action: {action}"

    def docker_container(
        self,
        action: _DockerContainerAction,
        container_id: str | None = None,
        image: str | None = None,
        command: str | None = None,
        name: str | None = None,
        detach: bool = True,
        ports: dict[str, str | int] | None = None,
        volumes: dict[str, dict[str, str]] | None = None,
        environment: dict[str, str] | None = None,
        network: str | None = None,
        timeout: int = 10,
        force: bool = False,
        remove_volumes: bool = False,
    ) -> str:
        """Run, execute inside, start, stop, or remove Docker containers.

        Actions:
        - `run`: start a container from `image` (optional `command`, `name`, `detach`, `ports`, `volumes`, `environment`, `network`).
        - `exec`: run `command` inside running `container_id`.
        - `start` / `stop`: start or stop `container_id` (optional `timeout` seconds for `stop`).
        - `remove`: remove `container_id` (`force`, `remove_volumes`).
        """
        try:
            result = self._container_dispatch(
                action,
                container_id=container_id,
                image=image,
                command=command,
                name=name,
                detach=detach,
                ports=ports,
                volumes=volumes,
                environment=environment,
                network=network,
                timeout=timeout,
                force=force,
                remove_volumes=remove_volumes,
            )
        except Exception as exc:
            logger.warning("docker_container_failed", action=action, error=str(exc))
            return f"Error in docker_container ({action}): {exc}"
        return result

    def _resource_dispatch(  # noqa: C901, PLR0911
        self,
        action: _DockerResourceAction,
        *,
        name: str | None,
        tag: str | None,
        path: str | None,
        dockerfile: str,
        repository: str | None,
        container_id: str | None,
        driver: str | None,
        internal: bool,
        labels: dict[str, str] | None,
        force: bool,
        rm: bool,
    ) -> str:
        client = self._get_client()
        if action == "pull_image":
            image_name = require_non_empty(name, "name")
            resolved_tag = tag or "latest"
            for _line in client.api.pull(image_name, tag=resolved_tag, stream=True, decode=True):
                pass
            return f"Image {image_name}:{resolved_tag} pulled successfully"
        if action == "build_image":
            build_path = require_non_empty(path, "path")
            build_tag = require_non_empty(tag, "tag")
            image, _logs = client.images.build(
                path=build_path,
                tag=build_tag,
                dockerfile=dockerfile,
                rm=rm,
                use_config_proxy=False,
            )
            return f"Image built successfully with ID: {image.id}"
        if action == "tag_image":
            image_id = require_non_empty(name, "name")
            repo = require_non_empty(repository, "repository")
            client.images.get(image_id).tag(repo, tag=tag)
            return f"Image {image_id} tagged as {repo}:{tag or 'latest'}"
        if action == "remove_image":
            image_id = require_non_empty(name, "name")
            client.images.remove(image_id, force=force)
            return f"Image {image_id} removed successfully"
        if action == "create_volume":
            vol_name = require_non_empty(name, "name")
            client.volumes.create(name=vol_name, driver=driver or "local", labels=labels)
            return f"Volume {vol_name} created successfully"
        if action == "remove_volume":
            vol_name = require_non_empty(name, "name")
            client.volumes.get(vol_name).remove(force=force)
            return f"Volume {vol_name} removed successfully"
        if action == "create_network":
            net_name = require_non_empty(name, "name")
            network = client.networks.create(
                name=net_name,
                driver=driver or "bridge",
                internal=internal,
                labels=labels,
            )
            return f"Network {net_name} created successfully with ID: {network.id}"
        if action == "remove_network":
            net_name = require_non_empty(name, "name")
            client.networks.get(net_name).remove()
            return f"Network {net_name} removed successfully"
        if action == "connect_network":
            net_name = require_non_empty(name, "name")
            cid = require_non_empty(container_id, "container_id")
            client.networks.get(net_name).connect(cid)
            return f"Container {cid} connected to network {net_name}"
        if action == "disconnect_network":
            net_name = require_non_empty(name, "name")
            cid = require_non_empty(container_id, "container_id")
            client.networks.get(net_name).disconnect(cid)
            return f"Container {cid} disconnected from network {net_name}"
        return f"Unsupported action: {action}"

    def docker_resource(
        self,
        action: _DockerResourceAction,
        name: str | None = None,
        tag: str | None = None,
        path: str | None = None,
        dockerfile: str = "Dockerfile",
        repository: str | None = None,
        container_id: str | None = None,
        driver: str | None = None,
        internal: bool = False,
        labels: dict[str, str] | None = None,
        force: bool = False,
        rm: bool = True,
    ) -> str:
        """Manage Docker images, volumes, and networks.

        Actions:
        - `pull_image`: pull `name` with `tag` (default `latest`).
        - `build_image`: build `tag` from `path` and `dockerfile` (`rm=True` removes intermediates).
        - `tag_image` / `remove_image`: tag `name` into `repository`[:`tag`] or remove `name` (`force`).
        - `create_volume` / `remove_volume`: create (`driver`, `labels`) or remove (`force`) volume `name`.
        - `create_network` / `remove_network`: create (`driver`, `internal`, `labels`) or remove network `name`.
        - `connect_network` / `disconnect_network`: attach or detach `container_id` on network `name`.
        """
        try:
            result = self._resource_dispatch(
                action,
                name=name,
                tag=tag,
                path=path,
                dockerfile=dockerfile,
                repository=repository,
                container_id=container_id,
                driver=driver,
                internal=internal,
                labels=labels,
                force=force,
                rm=rm,
            )
        except Exception as exc:
            logger.warning("docker_resource_failed", action=action, error=str(exc))
            return f"Error in docker_resource ({action}): {exc}"
        return result


@register_tool_with_metadata(
    name="docker",
    display_name="Docker",
    description="Container, image, volume, and network management",
    category=ToolCategory.DEVELOPMENT,
    file_access=ToolFileAccess.UNCONFINED,
    executes_code=True,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,
    default_execution_target=ToolExecutionTarget.WORKER,
    consumes_workspace_paths=True,
    icon="SiDocker",
    icon_color="text-blue-500",
    config_fields=[
        ConfigField(
            name="include_tools",
            label="Exposed Docker Commands",
            type="string[]",
            required=False,
            default=None,
            description="Optional allowlist of Docker command functions to expose. Leave unset to expose all commands.",
        ),
    ],
    dependencies=["docker"],
    docs_url="https://docs.mindroom.chat/tools/execution-and-coding/#docker",
    helper_text=(
        "Docker is privileged. Use a dedicated worker sandbox, or explicitly opt in to unsafe local execution "
        "before exposing host Docker access."
    ),
    function_names=("docker_container", "docker_inspect", "docker_resource"),
    workspace_writers=("docker_container", "docker_resource"),
    action_scoped_grants=("docker_container", "docker_inspect", "docker_resource"),
)
def docker_tools() -> type[Toolkit]:
    """Return Docker tools for container management."""
    return _DockerTools
