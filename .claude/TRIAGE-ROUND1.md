# Round 1 Triage

- F1: FIX. `src/mindroom/coalescing.py` still had a local automation exemption set that omitted `hook_dispatch`, so I switched coalescing to the shared `AUTOMATION_SOURCE_KINDS` constant.
- F1 reviewer-E variant: IGNORE. I could not find a live `"dispatch_hook"` branch in the current code, so there was no separate string-mismatch bug left to fix.
- F2: FIX. Hook suppression in `_prepare_dispatch()` only marked the primary event as responded, so I threaded `source_event_ids` into that path and mark the full batch.
- F3: FIX. `_resolve_text_dispatch_event()` dropped the internal synthetic `source_kind`, so coalesced voice turns lost their `voice` envelope classification.
- F4: FIX. Coalescing sorted synthetic `enqueue_time` in seconds against Matrix timestamps in milliseconds, so I normalized synthetic/fallback timestamps to milliseconds.
- F5: FIX. Reply-chain routing decisions were running before required full-thread hydration, so I hydrate before coalescing suppression and action resolution when `requires_full_thread_history` is true.
- F6: FIX. `_build_dispatch_payload_with_attachments()` discarded fallback images whenever at least one registered image existed, so I now merge both sets.
- F7: FIX. Trusted router relay messages with `com.mindroom.original_sender` were still entering the user-turn coalescing gate, so they now bypass coalescing and dispatch immediately.
- F8: FIX. Visible router echo reconciliation only checked the primary batch event, so I now search all `source_event_ids` and mark the whole batch against the discovered echo.
- F9: FIX. `ResolvedToolConfig` and `ToolkitDefinition` in `src/mindroom/config/models.py` were dead duplicates with no callers, so I removed them.
- F10: FIX. `tests/conftest.py` zeroed coalescing timers by matching hard-coded default numbers, so I changed it to detect whether the timer values were explicitly authored instead.
- F11: IGNORE. `_on_media_message`'s narrow type annotation is a typing nit with no runtime effect and no correctness impact on this PR.
- F12: IGNORE. The defensive audio branch in `_dispatch_prompt_for_event()` is unreachable under the current type contract, but removing it is cleanup rather than a bug fix.
- F13: IGNORE. The broader config additions are review-scope commentary rather than a concrete correctness bug, and the only actionable issue there was the dead duplicate code already removed as F9.
