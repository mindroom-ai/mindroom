## Summary

The only meaningful duplication in `src/mindroom/tools/thread_tags.py` is the repeated built-in tool registration wrapper pattern.
`thread_tags_tools` has the same metadata-decorated lazy import and toolkit-class return shape as nearby modules such as `thread_summary`, `scheduler`, `matrix_room`, and `matrix_message`.
This is active repetition, but it is small, declarative, and currently preserves per-tool metadata close to each registration.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
thread_tags_tools	function	lines 26-30	duplicate-found	thread_tags_tools register_tool_with_metadata lazy Toolkit factory custom_tools ThreadTagsTools; compared _tools wrappers and Matrix communication tool registrations	src/mindroom/tools/thread_summary.py:13-30, src/mindroom/tools/scheduler.py:13-30, src/mindroom/tools/matrix_room.py:13-30, src/mindroom/tools/matrix_message.py:13-32, src/mindroom/tools/attachments.py:14-39
```

## Findings

### Repeated metadata-decorated toolkit factory

`src/mindroom/tools/thread_tags.py:13-30` registers metadata with `register_tool_with_metadata`, performs a type-checking-only import for the toolkit class, lazily imports the concrete toolkit inside the factory, and returns the toolkit class.
The same behavior shape appears in `src/mindroom/tools/thread_summary.py:13-30`, `src/mindroom/tools/scheduler.py:13-30`, `src/mindroom/tools/matrix_room.py:13-30`, and `src/mindroom/tools/matrix_message.py:13-32`.

The duplication is structural rather than domain-level.
Each module must preserve distinct metadata fields such as `name`, `display_name`, `description`, `category`, `icon`, `icon_color`, and `function_names`.
The lazy import also appears intentional because the registry can import tool configuration modules without importing every optional toolkit implementation at module import time.

## Proposed Generalization

No refactor recommended for this file alone.
If this pattern becomes a maintenance burden across many tool modules, a minimal helper in `mindroom.tool_system.metadata` could generate simple lazy toolkit factories from a dataclass containing metadata and an import path.
That should only be considered if it reduces net complexity without hiding per-tool metadata or weakening type checking.

## Risk/tests

Generalizing this wrapper pattern would risk changing registry import timing, metadata registration side effects, static type visibility for returned toolkit classes, and optional dependency loading behavior.
Tests would need to cover built-in registry discovery, metadata contents for `thread_tags`, lazy import behavior, and successful construction of `ThreadTagsTools`.
