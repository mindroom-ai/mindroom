"""Build registered tools the way the C3 tool goldens were captured, so the owned adapter can be compared to them.

`tests/fixtures/agno_parity/README.md` describes the goldens.
`tests/agno_parity/capture_tools.py` captures them with this same setup, so capture and comparison cannot drift: one
agent of a config shaped like `mindroom config init` output, dummy credentials for tools that need settings, offline
replacements for SDK clients that contact their service while constructing, and a hidden host environment that refuses
network connections.
"""

from __future__ import annotations

import json
import os
import re
import socket
from contextlib import contextmanager, nullcontext
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
from packaging.version import Version

from mindroom.agents import build_agent_toolkit
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.tool_system.catalog import TOOL_METADATA, ensure_tool_registry_loaded
from mindroom.tool_system.dependencies import check_deps_installed, optional_dependency_environment
from mindroom.tool_system.dynamic_toolkits import defer_toolkit
from mindroom.vendor_telemetry import vendor_telemetry_env_values

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agno.tools import Toolkit

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "agno_parity" / "tools"
SCHEMAS_PATH = FIXTURE_DIR / "schemas.json"
ALL_ENABLED_SCHEMAS_PATH = FIXTURE_DIR / "schemas_all_enabled.json"
ARGUMENTS_PATH = FIXTURE_DIR / "arguments.json"

AGENT_NAME = "golden"
# Tools are built for one agent of a config shaped like `mindroom config init` output: file memory gives the agent a
# workspace, so output-file wrapping applies; delegation and the knowledge graph enable the tools that need them.
CONFIG = {
    "models": {"default": {"provider": "openai", "id": "golden-model"}},
    "memory": {"backend": "file"},
    "knowledge_graph": {"model": "default"},
    "agents": {
        AGENT_NAME: {
            "display_name": "Golden",
            "role": "Captures tool schemas.",
            "delegate_to": ["helper"],
            "knowledge_graph": True,
        },
        "helper": {"display_name": "Helper", "role": "Helps with delegated work."},
    },
}

_KEY = "golden-dummy"
_URL = "https://golden.invalid"
# Settings a tool cannot be constructed without, saved as dashboard credentials; nothing contacts these values.
DUMMY_CREDENTIALS: dict[str, dict[str, object]] = {
    "agentql": {"api_key": _KEY},
    "bitbucket": {"username": _KEY, "token": _KEY, "workspace": _KEY, "repo_slug": _KEY},
    "brightdata": {"api_key": _KEY},
    "browserbase": {"api_key": _KEY, "project_id": _KEY},
    "cartesia": {"api_key": _KEY},
    "clickup": {"api_key": _KEY, "master_space_id": _KEY},
    "confluence": {"url": _URL, "username": _KEY, "api_key": _KEY},
    "daytona": {"api_key": _KEY},
    "discord": {"bot_token": _KEY},
    "e2b": {"api_key": _KEY},
    "exa": {"api_key": _KEY},
    "firecrawl": {"api_key": _KEY},
    "gemini": {"api_key": _KEY},
    "google_bigquery": {"dataset": _KEY, "project": _KEY, "location": _KEY},
    # The client rejects keys without Google's prefix.
    "google_maps": {"key": "AIza-golden-dummy"},
    "groq": {"api_key": _KEY},
    "jira": {"server_url": _URL, "username": _KEY, "token": _KEY},
    "linear": {"api_key": _KEY},
    "linkup": {"api_key": _KEY},
    "lumalabs": {"api_key": _KEY},
    "mem0": {"api_key": _KEY},
    "neo4j": {"user": _KEY, "password": _KEY},
    "notion": {"api_key": _KEY, "database_id": _KEY},
    "openai": {"api_key": _KEY},
    "openweather": {"api_key": _KEY},
    "oxylabs": {"username": _KEY, "password": _KEY},
    "scrapegraph": {"api_key": _KEY},
    "searxng": {"host": _URL},
    # Its search_messages function exists only with a user token.
    "slack": {"token": _KEY, "user_token": _KEY},
    "spotify": {"access_token": _KEY},
    "sql": {"db_url": "sqlite://"},
    "tavily": {"api_key": _KEY},
    # The client rejects bot tokens without a colon.
    "telegram": {"token": "0:golden-dummy", "chat_id": _KEY},
    "todoist": {"api_token": _KEY},
    "webex": {"access_token": _KEY},
    "whatsapp": {"access_token": _KEY, "phone_number_id": _KEY},
    "zep": {"api_key": _KEY},
}
# Settings that a tool reads from the runtime environment instead of its credentials.
DUMMY_ENV = {
    "MINDROOM_AGENT_VAULT_ACCESS_API_URL": _URL,
    "MINDROOM_AGENT_VAULT_ACCESS_ADMIN_TOKEN": _KEY,
    "MINDROOM_AGENT_VAULT_ACCESS_UI_BASE_URL": _URL,
    "MINDROOM_AGENT_VAULT_ACCESS_EMAIL_DOMAIN": "golden.invalid",
}
# SDK clients that contact their service while a toolkit is constructed, replaced so it registers its functions offline.
OFFLINE_CLIENTS = {
    "e2b": "agno.tools.e2b.Sandbox",
    # Its Places client looks for Google Cloud credentials, retrying the metadata server for many seconds.
    "google_maps": "agno.tools.google.maps.places_v1",
    "jira": "agno.tools.jira.JIRA",
    "mem0": "agno.tools.mem0.MemoryClient",
    "neo4j": "agno.tools.neo4j.GraphDatabase",
    "zep": "agno.tools.zep.Zep",
}
# HTTP requests a toolkit sends while constructed, refused before they dial; the goldens record the refused outcome.
REFUSED_REQUESTS = {
    # Its instructions report the PyPI release check, which fails offline.
    "update_awareness": "mindroom.custom_tools.update_awareness.httpx.get",
}
# Overrides beyond the `all` and `enable_*` switches that a tool needs before it registers every function it can.
EXTRA_ENABLING_OVERRIDES: dict[str, dict[str, object]] = {
    # custom_scrape_website exists only with a query to run.
    "agentql": {"agentql_query": "{ title }"},
    "google_drive": {"download_file": True},
    "usage_stats": {"admin_scope": True},
}

