---
icon: lucide/workflow
---

# Agent Orchestration

This page records the reasons and assumptions behind how the orchestrator sets up rooms, recovers session storage, and refuses responses during runtime replacement.

## Room setup

Room setup checks managed room policy against freshly read room state every time; a configuration hash or persisted state cache was rejected because it would suppress checks for drift made outside MindRoom.
Setup makes one pass: nio owns transport retries, and an administrative failure the homeserver returns waits for the next setup attempt instead of an in-pass retry.

## Session Storage Recovery

When a session table lacks columns the installed MindRoom release requires, recovery moves the whole `sessions/` directory aside, so SQLite journals and sidecars stay with their database; see [Incompatible session databases](../deployment/storage.md#incompatible-session-databases) for the operator view.
Recovery assumes upgrades run while MindRoom is stopped, so it never overlaps active session writers.

## Runtime replacement

A response refused at the admission gate because its runtime is being replaced sends no Matrix notice: a refusal notice was tried and removed because `room_send` has no timeout, so a send on that path could stall replacement shutdown.
