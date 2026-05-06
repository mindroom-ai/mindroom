## Summary

The only meaningful duplication in `src/mindroom/tools/slack.py` is the standard lazy toolkit-class factory used by many modules under `src/mindroom/tools`.
`slack_tools` matches the same behavior as `discord_tools`, `telegram_tools`, `gmail_tools`, and `notion_tools`: import the toolkit class inside the registered factory and return the class unchanged.
No Slack-specific duplicated behavior was found elsewhere in `./src`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
slack_tools	function	lines 160-164	duplicate-found	slack_tools, SlackTools, def *_tools(), lazy import return Toolkit class	src/mindroom/tools/discord.py:86, src/mindroom/tools/telegram.py:55, src/mindroom/tools/gmail.py:139, src/mindroom/tools/notion.py:73, src/mindroom/tools/__init__.py:113, src/mindroom/tools/__init__.py:234
```

## Findings

### Lazy registered toolkit factory pattern

- `src/mindroom/tools/slack.py:160` defines `slack_tools`, imports `SlackTools` from `agno.tools.slack` inside the function, and returns the class.
- `src/mindroom/tools/discord.py:86` defines `discord_tools`, imports `DiscordTools` inside the function, and returns the class.
- `src/mindroom/tools/telegram.py:55` defines `telegram_tools`, imports `TelegramTools` inside the function, and returns the class.
- `src/mindroom/tools/gmail.py:139` defines `gmail_tools`, imports `GmailTools` inside the function, and returns the class.
- `src/mindroom/tools/notion.py:73` defines `notion_tools`, imports `NotionTools` inside the function, and returns the class.

These factories are functionally the same: each serves as a metadata-registered entry point that delays importing an optional toolkit dependency until the tool is actually loaded.
The differences to preserve are the imported toolkit class, source module, return type annotation, docstring, and decorator metadata.

The surrounding Slack metadata does not duplicate another module exactly.
Slack shares common config-field shapes such as `token`, `enable_send_message`, `enable_list_channels`, and `all` with other communication tools, but the specific field set, defaults, dependencies, docs URL, and function names are provider-specific.
That is related boilerplate, not a strong duplicate behavior candidate for this primary file.

## Proposed Generalization

No refactor recommended for this file.

A helper such as `lazy_toolkit_factory(import_path, class_name)` could remove a few repeated lines across many tool modules, but it would make the registered function names less explicit and would not materially reduce behavioral complexity.
The current duplication is small, readable, and aligned with the local tool-registration convention.

## Risk/tests

If this pattern were generalized later, tests should verify that tool metadata registration still exposes the same public tool names and that optional imports remain lazy for missing optional dependencies.
Relevant checks would include importing `mindroom.tools`, loading the Slack tool with `slack-sdk` installed, and confirming that modules for unrelated optional toolkits are not imported eagerly.
