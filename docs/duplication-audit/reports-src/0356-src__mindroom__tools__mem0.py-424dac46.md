Summary: The only meaningful duplication in `src/mindroom/tools/mem0.py` is the repeated tool-registry wrapper pattern used across many `src/mindroom/tools/*.py` modules.
`mem0_tools` is especially related to `zep_tools` because both register third-party memory toolkits with nearly identical lazy factory behavior and similar metadata shape.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
mem0_tools	function	lines 105-109	duplicate-found	mem0_tools, Mem0Tools, register_tool_with_metadata memory tools, lazy return toolkit factory	src/mindroom/tools/zep.py:98; src/mindroom/tools/csv.py:91; src/mindroom/tools/calculator.py:27; src/mindroom/tools/website.py:35; src/mindroom/tools/memory.py:17; src/mindroom/custom_tools/memory.py:34
```

## Findings

1. Lazy toolkit factory boilerplate is repeated across registry modules.
`src/mindroom/tools/mem0.py:105` imports `Mem0Tools` inside `mem0_tools()` and returns the toolkit class.
The same behavior appears in `src/mindroom/tools/zep.py:98`, `src/mindroom/tools/csv.py:91`, `src/mindroom/tools/calculator.py:27`, and `src/mindroom/tools/website.py:35`.
These functions all defer optional dependency imports until the registry factory is called, then return the imported Agno toolkit type.
The differences to preserve are the concrete import path, returned class, return annotation, and docstring text.

2. Third-party memory-tool registration has overlapping metadata with Zep, but the overlap is mostly categorical rather than a strong refactor target.
`src/mindroom/tools/mem0.py:13` and `src/mindroom/tools/zep.py:13` both register memory systems as `ToolCategory.PRODUCTIVITY`, `ToolStatus.REQUIRES_CONFIG`, `SetupType.API_KEY`, `Brain` icons, optional ID/API key fields, enable flags, dependencies, docs URLs, and memory function names.
The duplicated behavior is the UI/tool-registry description of a configurable external memory backend.
The differences to preserve are provider-specific field names (`config`, `org_id`, `project_id`, Mem0 enable flags versus Zep `session_id`, `ignore_assistant_messages`, `instructions`), package dependencies, docs URLs, and toolkit function names.

3. MindRoom's native `memory` tool is related but not a duplicate of `mem0_tools`.
`src/mindroom/tools/memory.py:17` registers metadata for the built-in agent memory tool, and `src/mindroom/custom_tools/memory.py:34` implements explicit memory operations.
This overlaps in user-facing memory concepts and icon/category metadata, but it does not duplicate `mem0_tools` behavior because it is context-bound, not a generic third-party Agno toolkit factory.

## Proposed Generalization

A small helper could reduce the repeated lazy factory body across simple tool registry modules, for example a `tool_class_factory(import_path: str, class_name: str)` helper in `src/mindroom/tool_system/metadata.py` or a nearby registry utility.
That would allow simple wrappers to reuse one implementation while preserving per-tool metadata decorators.
However, no refactor is recommended from this file alone because the existing three-line factory pattern is explicit, type-checker-friendly, and repeated consistently across many modules.

For the memory metadata overlap, no refactor is recommended.
The provider-specific configuration fields and function names are the parts most likely to change independently, so a shared memory-tool metadata builder would add indirection without eliminating much active complexity.

## Risk/tests

Changing the lazy factory pattern could affect optional dependency import timing and type annotations across many tools.
Tests would need to cover tool registry import without optional dependencies installed, metadata registration for `mem0` and `zep`, and resolving each factory into the expected toolkit class.
Changing memory metadata construction would need API/UI metadata snapshot coverage for config fields, dependencies, docs URLs, and function names.
