# Delegation owners: target resolution, approval resumes, and the running child

This record holds the decisions behind one owner for turning a delegation call into its target, one decision for how a saved approval's paused run resumes, and one context for the running child.
It records reasons, rejected alternatives, and accepted limitations; the code shows the mechanisms.

## Decisions

- The direct delegate tools and the native delegation driver stay two execution adapters, because runs that cannot pause (the OpenAI-compatible API, voice calls, minimal children, and workflow participants) still need a child that finishes inside the caller's tool call.
  Only reading a call's arguments into its target is shared; authorizing the target stays with each adapter.
- Authorization stays outside the shared resolver because the native driver must treat a refused authorization of a child it already started (interrupting or forgetting it, closing its visible tools, running the after-hook) differently from a refused resolution.
- The fresh and follow-up resolvers are separate entry points because a fresh direct call must still return a refusal when no caller identity exists, while a follow-up loads a session only its owner may read.
- Both adapters resolve the target, including the follow-up refusal for a subagent whose caller lost a tool or that belongs to a Dynamic Workflow, before authorizing it, so a follow-up that fails both checks shows the follow-up refusal on every channel.
- The caller's delegation allowlist is read from its config in one place, so the toolkit's description and the authorization check cannot disagree on it.
- The decision of how a saved approval's paused run resumes has one owner that returns the stream to resume from and the decided calls the delegation driver still answers.
  Each caller keeps its own execution: agents and teams stream through the driver, and a resumed delegated child drives its nested delegations inline, without an event sink.
- One context tracks the running child; the record adapter is a value that context holds rather than a second context, and the one check of whether a finished run is the child's lives with the context.

## Rejected alternatives

- Placing the shared resolver in `delegation/personas.py`: recovery imports lifecycle, which imports personas, so a resolver there that loads sessions would form an import cycle.
- A stream-level resume helper that also runs the delegation driver: it would move a resumed child's nested driving into the driver's callback task and give it an event sink, which reads back every previous pending grandchild tool, approved or denied, and adds a failure point before a grandchild's new pause is handled.
- Resuming a saved approval inside the driver loop itself: every agent approval resume's own event stream would move into the callback task.
- One pause mechanism for team members and delegated children: members pause inside the team's run record and children in their own sessions, so merging them is a redesign with no verified gain.
- Fewer persisted copies of a child (its session file, its parent's state, its job, and the sources behind its pending calls): each serves a different turn or crash window, and a follow-up can overwrite the session file while a parent still needs the earlier turn's result.

## Accepted limitations

- A finished-run record of another run in a child's stream is neither recorded nor numbered in the child's record.
  No reachable stream hands a child's observer another run's record, so the record's stream numbers match the child's own events.
- Two team members that pause on the same tool-call id fail the reply when it pauses, and team denials are routed by call id alone.
  Providers generate unique call ids, so this is accepted rather than handled.
