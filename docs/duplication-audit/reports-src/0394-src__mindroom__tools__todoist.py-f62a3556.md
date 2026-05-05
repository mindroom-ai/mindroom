## Summary

Top duplication candidate: `todoist_tools` follows the same registered lazy Agno toolkit wrapper pattern used by many modules in `src/mindroom/tools`.
This is real structural duplication, but the primary file has only one tiny behavior symbol and the duplication appears to be an intentional registry convention, so no refactor is recommended from this file alone.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
todoist_tools	function	lines 43-47	duplicate-found	todoist_tools; "from agno.tools.todoist import TodoistTools"; "def .*_tools() -> type"; register_tool_with_metadata lazy import wrappers	src/mindroom/tools/linear.py:44; src/mindroom/tools/trello.py:57; src/mindroom/tools/clickup.py:45; src/mindroom/tools/__init__.py:123
```

## Findings

### Registered lazy toolkit wrapper pattern

- Primary behavior: `src/mindroom/tools/todoist.py:43` defines `todoist_tools`, imports `TodoistTools` inside the function, and returns the toolkit class.
- Matching behavior exists in `src/mindroom/tools/linear.py:44`, `src/mindroom/tools/trello.py:57`, and `src/mindroom/tools/clickup.py:45`.
- These functions all serve the same runtime purpose: expose a metadata-registered tool factory while deferring the Agno toolkit import until the tool is loaded.
- The differences to preserve are tool-specific metadata, toolkit import path, return type, docstring wording, dependencies, config fields, and function names.
- `src/mindroom/tools/__init__.py:123` imports `todoist_tools` into the package-level registry surface, matching the broader one-function-per-tool module convention.

This is functional duplication, but it is also the codebase's current registration style for tool modules.
Because each wrapper is only a two-line lazy import and return, extracting only this function body would likely reduce little code while making imports and type annotations less explicit.

## Proposed generalization

No refactor recommended for `src/mindroom/tools/todoist.py` alone.

If the project later chooses to centralize the repeated pattern across many tool modules, the minimal direction would be a small metadata helper or code-generation step that creates lazy toolkit loader functions from an import path and class name.
That should be evaluated across the whole tool registry, not introduced for Todoist alone.

## Risk/tests

- Risk: A shared loader helper could obscure static typing and make optional dependency failures less obvious unless carefully typed and tested.
- Risk: Changing the tool registration pattern could affect metadata discovery and package-level exports in `src/mindroom/tools/__init__.py`.
- Tests to consider for any future refactor: metadata registration discovery for `todoist`, lazy import behavior when `todoist-api-python` is absent, and successful toolkit class resolution when the optional dependency is installed.
