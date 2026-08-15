"""Trusted policy, broker contracts, and durable state for agent repositories."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast
from urllib.parse import urlsplit

import httpx

from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.runtime_env_policy import (
    AGENT_REPOSITORY_ENV_BY_KEY,
    KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY,
)
from mindroom.tool_system.worker_routing import descriptive_worker_id_for_key

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_BINDING_VERSION = 1
_BINDING_DIRECTORY = "repository_bindings"
_GITHUB_REPOSITORY_NAME_MAX_LENGTH = 100
_LONG_NAME_DIGEST_LENGTH = 12
_SAFE_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")
_GITHUB_REPOSITORY_COMPONENT_PATTERN = re.compile(r"[A-Za-z0-9_.-]+")
_EXPECTED_BINDING_FIELDS = frozenset(
    {
        "version",
        "worker_key",
        "repository_id",
        "organization",
        "repository_name",
        "clone_url",
    },
)
_EXPECTED_LEASE_FIELDS = frozenset(
    {
        "repository_id",
        "organization",
        "repository_name",
        "clone_url",
    },
)
_AGENT_VAULT_ENSURE_PATH = "/v1/internal/mindroom/repositories/ensure"
_DEFAULT_AGENT_VAULT_NAME_PREFIX = "agent-vault"
_BROKER_HTTP_TIMEOUT_SECONDS = 15.0


class RepositoryBindingError(RuntimeError):
    """Raised when repository ownership or durable state cannot be trusted."""


class RepositoryBrokerError(RuntimeError):
    """Raised when the trusted repository broker cannot satisfy a request."""


class RepositoryOriginConflictError(RepositoryBindingError):
    """Raised when a workspace origin targets a different repository."""


@dataclass(frozen=True)
class RepositoryEnsureRequest:
    """Trusted request sent from MindRoom to its repository broker."""

    worker_key: str
    organization: str
    repository_name: str


@dataclass(frozen=True)
class RepositoryLease:
    """Credential-free repository identity returned by the broker."""

    repository_id: int
    organization: str
    repository_name: str
    clone_url: str


class RepositoryBroker(Protocol):
    """Minimal broker seam implemented by Agent Vault and test fakes."""

    async def ensure_repository(self, request: RepositoryEnsureRequest) -> RepositoryLease:
        """Ensure the request's one bound private repository."""
        ...


