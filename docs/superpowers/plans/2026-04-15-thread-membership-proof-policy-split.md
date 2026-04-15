# Thread Membership Proof/Policy Split Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split canonical thread membership, root-proof quality, and caller policy so room-level events do not hard-fail in best-effort paths while strict callers still fail closed on proof errors.

**Architecture:** Keep one canonical transitive resolver, but make it return structured outcomes instead of collapsing “room-level”, “threaded”, and “proof unavailable” into `None` or exceptions. Then add thin caller-policy wrappers so coalescing and interactive matching can fail open while dispatch, canonical root normalization, and other strict paths still propagate proof failures.

**Tech Stack:** Python 3.13, `matrix-nio`, pytest, structlog.

---

## Why This Follow-Up Exists

The transitive contract is correct, but two caller classes still use the canonical resolver incorrectly:

- coalescing uses a strict root-proof path, so ordinary room-level edits can raise `thread root ... not found during room scan`
- interactive numeric replies still inspect explicit `m.thread` metadata directly, so plain replies from non-thread clients miss active threaded prompts

The root problem is that the current resolver API does not distinguish:

1. definitely threaded
2. definitely room-level
3. membership could not be determined because root proof failed

## Production Surfaces That Must Change

### Core resolver and proof model

- `src/mindroom/matrix/thread_membership.py`
  - `ThreadMembershipAccess`
  - `resolve_event_thread_id()`
  - `resolve_related_event_thread_id()`
  - `thread_messages_root_has_children()`
  - `snapshot_thread_root_has_children()`
  - `room_scan_thread_root_has_children()`
  - `thread_messages_thread_membership_access()`
  - `snapshot_thread_membership_access()`
  - `room_scan_thread_membership_access()`
  - `room_scan_thread_membership_access_for_client()`
  - `resolve_thread_ids_for_event_infos()`

Required change:

- introduce a structured proof/result model that distinguishes:
  - proven thread root
  - not a thread root
  - proof unavailable
- expose strict and best-effort resolver wrappers over the same canonical traversal

### Caller policy split

- `src/mindroom/conversation_resolver.py`
  - `coalescing_thread_id()`
  - `_explicit_thread_id_for_event()`
  - `thread_membership_access()`
  - `_resolve_thread_context()`

Required change:

- keep strict behavior for dispatch/context extraction
- use best-effort behavior for coalescing

### Interactive replies must stop re-deriving raw thread identity

- `src/mindroom/interactive.py`
  - `handle_text_response()`
- `src/mindroom/turn_controller.py`
  - `_handle_message_inner()`
  - `_dispatch_text_message()`

Required change:

- pass already-resolved thread scope into interactive numeric reply handling
- remove direct dependence on `EventInfo.thread_id` for interactive text matching

### Other direct resolver callers that must align with the new structured API

- `src/mindroom/bot.py`
  - `_emit_reaction_received_hooks()`
- `src/mindroom/custom_tools/matrix_api.py`
  - `_requires_conversation_cache_write()`
  - `_redaction_requires_conversation_cache_write()`
- `src/mindroom/matrix/cache/thread_writes.py`
  - `_lookup_redaction_thread_id()`
  - `_resolve_thread_id_for_mutation()`
- `src/mindroom/thread_tags.py`
  - `normalize_thread_root_event_id()`

These callers do not all need behavior changes, but they must be moved onto the new resolver entrypoints consistently.

## Tests That Must Change

- `tests/test_threading_error.py`
  - add structured resolver coverage for:
    - proven root
    - not-a-root
    - proof unavailable
    - strict vs best-effort behavior
- `tests/test_edit_response_regeneration.py`
  - room-level edits must not hard-fail during coalescing
- `tests/test_interactive.py`
  - plain numeric reply to a threaded interactive prompt via plain-reply chain must resolve to the prompt thread
- `tests/test_turn_controller.py`
  - interactive numeric reply handling should consume the resolved thread id from ingress
- update any direct resolver-call tests in:
  - `tests/test_thread_mode.py`
  - `tests/test_matrix_api_tool.py`
  - `tests/test_thread_tags.py`

## Execution Order

### Task 1: Introduce structured proof/result in `thread_membership.py`

- [ ] Add `ThreadRootProof` and `ThreadResolution` types.
- [ ] Make root-proof accessors return proof quality instead of a bare boolean.
- [ ] Add strict and best-effort wrappers over the canonical transitive traversal.
- [ ] Update in-memory fixpoint helpers to use the structured result without regressing current transitive behavior.

### Task 2: Split caller policy in `conversation_resolver.py`

- [ ] Route dispatch/full-history extraction through the strict resolver.
- [ ] Route coalescing through the best-effort resolver.
- [ ] Verify room-level edits no longer abort coalescing when root proof says “not found”.

### Task 3: Fix interactive numeric replies at the root

- [ ] Change `interactive.handle_text_response()` to accept a resolved thread id.
- [ ] Pass the canonical resolved thread id from `turn_controller.py`.
- [ ] Add regression coverage for plain numeric replies in a threaded interactive prompt.

### Task 4: Update remaining direct resolver callers

- [ ] Move `bot.py`, `matrix_api.py`, `thread_writes.py`, and `thread_tags.py` onto the new resolver entrypoints.
- [ ] Keep their current policy semantics:
  - strict where canonical proof matters
  - fail-open where side effects are advisory

### Task 5: Verify and commit

- [ ] Run targeted pytest slices for:
  - resolver/proof behavior
  - edit regeneration
  - interactive replies
  - matrix API / thread tags regressions
- [ ] Run `pre-commit` on touched files.
- [ ] Commit only the focused refactor and regression tests.
