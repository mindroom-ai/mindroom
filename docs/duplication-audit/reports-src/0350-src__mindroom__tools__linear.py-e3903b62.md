## Summary

`linear_tools` is a small metadata-registered lazy Agno toolkit factory.
The behavior is duplicated broadly across `src/mindroom/tools`, especially in project-management wrappers such as Jira, ClickUp, Todoist, and Trello.
The duplication is intentional enough that no primary-file-specific refactor is recommended, though the wider tool registry could eventually generate these simple wrappers from declarative metadata.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
linear_tools	function	lines 44-48	duplicate-found	linear_tools; LinearTools; register_tool_with_metadata; def *_tools returning imported Agno toolkit; issue tracking project management tools	src/mindroom/tools/linear.py:44; src/mindroom/tools/jira.py:98; src/mindroom/tools/clickup.py:45; src/mindroom/tools/todoist.py:43; src/mindroom/tools/trello.py:57; src/mindroom/tools/wikipedia.py:42; src/mindroom/tools/calculator.py:27
```

## Findings

### 1. Lazy Agno toolkit factory wrappers are repeated across tool modules

- `src/mindroom/tools/linear.py:44` defines `linear_tools`, imports `LinearTools` inside the function, and returns the class.
- `src/mindroom/tools/jira.py:98`, `src/mindroom/tools/clickup.py:45`, `src/mindroom/tools/todoist.py:43`, and `src/mindroom/tools/trello.py:57` use the same behavior for adjacent issue/task/project-management integrations.
- Many simpler integrations also repeat the same pattern, such as `src/mindroom/tools/wikipedia.py:42` and `src/mindroom/tools/calculator.py:27`.

The shared behavior is a registry-facing callable that keeps the Agno import lazy for runtime dependency isolation while exposing a typed toolkit class to MindRoom's metadata registry.
Differences to preserve are the toolkit import path, return type annotation, docstring, and per-tool metadata supplied by `register_tool_with_metadata`.

### 2. API-key metadata shape is repeated, but field semantics differ slightly

- `src/mindroom/tools/linear.py:22` defines a single optional password `ConfigField` named `api_key`.
- `src/mindroom/tools/clickup.py:22` also defines `api_key` as a password field, but makes it required and adds placeholder/description.
- `src/mindroom/tools/trello.py:22` starts with an optional password `api_key`, then adds provider-specific secret/token fields.

The shared behavior is provider credential collection through `ConfigField`.
The differences are meaningful because some upstream toolkits expect different names, required flags, or companion fields.

## Proposed Generalization

No refactor recommended for this primary file.
The duplicate factory body is only three lines and is tied to per-module imports and type annotations, so abstracting it locally would likely reduce readability more than maintenance cost.

If the project later wants to reduce the broader wrapper boilerplate, the minimal safe direction would be:

1. Add a small declarative tool-spec mechanism under `src/mindroom/tool_system/metadata.py` or a neighboring module.
2. Keep explicit per-tool metadata in data objects while generating the lazy import factory.
3. Migrate only the simplest Agno passthrough modules first.
4. Leave custom wrappers and modules with subclassing or runtime behavior unchanged.
5. Verify registry loading, dependency isolation, and metadata rendering tests.

## Risk/tests

The main risk in generalizing this pattern is accidentally eager-importing optional Agno dependencies or losing precise function names and credential metadata.
Tests should cover tool registry import without optional packages installed, metadata returned for `linear`, and toolkit factory resolution for at least one migrated simple wrapper.
