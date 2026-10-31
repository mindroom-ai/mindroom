# Delegation owners: target resolution, pause state, approval resumes, and the running child

This record holds the decisions behind one owner for turning a delegation call into its target, one stored list of a delegation pause's calls, one decision for how a saved approval's paused run resumes, and one context for the running child.
It records reasons, rejected alternatives, and accepted limitations; the code shows the mechanisms.

## Decisions

- The direct delegate tools and the native delegation driver stay two execution adapters, because runs that cannot pause (the OpenAI-compatible API, voice calls, minimal children, and workflow participants) still need a child that finishes inside the caller's tool call.
  Only reading a call's arguments into its target is shared; authorizing the target stays with each adapter.
- Authorization stays outside the shared resolver because the native driver must treat a refused authorization of a child it already started (interrupting or forgetting it, closing its visible tools, running the after-hook) differently from a refused resolution.
- The fresh and follow-up resolvers are separate entry points because a fresh direct call must still return a refusal when no caller identity exists, while a follow-up loads a session only its owner may read.
- Both adapters resolve the target, including the follow-up refusal for a subagent whose caller lost a tool or that belongs to a Dynamic Workflow, before authorizing it.
  The direct tools used to authorize first, so a follow-up that failed both checks showed the authorization refusal there and the follow-up refusal on the native path; it now shows the follow-up refusal on both.
- The caller's delegation allowlist is read from its config in one place; the toolkit and the authorization check used to receive the same list from two owners.
- A delegation pause stores its pending calls once, as requirements, since each requirement holds its tool call; the separate tool list mirrored a framework pause shape that MindRoom no longer uses.
- The decision of how a saved approval's paused run resumes has one owner that returns the stream to resume from and the decided calls the delegation driver still answers.
  Each caller keeps its own execution: agents and teams stream through the driver, and a resumed delegated child drives its nested delegations inline, without an event sink.
- One context tracks the running child; the record adapter is a value that context holds rather than a second context, and the one check of whether a finished run is the child's lives with the context.

## Rejected alternatives

- Placing the shared resolver in `delegation/personas.py`: recovery imports lifecycle, which imports personas, so a resolver there that loads sessions would form an import cycle.
- A stream-level resume helper that also runs the delegation driver: it would move a resumed child's nested driving into the driver's callback task and give it an event sink, which reads back every previous pending grandchild tool, approved or denied, and adds a failure point before a grandchild's new pause is handled.
- Resuming a saved approval inside the driver loop itself: every agent approval resume's own event stream would move into the callback task.
- Merging the two execution adapters, one pause mechanism for team members and delegated children, and fewer persisted child copies: see the survey's rejected candidates; each covers a different run, turn, or crash window.

## Accepted limitations

- A finished-run record of another run in a child's stream takes no stream number in the child's record; before, it took one and was then dropped.
  No reachable stream hands a child's observer another run's record, so record ids are unchanged in practice.
- Two team members that pause on the same tool-call id fail the reply when it pauses, and team denials are routed by call id alone.
  Providers generate unique call ids, so this is accepted rather than handled.