_KEPT_ENV = ("PATH", "LANG", "LC_ALL", "TMPDIR")
# The update_awareness instructions name the running MindRoom version, which changes with every commit.
_MINDROOM_VERSION = str(Version(version("mindroom")))
MINDROOM_VERSION_PLACEHOLDER = "<MINDROOM_VERSION>"
_PYDANTIC_ERROR_URL = re.compile(r"https://errors\.pydantic\.dev/[0-9.]+/")
_PYDANTIC_ERROR_URL_PLACEHOLDER = "https://errors.pydantic.dev/<PYDANTIC_VERSION>/"


def _refuse_network(*_args: object, **_kwargs: object) -> None:
    msg = "Golden capture refuses network connections"
    raise OSError(msg)


def _refuse_request(url: str, **_kwargs: object) -> None:
    msg = "Golden capture refuses network connections"
    raise httpx.ConnectError(msg, request=httpx.Request("GET", url))


@contextmanager
def isolated_process(root: Path) -> Iterator[None]:
    """Hide the host environment and refuse connections, so a build is deterministic and contacts no service."""
    saved_env = dict(os.environ)
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    os.environ.clear()
    os.environ.update({name: saved_env[name] for name in _KEPT_ENV if name in saved_env})
    os.environ.update(vendor_telemetry_env_values(), HOME=str(home))
    try:
        with (
            patch.object(socket.socket, "connect", _refuse_network),
            patch.object(socket.socket, "connect_ex", _refuse_network),
            patch.object(socket, "getaddrinfo", _refuse_network),
        ):
            yield
    finally:
        os.environ.clear()
        os.environ.update(saved_env)


def golden_runtime(root: Path) -> tuple[Config, RuntimePaths]:
    """Return the capture config and a runtime under ``root`` holding the dummy credentials."""
    config_path = root / "config.yaml"
    config_path.write_text(json.dumps(CONFIG), encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=root / "storage",
        process_env={"MINDROOM_NO_AUTO_INSTALL_TOOLS": "1", **DUMMY_ENV},
    )
    ensure_tool_registry_loaded(runtime_paths)
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    for tool_name, credentials in DUMMY_CREDENTIALS.items():
        credentials_manager.save_credentials(tool_name, credentials)
    return Config.model_validate(CONFIG), runtime_paths


def missing_dependencies(tool_name: str) -> str | None:
    """Return the declared optional dependencies of one registered tool when this environment lacks them."""
    dependencies = TOOL_METADATA[tool_name].dependencies or []
    with optional_dependency_environment():
        installed = check_deps_installed(dependencies)
    return None if installed else ", ".join(dependencies)


def all_enabled_overrides(tool_name: str) -> dict[str, object]:
    """Return the authored overrides that switch on every function one registered tool can turn on."""
    metadata = TOOL_METADATA[tool_name]
    overrides: dict[str, object] = {
        field.name: True
        for field in (*(metadata.config_fields or ()), *(metadata.agent_override_fields or ()))
        if field.type == "boolean" and (field.name == "all" or field.name.startswith("enable_"))
    }
    overrides.update(EXTRA_ENABLING_OVERRIDES.get(tool_name, {}))
    return dict(sorted(overrides.items()))


def build_toolkit(
    tool_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    tool_config_overrides: dict[str, object] | None = None,
) -> Toolkit:
    """Build one registered tool as agent construction does, with its default configuration or authored overrides."""
    deferred_toolkits = ()
    if tool_name == "dynamic_tools":
        # Tool search exists only beside a deferred toolkit, and its own functions do not depend on which one.
        calculator = build_toolkit("calculator", config, runtime_paths)
        deferred_toolkits = (defer_toolkit("calculator", TOOL_METADATA["calculator"].description, calculator),)
    with (
        patch(OFFLINE_CLIENTS[tool_name]) if tool_name in OFFLINE_CLIENTS else nullcontext(),
        patch(REFUSED_REQUESTS[tool_name], side_effect=_refuse_request)
        if tool_name in REFUSED_REQUESTS
        else nullcontext(),
    ):
        toolkit = build_agent_toolkit(
            tool_name,
            agent_name=AGENT_NAME,
            config=config,
            runtime_paths=runtime_paths,
            worker_tools=[],
            runtime_overrides=None,
            tool_config_overrides=tool_config_overrides,
            execution_identity=None,
            deferred_toolkits=deferred_toolkits,
        )
    if toolkit is None:
        msg = f"The golden config does not enable tool {tool_name!r}"
        raise ValueError(msg)
    return toolkit


def normalize_version(value: object) -> object:
    """Replace the running MindRoom version, which the update_awareness instructions name, by its placeholder."""
    return json.loads(json.dumps(value).replace(_MINDROOM_VERSION, MINDROOM_VERSION_PLACEHOLDER))


def normalize_error_text(text: str) -> str:
    """Replace the pydantic version in error links, which changes with every pydantic upgrade."""
    return _PYDANTIC_ERROR_URL.sub(_PYDANTIC_ERROR_URL_PLACEHOLDER, text)
