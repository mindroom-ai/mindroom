"""Capture what Agno 3.0.9 sends providers for every registered tool, and how it handles tool-call arguments.

Run `.venv/bin/python -m tests.agno_parity.capture_tools` from the repository root to rewrite
`tests/fixtures/agno_parity/tools/`; `tests/fixtures/agno_parity/README.md` describes the fixtures.
Deleted together with Agno.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import sys
import tempfile
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from agno.agent import Agent
from agno.agent._tools import parse_tools
from agno.models.message import Message
from agno.models.openai import OpenAIChat
from agno.tools import Toolkit
from agno.tools.function import Function
from packaging.version import Version

from mindroom.agents import build_agent_toolkit
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.tool_system.catalog import TOOL_METADATA, ensure_tool_registry_loaded
from mindroom.tool_system.dependencies import check_deps_installed, optional_dependency_environment
from mindroom.tool_system.dynamic_toolkits import defer_toolkit
from mindroom.tool_system.registry_state import BUILTIN_TOOL_METADATA
from mindroom.vendor_telemetry import vendor_telemetry_env_values
from tests.agno_parity import tool_argument_probes as probes

if TYPE_CHECKING:
    from collections.abc import Iterator

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
}
# Registered tools that no offline capture can build, with the reason.
UNBUILDABLE = {
    "apify": "its functions come from Apify Actor builds, which only the service returns",
    "composio": "its functions come from the Composio action catalog, which only the service returns",
}
# Overrides beyond the `all` and `enable_*` switches that a tool needs before it registers every function it can.
EXTRA_ENABLING_OVERRIDES: dict[str, dict[str, object]] = {
    # custom_scrape_website exists only with a query to run.
    "agentql": {"agentql_query": "{ title }"},
    "google_drive": {"download_file": True},
    "usage_stats": {"admin_scope": True},
}
# Declared function names (`ToolMetadata.function_names`) that no configuration registers, by tool.
NEVER_REGISTERED_FUNCTIONS = {
    # Methods of async variants, which Agno registers under the name of their sync function.
    "brandfetch": ("asearch_by_brand", "asearch_by_identifier"),
    "browserbase": ("aclose_session", "aget_page_content", "anavigate_to", "ascreenshot"),
    # Helper methods and attributes that the toolkit never registers as functions.
    "discord": ("get_tool_config", "get_tool_description", "get_tool_name"),
    "duckdb": ("get_table_name_from_path",),
    "fal": ("on_queue_update",),
    "moviepy_video_tools": ("create_caption_clips", "parse_srt", "split_text_into_lines"),
    "newspaper": ("get_article_data",),
    "sql": ("run_sql",),
    "zoom": ("instructions",),
    # Registered only beside a knowledge base object, which the text `knowledge` config field cannot hold.
    "website": ("add_website_to_knowledge",),
    "wikipedia": ("search_wikipedia_and_update_knowledge_base",),
}

_KEPT_ENV = ("PATH", "LANG", "LC_ALL", "TMPDIR")
# The update_awareness instructions name the running MindRoom version, which changes with every commit.
_MINDROOM_VERSION = str(Version(version("mindroom")))
_MINDROOM_VERSION_PLACEHOLDER = "<MINDROOM_VERSION>"
_PYDANTIC_ERROR_URL = re.compile(r"https://errors\.pydantic\.dev/[0-9.]+/")
_PYDANTIC_ERROR_URL_PLACEHOLDER = "https://errors.pydantic.dev/<PYDANTIC_VERSION>/"


def _refuse_network(*_args: object, **_kwargs: object) -> None:
    msg = "Golden capture refuses network connections"
    raise OSError(msg)


@contextmanager
def isolated_process(root: Path) -> Iterator[None]:
    """Hide the host environment and refuse connections, so a capture is deterministic and contacts no service."""
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
    with patch(OFFLINE_CLIENTS[tool_name]) if tool_name in OFFLINE_CLIENTS else nullcontext():
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


def _golden_model() -> OpenAIChat:
    return OpenAIChat(id="golden-model", api_key="golden-capture")


def prepare_functions(toolkit: Toolkit) -> tuple[list[Function], list[str]]:
    """Run Agno's per-run preparation for an async agent run; return the functions and the tool instructions."""
    model = _golden_model()
    agent = Agent(model=model)
    prepared = parse_tools(agent, tools=[toolkit], model=model, async_mode=True)
    return [function for function in prepared if isinstance(function, Function)], list(agent._tool_instructions or ())


