# MindRoom Exhaustive Live Test Checklist

This document is the source-backed manual live-test plan for the current MindRoom repository.
It is intended to be reviewed, committed, and then executed in parallel by multiple Codex agents or humans.

## Scope

This checklist covers the core MindRoom runtime, the bundled dashboard and API, the OpenAI-compatible API, and the SaaS platform.
This checklist intentionally separates generic runtime behaviors from representative integration buckets so failures can be triaged by subsystem instead of by individual provider.

## Result Recording Rules

Use one result record per checklist item.
Record the test ID, environment, exact command or URL, room or thread identifiers, expected outcome, observed outcome, and evidence path.
When an item fails, leave the checkbox unchecked and append a short failure note directly below the item.
When an item is intentionally non-functional or placeholder, mark it complete only if the current placeholder behavior matches the expected outcome in this document.

```text
Test ID:
Environment:
Command or URL:
Room, Thread, User, or Account:
Expected Outcome:
Observed Outcome:
Evidence:
Failure Note:
```

## Environments

Use `core-local` for local Matrix plus local or configured model provider plus `uv run mindroom run`.
Use `hosted-pairing` for `uvx mindroom connect` plus local backend against hosted Matrix.
Use `dashboard-bundled` for the bundled frontend served by the same backend instance under test.
Use `saas-compose` for the local platform backend plus platform frontend sandbox.
Use `real-oauth` only for provider flows that cannot be meaningfully validated with local mocks.
Do not use dev-only auth shortcuts such as `NEXT_PUBLIC_DEV_AUTH=true` for the primary production-behavior pass.

## Evidence Minimums

Capture exact startup commands, API ports, and relevant environment variables for every runtime boot.
Capture Matrix room names, room IDs, message handles, and thread handles for every chat behavior.
Capture API request and response payloads for every backend endpoint check.
Capture screenshots for every frontend or SaaS UI result that depends on layout, banners, status chips, or placeholder messaging.
Capture log snippets for every failure, retry, hot-reload, scheduling restore, knowledge reindex, or worker cleanup event.

## 1. Core Runtime Boot And Lifecycle

Source anchors: `src/mindroom/orchestrator.py`, `src/mindroom/orchestration/runtime.py`, `src/mindroom/cli/main.py`, `src/mindroom/cli/config.py`, `src/mindroom/cli/connect.py`, `src/mindroom/cli/doctor.py`, `src/mindroom/cli/local_stack.py`, `src/mindroom/runtime_state.py`.

- [ ] `CORE-001` Run `mindroom doctor` before booting the runtime.
Expected outcome: Doctor reports actionable readiness information and fails clearly on broken provider, Matrix, config, or storage prerequisites.

- [ ] `CORE-002` Start `uv run mindroom run` against the chosen environment.
Expected outcome: The runtime creates or loads the internal user, router, configured agents, configured teams, and bundled API without requiring manual recovery.

- [ ] `CORE-003` Check `/api/health` and `/api/ready` on the same running instance.
Expected outcome: Health becomes available during startup and ready transitions only after the orchestrator finishes the initial boot path.

- [ ] `CORE-004` Verify Matrix state and account creation after first startup.
Expected outcome: Persisted Matrix state contains the expected router and agent identities and later restarts reuse them instead of recreating new users.

- [ ] `CORE-005` Perform a clean shutdown and restart the same instance.
Expected outcome: Restart restores rooms, runtime state, and persisted data without duplicate welcomes, duplicate rooms, or duplicate scheduled tasks.

- [ ] `CORE-006` Start the runtime while Matrix or another dependency is temporarily unavailable.
Expected outcome: Startup retries back off and recover on transient failures, while permanent startup failures are reported clearly and do not spin forever.

- [ ] `CORE-007` Run `uvx mindroom connect --pair-code ...` in a hosted pairing flow.
Expected outcome: Pairing persists the returned local client credentials, updates local environment configuration, and replaces owner placeholder tokens when the pairing response includes an owner user ID.

- [ ] `CORE-008` Run `mindroom avatars generate` and `mindroom avatars sync` after the runtime has initialized at least once.
Expected outcome: Managed avatar assets are generated for supported entities and sync succeeds through the router account without manual state surgery.

- [ ] `CORE-009` Exercise CLI config setup and inspection flows with `mindroom config init`, `mindroom config show`, `mindroom config edit`, `mindroom config validate`, and `mindroom config path`.
Expected outcome: Starter configs are generated for the chosen profile, expected companion files are created, the editor flow targets the active config, and validation plus path discovery match the runtime’s effective config location.

- [ ] `CORE-010` Exercise `mindroom local-stack-setup` in a local Matrix development environment.
Expected outcome: The command prepares the expected local stack artifacts and prints usable next-step guidance instead of requiring undocumented manual repair.

- [ ] `CORE-011` Run `mindroom config init` for a full or public starter profile with and without an explicit `MINDROOM_STORAGE_PATH`.
Expected outcome: The starter profile scaffolds the canonical Mind workspace files including `MEMORY.md`, writes or preserves the correct storage-root env settings, and keeps the starter knowledge-base path aligned with the effective runtime storage root.

## 2. Config Loading, Hot Reload, And Reconciliation

Source anchors: `src/mindroom/config/main.py`, `src/mindroom/file_watcher.py`, `src/mindroom/orchestrator.py`, `src/mindroom/orchestration/config_updates.py`, `src/mindroom/orchestration/rooms.py`, `src/mindroom/tool_system/plugins.py`, `src/mindroom/tool_system/skills.py`.

- [ ] `CONF-001` Edit a configured agent field such as role, tools, or instructions in `config.yaml` while the runtime is active.
Expected outcome: Only the affected entity is rebuilt or restarted and unaffected bots keep running.

- [ ] `CONF-002` Add a new agent to `config.yaml` during a live run.
Expected outcome: The new agent account is provisioned, joins the expected rooms, and becomes available without a full-process restart.

- [ ] `CONF-003` Remove an agent from `config.yaml` during a live run.
Expected outcome: The removed entity stops cleanly and no longer appears in routing, room membership management, or the dashboard.

