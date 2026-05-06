## Summary

The only behavior in `src/mindroom/tools/discord.py` is a metadata-registered lazy factory returning Agno's `DiscordTools`.
This factory shape is duplicated across many MindRoom tool wrapper modules, including Slack, Telegram, WhatsApp, and X.
No separate Discord channel/message implementation was found elsewhere in `./src`, so the duplication is the generic wrapper/registration pattern rather than Discord-specific business logic.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
discord_tools	function	lines 86-90	duplicate-found	"def *_tools()", "from agno.tools.discord import DiscordTools", "return *Tools", "discord", "DiscordTools"	src/mindroom/tools/slack.py:160; src/mindroom/tools/telegram.py:55; src/mindroom/tools/whatsapp.py:144; src/mindroom/tools/x.py:96; src/mindroom/tools/cartesia.py:77; src/mindroom/tools/lumalabs.py:77; src/mindroom/tools/wikipedia.py:42
```

## Findings

### Repeated lazy Agno toolkit factory wrapper

- `src/mindroom/tools/discord.py:86` imports `DiscordTools` inside `discord_tools()` and returns the class.
- `src/mindroom/tools/slack.py:160` imports `SlackTools` inside `slack_tools()` and returns the class.
- `src/mindroom/tools/telegram.py:55` imports `TelegramTools` inside `telegram_tools()` and returns the class.
- `src/mindroom/tools/whatsapp.py:144` imports `WhatsAppTools` inside `whatsapp_tools()` and returns the class.
- `src/mindroom/tools/x.py:96` imports `XTools` inside `x_tools()` and returns the class.

These are functionally the same wrapper behavior: keep the Agno toolkit import lazy for runtime dependency isolation, expose a registered MindRoom factory, and return the toolkit class unchanged.
The differences to preserve are the returned Agno class, the type annotation, and the docstring text.
The surrounding `register_tool_with_metadata(...)` blocks are intentionally per-tool because names, config fields, dependencies, docs URLs, categories, and function names differ.

## Proposed Generalization

No production refactor is recommended for this file alone.
The repeated factory body is real duplication, but each wrapper is only three lines and participates in typed imports plus per-module metadata registration.
A shared helper such as `lazy_toolkit_factory("agno.tools.discord", "DiscordTools")` would reduce those three lines but would likely weaken static typing or require extra boilerplate to preserve precise return annotations.

If this pattern is generalized in a larger dedicated cleanup, keep it narrow:

1. Add a small typed helper only if it can preserve dependency-lazy imports and clear factory names.
2. Convert a small batch of simple Agno wrappers first.
3. Leave complex wrappers such as `src/mindroom/tools/openbb.py`, `src/mindroom/tools/python.py`, and `src/mindroom/tools/shell.py` out of scope.
4. Verify tool metadata generation and runtime tool loading after conversion.

## Risk/tests

The main risk is changing import timing and optional dependency behavior for tools that are not installed in every environment.
Tests would need to cover metadata registration, importing `mindroom.tools`, and resolving the Discord toolkit when `requests` and Agno's Discord toolkit are available.
Because no production code was edited, no tests were run.
