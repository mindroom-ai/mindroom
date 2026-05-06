## Summary

Top duplication candidate: `invited_room_entity_names` duplicates the router-first configured entity enumeration used by `MultiAgentOrchestrator._configured_entity_names`.
The JSON list persistence in `load_invited_rooms` and `save_invited_rooms` is related to other small persisted-state helpers, but the validation, failure policy, and atomic write details are file-specific enough that I do not recommend a shared persistence abstraction from this module alone.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
invited_rooms_path	function	lines 21-23	related-only	invited_rooms_path, agent_state_root_path, sync token path, attachment record path	src/mindroom/bot_room_lifecycle.py:82; src/mindroom/matrix/room_cleanup.py:62; src/mindroom/matrix/sync_tokens.py:30; src/mindroom/attachments.py:163
load_invited_rooms	function	lines 26-46	related-only	load_invited_rooms, json.loads path.read_text, invalid file fallback, set[str] JSON list	src/mindroom/bot_room_lifecycle.py:86; src/mindroom/matrix/room_cleanup.py:62; src/mindroom/matrix/sync_tokens.py:105; src/mindroom/oauth/state.py:70; src/mindroom/memory/auto_flush.py:203; src/mindroom/attachments.py:580
save_invited_rooms	function	lines 49-62	related-only	save_invited_rooms, json.dumps sorted set, safe_replace, tmp_path, atomic JSON write	src/mindroom/bot_room_lifecycle.py:92; src/mindroom/oauth/state.py:102; src/mindroom/memory/auto_flush.py:231; src/mindroom/attachments.py:438; src/mindroom/config/main.py:1736; src/mindroom/api/config_lifecycle.py:197
should_accept_invites	function	lines 65-74	none-found	should_accept_invites, accept_invites, router.accept_invites, agent_config.accept_invites, agent_name in config.teams	src/mindroom/bot_room_lifecycle.py:74; src/mindroom/approval_manager.py:47; src/mindroom/config/agent.py:189; src/mindroom/config/models.py:506; src/mindroom/cli/config.py:782
invited_room_entity_names	function	lines 77-79	duplicate-found	invited_room_entity_names, configured entity names, router first, config.agents.keys, config.teams.keys	src/mindroom/matrix/room_cleanup.py:58; src/mindroom/orchestrator.py:722; src/mindroom/entity_resolution.py:48; src/mindroom/matrix/identity.py:265
should_persist_invited_rooms	function	lines 82-84	none-found	should_persist_invited_rooms, persist invited rooms, should_accept_invites wrapper	src/mindroom/bot_room_lifecycle.py:78; src/mindroom/matrix/room_cleanup.py:59; src/mindroom/matrix/invited_rooms_store.py:82
```

## Findings

### Duplicate configured entity enumeration

`src/mindroom/matrix/invited_rooms_store.py:77` returns `(ROUTER_AGENT_NAME, *config.agents.keys(), *config.teams.keys())`.
`src/mindroom/orchestrator.py:722` has the same behavior as a router-first list in `_configured_entity_names`.

The behavior is functionally duplicated: both define the complete configured entity set in the same order, including the router plus all configured agents and teams.
The only material difference is return type (`tuple[str, ...]` in the invited-room helper, `list[str]` in the orchestrator).
Other files such as `src/mindroom/entity_resolution.py:48` and `src/mindroom/matrix/identity.py:265` also enumerate router, agents, and teams, but they immediately transform those names into Matrix IDs or account keys, so they are related rather than direct duplication.

### Related JSON state persistence patterns

`src/mindroom/matrix/invited_rooms_store.py:26` and `src/mindroom/matrix/invited_rooms_store.py:49` implement a small JSON state file: read text, parse JSON, validate the expected shape, fail open, write a temporary file, and replace the target.
Related patterns exist in `src/mindroom/oauth/state.py:70` and `src/mindroom/oauth/state.py:102`, `src/mindroom/memory/auto_flush.py:203` and `src/mindroom/memory/auto_flush.py:231`, and `src/mindroom/attachments.py:438` and `src/mindroom/attachments.py:580`.

These are not a strong dedupe target from this file alone.
Each call site has different locking, corruption handling, validation shape, return type, and error policy.
`save_invited_rooms` also uses `safe_replace` and cleans up a UUID-suffixed temp file, while several related call sites use `Path.replace`.

## Proposed Generalization

If production code were to be changed, the minimal safe refactor would be:

1. Add a small `configured_entity_names(config: Config) -> tuple[str, ...]` helper in `mindroom.entity_resolution` or on `Config`.
2. Replace `invited_room_entity_names` internals with that helper, keeping its public function if callers prefer the invited-room domain name.
3. Replace `MultiAgentOrchestrator._configured_entity_names` internals with `list(configured_entity_names(config))`.
4. Leave JSON persistence helpers separate for now.

No broader persistence helper is recommended based on this audit.

## Risk/Tests

The entity enumeration refactor risk is low but should preserve router-first ordering and the current `list` return type for the orchestrator method.
Focused tests should cover any existing orchestration startup/hot-reload tests that assert entity ordering, plus room cleanup behavior that loads persisted invited rooms for router, agents, and teams.
No production code was edited for this report-only task.