- [ ] `CONF-004` Add or modify a configured team during a live run.
Expected outcome: The new or changed team becomes available with the updated membership and mode while unrelated entities remain stable.

- [ ] `CONF-005` Add, remove, or edit a configured knowledge base, skill, or plugin during a live run.
Expected outcome: Runtime caches invalidate correctly and the change is visible on the next relevant request without stale copies lingering.

- [ ] `CONF-006` Enable `matrix_room_access.reconcile_existing_rooms` for one restart and then disable it again.
Expected outcome: Existing managed rooms are reconciled once to the configured access policy and steady-state behavior returns after the flag is turned back off.

- [ ] `CONF-007` Edit only shared defaults such as `defaults.enable_streaming` during a live run without changing any entities.
Expected outcome: Unchanged bots pick up the new defaults in place without restart and subsequent responses reflect the updated default behavior immediately.

## 3. Room Provisioning, Router Management, And Onboarding

Source anchors: `src/mindroom/matrix/rooms.py`, `src/mindroom/topic_generator.py`, `src/mindroom/matrix/room_cleanup.py`, `src/mindroom/matrix/avatar.py`, `src/mindroom/bot.py`.

- [ ] `ROOM-001` Boot with at least one configured room that does not yet exist.
Expected outcome: MindRoom creates the room, applies topic and access settings, and invites the expected participants.

- [ ] `ROOM-002` Verify router onboarding in a newly managed room.
Expected outcome: The router posts a welcome message only in the intended empty-room onboarding scenario and the content reflects the currently available agents and commands.

- [ ] `ROOM-003` Send `!hi` in a managed room after startup.
Expected outcome: The router reproduces the current welcome guidance without changing room state or duplicating bot setup.

- [ ] `ROOM-004` Test `single_user_private`, `multi_user public`, `multi_user knock`, and invite-only exceptions.
Expected outcome: Joinability, room directory visibility, and restricted-room exceptions all match the configured policy.

- [ ] `ROOM-005` Put an agent in an external or unmanaged room and load the runtime plus dashboard.
Expected outcome: External room state is discoverable and can later be left intentionally instead of being silently mutated on startup.

- [ ] `ROOM-006` Start the runtime with orphaned bot memberships from a previous config.
Expected outcome: Orphaned MindRoom bots are cleaned up safely without ejecting the currently configured entities from their required rooms.

- [ ] `ROOM-007` Enable root space management and managed avatars when the config uses them.
Expected outcome: Root space creation, room grouping, and avatar propagation behave consistently and do not regress room access or membership.

## 4. Message Dispatch, DMs, Threads, And Reply Chains

Source anchors: `src/mindroom/bot.py`, `src/mindroom/routing.py`, `src/mindroom/thread_utils.py`, `src/mindroom/matrix/reply_chain.py`, `src/mindroom/matrix/event_info.py`, `src/mindroom/response_tracker.py`.

- [ ] `MSG-001` Send a non-command message in a single-agent room without any mention.
Expected outcome: The sole configured agent responds directly without invoking AI router selection.

- [ ] `MSG-002` Mention one agent by short room-visible handle.
Expected outcome: The targeted agent responds and no unrelated agent or team consumes the turn.

- [ ] `MSG-003` Mention one agent by full Matrix ID.
Expected outcome: Mention resolution still targets the correct agent and produces one response path.

- [ ] `MSG-004` Send a non-command message in a multi-agent room without an explicit mention.
Expected outcome: The router selects one eligible agent, produces the expected handoff behavior, and the selected agent continues the thread.

- [ ] `MSG-005` Send a message in a one-to-one DM room with an agent.
Expected outcome: The agent responds naturally without requiring a mention and later turns keep the same conversation continuity.

- [ ] `MSG-006` Create a thread with two or more human participants and then send a non-mention follow-up.
Expected outcome: Agents stay silent until explicitly mentioned and do not inject themselves into human-to-human conversation.

- [ ] `MSG-007` Repeat the multi-human thread test while bridge or relay accounts are listed in `bot_accounts`.
Expected outcome: Listed bot accounts do not count as extra humans for mention-protection rules.

- [ ] `MSG-008` Use a client flow that sends plain replies instead of native thread metadata.
Expected outcome: Reply-chain recovery maps the new message back to the correct conversation root and keeps history coherent.

- [ ] `MSG-009` Force a reconnect or retry during an active conversation and then inspect the room or thread history.
Expected outcome: Duplicate-response prevention suppresses repeated agent output for the same triggering event.

- [ ] `MSG-010` Edit a recent user message in a conversation that already has agent state.
Expected outcome: The edit path reprocesses the updated content in the correct conversation context without corrupting thread continuity.

- [ ] `MSG-011` Configure one agent with `thread_mode: room` and exercise a conversation in a managed room.
Expected outcome: The agent responds with plain room messages, stores room-level continuity, and skips thread-history derivation instead of behaving like the default threaded mode.

- [ ] `MSG-012` Configure `room_thread_modes` so one room uses `room` mode and another uses `thread` mode for the same agent.
Expected outcome: The effective response mode follows the room-specific override and router handoff respects the target agent’s effective mode in each room.

- [ ] `MSG-013` Continue a thread where one agent has already been responding and then explicitly mention a different eligible agent.
Expected outcome: The newly mentioned agent takes over the turn, the previous agent stays silent, and the handoff does not create duplicate or competing replies.

## 5. Streaming, Presence, Typing, Stop, And Large Messages

Source anchors: `src/mindroom/streaming.py`, `src/mindroom/matrix/presence.py`, `src/mindroom/matrix/typing.py`, `src/mindroom/stop.py`, `src/mindroom/matrix/large_messages.py`, `src/mindroom/tool_system/events.py`, `src/mindroom/bot.py`.

- [ ] `STR-001` Trigger a response large enough to stream progressively.
Expected outcome: The agent emits progressive message edits instead of waiting for one final message.

- [ ] `STR-002` Observe typing indicators during a slow response.
Expected outcome: Typing state appears and refreshes while the response is in flight and clears when the response finishes or fails.

