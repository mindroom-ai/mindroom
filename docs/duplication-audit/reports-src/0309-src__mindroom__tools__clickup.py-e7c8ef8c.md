Summary: No meaningful duplication found.
The `clickup_tools` symbol is a metadata-decorated Agno toolkit factory, and the same registry/factory pattern appears across many files under `src/mindroom/tools`.
Those modules are related boilerplate for registering distinct third-party toolkits, not duplicated ClickUp behavior worth consolidating from this file alone.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
clickup_tools	function	lines 45-49	related-only	clickup_tools, agno.tools.clickup, def *_tools, register_tool_with_metadata, API_KEY productivity task/project tools	src/mindroom/tools/clickup.py:45; src/mindroom/tools/linear.py:44; src/mindroom/tools/todoist.py:43; src/mindroom/tools/trello.py:57; src/mindroom/tools/jira.py:98
```

Findings:
No real duplicated ClickUp behavior was found elsewhere in `./src`.
The closest related code is the repeated tool registration/factory shape in `src/mindroom/tools/linear.py:44`, `src/mindroom/tools/todoist.py:43`, `src/mindroom/tools/trello.py:57`, and `src/mindroom/tools/jira.py:98`.
Each function returns a different Agno toolkit class after a local import, and each decorator carries service-specific metadata, config fields, dependencies, docs URLs, helper text, and function names.
That makes the common behavior limited to intentional registry boilerplate rather than shared business logic or repeated parsing/validation.

Proposed generalization:
No refactor recommended for this primary file.
A generic factory/decorator helper could reduce a few lines per tool module, but it would also hide explicit imports and metadata in a large table or dynamic import path.
For this file alone, the current explicit shape is clearer and lower risk.

Risk/tests:
No production change is proposed.
If a future broad tool-registration refactor is attempted, tests should cover registry metadata export, toolkit class resolution, optional dependency handling, and authored config validation for representative API-key tools including ClickUp.
