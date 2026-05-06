Summary: No meaningful duplication found.
`src/mindroom/tool_system/catalog.py` is a thin public facade over `tool_system.bootstrap` and `tool_system.metadata`.
The related files checked either consume this facade, define the underlying behavior, or expose different package-level facades with different domains.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MODULE_LEVEL	module	lines 1-57	related-only	ToolCatalog/catalog facade/__all__/export_tools_metadata/get_tool_by_name/ensure_tool_registry_loaded/direct metadata imports	src/mindroom/tool_system/metadata.py:577, src/mindroom/tool_system/metadata.py:1064, src/mindroom/tool_system/metadata.py:1095, src/mindroom/tool_system/metadata.py:1153, src/mindroom/tool_system/bootstrap.py:1, src/mindroom/api/tools.py:28, src/mindroom/api/tools.py:323, src/mindroom/config/main.py:935, src/mindroom/config/main.py:1158, src/mindroom/config/agent.py:284, src/mindroom/tools/__init__.py:11, src/mindroom/knowledge/status.py:1
```

Findings: No real duplication found.
`catalog.py` re-exports selected public metadata and registry APIs from `metadata.py` plus `ensure_tool_registry_loaded` from `bootstrap.py`.
The implementation behavior remains centralized in `metadata.py`, including tool lookup, metadata export, validation snapshot serialization, and authored override handling.
Callers such as `src/mindroom/api/tools.py:28`, `src/mindroom/config/main.py:935`, and `src/mindroom/config/agent.py:284` import from the facade rather than reimplementing the facade behavior.
Direct imports from `mindroom.tool_system.metadata` under `src/mindroom/tools/` are registration-site imports for `register_tool_with_metadata`, `ConfigField`, `SetupType`, `ToolCategory`, and `ToolStatus`; those are related to the same metadata system but are not duplicate catalog behavior.
`src/mindroom/tools/__init__.py:11` is another registry-style module, but it registers and exposes concrete Agno tool factories rather than providing the public tool metadata facade.
`src/mindroom/knowledge/status.py:1` is another small facade pattern, but it wraps knowledge-index status behavior and does not duplicate tool catalog functionality.

Proposed generalization: No refactor recommended.
The current split keeps behavior in `metadata.py`/`bootstrap.py` and provides a stable import surface in `catalog.py`.
Moving registration-site imports or unrelated package facades behind a shared helper would add indirection without removing active duplicated behavior.

Risk/tests: No behavior change is proposed.
If future work changes this facade, targeted tests should cover imports used by config validation, API tool metadata export, sandbox runner validation snapshots, and MCP registry/toolkit paths.