- [ ] `STR-003` Repeat a long response with the sender offline or in a presence state that should disable streaming.
Expected outcome: Streaming and presence-dependent behavior follow the configured or implemented gating rules instead of forcing the same path every time.

- [ ] `STR-004` Use the stop interaction on an in-flight response.
Expected outcome: The in-flight task cancels cleanly, partial streaming stops, and stop-related UI or reaction state is cleaned up.

- [ ] `STR-005` Trigger tool usage during a streamed response.
Expected outcome: Inline tool trace markers remain coherent across progressive edits and final output.

- [ ] `STR-006` Trigger a response that exceeds normal message size limits.
Expected outcome: Oversized output falls back to sidecar large-message storage while still leaving a valid preview or pointer event in the room.

- [ ] `STR-007` Disable `defaults.enable_streaming` or use an agent or room path where streaming is intentionally disabled.
Expected outcome: The runtime sends a normal non-streaming response path and does not emit progressive edits or streaming-specific affordances.

- [ ] `STR-008` Disable `defaults.show_stop_button` and then disable `show_tool_calls` for one agent.
Expected outcome: Stop-button reactions are suppressed when configured off and inline tool traces plus tool metadata are omitted when tool-call visibility is disabled.

- [ ] `STR-009` React with `🛑` when no tracked run is active, from an agent account, and from a user who lacks reply permission.
Expected outcome: Only authorized human reactions on actively tracked runs cancel work, while all other stop reactions are ignored or fall through to normal reaction handling without corrupting room state.

## 6. Teams And Multi-Agent Collaboration

Source anchors: `src/mindroom/teams.py`, `src/mindroom/team_runtime_resolution.py`, `src/mindroom/bot.py`.

- [ ] `TEAM-001` Address a configured team that runs in `coordinate` mode.
Expected outcome: The coordinator assigns distinct subtasks, synthesizes the outputs, and returns a final team response instead of raw duplicated member answers.

- [ ] `TEAM-002` Address a configured team that runs in `collaborate` mode.
Expected outcome: All members contribute on the same task and the final output reflects synthesis across those contributions.

- [ ] `TEAM-003` Mention multiple standalone agents in one message to create an ad-hoc team.
Expected outcome: MindRoom forms a dynamic team and chooses a sensible collaboration mode based on the request.

- [ ] `TEAM-004` Continue a thread that already contains multiple agent participants.
Expected outcome: Follow-up messages keep the existing team context instead of collapsing back to a single unrelated agent.

- [ ] `TEAM-005` Use a DM room that contains multiple agents.
Expected outcome: Main-timeline DM messages can materialize multi-agent teamwork without losing DM-specific privacy or continuity behavior.

- [ ] `TEAM-006` Include a private agent in a team or in a delegation path that should be unsupported.
Expected outcome: The request fails clearly with a materialization or unsupported-member explanation instead of silently misrouting.

- [ ] `TEAM-007` Mention an ad-hoc team where one or more requested members are off-room, private, or otherwise not materializable.
Expected outcome: The exact requested-member set is preserved in the rejection result with member-specific failure statuses and reasons, and MindRoom does not silently shrink the team to only the remaining materializable members.

## 7. Commands And Interactive Workflows

Source anchors: `src/mindroom/commands/parsing.py`, `src/mindroom/commands/handler.py`, `src/mindroom/commands/config_commands.py`, `src/mindroom/commands/config_confirmation.py`, `src/mindroom/interactive.py`, `src/mindroom/bot.py`.

- [ ] `CMD-001` Send `!help` with and without a topic.
Expected outcome: The router handles the command exclusively and returns the expected command guidance for the current runtime.

- [ ] `CMD-002` Send `!hi` in a room with active agents.
Expected outcome: The router handles the command and returns the current room-specific onboarding help.

- [ ] `CMD-003` Send an unknown command.
Expected outcome: The router returns clear failure or help behavior instead of routing the text to a normal agent response path.

- [ ] `CMD-004` Use command aliases such as `!listschedules`, `!cancel-schedule`, and `!edit-schedule`.
Expected outcome: Alias parsing resolves to the intended canonical command behavior.

- [ ] `CMD-005` Trigger a config-changing command that requires confirmation.
Expected outcome: The router posts a confirmation artifact, only the intended confirmation reactions are accepted, and the change is not applied before confirmation.

- [ ] `CMD-006` Restart the runtime while a config confirmation is still pending.
Expected outcome: Pending confirmation state survives restart and continues to work in the correct room and thread.

- [ ] `CMD-007` Use `!skill` when exactly one agent in the room can handle the skill.
Expected outcome: The skill resolves automatically without requiring an explicit agent mention.

- [ ] `CMD-008` Use `!skill` when multiple agents in the room have the same skill.
Expected outcome: The runtime refuses ambiguous execution and asks for disambiguation instead of guessing.

- [ ] `CMD-009` Exercise reaction-based interactive prompts that are scoped to one conversation.
Expected outcome: Reactions outside the intended room, message, or thread do not mutate the interactive workflow.

- [ ] `CMD-010` Use `!config show`, `!config get <path>`, and `!config set <path> <value>` in chat.
Expected outcome: The router uses the active runtime config path, returns current values correctly, and `set` produces a preview plus confirmation flow before applying the change.

- [ ] `CMD-011` Attempt malformed or invalid `!config set` inputs, including quote-parse failures and runtime-invalid changes.
Expected outcome: Parse or validation errors are explained clearly and no partial configuration change is applied.

## 8. Authorization And Room Access Policy

Source anchors: `src/mindroom/authorization.py`, `src/mindroom/config/auth.py`, `src/mindroom/config/matrix.py`, `src/mindroom/bot.py`, `src/mindroom/voice_handler.py`.

- [ ] `AUTH-001` Test a user listed in `authorization.global_users`.
Expected outcome: The user can interact across managed rooms without needing room-specific entries.

- [ ] `AUTH-002` Test room permissions keyed by room ID, full alias, and managed room key.
Expected outcome: Each identifier format matches the same intended access rule and does not fall through unexpectedly.

