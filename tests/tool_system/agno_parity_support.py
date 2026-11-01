"""Build registered tools the way the tool goldens were captured from Agno 3.0.9, and read them as the goldens do.

`tests/fixtures/agno_parity/README.md` describes the goldens.
The capture used this same setup: one agent of a config shaped like `mindroom config init` output, dummy credentials
for tools that need settings, offline replacements for SDK clients that contact their service while constructing, and
a hidden host environment that refuses network connections.
The parity tests compare what they build with the goldens through `golden_entry` and `argument_outcome`, and
`tests.agno_parity.regenerate_goldens` rewrites the goldens through the same two functions.
`agno_derivation` is Agno's schema derivation, which the owned generator's proof compares with.
"""

from __future__ import annotations

import json
import os
import re
import socket
from contextlib import contextmanager, nullcontext
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import httpx
from agno.tools import Toolkit
from agno.tools import function as agno_function
from agno.tools.function import Function
from agno.utils import json_schema
from packaging.version import Version

from mindroom.agents import build_agent_toolkit
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.dependencies import check_deps_installed, optional_dependency_environment
from mindroom.tool_system.dynamic_toolkits import DeferredToolkit, defer_toolkit
from mindroom.tool_system.invocation import (
    MISSING_TOOL_ERROR_TEXT,
    ToolArgumentError,
    ToolCallContext,
    execute_tool_call,
    parse_tool_arguments,
)
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.sandbox_proxy import tool_placement
from mindroom.vendor_telemetry import vendor_telemetry_env_values
from tests.agno_parity import tool_argument_probes as probes

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from mindroom.tool_system.tool_function import AdaptedToolkit, ToolFunction

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "agno_parity" / "tools"
SCHEMAS_PATH = FIXTURE_DIR / "schemas.json"
ALL_ENABLED_SCHEMAS_PATH = FIXTURE_DIR / "schemas_all_enabled.json"
ARGUMENTS_PATH = FIXTURE_DIR / "arguments.json"

