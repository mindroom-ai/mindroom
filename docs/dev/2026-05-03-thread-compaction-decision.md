# Thread-Scoped Compaction Decision

This note records the compaction behavior agreed for the history compaction PR on May 3, 2026.

## Goal

Compaction should be visible, durable, and easy to reason about from the user's perspective.

When a conversation needs history compaction, the user should see a compaction status message in the relevant thread.

That thread should wait for compaction to finish before the next agent reply continues.

Other rooms and other threads should continue normally.

## Decision

MindRoom should have three compaction options.

1. Manual requested compaction, where the agent calls the `compact_context` tool and the next reply in that same conversation scope runs forced compaction before answering.
2. Required automatic compaction, where history preparation runs compaction before replying because the thread history exceeds the hard context budget.
3. Disabled automatic compaction, where automatic compaction does not run.

Manual `compact_context` may still force compaction for the next reply when destructive compaction is available.

All compaction that runs as part of these paths must post the visible compaction lifecycle message and update it as the operation succeeds, fails, or times out.

## Non-Goals

There should not be a separate opportunistic or background post-response compaction path.

There should not be room-wide blocking while a single thread compacts.

There should not be an invisible compaction path for these flows.

## Why Before The Next Reply

Running compaction before the next reply is simpler than starting it after the previous response.

The next reply is the point where compacted history is actually needed.

This also keeps blocking naturally scoped to the thread that is about to use the compacted history.

It avoids a race where post-response compaction is running while a new message arrives.

It avoids requiring a background compaction registry whose only purpose is to make the next turn wait for work that could have been started by that next turn directly.

## User-Facing Behavior

When a thread needs compaction, the visible compaction message appears before the agent answer.

The agent answer starts only after compaction has completed or failed.

If compaction fails or times out, the thread receives the compaction failure status and the normal reply path handles the failure outcome.

No other thread should be blocked by that work.

## Implementation Implications

The compaction decision should be made during history preparation for the incoming reply.

The durable force marker created by `compact_context` should be consumed by the next history preparation for the same scope.

The visible lifecycle belongs to the foreground compaction run for that scope.

The post-response effects system should continue to handle memory persistence, thread summaries, and other non-compaction side effects.

The post-response effects system should not start compaction.

The policy should not return a `post_response` compaction decision.

The remaining automatic decisions should be `none` or `required`.

## Terms

`manual forced compaction` means the agent called `compact_context`, which records durable intent for the next reply in the same scope.

`required compaction` means history preparation determined that replying without compaction would exceed the hard budget.

`disabled compaction` means automatic compaction is disabled by configuration, while manual force may still be honored when compaction support is available.