- [ ] `AUTH-003` Test a room that is not in `room_permissions`.
Expected outcome: Access is controlled solely by `default_room_access` for that room.

- [ ] `AUTH-004` Test bridged or alternate identities configured through `authorization.aliases`.
Expected outcome: Alias mapping resolves to the canonical user ID before room access and reply-permission checks run.

- [ ] `AUTH-005` Configure `authorization.agent_reply_permissions` with both `*` and per-agent entries.
Expected outcome: Default reply rules and explicit per-entity overrides both enforce exactly as configured.

- [ ] `AUTH-006` Verify internal MindRoom identities and non-MindRoom bot accounts under the same scenario.
Expected outcome: Internal system identities bypass the intended checks, while `bot_accounts` still obey reply permission enforcement.

- [ ] `AUTH-007` Send a voice message from a user who is denied by reply permissions.
Expected outcome: Voice dispatch uses the original human sender for permission evaluation instead of the router or transcription sender.

## 9. Images, Files, Attachments, Videos, And Voice

Source anchors: `src/mindroom/matrix/image_handler.py`, `src/mindroom/matrix/media.py`, `src/mindroom/attachments.py`, `src/mindroom/attachment_media.py`, `src/mindroom/voice_handler.py`, `src/mindroom/bot.py`.

- [ ] `MEDIA-001` Send an unencrypted image with a text caption to a room with a vision-capable agent.
Expected outcome: The image and caption reach the agent and produce a response that reflects both the media and the text.

- [ ] `MEDIA-002` Send an encrypted image message.
Expected outcome: Media download and decryption succeed and the result is processed the same as the unencrypted case.

- [ ] `MEDIA-003` Send a captionless image.
Expected outcome: The runtime produces a sensible fallback prompt or interpretation path instead of dropping the media silently.

- [ ] `MEDIA-004` Send a file or video attachment in a thread and inspect attachment registration.
Expected outcome: The runtime persists the attachment, assigns a stable attachment ID, and makes it available to attachment-aware tool flows.

- [ ] `MEDIA-005` Use the `attachments` tool or another attachment-aware tool in the same thread where attachments were sent.
Expected outcome: The tool receives the expected attachment metadata and can resolve the referenced files.

- [ ] `MEDIA-006` Attempt to reuse attachment IDs from a different room or thread.
Expected outcome: Context filtering rejects out-of-scope attachments and prevents cross-thread or cross-room leakage.

- [ ] `MEDIA-007` Send a voice message when STT is enabled and configured.
Expected outcome: Voice is transcribed, normalized, and dispatched to the correct agent or team response path.

- [ ] `MEDIA-008` Send a voice message when STT is disabled or unavailable.
Expected outcome: The runtime returns the documented or implemented fallback behavior instead of pretending the audio was handled.

- [ ] `MEDIA-009` Repeat the voice flow with `voice.visible_router_echo` enabled and disabled.
Expected outcome: Visible router echo behavior matches the configuration without changing the underlying responder selection logic.

- [ ] `MEDIA-010` Exercise voice command intelligence with an explicit spoken help or skill request and with a similar non-command question.
Expected outcome: Explicit command intent can normalize to `!help` or `!skill`, but speculative command rewrites are rejected and natural-language queries stay natural language.

- [ ] `MEDIA-011` Speak or inject unavailable agent mentions while voice normalization runs in a room with limited available entities.
Expected outcome: Unavailable configured entities lose their `@` mention marker while available room-scoped entities keep valid mentions and dispatch correctly.

- [ ] `MEDIA-012` Send a voice message that becomes a normalized command or threaded follow-up and then inspect later follow-up or attachment-aware handling.
Expected outcome: The synthetic voice-derived message preserves original sender identity, thread context, attachment IDs, and raw-audio fallback metadata so router handling and later follow-ups can recover the correct audio context.

- [ ] `MEDIA-013` Repeat a voice flow that emits visible router echo, retry the same event, hand off to the final responder, and repeat with reply permissions denied.
Expected outcome: Router echo is emitted at most once per voice event, reused across retries or handoff when appropriate, and fully suppressed when reply permissions deny the sender.

## 10. Memory, Knowledge, Workspaces, Private Roots, And Cultures

Source anchors: `src/mindroom/agents.py`, `src/mindroom/memory/`, `src/mindroom/memory/auto_flush.py`, `src/mindroom/memory/config.py`, `src/mindroom/workspaces.py`, `src/mindroom/knowledge/manager.py`, `src/mindroom/knowledge/utils.py`, `src/mindroom/api/knowledge.py`.

- [ ] `MEM-001` Use a runtime configured with `memory.backend: mem0`.
Expected outcome: Agent memory persists across turns and later retrieval reflects the stored semantic memory state.

- [ ] `MEM-002` Use a runtime configured with `memory.backend: file`.
Expected outcome: Canonical file-memory roots are created in the expected workspace paths and durable memory files become the source of truth.

- [ ] `MEM-003` Enable `memory.team_reads_member_memory` and exercise a team conversation that depends on member context.
Expected outcome: Team context can read member memory when configured and does not do so when the option is disabled.

- [ ] `MEM-004` Enable memory auto-flush and create multiple eligible and ineligible sessions.
Expected outcome: Only eligible dirty sessions are flushed and `NO_REPLY` or other non-write paths do not create bogus memories.

- [ ] `MEM-005` Start with a shared knowledge base that already contains files.
Expected outcome: Startup indexing succeeds and agents assigned to the base can retrieve relevant information through knowledge usage.

- [ ] `MEM-006` Add, modify, and delete files in a watched knowledge base.
Expected outcome: Watcher-driven indexing updates the vector store, removes deleted content, and does not require full runtime restart.

- [ ] `MEM-007` Use a Git-backed knowledge base and trigger a sync or reindex cycle.
Expected outcome: The repo sync path updates the working tree and index consistently, including deleted-file cleanup.