class AgentVaultRepositoryBroker:
    """Strict HTTP adapter for Agent Vault's MindRoom repository endpoint."""

    def __init__(
        self,
        *,
        broker_url: str,
        broker_token_file: Path,
        vault_name_prefix: str,
    ) -> None:
        try:
            parsed_url = urlsplit(broker_url)
            port = parsed_url.port
        except ValueError as exc:
            msg = "Agent repository broker URL must be an uncredentialed origin-only HTTP(S) URL"
            raise RepositoryBrokerError(msg) from exc
        if (
            broker_url != broker_url.strip()
            or parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.path not in {"", "/"}
            or parsed_url.query
            or parsed_url.fragment
            or "?" in broker_url
            or "#" in broker_url
            or port == 0
        ):
            msg = "Agent repository broker URL must be an uncredentialed origin-only HTTP(S) URL"
            raise RepositoryBrokerError(msg)
        if not broker_token_file.is_absolute():
            msg = "Agent repository broker token file must use an absolute path"
            raise RepositoryBrokerError(msg)
        if not vault_name_prefix.strip():
            msg = "Agent repository worker-vault prefix must not be empty"
            raise RepositoryBrokerError(msg)
        self._broker_url = broker_url.rstrip("/")
        self._broker_token_file = broker_token_file
        self._vault_name_prefix = vault_name_prefix.strip()

    @classmethod
    def from_runtime(cls, runtime_paths: RuntimePaths) -> AgentVaultRepositoryBroker:
        """Build the adapter from primary-runtime-only environment values."""
        broker_url = (runtime_paths.env_value(AGENT_REPOSITORY_ENV_BY_KEY["broker_url"]) or "").strip()
        token_file_value = (runtime_paths.env_value(AGENT_REPOSITORY_ENV_BY_KEY["broker_token_file"]) or "").strip()
        missing = []
        if not broker_url:
            missing.append(AGENT_REPOSITORY_ENV_BY_KEY["broker_url"])
        if not token_file_value:
            missing.append(AGENT_REPOSITORY_ENV_BY_KEY["broker_token_file"])
        if missing:
            msg = f"Agent repository broker requires these environment values: {', '.join(missing)}"
            raise RepositoryBrokerError(msg)
        token_file = Path(token_file_value).expanduser()
        vault_prefix_env = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["agent_vault_vault_name_prefix"]
        vault_name_prefix = (
            runtime_paths.env_value(vault_prefix_env, default=_DEFAULT_AGENT_VAULT_NAME_PREFIX)
            or _DEFAULT_AGENT_VAULT_NAME_PREFIX
        )
        return cls(
            broker_url=broker_url,
            broker_token_file=token_file,
            vault_name_prefix=vault_name_prefix,
        )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_BROKER_HTTP_TIMEOUT_SECONDS)

    def _token(self) -> str:
        try:
            token = self._broker_token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            msg = "Could not read the Agent repository broker token file"
            raise RepositoryBrokerError(msg) from exc
        if not token:
            msg = "Agent repository broker token file is empty"
            raise RepositoryBrokerError(msg)
        return token

    async def ensure_repository(self, request: RepositoryEnsureRequest) -> RepositoryLease:
        """Call the one constrained Agent Vault ensure endpoint."""
        token = self._token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Vault": descriptive_worker_id_for_key(
                request.worker_key,
                prefix=self._vault_name_prefix,
            ),
        }
        payload = {
            "worker_key": request.worker_key,
            "organization": request.organization,
            "repository_name": request.repository_name,
        }
        try:
            async with self._client() as client:
                response = await client.post(
                    f"{self._broker_url}{_AGENT_VAULT_ENSURE_PATH}",
                    headers=headers,
                    json=payload,
                )
        except httpx.HTTPError as exc:
            msg = "Agent repository broker request failed"
            raise RepositoryBrokerError(msg) from exc
        if response.status_code not in {200, 201}:
            msg = f"Agent repository broker returned HTTP {response.status_code}"
            raise RepositoryBrokerError(msg)
        try:
            response_payload = response.json()
        except (json.JSONDecodeError, UnicodeError) as exc:
            msg = "Agent repository broker returned invalid JSON"
            raise RepositoryBrokerError(msg) from exc
        if not isinstance(response_payload, dict) or set(response_payload) != _EXPECTED_LEASE_FIELDS:
            msg = "Agent repository broker response has an invalid schema"
            raise RepositoryBrokerError(msg)
        data = cast("dict[str, object]", response_payload)
        repository_id = data["repository_id"]
        organization = data["organization"]
        repository_name = data["repository_name"]
        clone_url = data["clone_url"]
        if type(repository_id) is not int or not all(
            isinstance(value, str) for value in (organization, repository_name, clone_url)
        ):
            msg = "Agent repository broker response has invalid field types"
            raise RepositoryBrokerError(msg)
        lease = RepositoryLease(
            repository_id=repository_id,
            organization=cast("str", organization),
            repository_name=cast("str", repository_name),
            clone_url=cast("str", clone_url),
        )
        _validate_lease(request, lease)
        return lease


@dataclass(frozen=True)
class RepositoryBinding:
    """Immutable local binding from one worker identity to one GitHub repository ID."""

    version: int
    worker_key: str
    repository_id: int
    organization: str
    repository_name: str
    clone_url: str


def _repository_slug(value: str, *, label: str) -> str:
    slug = _SAFE_SLUG_PATTERN.sub("-", value.casefold()).strip("-")
    if not slug:
        msg = f"Cannot derive a repository {label} slug from {value!r}"
        raise RepositoryBindingError(msg)
    return slug


