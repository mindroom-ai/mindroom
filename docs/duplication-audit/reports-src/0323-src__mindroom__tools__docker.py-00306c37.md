## Summary

No meaningful Docker-specific duplication found.
`docker_tools` follows the same registered toolkit factory pattern used by many files in `src/mindroom/tools`, but its Docker metadata, dependency, docs URL, and function list are specific to the Agno Docker toolkit.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
docker_tools	function	lines 52-56	related-only	docker_tools; DockerTools; from agno.tools.docker; @register_tool_with_metadata; def *_tools()	src/mindroom/tools/docker.py:52; src/mindroom/tools/calculator.py:27; src/mindroom/tools/coding.py:52; src/mindroom/tools/web_browser_tools.py:42; src/mindroom/tools/wikipedia.py:42
```

## Findings

No real duplication requiring refactor was found for Docker behavior.

Related pattern only:

- `src/mindroom/tools/docker.py:13` registers static tool metadata, and `src/mindroom/tools/docker.py:52` lazily imports and returns the toolkit class.
- `src/mindroom/tools/calculator.py:13` / `src/mindroom/tools/calculator.py:27`, `src/mindroom/tools/web_browser_tools.py:13` / `src/mindroom/tools/web_browser_tools.py:42`, and `src/mindroom/tools/wikipedia.py:13` / `src/mindroom/tools/wikipedia.py:42` use the same module shape.
- The shared behavior is the registry wrapper pattern: a decorated zero-argument function returns a toolkit class after a local import.
- The differences to preserve are the per-tool metadata fields, dependencies, docs URLs, function names, config fields, categories, and toolkit import paths.

This looks like intentional declarative registration rather than harmful duplication.
Collapsing it would likely require a generic tool-spec table or dynamic importer, which would make the current explicit metadata modules harder to audit.

## Proposed Generalization

No refactor recommended.

If the project later decides to bulk-generate simple Agno wrappers, the smallest safe helper would need to live near `src/mindroom/tool_system/metadata.py` or a dedicated `src/mindroom/tools/factory.py`.
That is not justified for this file alone because the current explicit function is only five lines and the decorator payload is unique.

## Risk/tests

No production code changes were made.

If a future refactor generalizes these simple wrappers, tests should cover:

- Registry metadata export for the Docker tool, including `dependencies=["docker"]`, docs URL, and all listed function names.
- Lazy import behavior so optional Docker dependencies are not imported at module import time.
- Tool lookup through the existing registry path after `src/mindroom/tools/__init__.py` imports `docker_tools`.