- [ ] `MEM-008` Assign multiple knowledge bases to one agent and ask a query that spans them.
Expected outcome: Retrieval interleaves results fairly instead of allowing one base to dominate all top results.

- [ ] `MEM-009` Configure a private agent with `private.root`, `private.template_dir`, and `private.context_files`.
Expected outcome: Requester-local roots are created from the template without overwriting user edits and later accesses reuse the same private instance.

- [ ] `MEM-010` Configure `private.knowledge` for a private agent and compare chat behavior between the normal runtime and `/v1`.
Expected outcome: Requester-private knowledge remains isolated to the private runtime path and is not exposed through the shared `/v1` API surface.

- [ ] `MEM-011` Configure cultures in `automatic`, `agentic`, and `manual` modes and run agent conversations that should update shared practice knowledge.
Expected outcome: Automatic cultures capture and update shared knowledge, agentic cultures expose culture-aware runtime behavior without automatic writes, and manual cultures stay read-only while still injecting the culture description into context.

- [ ] `MEM-012` Assign multiple shared agents to the same culture and use them in separate conversations.
Expected outcome: The agents share one persisted culture state so later runs observe the same evolving cultural context instead of diverging per agent.

- [ ] `MEM-013` Exercise a private agent that belongs to a culture across two requester scopes.
Expected outcome: Culture state for private agents is isolated by requester scope and does not leak across different private-instance roots.

- [ ] `MEM-014` Configure `defaults.learning`, override one agent with `learning: false`, and compare runtime behavior plus persisted `learning/` state.
Expected outcome: Agents inherit learning from defaults unless explicitly disabled, disabled agents do not create or update learning state, and enabled agents persist learning data in their state roots.

- [ ] `MEM-015` Compare `learning_mode: agentic` with the default always-on learning mode for an otherwise identical agent.
Expected outcome: Both modes keep learning enabled, but agentic mode follows the agentic learning profile instead of the always-on mode.

- [ ] `MEM-016` Configure Mem0 with a local `sentence_transformers` embedder once with optional dependency auto-install enabled and once with it disabled or unavailable.
Expected outcome: The runtime auto-installs required local embedder dependencies when allowed and otherwise fails clearly instead of silently degrading memory setup.

- [ ] `MEM-017` Enable auto-flush for a private agent across multiple requester scopes and then change that agent back to a shared configuration.
Expected outcome: Dirty-session reprioritization and later flushes stay isolated to the original requester scope, stale private entries are purged when the agent stops being private, and persisted execution identity is reused for later writes.

## 11. Skills, Plugins, Tools, Workers, And Runtime Context

Source anchors: `src/mindroom/tool_system/skills.py`, `src/mindroom/tool_system/plugins.py`, `src/mindroom/tool_system/runtime_context.py`, `src/mindroom/tool_system/sandbox_proxy.py`, `src/mindroom/tool_system/dependencies.py`, `src/mindroom/tool_system/metadata.py`, `src/mindroom/api/tools.py`, `src/mindroom/api/skills.py`, `src/mindroom/api/workers.py`, `src/mindroom/api/sandbox_runner.py`.

- [ ] `TOOL-001` Load a skill from a bundled skill location, a plugin skill directory, and a user skill directory.
Expected outcome: Skill precedence, allowlisting, and eligibility gating all match the implemented load order and rules.

- [ ] `TOOL-002` Edit a `SKILL.md` file while the runtime is active.
Expected outcome: Skill caches invalidate automatically and the next skill use reflects the updated instructions.

- [ ] `TOOL-003` Exercise `!skill` for a direct-dispatch skill and for a model-invoked skill.
Expected outcome: Command dispatch rules, allowlist enforcement, and ambiguity handling behave consistently for both patterns.

- [ ] `TOOL-004` Add or remove a plugin path in config during a live run.
Expected outcome: Plugin-provided tools and skills appear or disappear cleanly without stale registrations remaining.

- [ ] `TOOL-005` Use a tool that writes back into Matrix, such as `matrix_message`.
Expected outcome: Tool runtime context preserves the correct room and thread target instead of posting to the wrong conversation.

- [ ] `TOOL-006` Use attachment-aware tools after sending files or media.
Expected outcome: Tool payloads receive the expected attachment IDs and context filtering prevents out-of-scope resolution.

- [ ] `TOOL-007` Exercise worker-backed tools under unscoped, `shared`, `user`, and `user_agent` execution modes where supported.
Expected outcome: Worker reuse and isolation semantics follow the configured execution scope instead of collapsing all calls into one behavior.

- [ ] `TOOL-008` Check `GET /api/workers` and `POST /api/workers/cleanup` with and without an available worker backend.
Expected outcome: Worker inspection and cleanup work when a backend exists and return the intended unavailable response when one does not.

- [ ] `TOOL-009` Use a long-lived session tool such as `claude_agent` when configured.
Expected outcome: Repeated calls with the same session identity continue the existing backend session and different session labels create separate sub-sessions.

- [ ] `TOOL-010` Trigger a tool whose optional extra is missing once with auto-install enabled and once with auto-install disabled.
Expected outcome: The enabled runtime installs the extra and retries successfully, while the disabled runtime raises a clear missing-dependencies error without partial tool registration.

- [ ] `TOOL-011` Exercise the sandbox-runner API by creating a credential lease, executing a tool call with token auth, retrying the consumed lease, and then checking worker listing plus idle-worker cleanup.
Expected outcome: Sandbox-runner endpoints require the configured token, credential overrides are accepted only via leases, one-time leases are consumed after use, known workers are listed with lifecycle metadata, and cleanup marks idle workers without deleting their persisted state.

## 12. Scheduling And Background Task Execution

Source anchors: `src/mindroom/scheduling.py`, `src/mindroom/background_tasks.py`, `src/mindroom/api/schedules.py`, `src/mindroom/commands/parsing.py`, `src/mindroom/bot.py`.

- [ ] `SCH-001` Create a one-time natural-language schedule in a room thread.
Expected outcome: The task is parsed, stored, acknowledged, and later executes in the originating thread.

