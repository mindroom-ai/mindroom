## Summary

The only meaningful duplication candidate in `src/mindroom/tools/reddit.py` is the standard built-in tool registration wrapper pattern.
`reddit_tools` repeats the same behavior used by many adjacent tool modules: a metadata-decorated, zero-argument function performs a lazy import of an Agno toolkit class and returns the class object.
This is real duplication, but it is also the repository's current registration convention, so no refactor is recommended from this file alone.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
reddit_tools	function	lines 79-83	duplicate-found	reddit_tools; RedditTools; decorated *_tools lazy import return Agno toolkit; register_tool_with_metadata	src/mindroom/tools/x.py:96-100; src/mindroom/tools/hackernews.py:49-53; src/mindroom/tools/discord.py:86-90; src/mindroom/tools/slack.py:160-164; src/mindroom/tool_system/metadata.py:749-821
```

## Findings

### 1. Metadata-decorated Agno toolkit class factories are repeated across tool modules

- `src/mindroom/tools/reddit.py:79-83` defines `reddit_tools`, imports `RedditTools` inside the function, and returns the toolkit class.
- `src/mindroom/tools/x.py:96-100` defines `x_tools`, imports `XTools` inside the function, and returns the toolkit class.
- `src/mindroom/tools/hackernews.py:49-53` defines `hackernews_tools`, imports `HackerNewsTools` inside the function, and returns the toolkit class.
- `src/mindroom/tools/discord.py:86-90` defines `discord_tools`, imports `DiscordTools` inside the function, and returns the toolkit class.
- `src/mindroom/tools/slack.py:160-164` defines `slack_tools`, imports `SlackTools` inside the function, and returns the toolkit class.

The behavior is functionally the same: each function exists as a registration factory consumed by `register_tool_with_metadata`, with the actual toolkit import deferred until the factory is called.
The decorator records the function as `ToolMetadata.factory` in `src/mindroom/tool_system/metadata.py:749-821`.

Differences to preserve:

- Each module has unique metadata: name, display name, category, status, config fields, dependency list, docs URL, and function names.
- Each function imports a different Agno toolkit class from a different module.
- The lazy import likely avoids importing optional dependencies until a configured tool is actually built.

## Proposed Generalization

No refactor recommended for this isolated file.

A possible future generalization would be a tiny helper that creates a lazy toolkit-class factory from an import path, but applying that only to `reddit_tools` would make the code less consistent with the surrounding modules.
If the project decides to reduce this pattern globally, the helper should live near `src/mindroom/tool_system/metadata.py` or in a focused tool-registration helper module, and it should preserve lazy imports and type-checking ergonomics.

## Risk/Tests

No production code was changed.

If this pattern is generalized later, tests should cover:

- Built-in tool metadata still registers the correct factory for `reddit`.
- Calling the factory still imports and returns `agno.tools.reddit.RedditTools`.
- Optional dependency behavior remains lazy, especially when `praw` is not installed.
- Existing config/credential initialization paths still pass metadata fields through unchanged.