def tool_schema(toolkit: Toolkit) -> dict[str, object]:
    """Return the golden entry of one built toolkit."""
    functions, tool_instructions = prepare_functions(toolkit)
    added = toolkit.add_instructions and toolkit.instructions is not None
    if tool_instructions != ([toolkit.instructions] if added else []):
        msg = f"{toolkit.name}: Agno adds instructions the golden does not record, such as function instructions"
        raise ValueError(msg)
    entry = {
        "instructions": toolkit.instructions,
        "add_instructions": toolkit.add_instructions,
        "functions": [
            {
                "name": function.name,
                "description": function.description,
                "parameters": function.parameters,
                "strict": function.strict,
                "requires_confirmation": function.requires_confirmation,
                "external_execution": function.external_execution,
                "approval_type": function.approval_type,
                "stop_after_tool_call": function.stop_after_tool_call,
                "show_result": function.show_result,
            }
            for function in functions
        ],
    }
    return json.loads(json.dumps(entry).replace(_MINDROOM_VERSION, _MINDROOM_VERSION_PLACEHOLDER))


def all_enabled_tool_schema(tool_name: str, config: Config, runtime_paths: RuntimePaths) -> dict[str, object]:
    """Return the all-enabled golden entry of one registered tool: its overrides, then its schema with them."""
    overrides = all_enabled_overrides(tool_name)
    return {"overrides": overrides, **tool_schema(build_toolkit(tool_name, config, runtime_paths, overrides))}


@dataclass(frozen=True)
class ToolSchemaCapture:
    """The golden entries of every buildable registered tool, and why each other tool was skipped."""

    schemas: dict[str, dict[str, object]]
    all_enabled_schemas: dict[str, dict[str, object]]
    skipped: dict[str, str]


def capture_tool_schemas(root: Path) -> ToolSchemaCapture:
    """Build every registered tool with its default configuration and, when it has switches, with all of them on."""
    capture = ToolSchemaCapture(schemas={}, all_enabled_schemas={}, skipped={})
    with isolated_process(root):
        config, runtime_paths = golden_runtime(root)
        for tool_name in sorted(BUILTIN_TOOL_METADATA):
            if tool_name in UNBUILDABLE:
                capture.skipped[tool_name] = UNBUILDABLE[tool_name]
            elif (dependencies := missing_dependencies(tool_name)) is not None:
                capture.skipped[tool_name] = f"missing optional dependencies: {dependencies}"
            else:
                capture.schemas[tool_name] = tool_schema(build_toolkit(tool_name, config, runtime_paths))
                if all_enabled_overrides(tool_name):
                    capture.all_enabled_schemas[tool_name] = all_enabled_tool_schema(tool_name, config, runtime_paths)
    return capture


@dataclass(frozen=True)
class ArgumentCase:
    """One tool call a model sends: the probe function it names and its raw arguments string."""

    function: str
    raw_arguments: str