def _matrix_requester_localpart(requester_id: str | None) -> str:
    if not requester_id or not requester_id.startswith("@") or ":" not in requester_id:
        msg = "Private agent repository naming requires an authoritative Matrix requester ID"
        raise RepositoryBindingError(msg)
    localpart, server_name = requester_id[1:].split(":", 1)
    if not localpart or not server_name:
        msg = "Private agent repository naming requires a non-empty Matrix requester localpart and server"
        raise RepositoryBindingError(msg)
    return localpart


def _bounded_repository_name(name: str) -> str:
    if len(name) <= _GITHUB_REPOSITORY_NAME_MAX_LENGTH:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:_LONG_NAME_DIGEST_LENGTH]
    prefix_length = _GITHUB_REPOSITORY_NAME_MAX_LENGTH - len(digest) - 1
    return f"{name[:prefix_length].rstrip('-')}-{digest}"


def derive_repository_name(*, prefix: str, worker_target: ResolvedWorkerTarget) -> str:
    """Derive the only repository name one trusted worker target may own."""
    if prefix != "MindRoom":
        msg = "Agent repository prefix must be exactly 'MindRoom'"
        raise RepositoryBindingError(msg)

    agent_name = worker_target.routing_agent_name
    if not agent_name:
        msg = "Agent repository naming requires a routing agent name"
        raise RepositoryBindingError(msg)
    agent_slug = _repository_slug(agent_name, label="agent")

    if worker_target.worker_scope == "shared":
        if not worker_target.worker_key:
            msg = "Shared agent repository naming requires a worker identity"
            raise RepositoryBindingError(msg)
        return _bounded_repository_name(f"{prefix}-{agent_slug}")

    private_names = worker_target.private_agent_names or frozenset()
    if worker_target.worker_scope != "user_agent" or agent_name not in private_names:
        msg = "Agent repositories require a shared worker or private.per=user_agent identity"
        raise RepositoryBindingError(msg)
    identity = worker_target.execution_identity
    requester_id = identity.requester_id if identity is not None else None
    username_slug = _repository_slug(
        _matrix_requester_localpart(requester_id),
        label="requester",
    )
    if not worker_target.worker_key:
        msg = "Private agent repository naming requires a worker identity"
        raise RepositoryBindingError(msg)
    return _bounded_repository_name(f"{prefix}-{agent_slug}-{username_slug}")


def _validate_lease(request: RepositoryEnsureRequest, lease: RepositoryLease) -> None:
    if not request.worker_key.strip():
        msg = "Repository binding requires a non-empty worker key"
        raise RepositoryBindingError(msg)
    if type(lease.repository_id) is not int or lease.repository_id <= 0:
        msg = "Repository broker returned an invalid repository ID"
        raise RepositoryBindingError(msg)
    if lease.organization != request.organization:
        msg = "Repository broker returned an unexpected organization"
        raise RepositoryBindingError(msg)
    if lease.repository_name != request.repository_name:
        msg = "Repository broker returned an unexpected repository name"
        raise RepositoryBindingError(msg)

    expected_clone_url = f"https://github.com/{request.organization}/{request.repository_name}.git"
    if lease.clone_url != expected_clone_url:
        msg = "Repository broker returned a non-canonical or credentialed clone URL"
        raise RepositoryBindingError(msg)


def _binding_from_payload(payload: object) -> RepositoryBinding:
    if not isinstance(payload, dict) or set(payload) != _EXPECTED_BINDING_FIELDS:
        msg = "Repository binding file has an invalid schema"
        raise RepositoryBindingError(msg)
    data = cast("dict[str, object]", payload)
    if data["version"] != _BINDING_VERSION:
        msg = "Repository binding file has an unsupported version"
        raise RepositoryBindingError(msg)
    repository_id = data["repository_id"]
    if type(repository_id) is not int or repository_id <= 0:
        msg = "Repository binding file has an invalid repository ID"
        raise RepositoryBindingError(msg)
    string_fields = ("worker_key", "organization", "repository_name", "clone_url")
    if any(not isinstance(data[field], str) or not cast("str", data[field]) for field in string_fields):
        msg = "Repository binding file has an invalid string field"
        raise RepositoryBindingError(msg)
    return RepositoryBinding(
        version=_BINDING_VERSION,
        worker_key=cast("str", data["worker_key"]),
        repository_id=repository_id,
        organization=cast("str", data["organization"]),
        repository_name=cast("str", data["repository_name"]),
        clone_url=cast("str", data["clone_url"]),
    )