# Registered tools no offline build can produce: their functions come from the Apify Actor builds and the Composio
# action catalog, which only those services return.
UNBUILDABLE = frozenset({"apify", "composio"})
AGENT_NAME = "golden"
# Tools are built for one agent of a config shaped like `mindroom config init` output: file memory gives the agent a
# workspace, so output-file wrapping applies; delegation, the knowledge graph, agent creation and contacts enable the
# tools that need them, and an empty worker tool list builds every tool in this process.
CONFIG = {
    "models": {"default": {"provider": "openai", "id": "golden-model"}},
    "memory": {"backend": "file"},
    "knowledge_graph": {"model": "default"},
    "agents": {
        AGENT_NAME: {
            "display_name": "Golden",
            "role": "Captures tool schemas.",
            "delegate_to": ["helper"],
            "created_agents_per_owner": 1,
            "contacts": {},
            "knowledge_graph": True,
            "knowledge_graph_edits": True,
            "worker_tools": [],
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
    "sql": {"db_url": "sqlite://"},
    "tavily": {"api_key": _KEY},
    # The client rejects bot tokens without a colon.
    "telegram": {"token": "0:golden-dummy", "chat_id": _KEY},
    "todoist": {"api_token": _KEY},
    "webex": {"access_token": _KEY},
    "whatsapp": {"access_token": _KEY, "phone_number_id": _KEY},
    "zep": {"api_key": _KEY},
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
        process_env={"MINDROOM_NO_AUTO_INSTALL_TOOLS": "1"},
    )
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    for tool_name, credentials in DUMMY_CREDENTIALS.items():
        credentials_manager.save_credentials(tool_name, credentials)
    return Config.model_validate(CONFIG), runtime_paths


def missing_dependencies(tool_name: str) -> str | None:
    """Return the declared optional dependencies of one registered tool when this environment lacks them."""
    dependencies = builtin_tool_registry().entries[tool_name].dependencies or []
    with optional_dependency_environment():
        installed = check_deps_installed(dependencies)
    return None if installed else ", ".join(dependencies)


def all_enabled_overrides(tool_name: str) -> dict[str, object]:
    """Return the authored overrides that switch on every function one registered tool can turn on."""
    metadata = builtin_tool_registry().entries[tool_name]
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
    deferred_toolkits = None
    if tool_name == "dynamic_tools":
        # Tool search exists only beside a deferred toolkit, and its own functions do not depend on which one.
        calculator = build_toolkit("calculator", config, runtime_paths)
        deferral, deferred = defer_toolkit(
            "calculator",
            builtin_tool_registry().entries["calculator"].description,
            adapt_toolkit(calculator),
            portable_search=True,
        )
        searched = (DeferredToolkit(deferral, deferred.functions),)

        def deferred_toolkits() -> tuple[DeferredToolkit, ...]:
            return searched

    with (
        patch(OFFLINE_CLIENTS[tool_name]) if tool_name in OFFLINE_CLIENTS else nullcontext(),
        patch(REFUSED_REQUESTS[tool_name], side_effect=_refuse_request)
        if tool_name in REFUSED_REQUESTS
        else nullcontext(),
    ):
        toolkit = build_agent_toolkit(
            tool_name,
            agent_tools=resolve_agent_tools(config, AGENT_NAME, builtin_tool_registry()),
            placement=tool_placement(runtime_paths, builtin_tool_registry()),
            runtime_paths=runtime_paths,
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


def golden_entry(adapted: AdaptedToolkit) -> dict[str, object]:
    """Return the golden entry of one adapted toolkit: its texts for the model, then each function the adapter made.

    ``instructions`` is what the system prompt shows, and ``guidance`` what the agent CLI and minimal mode show, before
    an agent drops the default ones that name a function it does not offer; the functions are in the order the toolkit
    offers them, which the adapter keeps.
    """
    entry = {
        "instructions": adapted.instructions,
        "guidance": adapted.guidance,
        "functions": [
            {
                "name": function.name,
                "description": function.description,
                "parameters": function.parameters,
                "strict": function.strict,
                "gate": function.gate,
                "stop_after_tool_call": function.stop_after_call,
            }
            for function in adapted.functions
        ],
    }
    normalized = normalize_version(entry)
    assert isinstance(normalized, dict)
    return normalized


def agno_derivation(entrypoint: Callable[..., Any]) -> tuple[str | None, dict[str, Any]]:
    """Return the description and parameter schema Agno's entrypoint processing derives for ``entrypoint``.

    Values annotated ``Any`` or ``object`` take any JSON value, as the owned generator renders them and as
    https://github.com/agno-agi/agno/pull/10423 proposes; Agno 3.0.9 renders an object that admits only ``{}``.
    """
    agno_schema = json_schema.get_json_schema_for_arg

    def unconstrained_any(type_hint: object) -> dict[str, Any] | None:
        return {} if type_hint is Any or type_hint is object else agno_schema(type_hint)

    function = Function(name="derived", entrypoint=entrypoint)
    # Agno caches each callable's derivation, so forget it around the patch: a cached schema would skip the patch,
    # and the patched one would reach later unpatched derivations.
    _forget_agno_derivation(entrypoint)
    try:
        with patch.object(json_schema, "get_json_schema_for_arg", unconstrained_any):
            function.process_entrypoint(strict=False)
    finally:
        _forget_agno_derivation(entrypoint)
    return function.description, function.parameters


def _forget_agno_derivation(entrypoint: Callable[..., Any]) -> None:
    """Drop Agno's cached schema derivations of ``entrypoint``."""
    agno_function._cached_entrypoint_schema.cache_clear()
    if (cache := agno_function._get_lifetime_cache(entrypoint)) is not None:
        cache.clear()


def probe_functions() -> dict[str, ToolFunction]:
    """Adapt the argument probes as one toolkit, keyed by function name."""
    toolkit = Toolkit(name="probes", tools=list(probes.PROBES))
    return {function.name: function for function in adapt_toolkit(toolkit).functions}


def golden_call_context() -> ToolCallContext:
    """Return the call context the argument goldens run under."""
    return ToolCallContext(run_id="run-1", session_id="session-1", user_id="@user:localhost")


async def argument_outcome(case: Mapping[str, Any], functions: Mapping[str, ToolFunction]) -> dict[str, object]:
    """Resolve and run one golden tool call as the owned loop does; return what the probe body or the model saw."""
    outcome: dict[str, object] = {"function": case["function"], "raw_arguments": case["raw_arguments"]}
    function = functions.get(case["function"])
    if function is None:
        return {**outcome, "error_text": MISSING_TOOL_ERROR_TEXT}
    parsed = parse_tool_arguments(case["raw_arguments"], function)
    if isinstance(parsed, ToolArgumentError):
        return {**outcome, "error_text": normalize_error_text(parsed.error_text)}
    outcome["arguments"] = parsed
    result = await execute_tool_call(function, parsed, golden_call_context(), middleware=())
    if result.is_error:
        return {**outcome, "error_text": normalize_error_text(result.content)}
    return {**outcome, "kwargs": json.loads(result.content)}
