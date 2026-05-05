## Summary

Top duplication candidate: command documentation exists in `src/mindroom/commands/parsing.py` and a smaller hard-coded quick-command list exists in `src/mindroom/commands/handler.py`.
No other meaningful duplicated command parsing behavior was found for this module.
Most neighboring code consumes `CommandType`, `Command`, `command_parser.parse`, or `get_command_help` rather than reimplementing them.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
CommandType	class	lines 16-27	related-only	CommandType enum command type schedule config help unknown; command dispatch enum usage	src/mindroom/commands/handler.py:219, src/mindroom/commands/handler.py:223, src/mindroom/commands/handler.py:236, src/mindroom/commands/handler.py:240, src/mindroom/commands/handler.py:255, src/mindroom/commands/handler.py:263, src/mindroom/commands/handler.py:281, src/mindroom/commands/handler.py:293, src/mindroom/commands/handler.py:354
_get_command_entries	function	lines 43-61	duplicate-found	command entries available commands quick commands help command docs	src/mindroom/commands/handler.py:158, src/mindroom/commands/handler.py:165, src/mindroom/commands/handler.py:166, src/mindroom/commands/handler.py:167, src/mindroom/commands/handler.py:168
get_command_list	function	lines 64-72	related-only	get_command_list Available commands command list	src/mindroom/commands/parsing.py:64, src/mindroom/commands/parsing.py:318, src/mindroom/custom_tools/config_manager.py:301
Command	class	lines 76-81	related-only	Command dataclass parsed command args raw_text command handler	src/mindroom/commands/handler.py:189, src/mindroom/commands/handler.py:194, src/mindroom/turn_controller.py:865, src/mindroom/turn_controller.py:918
_CommandParser	class	lines 84-195	none-found	CommandParser command_parser parse command patterns !schedule !config !help	src/mindroom/coalescing.py:125, src/mindroom/coalescing.py:137, src/mindroom/turn_controller.py:378, src/mindroom/turn_controller.py:1673
_CommandParser.parse	method	lines 97-195	related-only	command_parser.parse message startswith ! voice command event parse config args	src/mindroom/coalescing.py:125, src/mindroom/coalescing.py:137, src/mindroom/turn_controller.py:374, src/mindroom/turn_controller.py:378, src/mindroom/turn_controller.py:1671, src/mindroom/turn_controller.py:1673, src/mindroom/commands/config_commands.py:28
get_command_help	function	lines 198-331	duplicate-found	get_command_help !help config schedule quick commands available commands	src/mindroom/commands/handler.py:158, src/mindroom/commands/handler.py:165, src/mindroom/commands/handler.py:166, src/mindroom/commands/handler.py:167, src/mindroom/commands/handler.py:168, src/mindroom/custom_tools/config_manager.py:301, src/mindroom/custom_tools/config_manager.py:428
```

## Findings

### 1. Welcome quick-command text duplicates part of command documentation

`src/mindroom/commands/parsing.py:31` defines `_COMMAND_DOCS`, which is used by `_get_command_entries()` at `src/mindroom/commands/parsing.py:43`, `get_command_list()` at `src/mindroom/commands/parsing.py:64`, and the general `get_command_help()` output at `src/mindroom/commands/parsing.py:318`.
`src/mindroom/commands/handler.py:158` then manually builds the welcome message and repeats command guidance at `src/mindroom/commands/handler.py:165` through `src/mindroom/commands/handler.py:168` for `!hi`, `!schedule <time> <message>`, and `!help [topic]`.

The behavior is functionally duplicated because both places present user-facing command syntax and descriptions.
The welcome text is intentionally shorter and has a different heading, so it is not a literal duplicate, but it can drift from the canonical command docs.
One example of drift already exists: `_COMMAND_DOCS` describes schedule as `!schedule <task>` at `src/mindroom/commands/parsing.py:32`, while the welcome text says `!schedule <time> <message>` at `src/mindroom/commands/handler.py:167`.

Differences to preserve:
The welcome message should remain a compact curated subset.
The full help output should continue listing all documented commands and using markdown code formatting.

### 2. Config command parsing is related but not duplicated with top-level command parsing

`src/mindroom/commands/parsing.py:179` through `src/mindroom/commands/parsing.py:187` only recognizes the top-level `!config` command and stores the remaining text as `args_text`.
`src/mindroom/commands/config_commands.py:28` then parses that `args_text` with `shlex.split()`, defaulting to `show`, returning `parse_error` on unmatched quotes, and splitting operation from arguments.

This is related command parsing behavior, but it is intentionally layered rather than duplicated.
The top-level parser identifies Matrix chat commands and command families; the config command parser handles subcommand syntax and quoting after dispatch.
No refactor is recommended for this relationship.

### 3. Command detection is centralized rather than duplicated

`src/mindroom/coalescing.py:125` through `src/mindroom/coalescing.py:137` uses `command_parser.parse(event.body)` to bypass coalescing for commands.
`src/mindroom/turn_controller.py:374` through `src/mindroom/turn_controller.py:379` uses the same parser while checking newer unresponded thread messages, and `src/mindroom/turn_controller.py:1671` through `src/mindroom/turn_controller.py:1674` uses it for actual command dispatch.

These are separate call sites for the same shared behavior, not duplicated parser implementations.
The surrounding guards differ by context: coalescing excludes voice, image, and media events; turn execution excludes media events and voice source events.

## Proposed Generalization

For the real duplication, add a small helper in `src/mindroom/commands/parsing.py` only if the code is later edited for command documentation:

1. Keep `_COMMAND_DOCS` as the canonical source for command syntax and descriptions.
2. Add a helper that formats a selected subset of command docs, for example `get_command_entries_for(types: Sequence[CommandType], *, format_code: bool)`.
3. Have `_generate_welcome_message()` call that helper for `HI`, `SCHEDULE`, and `HELP`, while preserving the welcome heading and compact layout.
4. Decide whether schedule syntax should be canonicalized as `!schedule <task>` or `!schedule <time> <message>` before changing output.

No refactor is recommended for `CommandType`, `Command`, `_CommandParser`, `_CommandParser.parse`, or config subcommand parsing.

## Risk/tests

Risk is low if only welcome command lines are generated from the existing command docs.
The main behavior risk is user-facing text drift or losing the welcome message's concise wording.
Tests should cover `get_command_help()` general output, `get_command_list()`, and `_generate_welcome_message()` command snippets if this duplication is later refactored.
