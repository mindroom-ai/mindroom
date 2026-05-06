Summary: One small duplication candidate exists around constructing single user-role memory/chat messages.
The visible-thread-to-message conversion is related to live model context assembly, but the behavior differs enough that no shared helper is recommended from this file alone.
No meaningful duplication was found for memory context formatting or timestamp-prefix stripping.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_format_memories_as_context	function	lines 18-28	none-found	Automatically extracted; Previous memories; might be related; memory context formatting	src/mindroom/memory/functions.py:413; src/mindroom/memory/_file_backend.py:188; src/mindroom/custom_tools/memory.py:118
strip_user_turn_time_prefix	function	lines 31-33	related-only	strip_user_turn_time_prefix; bot-injected timestamp metadata; %Y-%m-%d %H:%M; timezone_abbrev	src/mindroom/response_runner.py:205; src/mindroom/response_runner.py:220; src/mindroom/ai.py:116; src/mindroom/response_runner.py:293
_build_conversation_messages	function	lines 36-49	related-only	ResolvedVisibleMessage to Message; role user assistant; message.body.strip; thread_history user_id	src/mindroom/execution_preparation.py:323; src/mindroom/execution_preparation.py:344; src/mindroom/execution_preparation.py:398
build_memory_messages	function	lines 52-60	duplicate-found	role user content prompt; messages = [{"role": "user", "content": content}]; normalize run input string	src/mindroom/memory/_mem0_backend.py:294; src/mindroom/ai_runtime.py:58; src/mindroom/execution_preparation.py:398
```

## Findings

### Single user-role memory message construction

- `src/mindroom/memory/_prompting.py:52` returns `[{"role": "user", "content": prompt}]` when no thread history is available.
- `src/mindroom/memory/_mem0_backend.py:294` independently builds the same one-message mem0 payload shape for manual memory insertion: `[{"role": "user", "content": content}]`.

Both call sites are preparing a mem0-compatible chat message list with a single user-authored content string.
The difference to preserve is naming and context: `_prompting.py` handles conversation persistence and may include prior thread messages, while `_mem0_backend.py` handles direct memory insertion and always creates a single user message.

### Related live-context conversion, not a duplicate

- `src/mindroom/memory/_prompting.py:36` converts `ResolvedVisibleMessage` history into plain dictionaries for mem0, using `user_id` equality to choose `"user"` vs `"assistant"` and dropping blank stripped bodies.
- `src/mindroom/execution_preparation.py:323` and `src/mindroom/execution_preparation.py:344` also convert visible Matrix messages into model-facing messages, but they preserve speaker labels, relayed-user semantics, max message limits, max length filtering, and Agno `Message` objects.

These are functionally related because both shape Matrix-visible thread history into chat messages.
They are not safe to share directly without mixing memory-persistence semantics with model-context semantics.

### Related timestamp handling, not a duplicate

- `src/mindroom/memory/_prompting.py:31` strips one bot-injected timestamp prefix from user text.
- `src/mindroom/response_runner.py:205` injects the same prefix shape for model-facing context, and `src/mindroom/ai.py:128` plus `src/mindroom/response_runner.py:260` call the stripping helper when comparing raw memory text with enriched model text.

This is already centralized on the stripping side.
The prefixing side has additional runtime concerns: timezone lookup, current time, Matrix event timestamps, and idempotence checks.

## Proposed Generalization

No refactor recommended for the related live-context or timestamp paths.

For the single-message duplication, a tiny helper could be introduced only if this pattern grows further:

1. Add a private helper in `src/mindroom/memory/_prompting.py`, such as `_user_memory_message(content: str) -> dict[str, str]`.
2. Use it in `build_memory_messages`.
3. Optionally expose a memory-package helper for `src/mindroom/memory/_mem0_backend.py` if avoiding one literal duplicate is worth the extra import.

Given there are only two simple call sites today, the helper may add more indirection than value.

## Risk/tests

The main risk of deduplicating `_build_conversation_messages` with live model-context assembly would be accidentally changing memory persistence:

- Memory saves currently store bare message bodies, not speaker-prefixed content.
- Memory saves classify messages only by exact `sender == user_id`.
- Blank history bodies are skipped after stripping whitespace.
- The current prompt is always appended as a user message.

Tests to update if a refactor is attempted:

- Memory message building with empty, whitespace-only, user-authored, and assistant-authored history.
- `store_conversation_memory` mem0 payload shape for history and no-history cases.
- Manual `add_mem0_agent_memory` payload shape.
- Timestamp stripping and idempotent prefixing around model-context preparation.
