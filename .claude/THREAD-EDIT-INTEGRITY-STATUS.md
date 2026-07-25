# Thread edit integrity status

Current PR: #1641.
Current frozen head before resumed work: `243ec1250a5a0fa6cce9fa7e2a2bd9704addbd11`.
Current production head: `09bfb180cf6b9400b9afd09f1a40761726d74fc9`.
Current production diff: `+756/-557`, net `+199`; hard ceiling is net `+200`.

Fresh exact-head native Codex `gpt-5.6-sol` xhigh review is `CHANGES REQUIRED`.
The confirmed blocker is post-hydration validation of v2 edit sidecars.
A raw-valid replacement can hydrate to malformed canonical `m.new_content`, become visible, and suppress an older valid replacement.
The same gap affects direct/full history, bundled preview, and cached point projection.

Planned narrow correction:

- Revalidate hydrated replacement content at the shared edit-extraction seam.
- When an original is available, revalidate the hydrated relation and identity against that original.
- Make cached point projection skip a hydrated-invalid newest candidate and request the next valid cached candidate.
- Add deterministic direct/full-history, bundled-preview, and SQLite/PostgreSQL cached fallback regressions.
- Keep production source at or below net `+200`.

No PostgreSQL, full pytest, Docker, all-file, or live validation may start without explicit resource-gate ownership.
The current heavy owner is none, but the user explicitly prohibited heavy tests for this phase.
Existing exact-head reviews and gates are stale as soon as this work is committed.
Do not merge, amend, force-push, or use temporary worktrees/evidence.
