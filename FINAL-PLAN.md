# ISSUE-224 — FINAL PLAN

**Synthesized 2026-05-21** from dual planner plans (`issue-224-plan-codex` `4b13513c2`, `issue-224-plan-claude` `5aebad01e`) and cross-feed critiques (`efbe5e0c3`, `eb496c8dd`). Both critiques returned **APPROVE_WITH_FIXES** and converged on the same merged design. This document is the single source of truth for the implementer.

## Issue summary

Add an `include_untagged: bool = False` parameter to the `list_thread_tags` tool so AIs can answer "what threads in this room are unresolved?" with one call. Today the tool can only return threads that have at least one tag, missing every untagged thread root. The headline query the feature unlocks is:

```python
list_thread_tags(exclude_tag="resolved", include_untagged=True)
```

## Approach

Extend the existing `list_tagged_threads` core helper in `src/mindroom/thread_tags.py` to optionally enumerate every thread root in the room via the existing `get_room_threads_page` helper in `src/mindroom/matrix/client_thread_history.py`, synthesize empty-tag entries (`tags={}`) for thread roots not present in the tag-state map, and apply the existing `tag` / `include_tag` / `exclude_tag` filters to the merged set. Default behavior (`include_untagged=False`) is byte-identical to today.

No new tool. No tag-policy changes. No multi-room queries. No activity threshold. No caller-driven pagination (the helper paginates to completion or to a safety cap and surfaces a `truncated` flag).

## Files to change

| File | Change |
|---|---|
| `src/mindroom/thread_tags.py` | Add `include_untagged: bool = False` to `list_tagged_threads`. New room-wide branch that paginates `/threads`, merges, applies filters. Adds `truncated: bool` to the returned payload when `include_untagged=True`. |
| `src/mindroom/custom_tools/thread_tags.py` | Add `include_untagged: bool = False` to the `ThreadTagsTools.list_thread_tags` async method. New docstring (see "Tool docstring" below). Validation: reject explicit `thread_id` with `include_untagged=True`. Suppress in-thread context fallback when `include_untagged=True` (force room-wide). |
| `tests/test_thread_tags.py` | Add helper-layer tests (see "Tests" below). |
| `tests/test_thread_tags_tool.py` | Add wrapper-layer tests (see "Tests" below). |
| `docs/tools/index.md` (lines ~73-80) | Update the `list_thread_tags` row to mention `include_untagged`. |
| `docs/tools/matrix-and-attachments.md` (example block ~line 114) | Add one example line: `list_thread_tags(exclude_tag="resolved", include_untagged=True)` with one-sentence note. |

**Files NOT to change:**
- `src/mindroom/tools_metadata.json` — verified: the relevant entry lists `function_names` only, not per-parameter schemas. Adding a parameter doesn't change the function name. Skip.
- `src/mindroom/tools/thread_tags.py` toolkit-level `description=` — the model reads the function docstring, not this. Skip.
- `skills/mindroom-docs/references/page__tools__*` — pre-commit hook ("Regenerate mindroom-docs skill references") regenerates these automatically. Skip manual edit.

## Parameter signature

```python
async def list_thread_tags(
    self,
    *,
    room_id: str | None = None,
    thread_id: str | None = None,
    tag: str | None = None,
    include_tag: str | None = None,
    exclude_tag: str | None = None,
    include_untagged: bool = False,  # NEW — appended last
) -> dict[str, Any]:
```

`include_untagged` appended **last** so it doesn't shift positional behavior for any existing caller.

## Validation rules

1. **Reject `thread_id` + `include_untagged=True`** with a structured error using the existing `ThreadTagsError` envelope: message *"`include_untagged=True` is only valid for room-wide queries; do not pass `thread_id`."*
2. **Suppress in-thread context fallback when `include_untagged=True`.** Today, if `room_id` is unset, the tool resolves the active thread's room from runtime context and may scope to that thread. When `include_untagged=True`, resolve only the room and force room-wide listing.
3. All existing validation (`tag` mutually exclusive with `include_tag`/`exclude_tag`, etc.) continues to apply.

## Pagination strategy

When `include_untagged=True` and the query resolves to room-wide:

