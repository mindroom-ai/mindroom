# ResponseRunner Simplification Plan

This plan reduces active duplication in `src/mindroom/response_runner.py` without inventing a generic executor too early.

The immediate goal is to make the team and agent locked paths structurally closer while preserving the behavior differences that still matter.

## Scope

The current duplication is concentrated in the tail after `run_cancellable_response()`.

That tail currently repeats cancelled-hook emission, final event id resolution, post-response effects, and pipeline outcome reporting in both the team and agent locked methods.

The team path is also still more inlined than the agent path, especially around preparation and delivery.

## Sequence

1. Extract one shared `_finalize_locked_response(...)` helper for the duplicated post-run tail.
2. Keep `swallow_late_cancellation` explicit so the agent path can keep its existing late-cancellation behavior.
3. Reuse `_PreparedResponseRuntime` instead of introducing a second overlapping prepared-state carrier.
4. Pull the team path toward the same preparation shape already used by the agent path.
5. Extract team-specific delivery helpers only where the team path already has obvious seams.
6. Re-evaluate whether the remaining locked shell is small and symmetric enough to unify.
7. Stop once the file is materially clearer, even if the two locked methods still remain separate.

## Guardrails

Do not introduce a new generic executor unless it clearly removes code and makes control flow easier to read.

Do not add a second prepared-runtime dataclass that overlaps with `_PreparedResponseRuntime`.

Do not force streaming and non-streaming into the same helper before the shared code is obvious.

Preserve the current behavior differences explicitly.

Those differences include `swallow_late_cancellation`, session type, thinking-message text, and the team path’s `delivery_target` versus `resolved_target` distinction.

If a helper starts needing many callbacks or opaque policy objects, stop and keep the caller-specific code local.

## First Increment

The first implementation step is to extract the shared finalization tail and make both locked methods call it.

That change is the safest simplification because it removes duplication without changing preparation or delivery structure.
