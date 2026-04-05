R2-F1. FIX
Reason: Coalesced turns were only persisting the primary event ID in run metadata, so secondary batch members could remain unseen and stale-run cleanup could miss them.
Change: Persist full batch source event IDs in run metadata and seen-event tracking while keeping the primary event ID as the reply anchor only.

R2-F2. FIX
Reason: Edit regeneration for coalesced turns rebuilt from only the edited event, which dropped sibling prompt content and let non-primary edits leave the stale run behind.
Change: Persist coalesced batch membership plus per-event prompt fragments, rebuild the full combined prompt on any member edit, and block regeneration if that metadata is unavailable.

R2-F3. FIX
Reason: Upload grace is only meant to absorb late media arrivals, but plain text during grace was being merged into the earlier batch.
Change: Keep grace-phase merging limited to media events; text flushes the current batch and starts a new debounce window.

R2-F4. FIX
Reason: Mixed batches could inherit `source_kind` from the primary event only, which misclassified media-containing turns such as image-then-text sequences.
Change: Derive batch `source_kind` from the full batch with precedence `voice > image > media > message`.