_SCALARS = "probe_scalars"
_OPTIONAL = "probe_optional"
_COLLECTIONS = "probe_collections"
_MODEL = "probe_model"
_AGENT = "probe_agent"
ARGUMENT_CASES = {
    "scalars_native_types": ArgumentCase(_SCALARS, '{"count": 3, "ratio": 2.5, "enabled": true, "label": "x"}'),
    "scalars_string_numbers": ArgumentCase(_SCALARS, '{"count": "3", "ratio": "2.5", "enabled": true, "label": "7"}'),
    "scalars_padded_true_string": ArgumentCase(
        _SCALARS,
        '{"count": 1, "ratio": 1, "enabled": " TRUE ", "label": "x"}',
    ),
    "scalars_false_string_for_text": ArgumentCase(
        _SCALARS,
        '{"count": 1, "ratio": 1.0, "enabled": false, "label": "false"}',
    ),
    "scalars_yes_string_for_flag": ArgumentCase(_SCALARS, '{"count": 1, "ratio": 1.0, "enabled": "yes", "label": "x"}'),
    "scalars_null_string_for_number": ArgumentCase(
        _SCALARS,
        '{"count": "null", "ratio": 1.0, "enabled": true, "label": "x"}',
    ),
    "scalars_integral_float_for_int": ArgumentCase(
        _SCALARS,
        '{"count": 3.0, "ratio": 2, "enabled": true, "label": "x"}',
    ),
    "scalars_fractional_float_for_int": ArgumentCase(
        _SCALARS,
        '{"count": 3.5, "ratio": 2, "enabled": true, "label": "x"}',
    ),
    "scalars_decimal_string_for_int": ArgumentCase(
        _SCALARS,
        '{"count": "3.5", "ratio": 2, "enabled": true, "label": "x"}',
    ),
    "scalars_missing_required": ArgumentCase(_SCALARS, '{"count": 1}'),
    "scalars_unknown_argument": ArgumentCase(
        _SCALARS,
        '{"count": 1, "ratio": 1.0, "enabled": true, "label": "x", "extra": 1}',
    ),
    "scalars_python_literal": ArgumentCase(_SCALARS, "{'count': 3, 'ratio': 2.5, 'enabled': True, 'label': 'x'}"),
    "scalars_invalid_json": ArgumentCase(_SCALARS, '{"count": 3'),
    "scalars_json_array": ArgumentCase(_SCALARS, "[1, 2]"),
    "scalars_json_string": ArgumentCase(_SCALARS, '"count"'),
    "scalars_json_null": ArgumentCase(_SCALARS, "null"),
    "scalars_empty_array": ArgumentCase(_SCALARS, "[]"),
    "optional_omitted": ArgumentCase(_OPTIONAL, "{}"),
    "optional_none_strings": ArgumentCase(_OPTIONAL, '{"note": "None", "limit": "none", "flag": "False"}'),
    "optional_values": ArgumentCase(_OPTIONAL, '{"note": "hi", "limit": "4", "flag": "true"}'),
    "optional_empty_arguments": ArgumentCase(_OPTIONAL, ""),
    "collections_coerced_values": ArgumentCase(_COLLECTIONS, '{"tags": ["a", "b"], "weights": {"x": "1.5", "y": 2}}'),
    "collections_nested_strings_kept": ArgumentCase(_COLLECTIONS, '{"tags": ["true", "null"], "weights": {}}'),
    "collections_wrong_item_type": ArgumentCase(_COLLECTIONS, '{"tags": [1], "weights": {}}'),
    "collections_json_encoded_list": ArgumentCase(_COLLECTIONS, '{"tags": "[\\"a\\"]", "weights": {}}'),
    "choice_valid": ArgumentCase("probe_choice", '{"mode": "fast"}'),
    "choice_invalid": ArgumentCase("probe_choice", '{"mode": "medium"}'),
    "model_from_object": ArgumentCase(_MODEL, '{"query_filter": {"field": "name", "limit": "5"}}'),
    "model_default_field": ArgumentCase(_MODEL, '{"query_filter": {"field": "name"}}'),
    "model_invalid_fields": ArgumentCase(_MODEL, '{"query_filter": {"limit": "many"}}'),
    "model_from_json_string": ArgumentCase(_MODEL, '{"query_filter": "{\\"field\\": \\"name\\"}"}'),
    "no_arguments_empty_object": ArgumentCase("probe_no_arguments", "{}"),
    "no_arguments_empty_array": ArgumentCase("probe_no_arguments", "[]"),
    "no_arguments_unknown_argument": ArgumentCase("probe_no_arguments", '{"extra": 1}'),
    "async_string_number": ArgumentCase("probe_async", '{"count": "4"}'),
    "async_invalid_number": ArgumentCase("probe_async", '{"count": "four"}'),
    "agent_parameter_skips_coercion": ArgumentCase(_AGENT, '{"query": 3}'),
    "agent_parameter_drops_model_value": ArgumentCase(_AGENT, '{"query": "x", "agent": "spoof"}'),
    "agent_parameter_unknown_argument": ArgumentCase(_AGENT, '{"query": "x", "extra": 1}'),
    "agent_parameter_missing_required": ArgumentCase(_AGENT, "{}"),
    "tool_raises": ArgumentCase("probe_raises", '{"message": "boom"}'),
    "unknown_function": ArgumentCase("probe_missing", "{}"),
}