- [ ] `SCH-002` Create a recurring natural-language schedule.
Expected outcome: The recurring schedule persists and runs repeatedly at the interpreted schedule without duplicating the schedule record.

- [ ] `SCH-003` Create a conditional or event-driven schedule.
Expected outcome: The runtime materializes the condition as a polling or recurring workflow rather than ignoring the condition text.

- [ ] `SCH-004` Mention specific agents inside a schedule request.
Expected outcome: Schedule creation validates that the mentioned agents are available in the room and rejects impossible targets clearly.

- [ ] `SCH-005` Use list, edit, cancel-one, and cancel-all schedule flows.
Expected outcome: Schedule APIs and command flows update the same underlying state and later UI or command views reflect the changes immediately.

- [ ] `SCH-006` Restart the runtime after one-time and recurring schedules have been created.
Expected outcome: Scheduled tasks restore from persisted state, expired one-time tasks are skipped, and valid future tasks continue to execute.

- [ ] `SCH-007` Edit an existing schedule and compare the before and after task metadata, including an attempted `once` to `cron` or `cron` to `once` type change.
Expected outcome: Valid edits preserve the existing task ID and original thread targeting, while illegal schedule-type changes fail clearly without mutating the stored task.

- [ ] `SCH-008` Stop and restart the runtime while scheduled tasks exist and inspect both router and non-router entities afterward.
Expected outcome: The router is the only entity that restores persisted schedules after restart, and router shutdown cancels its in-memory scheduled tasks before exit.

## 13. OpenAI-Compatible API

Source anchors: `src/mindroom/api/openai_compat.py`, `src/mindroom/api/main.py`, `src/mindroom/teams.py`.

- [ ] `OAI-001` Test `/v1/*` with `OPENAI_COMPAT_API_KEYS` configured and with unauthenticated mode configured.
Expected outcome: Authentication follows the documented lock, allow, and key-match rules exactly.

- [ ] `OAI-002` Request `GET /v1/models`.
Expected outcome: The response lists only eligible shared agents plus `auto` and `team/<name>` models and excludes router or incompatible private agents.

- [ ] `OAI-003` Send a non-streaming `POST /v1/chat/completions` request to a normal agent model.
Expected outcome: The response returns a standard OpenAI-compatible completion payload whose content comes from the selected MindRoom agent.

- [ ] `OAI-004` Send a streaming `POST /v1/chat/completions` request that triggers tool usage.
Expected outcome: The stream includes standard completion chunks plus inline MindRoom tool-event text and ends with `[DONE]`.

- [ ] `OAI-005` Send repeated requests with the same `X-Session-Id`.
Expected outcome: Conversation continuity and sessionful tool behavior persist across requests that share the same session ID.

- [ ] `OAI-006` Send a request to the `auto` model and to a `team/<name>` model.
Expected outcome: Auto-routing uses the same selection logic as Matrix routing and team models execute the full team workflow instead of degrading to one member.

- [ ] `OAI-007` Send a request to an unsupported private or incompatible worker-scoped agent.
Expected outcome: The API rejects the request with a clear compatibility error instead of exposing a broken or partial execution path.

- [ ] `OAI-008` Use a Matrix-dependent feature such as scheduling through `/v1`.
Expected outcome: Matrix-only features fail clearly when the required Matrix context is unavailable rather than appearing to succeed silently.

- [ ] `OAI-009` Configure dashboard auth and `/v1` auth separately and exercise both surfaces.
Expected outcome: Dashboard authentication and OpenAI-compatible API authentication remain independent and do not accidentally unlock or block each other.

- [ ] `OAI-010` Send repeated `/v1/chat/completions` requests using `X-LibreChat-Conversation-Id`, then repeat with both `X-LibreChat-Conversation-Id` and `X-Session-Id`.
Expected outcome: The LibreChat header preserves continuity when `X-Session-Id` is absent and explicit `X-Session-Id` takes precedence when both headers are present.

- [ ] `OAI-011` Send `/v1/chat/completions` requests that include multimodal `messages[].content` and accepted OpenAI fields such as `response_format`, `tools`, and `tool_choice`.
Expected outcome: Text parts of multimodal content are used as the prompt, non-text parts are ignored by the current implementation, and accepted-but-unsupported OpenAI fields do not change runtime behavior or crash the request.

- [ ] `OAI-012` Send repeated streaming and non-streaming requests to the `auto` model after routing resolves a specific responder.
Expected outcome: Session continuity, streamed identity, and later turns all bind to the resolved agent name rather than the literal `auto` model label.

## 14. Bundled Dashboard And Runtime API

Source anchors: `frontend/src/App.tsx`, `frontend/src/store/configStore.ts`, `frontend/src/services/configService.ts`, `frontend/src/components/**`, `src/mindroom/api/main.py`, `src/mindroom/api/knowledge.py`, `src/mindroom/api/credentials.py`, `src/mindroom/api/matrix_operations.py`, `src/mindroom/api/integrations.py`, `src/mindroom/api/google_integration.py`, `src/mindroom/api/homeassistant_integration.py`.

- [ ] `UI-001` Load the bundled dashboard in the same runtime instance being tested.
Expected outcome: The app shell loads, the selected tab matches the URL, and mobile and desktop navigation both reach every tab.

- [ ] `UI-002` Exercise standalone dashboard authentication where `MINDROOM_API_KEY` or platform auth is configured.
Expected outcome: Blocking diagnostics, standalone login, platform redirect behavior, and logout all match the active auth mode.

- [ ] `UI-003` Watch sync status during load and save operations.
Expected outcome: Sync state transitions through loading or syncing states and returns to a stable synced state after successful saves.

- [ ] `UI-004` Trigger a config validation or save failure such as a malformed draft or invalid field combination.
Expected outcome: Field or global diagnostics surface in the UI and the app does not silently discard or half-apply the change.

- [ ] `UI-005` Load the dashboard overview tab on a non-trivial config.
Expected outcome: Stats cards, search or filters, network graph, and config export all reflect the current runtime config rather than a stale draft.

