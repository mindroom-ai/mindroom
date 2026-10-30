# Inbound turns and turn state: one classification, one claim owner, one copy of each fact

Decisions and evidence behind the 2026-10 refactor of inbound turn handling and durable turn state.
The code shows what each owner does; this record keeps why, what was rejected, and what stays limited.

## Why

- An inbound event was classified at many sites with different inputs and trust rules, and the command predicates before and after the coalescing gate disagreed for hooks, triggers, relays, queued edits, and replies with tool markers; #2593 had patched one of them for scheduled text.
- A turn claim was a value released by equality and handed along by six mechanisms; each exit path released by hand, and a missed one kept the journal from ever replaying the source (#1638, #1765, #1780).
- "Event X was redacted in room R" had three copies with different scopes and lifetimes, kept in step by ten reconcilers; seven fixes in three months were this class (#1569, #2438, #2544, #2672, #2681, #2959, #3007).
- A turn record was one JSON blob per indexed event, written from an in-memory copy published before commit, so every writer merged with whatever else committed; #1646, #1671, #1639, and #2121 were all lost updates or a new field missing from one merge.

## Decisions

### Claims

- A claim is an identity handle; release removes exactly that handle and is idempotent.
- One claim registry per agent and database is shared by every turn store of that agent in the process.
  It replaces the guarantee pre-commit publication of the old in-memory ledger gave sibling stores during a runtime replacement or re-login, and it is stronger: it also covers a response that has not written anything yet.
- A contended claim defers instead of waiting.
  The old wait ran inside the room's journal lane, so a turn held for an approval could wedge the room until the approval decision, queued behind it in the same room, ran.
  The deferred source counts as owned and is retried when the blocking claim releases.
- A voice claim outlives an abandoned readiness task until that task ends, because readiness can still be finishing a checkpoint write a sibling store must not race.

### Classification and commands

- Provenance is classified once from the raw event before precheck, because precheck needs the requester, the claim needs the discovery alias, and final-reply recognition needs trusted content.
- The command is decided once after normalization, from the trace-stripped command text, by one predicate every stage calls.
- Authorization is never frozen into provenance: it stays current under the admission fence.
- Payload trust belongs to the normalized payload, not to the sender, because MindRoom produces voice metadata for a human's audio.
- Only a single-source turn of an ordinary message runs as a command, under its run requester's authority (a person, or the person an agent wrote it for).
  Hook, trigger, relay, schedule, selection, voice, media, and edit-regeneration text is conversation: a plugin or an agent can write it, yet it would run with a person's authority.
  This was the user's decision; only hook text changes in practice, because trigger and relay text begins with a mention.
- Per-run scheduled settings keep their intent-based acceptance, because a schedule a managed agent created and the router routed among several responders carries no original sender.

### Redactions

- One table, keyed by agent, room, and event, holds every redaction, written in the admission, hydration, or history-recovery transaction that saw it.
- Agent, not principal: "answered" is agent-scoped so it survives a re-login, and "redacted" must have the same lifetime, or a re-login would show deleted content again.
- Kept when the bot leaves a room: the rows hold ids only, and a bot that rejoins must not show content deleted while it was away.
- The deletion's effects follow the same scope: every login of the agent has the redacted plaintext scrubbed from its projections and pending rows; another agent's rows are never touched.
- Physical redaction and source deletion are different queries over the one table: deleting the message a relay answers for deletes the relay's source, and redacting one edit blocks only that edit's text.

### Turn rows

- Physical membership and alias lookup are separate tables: a physical source belongs to one turn, while an original and its relay may both index the original.
- A turn key names one immutable ordered source tuple; a different tuple is a new turn.
  That makes the key a delivery binding: a non-AI final answer binds to its turn when its outbox row becomes durable, and its acknowledgement completes exactly that turn.
- Edit revisions are recorded per turn, because one edit can be selected by one turn and consumed as context by others.
- A voice echo is a per-source publication fact and never the turn's answer identity: two finalized echoes in one coalesced routing turn otherwise completed the routing turn without sending the relay after a crash.
- Re-forming a batch is one explicit merge policy (absorb physical owners, keep the candidate's context, adopt an interrupted placeholder, copy watermarks and consumed-revision proofs), not a generic merge engine.
- Lookup prefers the turn that completed, so an edit of an original whose relay answered regenerates the relay's answer.
- The pending-turn write returns what it read and wrote instead of reading its rows again: every reply awaits it on the journal's single writer before it starts, and a new conversation holds one of the eight root-preparation slots meanwhile, so each statement it adds lengthens that wait and the queue of every other write.

### Migration

- Row ownership of the old per-index blobs is authoritative, not declared blob identity, because an interrupted pre-journal import left overlapping blobs that each still answered some index.
- Redactions and legacy summary provenance are extracted from every blob before any duplicate is discarded.
- Room-less tombstones are dropped: they cannot be told apart from the pre-v2026.10.31 cross-room ones that wedged threads, and legitimate ones aged out within 30 days.
- Outstanding non-AI final deliveries are bound to their converted turns: quiescence means no active response, not no outstanding transport work.

### Edit regeneration

- Edit regeneration takes its response action from the same policy normal turns use, so no separate team bot class resolves teams a second time; a rejected team regeneration still edits and settles its existing reply through the reply's records.

## Rejected alternatives

- A read cache derived from the rows: it would keep two copies of "answered" and the publication rules that came with them.
- Starting a response while its pending-turn write is still queued: the stored turn supplies the request's sources, run metadata, and recovery proof, and the write decides whether a response starts at all, so only the lifecycle-lock acquisition could overlap it.
- Writing the pending turn in the reply claim's transaction: the decision to start would move past the lifecycle lock into the response runner, which also serves edits, approvals, and replays.
- Whole blobs rewritten inside one transaction: it removes lost updates but keeps one copy per index and the stale-row cleanup.
- Principal-scoped redactions: they lose redactions on re-login and on departure.
- Releasing the lane slot while waiting on a contended claim: the wait still held the room's journal lane.
- Refusing a re-formed batch whose stored context differs: a configuration change before a replay would leave the turn unanswerable forever.
- Splitting the surviving sources of a batch that already has a reply across new replies: reply spans pin their sources, so a survivor would meet the finished reply and never be answered.
- Keeping a latest-event origin beside the batch's reduced source kind: one batch would carry two origins, so turn policy and the dispatch envelope could read different provenance.

## Limitations

- After a configuration change moves an interrupted turn's conversation, its adopted reply keeps its own placement, so that one turn's stored target and its visible reply can differ.
- Deleting one source of a batch whose reply already exists suppresses the whole request, as before; only a batch without a reply answers its surviving sources.
- On a database from before the Nio 1 journal, earlier logins' projections are not scrubbed by the conversion, because that upgrade dropped the membership rows that name them.

## Evidence

- A characterization oracle (`tests/turn_state_oracle/`) recorded the old store's observables before the rewrite; it found the dropped echo, a re-formed batch copying another turn's placeholder until a lookup raised "Multiple turns own visible response", and an ownership error that re-raised on every retry.
- While the pending-turn write read its rows again, a new one-source turn ran 23 statements on the writer, against 5 for the ledger upsert it replaced; returning what it read and wrote brought it to 8.
  With 30 new conversations sent at once, most of the wait this added before `Processing` was later messages waiting for one of the eight root-preparation slots.
