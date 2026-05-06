Summary: One meaningful duplication candidate found.
`TurnPolicy._should_queue_follow_up_in_active_response_thread` and `AgentBot._should_queue_follow_up_in_active_response_thread` duplicate active-thread follow-up queue gating around automation source kinds, agent senders, target/thread requirements, and mention exclusions.
The remaining symbols in `dispatch_source.py` are small source-kind classification/protocol helpers that are either the source of truth or have only adjacent, non-duplicated behavior elsewhere.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_HasContent	class	lines 27-28	related-only	content protocol, event.content, source content, com.mindroom.source_kind	src/mindroom/dispatch_source.py:70; src/mindroom/coalescing_batch.py:62; src/mindroom/dispatch_handoff.py:108; src/mindroom/matrix/visible_body.py:85
_HasSource	class	lines 32-33	related-only	source protocol, event.source content, source.get content	src/mindroom/dispatch_source.py:72; src/mindroom/coalescing_batch.py:62; src/mindroom/dispatch_handoff.py:108; src/mindroom/matrix/media.py:103; src/mindroom/matrix/visible_body.py:78
_HasSourceKind	class	lines 37-38	related-only	source_kind attribute, PendingEvent source_kind, MessageEnvelope source_kind	src/mindroom/dispatch_source.py:87; src/mindroom/coalescing_batch.py:31; src/mindroom/coalescing_batch.py:49; src/mindroom/dispatch_handoff.py:74; src/mindroom/turn_policy.py:589
_HasSourceKindOverride	class	lines 42-43	related-only	source_kind_override, PreparedTextEvent source_kind_override, effective source kind	src/mindroom/dispatch_source.py:89; src/mindroom/dispatch_handoff.py:50; src/mindroom/coalescing.py:106; src/mindroom/inbound_turn_normalizer.py:202
_HasSender	class	lines 47-48	related-only	sender protocol, sender_is_trusted, trusted sender ids, original sender	src/mindroom/dispatch_source.py:66; src/mindroom/matrix/visible_body.py:13; src/mindroom/authorization.py:145; src/mindroom/coalescing_batch.py:87
is_automation_source_kind	function	lines 51-53	duplicate-found	automation source kind, scheduled hook hook_dispatch, queue follow-up automation gating	src/mindroom/dispatch_source.py:17; src/mindroom/response_lifecycle.py:156; src/mindroom/turn_policy.py:576; src/mindroom/bot.py:1494; src/mindroom/coalescing.py:50
_source_kind_from_content	function	lines 56-58	none-found	com.mindroom.source_kind, content.get source_kind, source kind from content	src/mindroom/dispatch_source.py:56; src/mindroom/inbound_turn_normalizer.py:193; src/mindroom/scheduling.py:882; src/mindroom/matrix/large_messages.py:42
_trusted_source_kind_from_event_content	function	lines 61-77	related-only	trusted sender source kind, sender_is_trusted content, event source content	src/mindroom/dispatch_source.py:61; src/mindroom/matrix/visible_body.py:13; src/mindroom/matrix/visible_body.py:49; src/mindroom/authorization.py:145
is_voice_event	function	lines 80-96	related-only	is_voice_event, voice source kind, audio message event, source_kind_override voice	src/mindroom/dispatch_source.py:80; src/mindroom/coalescing.py:125; src/mindroom/matrix/media.py:64; src/mindroom/inbound_turn_normalizer.py:158; src/mindroom/dispatch_handoff.py:122
```

Findings:

1. Active-thread follow-up queue gating is duplicated across the old bot shell and the turn-policy collaborator.

- `src/mindroom/turn_policy.py:576` implements `_should_queue_follow_up_in_active_response_thread`.
- `src/mindroom/bot.py:1494` implements another `_should_queue_follow_up_in_active_response_thread`.
- Both require a target, source envelope, thread context, no mentioned agents, no non-agent mentions, a non-automation source kind via `is_automation_source_kind`, and a non-agent sender via `is_agent_id`.
- Both then ask whether the target has an active response, though `turn_policy.py:595` also preserves the newer `dispatch_policy_source_kind == ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND` override path.
- Differences to preserve: `turn_policy.py` accepts a `has_active_response_for_target` callback that may be `None`, and it allows explicit active-thread follow-up policy source kinds to queue even without the callback result.

2. Voice/source-kind detection has adjacent classification, but not duplicated behavior.

- `src/mindroom/dispatch_source.py:80` classifies voice by `source_kind`, `source_kind_override`, or trusted internal `com.mindroom.source_kind`.
- `src/mindroom/matrix/media.py:64` classifies raw Matrix audio event types.
- `src/mindroom/inbound_turn_normalizer.py:158` normalizes raw audio into a `PreparedTextEvent` and stamps both `com.mindroom.source_kind` and `source_kind_override` as `"voice"`.
- `src/mindroom/coalescing.py:125` consumes `is_voice_event` to prevent voice-command text from bypassing coalescing.
- These pieces are sequential stages of the same pipeline, not competing implementations.

3. Trusted content/source extraction is centralized here, with only generic neighboring helpers elsewhere.

- `src/mindroom/dispatch_source.py:56` is the only direct extractor for `com.mindroom.source_kind`.
- `src/mindroom/coalescing_batch.py:62` and `src/mindroom/dispatch_handoff.py:108` have similar `event.source["content"]` dict extraction helpers, but they extract different payload metadata and do not interpret source kinds.
- `src/mindroom/matrix/visible_body.py:13` has a similar trusted-sender predicate shape, but it gates visible body metadata rather than source-kind metadata.

Proposed generalization:

1. Move the shared active-thread follow-up gate into `turn_policy.py` as the single implementation, or delete the `AgentBot` copy if it is no longer on an active path.
2. Preserve the current `TurnPolicy` behavior as canonical, including `dispatch_policy_source_kind == ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND`.
3. Keep `dispatch_source.py` unchanged for source-kind classification; it is already the shared source of truth.
4. No refactor recommended for `_source_kind_from_content`, `_trusted_source_kind_from_event_content`, or `is_voice_event`.

Risk/tests:

- The queueing duplication is behavior-sensitive because follow-up messages can either interrupt an active response thread or dispatch immediately.
- Tests should cover human follow-up queuing for normal messages, scheduled/hook automation exclusion, agent-sender exclusion, mention exclusion, and explicit `ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND` policy override.
- If the `AgentBot` method remains reachable, replacing it with the `TurnPolicy` implementation should include an integration-level test around active thread follow-up handling.