- [ ] `UI-006` Perform create, edit, and delete flows on the Agents tab.
Expected outcome: Agent CRUD supports private settings, delegation, learning and memory flags, knowledge base assignment, and policy previews without producing inconsistent draft state.

- [ ] `UI-007` Perform create, edit, and delete flows on the Teams tab.
Expected outcome: Team mode, room assignment, model override, and member selection persist correctly.

- [ ] `UI-008` Perform create, edit, and delete flows on the Culture tab.
Expected outcome: Culture assignment enforces one-culture-per-agent behavior and persists the chosen mode and description.

- [ ] `UI-009` Perform create, edit, and delete flows on the Rooms tab.
Expected outcome: Room membership, descriptions, and room-model overrides persist correctly and stay aligned with backend config.

- [ ] `UI-010` Use the Schedules tab after creating schedules through chat or API.
Expected outcome: The page lists tasks, supports editing and cancellation, and renders schedule timing consistently with the runtime timezone.

- [ ] `UI-011` Use the External Rooms tab after placing agents in unmanaged rooms.
Expected outcome: The UI lists per-agent room memberships and single or bulk leave actions mutate the actual runtime state.

- [ ] `UI-012` Use the Models tab for create, duplicate, edit, filter, and delete flows.
Expected outcome: Provider-specific fields, model IDs, base URLs, advanced settings, and masked API-key state all behave consistently.

- [ ] `UI-013` Use the Memory tab to switch backends and edit embedder or auto-flush settings.
Expected outcome: Memory settings persist correctly and provider-specific helper text or warnings match the active backend.

- [ ] `UI-014` Use the Knowledge tab for local and Git-backed bases.
Expected outcome: Base CRUD, upload, delete, status, file listing, and reindex flows all hit the expected backend state and guard against unsaved settings where required.

- [ ] `UI-015` Use the Credentials tab for create, edit, save, delete, test, and copy flows.
Expected outcome: Service validation, masked values, raw JSON editing, and credential-status loading all match backend behavior.

- [ ] `UI-016` Use the Voice tab to edit STT and command-intelligence settings.
Expected outcome: Enablement, router echo, host normalization, API key fields, and model settings all persist and display the effective current state.

- [ ] `UI-017` Use the Tools or Integrations tab with agents across different execution scopes.
Expected outcome: Catalog filters, provider connect flows, setup gating, and scope-aware restrictions behave consistently with runtime capability rules.

- [ ] `UI-018` Use the Skills tab to create, edit, save, switch, and delete skills.
Expected outcome: Skill origin labeling, kebab-case name validation, unsaved-change prompts, and editable-versus-read-only behavior all work correctly.

- [ ] `UI-019` Put the Agents and Teams editors into draft states where policy preview cannot be derived or a member becomes team-ineligible.
Expected outcome: Scoped tool previews fail closed when policy derivation is unavailable and team member pickers show explicit eligibility reasons instead of leaving blocked members selectable.

- [ ] `UI-020` Use the External Rooms tab and trigger a bulk leave where at least one room succeeds and another fails.
Expected outcome: Mixed-success feedback reports both the rooms that were left and the rooms that failed instead of collapsing partial failure into a generic success or error state.

- [ ] `UI-021` Use the Tools or Integrations tab while switching between `shared`, `user`, and `user_agent` execution scopes during load.
Expected outcome: Shared-only integrations and dashboard-managed credential controls are marked unsupported for non-shared scopes, stale in-flight scope requests do not bleed status across selections, and unsaved draft scope overrides are treated as non-authoritative.

## 15. SaaS Platform

Source anchors: `saas-platform/platform-frontend/src/app/**`, `saas-platform/platform-frontend/src/components/**`, `saas-platform/platform-frontend/src/hooks/**`, `saas-platform/platform-frontend/src/lib/api.ts`, `saas-platform/platform-backend/src/backend/routes/**`.

- [ ] `SAAS-001` Load the public landing page on the platform frontend.
Expected outcome: Main sections, navigation, footer, pricing toggle, theme behavior, and CTA links all render and navigate correctly.

- [ ] `SAAS-002` Exercise login and signup flows on the platform frontend.
Expected outcome: Email-password and provider auth entrypoints begin the correct Supabase or callback flow and return users to the intended destination.

- [ ] `SAAS-003` Exercise auth callback paths including an admin-targeted callback.
Expected outcome: Normal users land in the normal dashboard flow and `/admin` redirects succeed only for accounts that actually have admin rights.

- [ ] `SAAS-004` Load privacy and terms pages.
Expected outcome: Legal pages render as static public content without requiring auth or backend mutation.

- [ ] `SAAS-005` Open the customer dashboard with a new authenticated user.
Expected outcome: Account bootstrap and free-subscription bootstrap happen automatically when they do not already exist.

- [ ] `SAAS-006` Verify cross-subdomain SSO cookie behavior after customer dashboard load and logout.
Expected outcome: The expected SSO cookie is created or refreshed when signed in and removed when the user logs out.

- [ ] `SAAS-007` Load the no-instance customer dashboard state.
Expected outcome: The UI shows provisioning guidance or wait messaging that is distinct from generic failure messaging.

- [ ] `SAAS-008` Load the single-instance customer dashboard state.
Expected outcome: Instance card details, URLs, copy actions, status labels, and launch gating all reflect the actual backend instance state.

- [ ] `SAAS-009` Exercise customer instance lifecycle actions for running, stopped, restarting, provisioning, failed, error, and deprovisioned states.
Expected outcome: Start, stop, restart, provision, and reprovision actions expose only the allowed actions for the current state and poll until the state settles.

- [ ] `SAAS-010` Attempt instance actions on an instance not owned by the authenticated user.
Expected outcome: Backend ownership checks reject the action instead of letting the user mutate another account's instance.

- [ ] `SAAS-011` Load the billing page for a customer with a subscription.
Expected outcome: Current plan, limits, trial and billing dates, and cancellation banners all match backend subscription state.

- [ ] `SAAS-012` Load the upgrade flow and initiate checkout.
Expected outcome: Plan pricing, billing-cycle toggles, quantity logic, enterprise contact behavior, and checkout redirection all use backend pricing configuration.

