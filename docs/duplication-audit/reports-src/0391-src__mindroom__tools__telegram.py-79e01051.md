## Summary

No meaningful Telegram-specific duplication found.
`telegram_tools` follows the same lightweight registration/lazy-import adapter pattern used by many built-in tool modules, but the duplicated behavior is generic tool metadata boilerplate rather than duplicated Telegram message handling.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
telegram_tools	function	lines 55-59	related-only	telegram_tools, TelegramTools, agno.tools.telegram, enable_send_message, function_names send_message, communication tool lazy import	src/mindroom/tools/telegram.py:55; src/mindroom/tools/discord.py:86; src/mindroom/tools/slack.py:160; src/mindroom/tools/webex.py:56; src/mindroom/tools/whatsapp.py:144; src/mindroom/tools/twilio.py:105; src/mindroom/tools/__init__.py:120
```

## Findings

No real duplicated Telegram behavior was found elsewhere under `src`.
The only direct references to `TelegramTools` and `telegram_tools` are in `src/mindroom/tools/telegram.py:10`, `src/mindroom/tools/telegram.py:55`, and the package export/import entries in `src/mindroom/tools/__init__.py:120` and `src/mindroom/tools/__init__.py:241`.

There is related boilerplate across communication tool modules.
`src/mindroom/tools/telegram.py:13` registers metadata for a configured communication toolkit and `src/mindroom/tools/telegram.py:55` lazily imports and returns the Agno toolkit class.
The same adapter shape appears in `src/mindroom/tools/discord.py:13` and `src/mindroom/tools/discord.py:86`, `src/mindroom/tools/slack.py:13` and `src/mindroom/tools/slack.py:160`, `src/mindroom/tools/webex.py:13` and `src/mindroom/tools/webex.py:56`, `src/mindroom/tools/whatsapp.py:13` and `src/mindroom/tools/whatsapp.py:144`, and `src/mindroom/tools/twilio.py:13` and `src/mindroom/tools/twilio.py:105`.
These modules share the mechanics of declaring `ConfigField` values, registering metadata, and returning an Agno toolkit class, but their provider-specific field names, dependencies, docs URLs, function names, and toolkit classes differ.

## Proposed Generalization

No refactor recommended for `telegram_tools`.
The current function is only five lines and keeps the lazy import explicit.
A generic helper for class-returning tool factories would save little code and would likely make type checking and per-provider metadata less direct.

If this boilerplate grows further, the smallest useful generalization would be a dedicated metadata-building helper for repeated boolean enable fields such as `enable_send_message` and `all`, not a Telegram-specific abstraction.

## Risk/tests

No behavior change is proposed.
If a future refactor centralizes built-in tool registration boilerplate, tests should cover metadata registration for `telegram`, lazy import behavior for optional Agno dependencies, and exported availability through `src/mindroom/tools/__init__.py`.
