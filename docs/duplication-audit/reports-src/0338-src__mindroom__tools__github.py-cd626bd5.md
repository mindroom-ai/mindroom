Summary: `github_tools` repeats the repository-wide registered-tool factory pattern: a metadata decorator followed by a tiny function that performs a local Agno toolkit import and returns the toolkit class.
This same behavior appears in many `src/mindroom/tools/*.py` modules, including repository/project-management neighbors such as Bitbucket, Linear, Jira, and Trello.
No GitHub-specific API wrapping or request logic is duplicated elsewhere in `./src`.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
github_tools	function	lines 86-90	duplicate-found	github_tools, GithubTools, repository management, def *_tools returns Agno toolkit class, create_pull_request, list_repositories	src/mindroom/tools/bitbucket.py:88, src/mindroom/tools/linear.py:44, src/mindroom/tools/jira.py:98, src/mindroom/tools/trello.py:57, src/mindroom/tool_system/metadata.py:509, src/mindroom/tools/__init__.py:69
```

Findings:

1. Registered Agno toolkit factory boilerplate is duplicated.
   `src/mindroom/tools/github.py:86` defines `github_tools`, imports `GithubTools` inside the function, and returns the class.
   `src/mindroom/tools/bitbucket.py:88`, `src/mindroom/tools/linear.py:44`, `src/mindroom/tools/jira.py:98`, and `src/mindroom/tools/trello.py:57` use the same behavior with different toolkit classes and metadata.
   The functional shape is the same: metadata registration stores a callable factory, then `src/mindroom/tool_system/metadata.py:509` calls that factory before instantiating the toolkit at `src/mindroom/tool_system/metadata.py:554`.
   Differences to preserve are the provider-specific metadata, config fields, dependency names, docs URLs, and function-name allowlists.

2. GitHub-specific repository/issue management overlaps only conceptually with other integrations.
   Bitbucket exposes repository and pull-request functions at `src/mindroom/tools/bitbucket.py:77`, and Linear/Jira expose issue-management functions at `src/mindroom/tools/linear.py:33` and `src/mindroom/tools/jira.py:96`.
   This is related behavior rather than duplicate implementation because each module delegates to a different Agno toolkit and does not implement the service calls locally.

Proposed generalization:

No refactor recommended for this file.
The duplicate factory body is active and repeated, but it is only three lines per tool and currently keeps imports lazy, typing straightforward, and provider metadata colocated with registration.
A possible future cleanup would be a small helper in `mindroom.tool_system.metadata` that builds a registered class-returning factory from a toolkit import path, but that would touch many tool modules for limited maintenance gain.

Risk/tests:

Changing this pattern could affect registry loading, optional dependency import timing, and type-checking behavior for every tool module.
If generalized later, tests should cover `ensure_tool_registry_loaded`, `get_tool_by_name("github", ...)`, metadata export for GitHub, optional dependency absence, and at least one neighboring tool such as Bitbucket or Linear.
