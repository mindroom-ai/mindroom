# Duplication Audit: `src/mindroom/tools/bitbucket.py`

## Summary

No meaningful Bitbucket-specific duplication found.
The only behavior symbol, `bitbucket_tools`, is a tiny lazy loader for the Agno `BitbucketTools` class.
Its structure matches the repository-wide tool registration pattern used by other `src/mindroom/tools/*.py` modules, but extracting that pattern would mostly hide simple, explicit imports and metadata declarations rather than remove duplicated product behavior.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
bitbucket_tools	function	lines 88-92	related-only	"bitbucket_tools", "BitbucketTools", "def .*_tools()", "Return .* tools", "agno.tools.github", "agno.tools.jira", "agno.tools.linear", "agno.tools.trello"	src/mindroom/tools/bitbucket.py:88; src/mindroom/tools/github.py:86; src/mindroom/tools/jira.py:98; src/mindroom/tools/linear.py:44; src/mindroom/tools/trello.py:57; src/mindroom/tools/__init__.py:34
```

## Findings

No real duplication requiring refactor was found for `bitbucket_tools`.

Related registration idiom:

- `src/mindroom/tools/bitbucket.py:88` lazily imports and returns `agno.tools.bitbucket.BitbucketTools`.
- `src/mindroom/tools/github.py:86`, `src/mindroom/tools/jira.py:98`, `src/mindroom/tools/linear.py:44`, and `src/mindroom/tools/trello.py:57` use the same shape: a metadata-decorated factory function that lazily imports and returns one Agno toolkit class.
- `src/mindroom/tools/__init__.py:34` imports `bitbucket_tools` into the central tools registry alongside the other providers.

This is functionally related because each wrapper exposes a toolkit class through MindRoom metadata registration while avoiding runtime imports until the tool provider is used.
It is not a strong duplication candidate because the duplicated body is only two lines, the metadata differs substantially per provider, and the current explicit functions keep type-checking imports and provider docs straightforward.

## Proposed Generalization

No refactor recommended.

A generic helper such as `lazy_agno_toolkit("agno.tools.bitbucket", "BitbucketTools")` could collapse the two-line function bodies across many tool modules, but it would add indirection around typed imports and would not address the larger, intentionally provider-specific metadata blocks.

## Risk/tests

No production code changes were made.

If this pattern were ever generalized, tests should cover tool metadata registration, lazy import behavior when optional Agno dependencies are absent, and registry imports from `src/mindroom/tools/__init__.py`.
