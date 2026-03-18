# Team Runtime Resolution Refactor Plan

## Objective
Replace the current split ownership of team runtime eligibility with one shared resolution layer.
Keep the refactor narrow by unifying team member runtime resolution and exact materialization without redesigning routing or response rendering.

## Current Problem
[`src/mindroom/bot.py`](/home/basnijholt/Work/dev/mindroom-worktrees/chore-issue-384-read-and-understand/src/mindroom/bot.py) decides responder pools and response ownership with one notion of which agents are currently materializable.
[`src/mindroom/teams.py`](/home/basnijholt/Work/dev/mindroom-worktrees/chore-issue-384-read-and-understand/src/mindroom/teams.py) separately decides whether an exact team request can be materialized and which members to instantiate.
[`src/mindroom/api/openai_compat.py`](/home/basnijholt/Work/dev/mindroom-worktrees/chore-issue-384-read-and-understand/src/mindroom/api/openai_compat.py) has its own configured-team materialization loop.
This allows room visibility, sender visibility, supported-team policy, runtime liveness, and request-time agent creation failures to drift apart.
The recent `bot.running` fix closed one bug, but it did not remove the split ownership.

## Refactor Boundary
The new source of truth will own only team runtime eligibility and exact member materialization.
The new source of truth will not own team intent selection, AI mode selection, response-owner election, or Matrix and OpenAI response formatting.
Matrix-specific owner election stays in [`src/mindroom/bot.py`](/home/basnijholt/Work/dev/mindroom-worktrees/chore-issue-384-read-and-understand/src/mindroom/bot.py).
Team execution and rendering stay in [`src/mindroom/teams.py`](/home/basnijholt/Work/dev/mindroom-worktrees/chore-issue-384-read-and-understand/src/mindroom/teams.py).
HTTP request handling stays in [`src/mindroom/api/openai_compat.py`](/home/basnijholt/Work/dev/mindroom-worktrees/chore-issue-384-read-and-understand/src/mindroom/api/openai_compat.py).