def normalize_error_text(text: str) -> str:
    """Replace the pydantic version in error links, which changes with every pydantic upgrade."""
    return _PYDANTIC_ERROR_URL.sub(_PYDANTIC_ERROR_URL_PLACEHOLDER, text)


def prepare_probe_functions() -> dict[str, Function]:
    """Return the probe tools by name, prepared as Agno prepares an async agent run's tools."""
    functions, _instructions = prepare_functions(Toolkit(name="probes", tools=list(probes.PROBES)))
    return {function.name: function for function in functions}


async def capture_argument_case(case: ArgumentCase, functions: dict[str, Function]) -> dict[str, object]:
    """Resolve and execute one tool call as Agno's model loop does; return what the probe body or the model saw."""
    model = _golden_model()
    outcome: dict[str, object] = {"function": case.function, "raw_arguments": case.raw_arguments}
    tool_call = {
        "id": "call_golden",
        "type": "function",
        "function": {"name": case.function, "arguments": case.raw_arguments},
    }
    rejected: list[Message] = []
    function_calls = model.get_function_calls_to_run(
        Message(role="assistant", tool_calls=[tool_call]),
        rejected,
        functions,
    )
    if rejected:
        [message] = rejected
        return {**outcome, "error_text": normalize_error_text(str(message.content))}
    results: list[Message] = []
    async for _event in model.arun_function_calls(function_calls, results):
        pass
    [result] = results
    # The tool message's arguments, which history keeps, after Agno drops values the model sent for injected names.
    outcome["arguments"] = result.tool_args
    if result.tool_call_error:
        return {**outcome, "error_text": normalize_error_text(str(result.content))}
    return {**outcome, "kwargs": json.loads(str(result.content))}


def capture_argument_cases() -> dict[str, dict[str, object]]:
    """Return the golden outcome of every argument case."""
    functions = prepare_probe_functions()

    async def capture_all() -> dict[str, dict[str, object]]:
        return {case_id: await capture_argument_case(case, functions) for case_id, case in ARGUMENT_CASES.items()}

    return asyncio.run(capture_all())


def write_golden(path: Path, value: object, *, sort_keys: bool) -> None:
    """Write one fixture as pretty JSON, with its keys sorted or in the order they were built."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=sort_keys, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    """Rewrite the tool fixtures and report the tools left out."""
    with tempfile.TemporaryDirectory(prefix="tool-golden-") as root:
        capture = capture_tool_schemas(Path(root).resolve())
    # Schemas keep the key order providers receive, such as parameters in signature order.
    write_golden(SCHEMAS_PATH, capture.schemas, sort_keys=False)
    write_golden(ALL_ENABLED_SCHEMAS_PATH, capture.all_enabled_schemas, sort_keys=False)
    write_golden(ARGUMENTS_PATH, capture_argument_cases(), sort_keys=True)
    for tool_name, reason in capture.skipped.items():
        sys.stdout.write(f"skipped {tool_name}: {reason}\n")


if __name__ == "__main__":
    main()
