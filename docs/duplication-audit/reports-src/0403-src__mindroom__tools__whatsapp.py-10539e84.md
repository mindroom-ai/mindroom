# Duplication Audit: src/mindroom/tools/whatsapp.py

## Summary

The only behavior symbol in `src/mindroom/tools/whatsapp.py` is a toolkit factory that lazily imports and returns an Agno toolkit class.
That exact factory pattern is repeated across most `src/mindroom/tools/*` registration modules, including the closest communication-tool peers.
This is real duplication, but it is small, explicit, and tied to import-time dependency isolation, so no refactor is recommended for this file alone.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
whatsapp_tools	function	lines 144-148	duplicate-found	whatsapp_tools; def *_tools; Return * tools; agno.tools.whatsapp; communication tool factories	src/mindroom/tools/twilio.py:105; src/mindroom/tools/telegram.py:55; src/mindroom/tools/discord.py:86; src/mindroom/tools/slack.py:160; src/mindroom/tools/cartesia.py:77; src/mindroom/tools/google_sheets.py:85
```

## Findings

### Repeated lazy toolkit factory wrappers

- `src/mindroom/tools/whatsapp.py:144` defines `whatsapp_tools()`, imports `WhatsAppTools` inside the function, and returns the imported class.
- `src/mindroom/tools/twilio.py:105` defines `twilio_tools()` with the same lazy import and class return shape for `TwilioTools`.
- `src/mindroom/tools/telegram.py:55` defines `telegram_tools()` with the same lazy import and class return shape for `TelegramTools`.
- `src/mindroom/tools/discord.py:86` defines `discord_tools()` with the same lazy import and class return shape for `DiscordTools`.
- `src/mindroom/tools/slack.py:160` defines `slack_tools()` with the same lazy import and class return shape for `SlackTools`.

These functions perform the same behavior: defer importing an optional Agno toolkit until the registered factory is called, then return the toolkit class object to the tool registry.
The differences to preserve are the concrete toolkit import path, return type annotation, function name, docstring, and the metadata attached by `register_tool_with_metadata`.

## Proposed Generalization

No refactor recommended for this file alone.

A possible repository-wide cleanup would be a tiny helper in `mindroom.tool_system.metadata` or a nearby tool registry module that creates lazy toolkit factories from an import path and class name.
However, adopting that helper would touch many tool modules and could make static type annotations and per-tool docstrings less direct.
Given the current scope, the explicit wrappers are low risk and easy to read.

## Risk/Tests

The main risk of generalizing this pattern is changing when optional dependencies are imported.
Any refactor would need tests or import checks proving that unavailable optional tool dependencies still do not break registry import, while configured tools still load their Agno class at runtime.
For this audit-only task, no production code was changed and no tests were run.
