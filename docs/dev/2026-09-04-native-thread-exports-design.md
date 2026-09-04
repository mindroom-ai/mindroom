# Native workspace thread exports

Date: 2026-09-04

## Goal

Move the `thread-export-plugin` into MindRoom as a built-in feature configured per agent, and archive the plugin repository.
The plugin already delegates every export to `mindroom.thread_export`; what it adds is trigger plumbing, target resolution, and workarounds for the plugin boundary.
Upstreaming removes the workarounds and lets the feature use runtime state the plugin had to re-derive from disk.

## What the plugin re-derives that the runtime already owns

| Plugin | Runtime |
| --- | --- |
| Fresh Matrix login per pass through `login_agent_user`, then `client.close()` | Each bot's authenticated `nio.AsyncClient` |
| `open_event_journal` and `bind_event_journal` per pass | The bot's bound `EventJournalStore` and its principal store |
| Scans `private_instances/` on disk and caches validated roots in module globals with a revision counter | Discovery is cheap once it runs off the event loop; the cache existed to survive plugin hot reload |
| `managed_account_user_id` from `matrix_state.yaml` | `bot.agent_user.user_id` |
| Untyped `settings: Mapping[str, object]` with `isinstance` fallbacks | A Pydantic field on `AgentConfig` |
| `asyncio.run` on a private loop in a worker thread, with a single-flight lock smuggled into a core package `__dict__` | One orchestrator-owned task; storage calls run through `asyncio.to_thread` |
| `is_active()` predicate threaded through every function, `_live_hook_seen`, staged-module guards | Not needed for built-in code |

## Design

### Configuration

```yaml
agents:
  code:
    thread_exports: true            # defaults below
  research:
    thread_exports:
      invited_rooms: false          # config rooms only
      private_room_scope: owner     # private agents only
```

`AgentConfig.thread_exports: AgentThreadExportConfig | None = None`.
A `before` validator maps `true` to the default model and `false` to `None`.
`AgentThreadExportConfig` has `invited_rooms: bool = True` and `private_room_scope: Literal["owner", "owner_and_agent"] = "owner_and_agent"`.
The debounce is a module constant of two seconds; the plugin setting was never tuned in practice.

### Modules

`thread_export/workspace_sync.py` owns the feature.
`WorkspaceThreadExportRunner` holds the pending state (dirty room IDs, full-pass flag), the debounced single-flight loop, target resolution for shared and private agents, cleanup for agents whose `thread_exports` was removed, and one export pass.
Its dependencies arrive through a `WorkspaceThreadExportDeps` dataclass: `runtime_paths`, `config_provider`, and `bot_provider`.
The orchestrator owns one runner for its whole lifetime: `start()` on the first support-service sync, `queue_full_pass()` after bots start and after every config reload, `stop()` at shutdown.
A full pass with no agent enabling exports is the cleanup sweep for agents that used to, so there is no separate on/off lifecycle.
`mark_room_activity(room_id)` is the trigger the bots call.

Private-instance enumeration lives in `private_instance_identity_store.private_instances_for_agent`, which owns the on-disk layout and the record validation; the runner only turns each instance into a workspace target.

### Triggers

The plugin used the `message:received` and `message:after_response` hooks.
Neither fires for edits, redactions, or messages a bot ignores, and `message:after_response` exists only because a bot's own reply never reaches `message:received`.
The runtime has one place that sees every conversation event exactly once per bot: `JournalIngress._admit`.
A new `on_room_activity: Callable[[str], None]` callback fires there after a successful `ADMITTED` result for `MESSAGE`, `MEDIA`, and `REDACTION` kinds, for both actionable and context-only events.
`JournalDispatcher` passes it through, `AgentBot` receives it as a constructor argument, and `create_bot_for_entity` forwards the orchestrator's coordinator method.
Every bot in a room fires for the same event; the dirty set deduplicates.

### Export pass

`service.py` splits the current `export_threads_to_targets_once` into two layers.
`ThreadExportSource` pairs a `nio.AsyncClient`, a `ProjectedThreadReader`, and the rooms it reads.
`export_threads_to_sources` runs the shared body: validate targets, export each source, reconcile a full pass, return stats.
`export_threads_to_targets_once` keeps the CLI behaviour: read `matrix_state.yaml`, log in per account group, open and bind the journal, build sources, delegate.
The runner builds sources from live bots instead: configured rooms read through the router bot, invited rooms through the invited entity's bot, each with `export_conversation_reader(client=bot.client, store=bot.journal_principal(), self_sender=bot.agent_user.user_id)`.
A bot that is not running, the router included, makes its rooms unreadable for that pass: failures are recorded, nothing is retracted, and the full pass queued after bots start or after a reload catches up. A pass that crashes keeps the pending set for the next trigger.

### Blocking I/O

The plugin moved whole passes onto a worker thread because YAML re-parsing blocked the event loop for seconds.
A live client cannot leave its loop, so the pass stays on the loop and the storage layer moves instead: `execution.py` wraps `write_thread_payload`, `remove_stale_thread_exports`, `write_room_index`, `remove_room_export`, and `room_has_thread_exports` in `asyncio.to_thread`, and `service.py` does the same for `prepare_export_root` and full-pass reconciliation.
`_serialized_export_mutation` already makes those functions thread-safe.
The CLI gets the same code path and the same behaviour.

### Targets

Shared agent: `agents/<name>/workspace/thread_exports`, membership scope `(agent_user_id,)`.
Private agent: one target per instance root under `private_instances/`, workspace resolved through `resolve_agent_workspace_from_state_path`, scope `(owner,)` or `(owner, agent_user_id)` by `private_room_scope`.
An instance counts only when its scope record validates and names the agent's current `private.per` scope; anything else has its stale export tree cleared.
Discovery runs once per pass through `asyncio.to_thread`, so no cache, revision counter, or eviction path exists.
On a full pass, every configured agent without `thread_exports` has its export trees cleared, so removing the field from `config.yaml` cleans up on hot reload.
`clear_thread_export_root` refuses roots without the ownership marker, so cleanup can never touch user files.

### Membership

`_authorized_room_accumulators` keeps using `client.joined_members` on the live client.
The retain-on-lookup-failure rule stays: a failed lookup writes nothing and deletes nothing, a definitive absence retracts the room.

### Not carried over

- `debounce_seconds` setting.
- Hot-reload guards, `_runner_tasks`, the shared pass lock.
- Removing exports for an agent whose Matrix account is missing; a bot that has not started simply contributes nothing to the pass.

## Testing

- `tests/test_thread_export_workspace_sync.py`: debounce and coalescing, full pass subsumes dirty rooms, a failing pass does not stop the runner, disabled-agent cleanup, shared and private target resolution, private identity validation, membership scopes, bot-not-ready leaves work pending.
- `tests/test_private_instance_identity.py`: `private_instances_for_agent` enumeration and ownership flags.
- `tests/test_journal_ingress.py`: `on_room_activity` fires for admitted conversation kinds only.
- Config tests for the `thread_exports` field and its `true`/`false` shorthand.
- Existing `test_thread_export_*` suites keep covering the CLI path through the new split.

## Docs

- `docs/configuration/agents.md`: new `thread_exports` section with layout, private-instance behaviour, and the semantic-search recipe from the plugin README.
- `docs/cli.md`: one line pointing from `threads export` to the per-agent setting.
- `docs/plugins.md`: community plugin table no longer lists the archived plugin.

## Plugin repository

`thread-export-plugin` gets a README banner in the `workloop-plugin` style pointing at the native setting and the upstreaming pull request, then is archived on GitHub after merge.
