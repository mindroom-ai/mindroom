## Summary

The only behavior symbol in `src/mindroom/tools/jira.py` is `jira_tools`, a lazy factory that imports and returns Agno's `JiraTools` class after metadata registration.
This pattern is duplicated across many `src/mindroom/tools/*` modules, including closer issue/project-management integrations such as Linear, ClickUp, Trello, and GitHub.
No Jira-specific duplicate implementation exists elsewhere under `./src`; the shared behavior is the repeated "registered wrapper function returns external toolkit class" pattern.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
jira_tools	function	lines 98-102	duplicate-found	"jira_tools", "JiraTools", "def *_tools()", "Return * tools", issue tracking/project management tool factories	src/mindroom/tools/linear.py:44-48; src/mindroom/tools/clickup.py:45-49; src/mindroom/tools/trello.py:57-61; src/mindroom/tools/github.py:86-90; src/mindroom/tools/__init__.py:80,200; src/mindroom/tools/jira.py:98-102
```

## Findings

1. Repeated lazy Agno toolkit factory pattern.
   `jira_tools` in `src/mindroom/tools/jira.py:98-102` imports `JiraTools` inside the function and returns the class.
   The same behavior appears in `src/mindroom/tools/linear.py:44-48`, `src/mindroom/tools/clickup.py:45-49`, `src/mindroom/tools/trello.py:57-61`, and `src/mindroom/tools/github.py:86-90`, with only the external Agno module/class and docstring differing.
   A wider search found the same three-line lazy factory pattern in many other tool modules, so this is an active repeated convention rather than an isolated copy.

2. Related but not identical issue/project management metadata.
   Jira and Linear both describe "Issue tracking and project management" at `src/mindroom/tools/jira.py:16` and `src/mindroom/tools/linear.py:16`, and both register development-category API-key tools.
   GitHub overlaps on issue operations, and ClickUp/Trello overlap on project/task management, but each module has service-specific config fields, dependencies, function names, docs URLs, and icons.
   This metadata should remain explicit unless a broader tool metadata generation scheme is introduced.

## Proposed Generalization

No immediate refactor recommended for `jira_tools` alone.
The duplicated behavior is small, readable, and matches the existing tool registration convention.

If the project decides to deduplicate this pattern broadly, the minimal helper would be a small lazy import factory builder in `src/mindroom/tool_system/metadata.py` or a new focused `src/mindroom/tool_system/toolkit_factory.py`.
It would accept a module path and class name and return a registered callable, while preserving each module's explicit metadata declarations.
That broader change would need to update many tool modules together and is not justified by this single Jira audit.

## Risk/tests

The main risk in abstracting the factory is changing import timing.
These functions currently defer optional Agno toolkit imports until the tool is selected, which matters for optional dependencies.
Any refactor should verify tool registry loading and optional dependency behavior, especially for tools whose external packages are not installed.

Suggested tests if refactored:

- Existing tool metadata export/registry tests for `jira`.
- A focused test that importing `mindroom.tools.jira` does not import `agno.tools.jira` eagerly.
- A focused test that calling `jira_tools()` returns the same `agno.tools.jira.JiraTools` class.
