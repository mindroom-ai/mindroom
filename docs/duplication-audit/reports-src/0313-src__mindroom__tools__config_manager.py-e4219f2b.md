## Summary

The only meaningful duplication in `src/mindroom/tools/config_manager.py` is the standard tool registration shim pattern repeated across many modules under `src/mindroom/tools`.
This file's behavior is limited to registering metadata and lazily returning `ConfigManagerTools`; there is no duplicated config-manager business logic in this module.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
config_manager_tools	function	lines 34-38	duplicate-found	config_manager_tools; register_tool_with_metadata; def *_tools() -> type[...] lazy custom_tools import	src/mindroom/tools/scheduler.py:13-30; src/mindroom/tools/attachments.py:19-42; src/mindroom/tools/coding.py:20-56; src/mindroom/tools/subagents.py:13-30; src/mindroom/custom_tools/config_manager.py:101-123
```

## Findings

### Repeated tool registration shim pattern

`src/mindroom/tools/config_manager.py:19-38` follows the same behavior as many tool modules: decorate a zero-argument factory with `register_tool_with_metadata`, import the concrete toolkit only under `TYPE_CHECKING` at module import time, lazily import the concrete toolkit inside the factory, and return the toolkit class.

Representative matching candidates:

- `src/mindroom/tools/scheduler.py:13-30` registers metadata, lazily imports `SchedulerTools`, and returns the class.
- `src/mindroom/tools/attachments.py:19-42` registers metadata, includes managed init args, lazily imports `AttachmentTools`, and returns the class.
- `src/mindroom/tools/coding.py:20-56` registers metadata, includes config fields and execution target, lazily imports `CodingTools`, and returns the class.
- `src/mindroom/tools/subagents.py:13-30` registers metadata, lazily imports `SubAgentsTools`, and returns the class.

The shared behavior is real but intentionally structural: each module provides declarative metadata and a lazy class-returning factory so the registry can discover tools without importing every custom toolkit eagerly.
Differences to preserve include each tool's metadata values, optional `config_fields`, optional `managed_init_args`, optional execution target, dependency list, docs URL, and `function_names`.

`src/mindroom/custom_tools/config_manager.py:101-123` was checked as the concrete toolkit implementation.
It contains the actual `ConfigManagerTools` class and does not duplicate the registration shim's behavior.

## Proposed Generalization

No refactor recommended for this file alone.

Although the registration shim pattern is widely repeated, the current module has only five executable lines and the repeated pieces are mostly declarative metadata.
A helper such as `register_lazy_tool(module_path, class_name, metadata)` could reduce boilerplate across all tool modules, but it would trade explicit imports and type-checking clarity for string-based indirection.
That broader refactor should only be considered if many tool registration files are being changed together.

## Risk/tests

If the shim pattern were generalized, tests should cover registry loading, metadata preservation, lazy import behavior, managed init args, and type-visible factory return behavior for at least config-manager plus one simple external tool and one managed-init custom tool.
For this audit, no production code was changed and no tests were run.
