# One Matrix thread reader, and a turn's thread decided once

## Decisions

- Tools and outbound mutations prove thread roots by reading the candidate's thread strictly from the conversation projection, the reader turns use.
  The client-side `/messages` room scan they used was a second reader with its own grouping and edit folding, and it had already drifted: it stopped at the first empty page even when the page carried an `end` token, which the Matrix spec allows, and its "root not seen" result was a definitive "no thread".
- A read with no readable child disproves a root only when a fresh walk of that thread read everything, and only for callers that act on the answer (tools and outbound mutations).
  Hydration drops replies it cannot decrypt and marks the walk incomplete, so absence there is not evidence.
  The projection's own completeness is not used for this, because it also folds in room-wide history recovery, which concerns the room timeline rather than one thread's relations; a recovery that stopped at its ceiling would otherwise refuse every redaction of a plain message in that room.
  A projected reply proves the root without a walk.
  The turn resolver keeps accepting that absence: refusing it would make ingress raise on every journal retry, and a reply to a message whose replies the bot can never decrypt would never be answered.
  Such a reply is answered as a reply to a plain message, which starts its own thread.
- The summary pin re-check is the one caller that walks a thread on the server again after its hydration marker stands, because it must see a pin another runtime wrote while the model ran.
  The walk is its own: it never joins an earlier in-flight hydration, it succeeds only when the current membership accepted its install, and it reports its own completeness, because the durable marker keeps the widest walk ever recorded.
  A re-check that could not read the whole thread suppresses the summary, since a pin can be an edit of any earlier summary.
  The walk also fetches the direct edits of every summary the re-check already holds, because Tuwunel's recursive relation walk can page a reply's edits out while its direct-edit lookup stays right.
- Ingress decides a turn's thread once (`coalescing_thread_id`), and that decision is persisted for replay.
  Dispatch builds the target from it under the room's current thread mode, then reads context for the thread the target says the source is in, so context and target cannot disagree.
  Edit regeneration reads the recorded target's thread strictly, whatever the room's thread mode is now.
- Every write of `matrix_state.yaml` goes through one read-modify-write with no await inside, under a reentrant lock that also covers the loader's legacy rewrite.
  Writers used to save whole copies they had loaded before awaiting the network, which lost a bot's freshly persisted device and token and made its next restart fail permanently.

## Rejected alternatives

- Keeping the room scan as a second opinion for tools: two owners for one fact, and the scan was the reader that drifted.
- Fixing the scan's empty-page stop in place: removes the symptom and keeps two readers.
- Requiring complete history for every negative root proof, turns included: wedges turns whose thread replies the bot cannot decrypt.
- Reconciling the dispatch-time resolution with the ingress decision through a refresh: still two decisions, and the ingress one is the one the batch was formed under and the one replay uses.
- Letting a decided thread override room mode in `build_message_target`: router relays rely on the target agent's room mode applying to a threaded source.
- A file lock for `matrix_state.yaml`: one process owns the file, and the bug was a copy held across awaits, not a second process.

## Limitations

- A plaintext upload is rechecked before its event is sent, but encryption enabled in the moment between that recheck and the send still sends the plaintext reference; the window holds no network round trip except the send itself.
- A redaction or reply whose target has an edit this bot cannot decrypt is refused, because that thread's walk is incomplete.
- A thread longer than one projected page (2,000 messages) is never summarized automatically.
- The room timeline read of the `matrix_message` tool still folds edits client-side over raw `/messages` pages; it answers "what was said in this room lately" across threads, which the projection has no read for.
