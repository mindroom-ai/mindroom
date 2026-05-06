Summary: The only duplicated behavior in `src/mindroom/tools/zendesk.py` is the standard tool-module factory pattern: a registered `*_tools()` function lazily imports an Agno toolkit class and returns it.
No Zendesk-specific customer-support or help-center search behavior is duplicated elsewhere under `src`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
zendesk_tools	function	lines 63-67	duplicate-found	zendesk_tools ZendeskTools search_zendesk enable_search_zendesk; lazy Agno toolkit factories returning *Tools	src/mindroom/tools/airflow.py:56; src/mindroom/tools/confluence.py:77; src/mindroom/tools/github.py:86; src/mindroom/tools/__init__.py:272
```

## Findings

### Repeated lazy Agno toolkit factory wrapper

`src/mindroom/tools/zendesk.py:63` defines `zendesk_tools()`, imports `ZendeskTools` inside the function at `src/mindroom/tools/zendesk.py:65`, and returns the toolkit class at `src/mindroom/tools/zendesk.py:67`.
The same behavior appears in other tool modules:

- `src/mindroom/tools/airflow.py:56` imports `AirflowTools` inside `airflow_tools()` at `src/mindroom/tools/airflow.py:58` and returns it at `src/mindroom/tools/airflow.py:60`.
- `src/mindroom/tools/confluence.py:77` imports `ConfluenceTools` inside `confluence_tools()` at `src/mindroom/tools/confluence.py:79` and returns it at `src/mindroom/tools/confluence.py:81`.
- `src/mindroom/tools/github.py:86` imports `GithubTools` inside `github_tools()` at `src/mindroom/tools/github.py:88` and returns it at `src/mindroom/tools/github.py:90`.
- `src/mindroom/tools/__init__.py:272` uses the same pattern for `_openclaw_compat_tools()`, importing `Toolkit` at `src/mindroom/tools/__init__.py:274` and returning it at `src/mindroom/tools/__init__.py:276`.

The shared behavior is a registry entrypoint that delays importing optional Agno toolkit modules until the tool is actually resolved.
The differences to preserve are the concrete import path, returned class, function name, docstring, and decorator metadata for each tool.

I also searched for Zendesk-specific terms (`ZendeskTools`, `search_zendesk`, `enable_search_zendesk`, `help center`, and customer support wording).
Outside generated metadata in `src/mindroom/tools_metadata.json`, no other source module duplicates Zendesk help-center search configuration or behavior.

## Proposed Generalization

No refactor recommended for this file.
Although the lazy factory shape is duplicated, each function is very small and carries type annotations plus decorator-bound metadata.
A helper would likely obscure the registry surface or require dynamic imports while saving only two executable lines per module.

## Risk/Tests

The main risk of changing this pattern would be breaking tool registration, optional dependency import timing, or static type checking for toolkit return types.
If a future broad refactor centralizes these factories, tests should verify that `zendesk` remains registered, `get_tool_by_name("zendesk", ...)` still resolves `ZendeskTools`, optional dependencies are not imported during package import, and `function_names` still exposes `search_zendesk`.
