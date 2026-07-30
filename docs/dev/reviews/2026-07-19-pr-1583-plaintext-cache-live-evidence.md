# PR 1583 Plaintext Cache Live Evidence

This evidence was first collected on 2026-07-19 against commit `0acc7e490` from PR #1583.

The complete scenario was rerun against integrated production-source commit `fc6b4f129` after merging current `main`.

The integrated run exercised the actual SQLite version 11 to version 12 reset, rebuilt the cache from a cold sync, and then passed the same encrypted lifecycle on the version 12 schema.

The run used a disposable local Synapse, disposable Matrix accounts, a real encrypted Matrix room, a deterministic OpenAI-compatible model stub, and the PR's SQLite event cache.

No production service, database, room, token, or user data was read or changed.

The evidence contains no access tokens or plaintext message bodies.

## Integrated rerun

The version 12 runtime transactionally reset the disposable version 11 cache and rejected every old generation-bound sync checkpoint before rebuilding principal-owned rows.

The new encrypted sidecar event was `$YnqnZwdcEsYtcBV0XKu37-HboaIS8oW_tBxsChOPVwk`.

Its decrypted payload was 337,861 bytes with SHA-256 `0269501a5522f55022369921426185663e5047512a4674c34d13f3d7565bc76f`.

Alpha, Beta, and Router independently owned matching plaintext and event-to-MXC reference rows.

Alpha and Beta had different principal-bound cache generations.

After a graceful restart, Router, Alpha, and Beta each restored a certified version 2 sync checkpoint.

Alpha's plaintext `cached_at` remained exactly `1784488158.4635851` across the restarted read.

Redacting original event `$EHrR8b2jLjMRX6RJhaX17aC25gR_4b9osNSf8WWDL84` removed dependent sidecar edit `$riNe0TNBL8-rc0x3XOrkmnqPznn1dyiQPsYMEH1auMk`, their indexes and references, and their plaintext for both principals while retaining two tombstones per principal.

A second restart rejected direct attempts to restore either redacted event or its plaintext.

A real kick moved Alpha from joined epoch 2 to departed epoch 3 and removed every Alpha-owned row in the room without changing Beta's plaintext bytes or `cached_at`.

A real invite and join moved Alpha to joined epoch 6, allowed a new event, and did not resurrect Alpha's old plaintext.

Final public cache reads returned Beta's surviving event and plaintext only in the correct room and principal scope.

The final runtime health endpoint was healthy, with zero E2EE decryption failures, key requests, or failure notices.

## Initial failure and correction

The first live run created a real encrypted large-message sidecar but failed to hydrate it.

The failure showed that the sidecar path passed the complete Matrix JWK object to `nio.decrypt_attachment` instead of the JWK's `key["k"]` value.

Commit `0acc7e490` corrected that owning boundary and added a real `nio` encrypt-to-decrypt regression test.

The complete live scenario was restarted and passed after that correction.

## Encrypted sidecar and principal isolation

The disposable room was `!zjUjSsUeDPteLoqiSO:localhost`.

Authenticated room state reported `m.megolm.v1.aes-sha2`.

The large sidecar event was `$SbOfZ1HyDKrB-Pvf7vgS1wM0UJQhwxFDkJAMStxugUU`.

An authenticated Matrix event read reported the wire event type as `m.room.encrypted`.

The decrypted sidecar payload was 337,861 bytes with SHA-256 `f2173cb9661068fae15b8e0b09ea5ced1cd6eaf98cfb802301ea4ba9eba53720`.

Alpha, Beta, and Router each had an independently principal-owned plaintext row with the same payload hash.

Alpha, Beta, and Router each had an event-to-MXC reference for the same room-scoped sidecar.

The public cache API returned Beta's event and plaintext in the owning room.

The same event and plaintext lookups returned `None` for a wrong room and for an unjoined principal.

## Restart persistence

The runtime was stopped gracefully and restarted against the same SQLite database and Matrix crypto state.

Router, Alpha, and Beta each logged `matrix_sync_token_restored` with `certified=True`.

The restarted runtime logged durable `mxc_text_cache_hit` reads for the encrypted sidecar.

Alpha's plaintext `cached_at` value remained exactly `1784486336.918538` before and after the restarted read, proving reuse rather than a rewrite.

## Redaction and non-resurrection

A second encrypted sidecar used original event `$nqyaHmlyMwh2O4ZE2xkkZWMyXVDFEzySEQmrbePzm_A` and dependent edit `$1iK9K9AXjk3QzaXc7ImJRjlskn-o0Yqlff1YWkOQKp4`.

Before redaction, Alpha and Beta independently owned plaintext rows for the second sidecar.

Redacting the original removed both event rows, the edit mapping, thread rows, event-to-MXC references, and the room-scoped plaintext row for both principals.

Each principal retained exactly two tombstones, one for the original and one for the dependent edit.

The runtime was restarted again against the same durable state.

Attempts to store the original, dependent edit, or plaintext returned no readable event, no readable plaintext, and `False` from the plaintext write for both principals.

## Leave and rejoin

Alpha began joined at membership epoch 0.

A real Matrix kick moved Alpha to departed membership epoch 1.

Alpha then had zero room-scoped event, edit, thread, reference, plaintext, tombstone, or thread-state rows.

Beta remained joined and its plaintext row was byte-for-byte unchanged, including the original `cached_at` value.

The authenticated joined-member response contained Beta and did not contain Alpha.

A real Matrix invite and join moved Alpha back to joined membership epoch 4.

Alpha successfully cached a new post-rejoin event.

Alpha's old plaintext did not reappear merely because Alpha rejoined, while Beta's surviving plaintext remained readable.

## Result

The live runtime health endpoint was healthy with zero E2EE decrypt failures, zero key requests, and zero failure notices.

The scenario passed encrypted wire transport, real sidecar decryption, principal and room isolation, certified restart reuse, atomic redaction, durable tombstone rejection, principal-scoped leave cleanup, and safe rejoin.

PostgreSQL parity remains validated by the PR's disposable real-PostgreSQL backend contract suite rather than by this SQLite live runtime.