1. Fetch room-wide tag state via existing `_get_room_thread_tags_states` in `src/mindroom/thread_tags.py:610`.
2. Call new `enumerate_room_thread_root_ids(client, room_id, ...)` helper added near `get_room_threads_page` in `src/mindroom/matrix/client_thread_history.py:1106`. This helper:
   - Uses page size **100**. Synapse's `/threads` accepts up to 100; the same module already uses 100 for `room_messages` scans (`client_thread_history.py:1046`). The `_MAX_THREAD_LIMIT=50` in `matrix_room.py` is a caller-imposed cap on the agent-facing tool, not a homeserver constraint, and does not apply here.
   - Hard cap `max_thread_roots=2000` (constant in `client_thread_history.py`). When exceeded, stop and return `truncated=True`.
   - Loop termination safety: track seen page tokens; stop if (a) a non-empty page contributes zero new roots, or (b) the same `next_token` is returned twice. This prevents a buggy or misbehaving homeserver cursor from looping forever even with a unique-roots cap.
   - Deduplicate while preserving `/threads` page order.
   - Propagates `RoomThreadsPageError` upward, preserving `response`, `errcode`, and `retry_after_ms` (mirror the pattern in `src/mindroom/custom_tools/matrix_room.py:321-331`).
3. Merge: for each enumerated root not in the tag-state map, synthesize an entry with `tags={}`. Preserve `/threads` order for live roots; append any tagged-state-only entries (threads that have tag state but weren't returned by `/threads` — likely deleted/redacted roots) at the end.
4. Apply filters to the merged set:
   - `exclude_tag="resolved"` passes empty-tag threads (no tags ≠ excluded tag present).
   - `include_tag=X` and `tag=X` fail empty-tag threads (no tags ⇒ no match).

## Tool docstring

Replace the `list_thread_tags` docstring with:

```
List Matrix thread tags in a room.

Inspect which threads in a room have which tags. Default mode returns only
threads that have at least one tag stored as room state. Pass
`include_untagged=True` to also surface every other thread root in the room
(threads with no tags appear with an empty `tags` dict). This enables the
headline query for "what threads are still unresolved?":

    list_thread_tags(exclude_tag="resolved", include_untagged=True)

Parameters:
  room_id: Room to query. Defaults to the current Matrix tool runtime
    room when invoked from a runtime context.
  thread_id: When set, restrict the query to a single thread root. Incompatible
    with `include_untagged=True` (raises a validation error).
  tag: Return only matching threads that carry this exact tag. In room-wide
    mode, untagged threads are filtered out.
  include_tag: Filter to threads that have this tag. Untagged threads are
    filtered out.
  exclude_tag: Filter to threads that do NOT have this tag. Untagged threads
    pass (they have no tags, so no excluded tag is present).
  include_untagged: When True, also enumerate every thread root in the room
    via Matrix `/threads` and synthesize empty-tag entries for ones with no
    tag state. The payload then also includes `include_untagged: true` and
    `truncated: bool`. Untagged threads are filtered out by `tag=` or
    `include_tag=`; use `exclude_tag=` alone for the unresolved-threads query.
    Defaults to False.

Returns:
  Dict with `room_id`, `tag_state` (mapping of thread_id -> tag dict), and,
  when `include_untagged=True`, `include_untagged: bool` and `truncated: bool`.
```

## Payload additions

When `include_untagged=True` and the query is room-wide, the success payload includes:

```json
{
  "include_untagged": true,
  "truncated": false,
  "tag_state": { "$threadId": { "resolved": {...} }, "$threadId2": {} }
}
```

`truncated` is **always present** when `include_untagged=True` (`false` on complete results, `true` when the cap was hit). This makes the contract deterministic for both live verification and model synthesis. When `include_untagged=False`, neither key appears (back-compat with current callers).

## Tests

### Helper layer (`tests/test_thread_tags.py`)

1. **Default behavior unchanged.** `list_tagged_threads(include_untagged=False)` does not call `get_room_threads_page` / `enumerate_room_thread_root_ids` (assert via mock).
2. **Union behavior.** Mock `_get_room_thread_tags_states` returning 2 tagged roots and `enumerate_room_thread_root_ids` returning 5 roots (3 untagged, 2 overlap). Result: 5 entries, 3 with `tags={}`, 2 with tags, preserved `/threads` order for live roots.
3. **Tagged-state-only threads.** Tagged state has thread root `$X` that `/threads` did not return. `$X` appears at the end of results with its tag dict.
4. **Full pagination.** Mock 3 pages of `get_room_threads_page` (page 1 next_token=A, page 2 next_token=B, page 3 next_token=None). Helper makes 3 calls, returns union of all roots.
5. **Truncation at cap.** Mock pages returning 2500 unique roots (exceeds `max_thread_roots=2000`). Helper stops at 2000 and returns `truncated=True`.
6. **Repeated-token guard.** Mock pages where `next_token` repeats. Helper detects and stops, returns `truncated=True`.
7. **Zero-new-roots-on-non-empty-page guard.** Mock a non-empty page that contributes only duplicates. Helper stops, returns `truncated=True`.
8. **`RoomThreadsPageError` propagation.** Raise from `get_room_threads_page`. `list_tagged_threads` propagates with `response`, `errcode`, `retry_after_ms` preserved on the structured error.
9. **Filter semantics on synthesized entries.**
   - `exclude_tag="resolved"` includes untagged.
   - `include_tag="resolved"` excludes untagged.
   - `tag="resolved"` excludes untagged.

### Wrapper layer (`tests/test_thread_tags_tool.py`)

10. **Default unchanged.** Existing wrapper-layer tests pass without modification.
11. **Headline query smoke.** `list_thread_tags(exclude_tag="resolved", include_untagged=True)` from runtime context returns merged map with both tagged-not-resolved and untagged threads.
12. **`thread_id` + `include_untagged=True` rejected.** Returns structured `ThreadTagsError`-shaped error with the validation message.
13. **In-thread context override.** When called from active-thread runtime context with `include_untagged=True` and no explicit `thread_id`, listing is room-wide (does not scope to active thread).
14. **`tag=` excludes untagged.** Explicit test mirroring helper test 9c at the wrapper layer.
15. **Enumeration error surface.** `/threads` failure surfaces with structured error fields preserved end-to-end.
16. **`truncated` flag in payload.** Mock cap-hit; assert `truncated=True` in the wrapper-returned payload.
17. **Schema exposure.** `ThreadTagsTools().async_functions["list_thread_tags"]` after `process_entrypoint` exposes `include_untagged` as a boolean parameter with default `False` and a description fragment matching the docstring. This is the only test that pins the model-visible JSON schema.

## Live test plan

In DevAgent's MindRoom Dev room (`!cvldK8hd7XU2d6rmLq:mindroom.chat`):

1. Call `list_thread_tags(exclude_tag="resolved")` (today's strict mode). Record the set of returned thread IDs (currently 6 in this room).
2. Call `list_thread_tags(exclude_tag="resolved", include_untagged=True)`. Assert:
   - Result is a **strict superset** of step 1.
   - Contains untagged thread roots with `tags={}`.
   - `include_untagged=true` and `truncated=false` present in the payload.
3. Cross-check at least one new entry against `matrix_room(action="threads")` to confirm those thread roots actually exist in the room (not synthetic / hallucinated).
4. Optional sanity: call `list_thread_tags(include_tag="resolved", include_untagged=True)` and confirm untagged threads are NOT included (filter semantics correct on the inclusive flag).

## Out of scope (explicit)

- No new `list_unresolved_threads` tool.
- No tag-policy changes (no auto-seeding, no rename, no deprecate).
- No multi-room queries.
- No activity / message-count threshold filter.
- No caching (on-demand only).
- No caller-driven pagination (`next_token` is NOT surfaced; helper paginates to completion or to the `max_thread_roots` cap).
- No author/sender filter.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Runaway `/threads` enumeration on huge rooms (10k+ threads) | `max_thread_roots=2000` hard cap + `truncated=True` signal + repeated-token / zero-new-roots loop guards. Worst case: ~20 round-trips at page=100. |
| Tagged-state-only orphans (tag exists for thread root that `/threads` no longer returns — deletion/redaction) | Preserve them at the end of the merged map so we don't silently lose tag history. |
| Model misuse: agent passes `include_tag="resolved", include_untagged=True` expecting resolved-OR-untagged | Docstring explicitly states `include_tag=` excludes untagged. Wrapper-layer test 14 pins this. Tool description in the example block uses the correct shape (`exclude_tag="resolved", include_untagged=True`). |
| Schema not exposed (Agno fails to plumb the new parameter through to the model-visible JSON schema) | Wrapper-layer test 17 asserts the schema explicitly, catching any regression at unit-test time. |

## Pre-flight checks for implementer

Before opening a worktree shell:
1. Confirm `origin/main` is the base: `git log --oneline -1` should show `fbf5aa96f` or later.
2. `uv run pytest tests/test_thread_tags.py tests/test_thread_tags_tool.py -x` baseline passes on `origin/main`.
3. After implementing, run with explicit timeout in any pytest invocation: `uv run pytest tests/test_thread_tags.py tests/test_thread_tags_tool.py -x --no-cov` with `timeout=600` from the shell tool.

## Acceptance gate

- All 17 new/updated tests pass.
- Pre-commit clean (ruff, ty, tach).
- Full pytest slice for affected modules clean.
- Live test in DevAgent room confirms strict-superset behavior and at least one new untagged entry visible.