class RepositoryBindingStore:
    """Write-once repository bindings rooted in canonical MindRoom storage."""

    def __init__(self, runtime_paths: RuntimePaths) -> None:
        self._storage_root = runtime_paths.storage_root.expanduser().resolve()
        self._root = self._storage_root / _BINDING_DIRECTORY

    def _binding_path(self, worker_key: str) -> Path:
        """Return the non-reversible binding path for one worker key."""
        digest = hashlib.sha256(worker_key.encode("utf-8")).hexdigest()
        return self._root / f"{digest}.json"

    def _lock_path(self, worker_key: str) -> Path:
        digest = hashlib.sha256(worker_key.encode("utf-8")).hexdigest()
        return self._root / f".{digest}.lock"

    def workspace_lock_path(self, worker_key: str) -> Path:
        """Return the out-of-workspace lock used for local Git configuration."""
        digest = hashlib.sha256(worker_key.encode("utf-8")).hexdigest()
        return self._root / f".{digest}.workspace.lock"

    def _ensure_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        if self._root.is_symlink() or self._root.resolve().parent != self._storage_root:
            msg = "Repository binding directory must stay inside configured storage_root"
            raise RepositoryBindingError(msg)

    def _read_unlocked(self, worker_key: str) -> RepositoryBinding | None:
        path = self._binding_path(worker_key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            msg = f"Could not read repository binding: {exc}"
            raise RepositoryBindingError(msg) from exc
        binding = _binding_from_payload(payload)
        if binding.worker_key != worker_key:
            msg = "Repository binding worker identity does not match its storage key"
            raise RepositoryBindingError(msg)
        return binding

    def read(self, worker_key: str) -> RepositoryBinding | None:
        """Read one binding under the same lock used by writers."""
        self._ensure_root()
        with advisory_file_lock(self._lock_path(worker_key)):
            return self._read_unlocked(worker_key)

    def bind(self, request: RepositoryEnsureRequest, lease: RepositoryLease) -> RepositoryBinding:
        """Persist or replay one exact binding; reject every attempted rebind."""
        _validate_lease(request, lease)
        candidate = RepositoryBinding(
            version=_BINDING_VERSION,
            worker_key=request.worker_key,
            repository_id=lease.repository_id,
            organization=lease.organization,
            repository_name=lease.repository_name,
            clone_url=lease.clone_url,
        )
        self._ensure_root()
        with advisory_file_lock(self._lock_path(request.worker_key)):
            existing = self._read_unlocked(request.worker_key)
            if existing is not None:
                if existing != candidate:
                    msg = "Worker identity already has a different immutable repository binding"
                    raise RepositoryBindingError(msg)
                return existing

            path = self._binding_path(request.worker_key)
            write_json_file_durable(
                path,
                asdict(candidate),
                strict_atomic_replace=True,
                indent=2,
                sort_keys=True,
                trailing_newline=True,
            )
            path.chmod(0o600)
            return candidate


def _run_local_git(workspace: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one credential-free, non-network Git metadata command."""
    return subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
    )


def _github_transport_and_path(url: str) -> tuple[str, str] | None:
    """Parse supported credential-free GitHub transports into a repository path."""
    if url.startswith("git@github.com:"):
        return "ssh", url.removeprefix("git@github.com:")

    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    valid_https = scheme == "https" and parsed.username is None and parsed.password is None and port in {None, 443}
    valid_ssh = scheme == "ssh" and parsed.username == "git" and parsed.password is None and port in {None, 22}
    if (
        not (valid_https or valid_ssh)
        or parsed.hostname is None
        or parsed.hostname.casefold() != "github.com"
        or parsed.query
        or parsed.fragment
    ):
        return None
    return scheme, parsed.path.removeprefix("/")


def _normalized_github_repository(url: str) -> tuple[str, str, str] | None:
    """Return transport plus credential-free canonical GitHub repository identity."""
    transport_and_path = _github_transport_and_path(url)
    if transport_and_path is None:
        return None
    transport, path = transport_and_path

    path = path.removesuffix("/")
    parts = path.split("/")
    if len(parts) != 2:
        return None
    organization, repository = parts
    if repository.casefold().endswith(".git"):
        repository = repository[:-4]
    if not organization or not repository:
        return None
    if not all(_GITHUB_REPOSITORY_COMPONENT_PATTERN.fullmatch(component) for component in (organization, repository)):
        return None
    return transport, organization.casefold(), repository.casefold()


def configure_repository_workspace(
    *,
    workspace: Path,
    clone_url: str,
    lock_path: Path,
) -> Path:
    """Initialize one workspace and set its immutable credential-free origin."""
    resolved_workspace = workspace.expanduser().resolve()
    resolved_workspace.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    expected = _normalized_github_repository(clone_url)
    if expected is None or expected[0] != "https":
        msg = "Agent repository workspace requires a canonical credential-free HTTPS origin"
        raise RepositoryBindingError(msg)
    with advisory_file_lock(lock_path):
        git_metadata = resolved_workspace / ".git"
        if git_metadata.is_symlink() or (git_metadata.exists() and not git_metadata.is_dir()):
            msg = "Agent repository workspace Git metadata must be a local directory"
            raise RepositoryBindingError(msg)
        if not git_metadata.exists():
            initialized = _run_local_git(resolved_workspace, "init", "--initial-branch=main")
            if initialized.returncode != 0:
                msg = "Could not initialize the agent repository workspace"
                raise RepositoryBindingError(msg)

        remotes = _run_local_git(resolved_workspace, "remote")
        if remotes.returncode != 0:
            msg = "Could not inspect the agent repository workspace remotes"
            raise RepositoryBindingError(msg)
        if "origin" in remotes.stdout.splitlines():
            raw_fetch_urls = _run_local_git(
                resolved_workspace,
                "config",
                "--local",
                "--get-all",
                "remote.origin.url",
            )
            raw_push_urls = _run_local_git(
                resolved_workspace,
                "config",
                "--local",
                "--get-all",
                "remote.origin.pushurl",
            )
            fetch_urls = _run_local_git(resolved_workspace, "remote", "get-url", "--all", "origin")
            push_urls = _run_local_git(resolved_workspace, "remote", "get-url", "--push", "--all", "origin")
            raw_fetch_values = raw_fetch_urls.stdout.splitlines()
            raw_push_values = raw_push_urls.stdout.splitlines()
            if not raw_push_values:
                raw_push_values = raw_fetch_values
            actual_urls = [
                *raw_fetch_values,
                *raw_push_values,
                *fetch_urls.stdout.splitlines(),
                *push_urls.stdout.splitlines(),
            ]
            if (
                raw_fetch_urls.returncode != 0
                or raw_push_urls.returncode not in {0, 1}
                or fetch_urls.returncode != 0
                or push_urls.returncode != 0
                or not actual_urls
                or any(_normalized_github_repository(url) != expected for url in actual_urls)
            ):
                msg = "Agent repository workspace has an origin for a different repository"
                raise RepositoryOriginConflictError(msg)
            return resolved_workspace

        added = _run_local_git(resolved_workspace, "remote", "add", "origin", clone_url)
        if added.returncode != 0:
            msg = "Could not configure the agent repository workspace origin"
            raise RepositoryBindingError(msg)
    return resolved_workspace
