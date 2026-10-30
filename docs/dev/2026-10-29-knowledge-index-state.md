# Knowledge index state owned by the collection

This record holds the reasons behind the knowledge index state layout, the alternatives rejected, and the limits accepted.

## Why the state moved into the collection

The candidate checkpoint, its journal, and the two seals were built when Chroma held the vectors, which MindRoom could not write in the same transaction as its own bookkeeping.
Once the store became a MindRoom-owned SQLite file, a second record of "file P is indexed at signature S" outside it could only drift, and the repair code that kept the two aligned (vector probes, unclaimed-row probes, seal row counts, journal compaction) was the larger part of the lifecycle.
Rows and claims now change in one transaction, so every collection is a correct resume point and no probe is needed.

## Decisions

- A collection's `meta.stage` (`building` or `complete`) and `meta.rebuild` are the only lifecycle facts it stores; `stage` separates an unfinished build, which is resumed whatever its free space, from a complete one, which is resumed only if the publication names it as retired.
- The publication names the collection it retired (`retired`), written atomically with it by the publisher.
  A flag cleared after cleanup was tried first: a published forced rebuild later retired by an ordinary publication still carried `meta.rebuild` and would have kept forcing rebuild handling, and a leftover an interrupted cleanup spared could not be told from a resume point.
- `vector_store.SCHEMA_VERSION` is the file's table layout and is migrated in place, while `indexing_config._VECTOR_STORE_FORMAT` stays the vectors' meaning; bumping the format would have re-embedded every base.
- A new candidate is a `VACUUM INTO` copy of the publication instead of a row-by-row copy: the copy is compact, carries the keyword index and the claims, and appears whole or not at all.
- The publication and the refresh status are separate files, each with one kind of writer: the publisher under the source-root lock, and the runtime process under a per-base advisory lock.
  The watcher's unlocked load-modify-save of the shared file was what reverted publications.
- "Refreshing" is not persisted: a persisted running mark is a second owner that lies after a crash; the scheduler's task map answers it.
- A refresh status records `reason_generation`, so a success clears only the source-change marks made before it started.
- One scheduler per process (`runtime_refresh_scheduler`) serves the bots, the API, and file memory, so one semaphore bounds every refresh child; file memory reaches it through the accessor because threading a scheduler through `memory/` would change every memory entry point for one call.
- A child that ran but reported nothing is judged by one comparison: whether the publication changed since the runtime read it before spawning.
- A base whose startup conversion fails is not converted again: its legacy files are deleted with the failure, because refreshes publish over the partly converted base and a later conversion would restore the older publication; its next refresh rebuilds what did not convert.

## Assumptions

- One runtime process per storage root, as `mindroom run`'s runtime lock enforces; the status lock keeps writes whole across a second runtime, but result ordering between two runtimes is not guaranteed.
- In production a cancelled child is killed by SIGTERM, which rolls back its open SQLite transaction; in-process cancellation waits for each store call's thread to finish before the source-root lock is released.
- The startup conversion runs before any refresh or search, with no refresh child of an older release still running.

## Limitations

- A retired collection taken over by the next refresh re-embeds the files its successor already embedded, as before: copying those rows from the publication instead would save the embeddings but is a separate optimization.
- The corpus signature includes each file's stamp, as before, and a file signed within two seconds of its last change is claimed without one; so a later refresh finds the corpus changed though no content did, takes over the retired collection, embeds that file again, and marks the source's other bases stale. Hashing content only would change every stored signature and make a checkout that rewrites timestamps rehash the corpus on every check, so it is left for its own change.
- A child cancelled after its Git sync moved a shared checkout does not report the move, so other bases on that checkout notice it at their own next poll or access.
- A first build whose publication write fails after it was marked complete is resumed from its claims; a later build in the same window is deleted as a leftover and costs one copy plus the files it had embedded.
- While a failing file-memory index retries, memory search still reports the last failure, because file memory resolves its index without the scheduler.

## Rejected alternatives

- Keeping one state file and locking the watcher's write: fixes the revert but keeps two writers in two processes and the inference of what the child did from state fingerprints.
- Moving the checkpoint into its own SQLite file beside the base: still two stores that must agree.
- Dropping the retired collection and always copying: simpler, but the documented behavior is that a refresh rewrites only the files that changed.
- Passing the scheduler through `memory/`: see the decision above.
- Failing startup when a base's conversion fails: it keeps the legacy state authoritative until the conversion succeeds, but one corrupt collection file would stop every start until an operator deleted it.
