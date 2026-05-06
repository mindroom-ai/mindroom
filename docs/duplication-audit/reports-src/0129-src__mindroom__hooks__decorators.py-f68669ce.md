Summary: No meaningful duplication found.
`src/mindroom/hooks/decorators.py` has related patterns in tool metadata registration and plugin module scanning, but the hook-specific combination of async callback validation, typed hook metadata attachment, metadata retrieval, and deduplicated module hook discovery appears isolated.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
HookMetadata	class	lines 20-28	related-only	HookMetadata dataclass metadata decorator registry ToolMetadata RegisteredHook	src/mindroom/tool_system/metadata.py:724; src/mindroom/hooks/types.py:132; src/mindroom/hooks/registry.py:75
hook	function	lines 31-61	related-only	def hook register_tool_with_metadata timed decorator validate_event_name inspect.iscoroutinefunction setattr metadata	src/mindroom/tool_system/metadata.py:749; src/mindroom/timing.py:203; src/mindroom/hooks/types.py:54
hook.<locals>.decorator	nested_function	lines 45-59	related-only	nested decorator metadata setattr async callback validation inspect.iscoroutinefunction register decorator	src/mindroom/tool_system/metadata.py:800; src/mindroom/timing.py:210
get_hook_metadata	function	lines 64-69	none-found	get_hook_metadata __mindroom_hook_metadata__ getattr isinstance HookMetadata	src/mindroom/hooks/registry.py:55; src/mindroom/hooks/decorators.py:80
iter_module_hooks	function	lines 72-84	related-only	iter_module_hooks vars(module).values seen_ids module discovery metadata callbacks	src/mindroom/tool_system/plugins.py:298; src/mindroom/oauth/registry.py:54; src/mindroom/tool_system/plugins.py:347
```

Findings:

No real duplication requiring refactor was found.

Related-only observations:

- `hook()` in `src/mindroom/hooks/decorators.py:31` and `register_tool_with_metadata()` in `src/mindroom/tool_system/metadata.py:749` are both decorator factories that build typed metadata in an inner decorator.
  They are not functionally duplicate because hook metadata is attached to async callback objects for later module discovery, while tool metadata is registered into global built-in/plugin registries and carries substantially different fields and plugin ownership behavior.
- `hook.<locals>.decorator()` in `src/mindroom/hooks/decorators.py:45` and `register_tool_with_metadata.<locals>.decorator()` in `src/mindroom/tool_system/metadata.py:800` share the shape of "create metadata, return original callable".
  The behavior differs materially: hook callbacks must be coroutine functions and receive an object attribute, while tool factories are registered by module scope and plugin registration context.
- `iter_module_hooks()` in `src/mindroom/hooks/decorators.py:72` and `_cancel_plugin_module_tasks()` in `src/mindroom/tool_system/plugins.py:287` both iterate `vars(module).values()` with identity-based deduplication/avoidance of repeated work.
  They are not good candidates for a shared helper because one discovers decorated callbacks and preserves a list of callbacks, while the other scans loaded plugin modules for live asyncio tasks and cancels them.
- `_module_oauth_provider_callback()` in `src/mindroom/oauth/registry.py:54` is another plugin module introspection point, but it looks up a single named callable instead of discovering all decorated values.

Proposed generalization: No refactor recommended.
The related patterns are small, domain-specific, and have different lifecycle semantics.
Sharing a generic "decorator metadata" or "module value scanner" helper would add abstraction without removing active duplicated hook behavior.

Risk/tests:

- No production changes were made.
- If this area is refactored later, tests should cover plugin hook discovery from both tools and hooks modules, duplicate module aliases, async-only hook validation, hook priority/name overrides, and tool metadata registration.
- Existing relevant test areas include `tests/test_plugins.py`, `tests/test_hook_execution.py`, `tests/test_hook_sender.py`, `tests/test_cancelled_response_hook.py`, and `tests/test_agno_history.py`.
