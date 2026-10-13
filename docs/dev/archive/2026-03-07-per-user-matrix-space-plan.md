# Per-User Matrix Space Plan

This archived plan records why MindRoom maintains one optional private root Matrix Space per installation and the limits chosen for it.

## Why

The Space groups an installation's long-lived managed rooms, so a Matrix client can show them in one place.

## Decisions

- The Space is a workspace shell for client navigation and discovery, not a source of truth for permissions, routing, room visibility, or bot membership.
  Room membership checks stay authoritative, so the Space layers on top of the room lifecycle instead of the room model being rewritten around Spaces.
- Managed room access settings stay the only source of truth for room join rules and directory visibility, and the Space does not mirror them.
  The Space stays private and invite-only even when managed rooms are public, because it organizes the installation's workspace rather than publishing a community index.
- The router owns the Space because it already creates and reconciles the managed rooms.

## Assumptions

- Matrix clients vary in how they render Spaces, so the Space relies only on the standard `m.space` room type and `m.space.child` events.
- `m.space.child` is the only relation needed because the Space hierarchy APIs key off child events on the parent Space.
- Clients such as Element and Cinny render Spaces natively, so the feature needed no MindRoom frontend changes.

## Non-goals and limitations

- The Space holds only managed rooms: no DMs, temporary rooms, or unconfigured external rooms, and no multiple or nested Spaces.
- Child links are additive and non-destructive: a room that is no longer managed keeps its link in the Space.
- The config stays minimal because there is exactly one root Space: its alias and topic are not configurable.
