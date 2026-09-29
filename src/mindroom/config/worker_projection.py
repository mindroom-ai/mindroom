"""The part of the authored config that sandbox runners and dedicated workers receive.

Worker code runs in the process that holds this data, so it is built by allowlist:
a runner resolves the requesting agent, its execution scope and workspace, its
file access, knowledge paths, and plugin tools from these fields and nothing else.
Models, MCP servers, plugin settings, tool overrides, memory provider settings,
Git sources, prompts, rooms, teams, access policy, and every other section stay
in the primary runtime.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from pydantic import ValidationError

from mindroom.config.models import ToolConfigEntry

_DEFAULTS_FIELDS = (
    "file_access",
    "worker_scope",
    "worker_grantable_credentials",
    "tool_output_auto_save_threshold_bytes",
)
_AGENT_FIELDS = (
    "display_name",
    "include_default_tools",
    "memory_backend",
    "knowledge_bases",
    "worker_scope",
    "file_access",
    "delegate_to",
)
_PRIVATE_FIELDS = ("per", "root", "template_dir")
_PRIVATE_KNOWLEDGE_FIELDS = ("enabled", "path")
_KNOWLEDGE_BASE_FIELDS = ("path",)
_PLUGIN_FIELDS = ("path", "enabled")
_MEMORY_FIELDS = ("backend",)


def _mapping(value: object) -> Mapping[str, object] | None:
    return cast("Mapping[str, object]", value) if isinstance(value, Mapping) else None


def _fields(data: Mapping[str, object], names: tuple[str, ...]) -> dict[str, object]:
    return {name: data[name] for name in names if name in data}


def _tool_name(entry: object) -> str:
    try:
        return ToolConfigEntry.model_validate(entry).name
    except ValidationError:
        # The validation error would repeat the entry, including any credential in its overrides.
        msg = "Tool entries must be tool names or single-key mappings"
        raise ValueError(msg) from None


def _with_tool_names(projected: dict[str, object], data: Mapping[str, object]) -> dict[str, object]:
    """Keep only tool names, because authored tool overrides can hold credentials."""
    raw_tools = data.get("tools")
    if isinstance(raw_tools, list):
        projected["tools"] = [_tool_name(entry) for entry in raw_tools]
    return projected


def _agent(data: Mapping[str, object]) -> dict[str, object]:
    agent = _with_tool_names(_fields(data, _AGENT_FIELDS), data)
    raw_private = _mapping(data.get("private"))
    if raw_private is not None:
        private = _fields(raw_private, _PRIVATE_FIELDS)
        raw_knowledge = _mapping(raw_private.get("knowledge"))
        if raw_knowledge is not None:
            private["knowledge"] = _fields(raw_knowledge, _PRIVATE_KNOWLEDGE_FIELDS)
        agent["private"] = private
    return agent


def _plugin(entry: object) -> object | None:
    """Keep a plugin's path, which a string entry is by itself, and drop its settings and hooks."""
    if isinstance(entry, str):
        return entry
    mapping = _mapping(entry)
    return _fields(mapping, _PLUGIN_FIELDS) if mapping is not None else None


def worker_config_data(config_data: Mapping[str, object]) -> dict[str, object]:
    """Return the allowlisted part of authored config data that runners and workers resolve."""
    projected: dict[str, object] = {}
    defaults = _mapping(config_data.get("defaults"))
    if defaults is not None:
        projected["defaults"] = _with_tool_names(_fields(defaults, _DEFAULTS_FIELDS), defaults)
    agents = _mapping(config_data.get("agents"))
    if agents is not None:
        projected["agents"] = {
            name: _agent(agent) for name, raw_agent in agents.items() if (agent := _mapping(raw_agent)) is not None
        }
    raw_plugins = config_data.get("plugins")
    if isinstance(raw_plugins, list):
        projected["plugins"] = [plugin for entry in raw_plugins if (plugin := _plugin(entry)) is not None]
    memory = _mapping(config_data.get("memory"))
    if memory is not None:
        projected["memory"] = _fields(memory, _MEMORY_FIELDS)
    knowledge_bases = _mapping(config_data.get("knowledge_bases"))
    if knowledge_bases is not None:
        projected["knowledge_bases"] = {
            base_id: _fields(base, _KNOWLEDGE_BASE_FIELDS)
            for base_id, raw_base in knowledge_bases.items()
            if (base := _mapping(raw_base)) is not None
        }
    return projected
