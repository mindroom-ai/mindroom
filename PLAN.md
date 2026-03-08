# Per-User Matrix Space Plan

## Goal
Implement one optional private root Matrix Space per MindRoom installation to group the long-lived managed rooms for a single MindRoom user.
Keep the existing room-centric authorization, routing, and membership behavior unchanged.

## Product Decision
The Space is a workspace shell for client navigation and discovery, not a source of truth for permissions, routing, or bot membership.
Version 1 is opt-in and disabled by default.
Version 1 includes only room IDs returned by managed room reconciliation.
Version 1 excludes DMs, temporary rooms, unconfigured external rooms, and nested spaces.
Version 1 does not automatically prune stale child links for rooms that were previously managed.

## Why This Fits The Current Architecture
MindRoom already creates and reconciles managed rooms centrally in `src/mindroom/matrix/rooms.py`.
Room startup and reinvitation already flow through the router in `src/mindroom/orchestrator.py`.
The current `MatrixState` model in `src/mindroom/matrix/state.py` tracks rooms separately from accounts, which is a clean place to add first-class Space state.
This lets us layer a Space on top of the current room lifecycle instead of rewriting the room model around Spaces.

## Scope
Create or resolve a single root Space for the installation when enabled.
Invite the configured `mindroom_user` to that Space when the internal user exists.
Attach all current managed room IDs returned by `ensure_all_rooms_exist()` as children of that Space.
Re-run Space reconciliation on startup and config reloads.

## Non-Goals
Do not replace room membership checks with space membership checks.
Do not use the Space to infer bot routing, authorization, or room visibility.
Do not auto-add DMs or arbitrary external rooms.
Do not add multiple Spaces or nested Spaces in the first pass.
Do not delete rooms or aggressively clean up old Space links in the first pass.

## Proposed Config
Add a new `matrix_space` config block in `src/mindroom/config/matrix.py` and wire it into `src/mindroom/config/main.py`.
Keep the config intentionally small because this feature manages exactly one root Space and does not need user-tunable aliases or topics in version 1.

```yaml
matrix_space:
  enabled: false
  name: MindRoom
```

`enabled` turns the feature on or off.
`name` controls the display name of the root Space.
The alias localpart is derived automatically from a reserved internal helper.
The topic is fixed in code for version 1.

## Validation Rules
The default should be disabled so current installs behave exactly as they do today.
Do not require `mindroom_user` when the feature is enabled.
If `mindroom_user` is absent, the Space is still created and simply skips the internal-user invite step.

## Naming And State
Add a helper such as `managed_space_alias_localpart()` to `src/mindroom/matrix/identity.py`.
Use a reserved root alias localpart so it cannot collide with normal managed room keys by accident.
Extend `MatrixState` in `src/mindroom/matrix/state.py` with a single `space_room_id` field.
Do not add a generic `spaces` mapping in version 1.

## Matrix Client Primitives
Add a `create_space()` helper to `src/mindroom/matrix/client.py`.
Implement it with `AsyncClient.room_create(..., space=True)` so the room is created as type `m.space`.
Create the Space as private and invite-only in version 1.
Add a helper to upsert an `m.space.child` state event in the Space for each managed room.
Use the existing server-name helper as the source for the required `via` entry for each child relation.
Prefer `m.space.child` as the only required relation in version 1 because the Space hierarchy APIs key off child events on the parent Space.

## Space Reconciliation Module
Keep the implementation small by adding focused root-Space helpers next to the current Matrix room helpers.
Resolve an existing alias first and only create the Space when it does not already exist.
Persist the Space ID into `MatrixState`.
Expose one function that ensures the root Space exists and links the supplied managed room IDs.

## Orchestration Changes
Call the Space reconciliation entry point from `src/mindroom/orchestrator.py` after managed rooms have been ensured.
Use the router client as the owner of the Space because the router already creates and reconciles managed rooms.
After the Space exists, explicitly invite `mindroom_user` to it instead of relying on incidental invite loops.
Keep Space reconciliation as a separate post-room phase instead of folding it into `ensure_all_rooms_exist()`.
On config reload, add a dedicated `matrix_space_changed` guard so Space-only config changes reconcile the workspace shell without piggybacking on room invitation churn.

## Relationship To Existing Room Access
Keep `matrix_room_access` as the only source of truth for managed room join rules and room directory visibility.
Do not mirror room publicness onto the root Space in version 1.
Keep the root Space private even when managed rooms are public because the feature is meant to organize one user’s workspace rather than publish a community index.

## UI And Template Changes
Add the new config block to `src/mindroom/config_template.yaml`.
Add the new config block to the generated template output in `src/mindroom/cli/config.py`.
Keep the default disabled in templates.
Do not make any frontend changes in the first pass because clients like Element or Cinny already render Spaces natively.

## Tests
Add config model tests for defaults and YAML null handling.
Add `MatrixState` tests covering serialization and backward-compatible loading when older state files do not have a `space_room_id` key.
Add `matrix/client.py` tests for idempotent child-link upserts and error handling.
Add Matrix root-Space tests for existing Space resolution, fresh creation, state persistence, and no-op behavior when disabled.
Add orchestrator tests confirming Space reconciliation runs after room reconciliation and on config reloads without unnecessary bot restarts.

## Implementation Order
1. Add config and state primitives.
2. Add Matrix client helpers for Space creation and child linking.
3. Add the new `matrix/spaces.py` reconciliation module.
4. Wire the reconciliation flow into the orchestrator and config reload path.
5. Add tests and update config templates.

## Risks And Mitigations
Some Matrix clients vary in how they render Spaces, so the first pass should depend only on the standard `m.space` room type and `m.space.child` relations.
Existing installs may already have manually created Spaces, so reconciliation should resolve by alias before creating anything.
Stale room links are possible if managed rooms are removed from config, so version 1 should document that links are additive and non-destructive.

## Acceptance Criteria
When `matrix_space.enabled` is `false`, startup and reload behavior remain unchanged.
When `matrix_space.enabled` is `true`, MindRoom creates or resolves one private root Space and stores its room ID in `MatrixState`.
If `mindroom_user` is configured, it is invited to the root Space.
All currently managed room IDs returned by room reconciliation are linked under the root Space with `m.space.child` events.
Config reloads reconcile Space metadata and add newly managed rooms without restarting unaffected bots or forcing unnecessary room invitation churn.
