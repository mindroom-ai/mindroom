## Summary

`trello_tools` repeats the standard MindRoom Agno toolkit wrapper behavior used by many single-tool modules: a metadata-decorated function performs a lazy import of one `agno.tools.*` toolkit class and returns that class unchanged.
This is real structural duplication, but it is intentionally shallow and tied to per-tool metadata, so no refactor is recommended for this module alone.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
trello_tools	function	lines 57-61	related-only	trello_tools; TrelloTools; def *_tools; from agno.tools.* import *Tools; return *Tools	src/mindroom/tools/linear.py:44-48; src/mindroom/tools/clickup.py:45-49; src/mindroom/tools/todoist.py:43-47; src/mindroom/tools/jira.py:98-102; src/mindroom/tools/__init__.py:125
```

## Findings

### Repeated lazy Agno toolkit class factory pattern

- `src/mindroom/tools/trello.py:57-61` defines `trello_tools`, imports `TrelloTools` inside the function, and returns the class unchanged.
- `src/mindroom/tools/linear.py:44-48` does the same for `LinearTools`.
- `src/mindroom/tools/clickup.py:45-49` does the same for `ClickUpTools`.
- `src/mindroom/tools/todoist.py:43-47` does the same for `TodoistTools`.
- `src/mindroom/tools/jira.py:98-102` does the same for `JiraTools`.

The shared behavior is not Trello-specific API logic.
Each function is a registry-facing factory that delays importing an optional Agno toolkit dependency until the tool is selected, then returns the toolkit class for later instantiation by the tool system.

Differences to preserve:

- Each module has distinct metadata in `register_tool_with_metadata`, including config fields, dependencies, docs URL, category, icon, and function names.
- The imported Agno toolkit class differs per module.
- The local import is likely intentional because many tool dependencies are optional.

## Proposed Generalization

No refactor recommended for this module alone.

A possible helper such as `make_lazy_toolkit_factory("agno.tools.trello", "TrelloTools")` would reduce a few lines per module but would make the typed return signatures and simple import behavior less explicit.
Because the meaningful content in these modules is their metadata, the duplicated factory body is too small to justify a shared abstraction without a larger generated-tool-registry effort.

## Risk/Tests

No production code was changed.

If this pattern were generalized later, tests should cover:

- Tool registration still exposes `trello` metadata through the registry.
- Optional dependency import remains lazy and does not import `agno.tools.trello` during module import.
- The `trello_tools()` callable still returns the exact `TrelloTools` class.
- Metadata export continues to include Trello function names and config fields unchanged.

## Questions Or Assumptions

Assumption: this audit should report duplication only and should not edit production code, as requested.
