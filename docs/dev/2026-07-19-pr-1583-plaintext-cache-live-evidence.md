# PR 1583 Plaintext Cache Live Evidence

This records live runs on 2026-07-19 of PR #1583's SQLite event cache with encrypted large-message sidecars, covering principal and room isolation of decrypted plaintext, restart reuse, redaction, and leave and rejoin.
The event cache it exercised was later removed; see [Matrix Event Cache Live Evidence](2026-07-18-matrix-event-cache-live-evidence.md).

## Environment

- The first passing run used commit `0acc7e490` from PR #1583, and the complete scenario was rerun against integrated production-source commit `fc6b4f129` after merging current `main`; PR #1583 was squash-merged as `b1949a4dc`, so neither branch commit is in this repository.
- Both runs used a disposable local Synapse, disposable Matrix accounts for the principals Alpha, Beta, and Router, a real encrypted Matrix room, a deterministic OpenAI-compatible model stub, and the PR's SQLite event cache.
- No production service, database, room, token, or user data was read or changed, and the evidence contains no access tokens or plaintext message bodies.

## Limitations

PostgreSQL parity at the tested PR head was validated by the disposable real-PostgreSQL backend contract suite, not by these SQLite live runs.

## Initial failure

The first live run created a real encrypted large-message sidecar but failed to hydrate it, because the sidecar path passed the complete Matrix JWK object to `nio.decrypt_attachment` instead of the JWK's `key["k"]` value.
Commit `0acc7e490` corrected that and added a real `nio` encrypt-to-decrypt regression test, and the complete live scenario was restarted and passed.

## First passing run

- Authenticated room state reported `m.megolm.v1.aes-sha2`, and an authenticated event read reported the sidecar's wire type as `m.room.encrypted`.
- The decrypted sidecar payload was 337,861 bytes with SHA-256 `f2173cb9661068fae15b8e0b09ea5ced1cd6eaf98cfb802301ea4ba9eba53720`.
- Alpha, Beta, and Router each had an independently principal-owned plaintext row with that hash and an event-to-MXC reference for the same room-scoped sidecar.
- The public cache API returned Beta's event and plaintext in the owning room, and the same lookups returned `None` for a wrong room and for an unjoined principal.
- After a graceful restart against the same SQLite database and Matrix crypto state, Router, Alpha, and Beta each logged `matrix_sync_token_restored` with `certified=True`, and the sidecar reads logged durable `mxc_text_cache_hit`.
- Alpha's plaintext `cached_at` was identical before and after the restarted read, proving reuse rather than a rewrite.
- For a second sidecar whose original event had a dependent edit and whose plaintext both Alpha and Beta owned, redacting the original removed both event rows, the edit mapping, thread rows, event-to-MXC references, and the room-scoped plaintext row for both principals.
- Each principal retained exactly two tombstones, one for the original and one for the dependent edit.
- After another restart, attempts to store the original, the dependent edit, or the plaintext returned no readable event, no readable plaintext, and `False` from the plaintext write for both principals.
- A real Matrix kick moved Alpha from joined membership epoch 0 to departed epoch 1 and left Alpha zero room-scoped event, edit, thread, reference, plaintext, tombstone, or thread-state rows.
- Beta's plaintext row stayed byte-for-byte unchanged, including its `cached_at`, and the authenticated joined-member response contained Beta and not Alpha.
- A real Matrix invite and join moved Alpha to joined epoch 4, after which Alpha cached a new event, Alpha's old plaintext did not reappear, and Beta's surviving plaintext remained readable.
- The runtime health endpoint was healthy with zero E2EE decryption failures, key requests, and failure notices.

## Integrated rerun

The rerun exercised the actual SQLite version 11 to version 12 reset: the version 12 runtime reset a disposable version 11 cache, rejected every old sync checkpoint, rebuilt the cache from a cold sync, and then passed the same encrypted lifecycle on the version 12 schema.
Run-specific results:

- The decrypted sidecar payload was 337,861 bytes with SHA-256 `0269501a5522f55022369921426185663e5047512a4674c34d13f3d7565bc76f`.
- Alpha and Beta had different principal-bound cache generations.
- After the graceful restart, Router, Alpha, and Beta each restored a certified version 2 sync checkpoint.
- The kick moved Alpha from joined epoch 2 to departed epoch 3, and the invite and join moved Alpha to joined epoch 6.
