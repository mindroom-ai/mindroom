# OpenRouter Reasoning-Details Coalescing

## Problem

OpenRouter emits `reasoning_details` in streaming delta chunks. Agno 2.6.12
extends provider-data lists verbatim, so a single indexed reasoning block becomes
thousands of tiny dictionaries. MindRoom persists and replays those dictionaries,
inflating session storage, request payloads, token accounting, and allocator
high-water memory.

## Design

Add an OpenRouter-only compatibility layer to `MindRoomOpenRouter`.

- During streaming aggregation, merge adjacent dictionary fragments only when
  both have `type == "reasoning.text"`, both have string `text` values, and
  their dictionaries are exactly equal after removing `text`. Missing and
  explicitly null metadata are therefore different, so signatures, IDs, and
  future provider fields can never be silently dropped. At least one stable
  block discriminator must also be present: a numeric `index` or a non-empty
  string `id`. Non-adjacent fragments remain separate even when their metadata
  matches.
- Concatenate only the `text` field. Leave malformed values, non-text reasoning
  types, and ambiguous or conflicting fragments separate.
- Before wire formatting, apply the same non-mutating normalization to persisted
  assistant messages. This immediately prevents old polluted history from being
  replayed verbatim without rewriting the database in the request path.
- Leave non-text reasoning types and unrelated provider data untouched.

## Existing Data

After deploying the compatibility layer, stop Mom's service, checkpoint its WAL,
create a timestamped backup through SQLite's backup API, and verify the backup with
`PRAGMA integrity_check` before mutation. Transactionally coalesce compatible
fragments in every persisted occurrence: run messages, top-level
`model_provider_data`, and stored events when present. Validate database integrity,
session/run counts, detail counts, and concatenated content before and after. Run
`VACUUM` only after validation when physical file-size reduction is desired.

## Testing

Regression tests will prove that:

1. streamed same-index text fragments become one stored detail;
2. different indexes or conflicting metadata remain separate;
3. missing/conflicting signatures and IDs remain separate;
4. malformed details, non-string text, multiple details per delta, and
   non-adjacent matching identities are handled conservatively;
5. replay normalization does not mutate persisted `Message` objects;
6. unrelated provider data survives unchanged.

Deployment verification will check service health, database integrity and size,
the effective model configuration, and fresh logs after the controlled restart.
