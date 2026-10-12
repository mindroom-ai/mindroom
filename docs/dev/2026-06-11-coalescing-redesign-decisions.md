# Coalescing redesign decisions (issue #1241)

This record keeps the ordering and batching decisions signed off on 2026-06-11 while triaging the ingress and coalescing tests against the first-principles spec in issue #1241.
It covers which guarantees the redesign kept, weakened, or deliberately dropped, and why.

## Decisions

- Ingress never holds one conversation behind another conversation's in-flight turn: a thread reply dispatches while the room-root turn is still running, because serializing responses in one thread is the response lifecycle lock's job, never ingress.
  This fixes thread replies being held at the gate until the room-root dispatch completed.
  PR #1240's approach, dispatching a second root batch only once the first response had started, was superseded by immediate dispatch with no response-start gating, so a multi-minute turn cannot delay an unrelated conversation.
- Commands are control inputs, not conversation content, so they skip coalescing and execute immediately.
  A `!command` posted after not-yet-dispatched conversational messages therefore runs before them; ordering commands against conversational turns was deliberately dropped.
  A command whose target conversation does not exist yet, such as a thread reply whose root is still pending ingress, fails visibly telling the user the target is not ready, instead of acting on the wrong scope or being dropped silently.

## Accepted limitations

- Receipt order is guaranteed per sender only, which is what issue #1241 requires, and its spec forbids enforcing more.
  Within one conversation, a machine-originated event, which skips debounce, may dispatch while another sender's burst is still debouncing, and that burst then lands as a follow-up.
- An open burst waits for same-sender ingress that arrived inside its window but is still resolving, such as a voice note awaiting transcription, because that ingress may belong to the burst.
  The wait holds even when that ingress later resolves to a different conversation, so one sender's slow ingress can delay that sender's burst in another conversation.
- A follow-up still resolving when its conversation's response completes may become its own next turn instead of joining the combined follow-up.
  The guarantee is no loss plus per-sender order; the wake-generation and backlog-freeze machinery that guaranteed joining was removed rather than kept for this case.
  This was accepted on the condition that the degradation stays measurable, which is why the `follow_up_missed_combined_turn` log event exists.
- Two idle fires into one conversation at the same moment, such as overlapping scheduled fires or a zero-debounce deferred voice burst, may become a turn plus a follow-up instead of one merged turn.
- Failed voice transcription is not required to split the surrounding texts into separate batches; the surviving texts follow normal conversation identity and batching.
- Neither of the last two allowances materialized when the redesign shipped: the deferred room-scope voice burst still dispatches as one turn, overlapping scheduled fires still buffer behind the first, and failed room voice still leaves surviving roots uncoalesced, so the tests kept those stronger assertions.