## Proposed Shape
Create a new transport-independent module at [`src/mindroom/team_runtime_resolution.py`](/home/basnijholt/Work/dev/mindroom-worktrees/chore-issue-384-read-and-understand/src/mindroom/team_runtime_resolution.py).
Keep [`src/mindroom/runtime_resolution.py`](/home/basnijholt/Work/dev/mindroom-worktrees/chore-issue-384-read-and-understand/src/mindroom/runtime_resolution.py) as the lower-level single-agent runtime helper and let the new module compose it.
Move the authoritative definition of live shared-agent availability into the new module.
Move the authoritative exact-team materialization flow into the new module.
Keep [`TeamResolution`](/home/basnijholt/Work/dev/mindroom-worktrees/chore-issue-384-read-and-understand/src/mindroom/teams.py#L287) and the existing intent and policy logic in [`src/mindroom/teams.py`](/home/basnijholt/Work/dev/mindroom-worktrees/chore-issue-384-read-and-understand/src/mindroom/teams.py), but feed it from the shared runtime result instead of rebuilding the same checks in each caller.

## New Shared Types
Add a dataclass that describes one runtime-visible shared agent with fields for `name`, `running`, and whether it is eligible for shared team participation.
Add a dataclass that describes one exact requested member materialization attempt with fields for `name`, `agent`, and failure state.
Add a dataclass that describes the final exact-team materialization result with requested names, instantiated agents, display names, and any rejected members.

## New Shared Functions
Add `resolve_live_shared_agent_names(orchestrator, config=None)` to return the canonical set of running shared agents and hide dead bots, unknown bots, and the router.
Add `evaluate_exact_requested_team_members(requested_agent_names, materializable_agent_names, reason_prefix)` to produce one consistent missing-member decision before any execution starts.
Add `materialize_exact_team_members_from_orchestrator(...)` to build exact members for Matrix team execution using orchestrator-backed knowledge managers and request identity.
Add `materialize_exact_team_members_from_config(...)` to build exact members for `/v1` configured teams by creating agents directly from config and runtime paths.
Keep these entry points thin and share the common failure policy and member accounting underneath them instead of introducing a generic callback-heavy abstraction.

## Call Site Changes
Replace [`materializable_orchestrator_agent_names()`](/home/basnijholt/Work/dev/mindroom-worktrees/chore-issue-384-read-and-understand/src/mindroom/teams.py#L1016) with the shared `resolve_live_shared_agent_names()` helper.
Update [`AgentBot._materializable_agent_names()`](/home/basnijholt/Work/dev/mindroom-worktrees/chore-issue-384-read-and-understand/src/mindroom/bot.py#L1493) to call the new helper.
Update [`team_response()`](/home/basnijholt/Work/dev/mindroom-worktrees/chore-issue-384-read-and-understand/src/mindroom/teams.py#L1176) and the streaming path to use the shared exact-team materializer instead of their local exact-member flow.
Replace the local member loop inside [`_build_team()`](/home/basnijholt/Work/dev/mindroom-worktrees/chore-issue-384-read-and-understand/src/mindroom/api/openai_compat.py#L1085) with the shared config-based exact-team materializer.
Keep `decide_team_formation()` and `resolve_configured_team()` in place, but make them consume authoritative materializable names from the new module.

## Non-Goals
Do not redesign how ad hoc team intent is selected from mentions, threads, or DM context.
Do not change private-agent or unsupported-team policy.
Do not change Matrix response-owner election beyond feeding it the corrected shared materializable set.
Do not change team response formatting, streaming payload shape, or `/v1` protocol behavior except where exact member failure handling becomes shared.

## Implementation Sequence
Step 1 is to add the new `team_runtime_resolution` module and move the current live-shared-agent check into it.
Step 2 is to move exact-member materialization logic out of [`src/mindroom/teams.py`](/home/basnijholt/Work/dev/mindroom-worktrees/chore-issue-384-read-and-understand/src/mindroom/teams.py) and make Matrix team execution call the new helper.
Step 3 is to switch [`src/mindroom/api/openai_compat.py`](/home/basnijholt/Work/dev/mindroom-worktrees/chore-issue-384-read-and-understand/src/mindroom/api/openai_compat.py) to the same exact-member materialization flow.
Step 4 is to delete now-redundant local helpers and make sure the user-facing reject messages stay unchanged.

## Test Plan
Keep the existing regression in [`tests/test_multi_agent_bot.py`](/home/basnijholt/Work/dev/mindroom-worktrees/chore-issue-384-read-and-understand/tests/test_multi_agent_bot.py) that rejects explicit requests when one mentioned bot exists in `agent_bots` but is not running.
Keep the existing regression in [`tests/test_team_media_fallback.py`](/home/basnijholt/Work/dev/mindroom-worktrees/chore-issue-384-read-and-understand/tests/test_team_media_fallback.py) that rejects exact team execution when one orchestrator member is not running.
Add direct unit coverage for the new live-shared-agent helper so dead bots, router entries, and unknown names are filtered consistently.
Add `/v1` coverage in [`tests/test_openai_compat.py`](/home/basnijholt/Work/dev/mindroom-worktrees/chore-issue-384-read-and-understand/tests/test_openai_compat.py) that a configured team rejects with the same exact-member failure reason when one member cannot be created.
Run `just test-backend` and `pre-commit run --all-files` after the refactor.

## Risks To Watch
Do not let the new module pull in Matrix-only concepts such as response ownership or room parsing.
Do not let the new module depend on `AgentBot` instances directly outside the narrow orchestrator liveness read.
Preserve the current user-facing rejection strings so the behavior change is architectural, not cosmetic.
Avoid creating a generic callback abstraction that is harder to understand than the existing duplication.

## Done Criteria
`bot.py`, `teams.py`, and `openai_compat.py` no longer each define their own exact team member materialization loop.
There is one authoritative helper for live shared-agent names.
There is one authoritative helper for exact requested team member materialization and failure reporting.
The existing Matrix regressions still pass and `/v1` has matching regression coverage.

## Feedback
This is a good refactor target as long as it stays a narrow consolidation instead of becoming a new framework.
Keep `TeamResolution`, intent selection, and response-owner election where they are, and let the new module own only live shared-agent discovery and exact member materialization.
Prefer one small materialization result type over a broader callback-heavy abstraction.
Add one more regression that streaming team execution ignores router placeholders the same way the non-streaming helper already does.
Preserve the current user-facing rejection strings exactly so the change stays architectural rather than cosmetic.
