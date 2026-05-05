## Summary

The only behavior symbol in `src/mindroom/tools/confluence.py` is the `confluence_tools` factory.
It duplicates the standard lazy Agno toolkit factory shape used across many `src/mindroom/tools/*` modules: import the toolkit class inside the registered factory and return the class unchanged.
The closest domain-related neighbor is `src/mindroom/tools/jira.py`, which shares Atlassian API-key metadata and the same factory behavior, but its configuration schema and function set are tool-specific.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
confluence_tools	function	lines 77-81	duplicate-found	confluence_tools; agno.tools.confluence; lazy Agno toolkit factory; Return .* tools; Atlassian Jira metadata	src/mindroom/tools/jira.py:98; src/mindroom/tools/bitbucket.py:88; src/mindroom/tools/github.py:86; src/mindroom/tools/zendesk.py:63; src/mindroom/tools/cartesia.py:77; src/mindroom/tool_system/metadata.py:749; src/mindroom/tool_system/registry_state.py:160; src/mindroom/tools/__init__.py:47
```

## Findings

### 1. Repeated registered toolkit factory wrapper

`src/mindroom/tools/confluence.py:77` defines a zero-argument factory that imports `ConfluenceTools` from `agno.tools.confluence` and returns the class.
The same behavior appears in many tool modules, including `src/mindroom/tools/jira.py:98`, `src/mindroom/tools/bitbucket.py:88`, `src/mindroom/tools/github.py:86`, `src/mindroom/tools/zendesk.py:63`, and `src/mindroom/tools/cartesia.py:77`.
The behavior is duplicated because every factory exists only to defer the optional Agno import until the registry asks for the toolkit class.
`register_tool_with_metadata` stores that function as metadata at `src/mindroom/tool_system/metadata.py:749`, and `register_builtin_tool_metadata` installs it into the runtime registry at `src/mindroom/tool_system/registry_state.py:160`.

Differences to preserve:
Each module has a different return type annotation, docstring, import path, and toolkit class name.
Those differences are mostly typing and documentation, while the runtime behavior is the same lazy import-and-return pattern.

### 2. Related Atlassian metadata, but not enough identical behavior to merge

`src/mindroom/tools/confluence.py:13` and `src/mindroom/tools/jira.py:13` both register Atlassian development tools with API-key-style setup, blue brand icons, docs URLs under Agno's `others` toolkit docs, username/password-or-token-style authentication fields, dependencies, and explicit `function_names`.
This is related setup behavior, but it is not a strong duplication candidate for the primary symbol because `confluence_tools` itself only returns the class.
The field names also differ in meaningful ways: Confluence uses `url`, `api_key`, and `verify_ssl`, while Jira uses `server_url`, `token`, feature-enable booleans, and `all`.

Differences to preserve:
The constructor keyword names appear to match the upstream Agno toolkits and should not be normalized without checking those toolkit constructors.
The user-facing labels and enabled-tool flags are specific to each integration.

## Proposed generalization

A small helper could remove the repeated lazy import-and-return bodies across tool modules, for example `lazy_toolkit_factory(module_path: str, class_name: str)` in `mindroom.tool_system.metadata` or a focused `mindroom.tool_system.toolkit_factories` module.
However, no refactor is recommended for `confluence_tools` alone because the current explicit function keeps static return annotations clear, avoids dynamic imports by string, and matches the existing registry convention.

If the project chooses to generalize the broad pattern later, keep it mechanical and verify that optional dependency errors remain deferred until factory invocation.
Do not generalize the Atlassian config fields unless upstream Agno constructor signatures are first confirmed to accept a shared schema.

## Risk/tests

No production code was changed.
If the factory pattern is refactored later, tests should cover built-in tool registration for Confluence and at least one other lazy optional dependency, asserting that importing `mindroom.tools` does not require the optional dependency and that invoking the registered factory returns the expected toolkit class after the dependency is installed.
If any Atlassian metadata is generalized later, tests should cover config-field serialization for Confluence and Jira separately, including field names, defaults, required flags, and `function_names`.