- [ ] `SAAS-013` Exercise Stripe portal access and existing-subscriber checkout redirection.
Expected outcome: Existing subscribers are redirected to the appropriate management flow instead of accidentally creating duplicate subscriptions.

- [ ] `SAAS-014` Exercise customer cancel and reactivate subscription flows.
Expected outcome: Backend state changes are reflected in the frontend without stale banners or stale limits.

- [ ] `SAAS-015` Load the usage page for both populated and empty data cases.
Expected outcome: Aggregated metrics, limit math, charts, and empty states all render correctly.

- [ ] `SAAS-016` Exercise settings and GDPR flows for export, consent updates, deletion scheduling, sign-out after deletion scheduling, and cancel deletion.
Expected outcome: Consent changes use the intended optimistic behavior and deletion state replaces the normal danger zone with pending-deletion messaging.

- [ ] `SAAS-017` Attempt to access admin routes as a non-admin user.
Expected outcome: Admin gatekeeping blocks access instead of rendering privileged pages.

- [ ] `SAAS-018` Exercise the admin dashboard, accounts, instances, subscriptions, audit logs, and usage pages as an admin user.
Expected outcome: Admin tables, metrics, detail pages, and state-specific instance actions all render and operate against the real backend.

- [ ] `SAAS-019` Exercise backend-only platform endpoints such as `/health`, pricing endpoints, provisioner endpoints, and Stripe webhooks.
Expected outcome: Health reflects dependency state, provisioner auth is enforced, pricing endpoints match configured plans, and Stripe events mutate subscription state correctly.

- [ ] `SAAS-020` Exercise known placeholder or partial admin and support surfaces.
Expected outcome: Placeholder features remain explicitly placeholder and do not masquerade as completed end-to-end functionality.

## 16. Representative Integration Buckets

Source anchors: `src/mindroom/api/tools.py`, `src/mindroom/api/integrations.py`, `src/mindroom/api/google_integration.py`, `src/mindroom/api/homeassistant_integration.py`, `frontend/src/components/GoogleIntegration/GoogleIntegration.tsx`, `frontend/src/components/HomeAssistantIntegration/HomeAssistantIntegration.tsx`, `docs/tools/builtin.md`.

- [ ] `INT-001` Test a no-auth research tool bucket such as `duckduckgo`, `wikipedia`, or `website`.
Expected outcome: The tool is callable without credential setup and its results appear correctly in normal agent responses.

- [ ] `INT-002` Test an API-key-based tool bucket such as `exa`, `github`, or `tavily`.
Expected outcome: Missing credentials fail clearly, valid credentials are detected correctly, and successful calls return usable output.

- [ ] `INT-003` Test an OAuth-backed tool bucket such as Google, Spotify, or Home Assistant.
Expected outcome: Connect, callback, status, and disconnect flows all work end-to-end and reflect the correct configured service state.

- [ ] `INT-004` Test a Matrix-writing tool bucket such as `matrix_message`.
Expected outcome: Tool-generated messages appear in the intended room or thread with correct sender and context behavior.

- [ ] `INT-005` Test a sandboxed code-execution bucket such as `file`, `shell`, or `python`.
Expected outcome: Calls route through the expected worker backend, honor the configured execution scope, and preserve state only when the scope says they should.

- [ ] `INT-006` Test a long-lived session bucket such as `claude_agent`.
Expected outcome: Stable session identifiers preserve backend session continuity and distinct sub-session labels remain isolated from each other.

- [ ] `INT-007` Test an attachment-aware bucket such as `attachments` plus a downstream file-consuming tool.
Expected outcome: Attachment metadata survives the tool boundary and context scoping still prevents cross-room leakage.

- [ ] `INT-008` Compare runtime integration behavior with the dashboard catalog and metadata presentation.
Expected outcome: UI availability, required credentials, and runtime capability do not contradict each other for the same integration.

- [ ] `INT-009` Exercise Google integration admin bootstrap and reset through the bundled API or dashboard using `/api/google/configure` and `/api/google/reset`.
Expected outcome: OAuth client credentials are written to the active runtime environment, config reload succeeds, and the dashboard state transitions between admin-setup-required and ready-to-connect without stale status.

- [ ] `INT-010` Exercise Home Assistant via both OAuth and long-lived-token setup, then call `/api/homeassistant/entities` and `/api/homeassistant/service`.
Expected outcome: Both connection modes persist usable credentials, entity listing reflects the live instance, and service calls succeed or fail clearly against the actual Home Assistant runtime.

- [ ] `INT-011` Compare one OAuth-backed integration under `shared`, `user`, and `user_agent` execution scopes, including an unsaved draft scope override in the dashboard.
Expected outcome: Shared-only integrations remain visible but unsupported outside shared scope, dashboard credential status becomes non-authoritative for unsaved draft scope overrides, and connect flows reject isolating scopes the runtime does not support.

- [ ] `INT-012` Exercise Google or Home Assistant OAuth callbacks after changing user or target service context, then retry credential writes with stale or mismatched dashboard state.
Expected outcome: OAuth callback state stays bound to the original user and service, stale or mismatched callback attempts are rejected, and persisted credential writes only apply to the committed supported scope.

## 17. Suggested Parallel Execution Split

Use workstream `A` for boot, config reload, room provisioning, and router behavior.
Use workstream `B` for message dispatch, streaming, stop behavior, teams, and commands.
Use workstream `C` for authorization, media, memory, knowledge, private roots, and scheduling.
Use workstream `D` for skills, plugins, workers, representative integrations, and the OpenAI-compatible API.
Use workstream `E` for the bundled dashboard and runtime API surfaces.
Use workstream `F` for the SaaS platform and platform backend operator flows.

## Exit Criteria

Do not call the live test complete until every checklist item has a recorded result or an explicit justified skip reason.
Do not merge fixes discovered during execution without linking them back to the failing test IDs from this checklist.
When the first execution pass finishes, convert repeated failures into a smaller regression smoke suite built from the highest-value IDs in this document.
