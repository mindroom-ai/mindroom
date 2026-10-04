---
icon: lucide/file-down
---

# Thread Exports

`thread_exports` keeps a YAML copy of the agent's conversation history inside its workspace, so its `file` and `shell` tools can grep past threads without any Matrix API access.

```yaml
agents:
  code:
    thread_exports: true            # defaults below
  research:
    thread_exports:
      invited_rooms: false          # config rooms only
      private_room_scope: owner     # private agents only
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `invited_rooms` | bool | `true` | Also export user-created rooms the agent joined through invites. Current membership is always required |
| `private_room_scope` | string | `"owner_and_agent"` | Private agents only. Within the agent's configured and invited rooms, `owner_and_agent` requires both the requester and agent to be joined; `owner` requires only the requester |

Exports land at `<storage_root>/agents/<agent>/workspace/thread_exports/<urlencoded room key>/<urlencoded thread id>.yaml`, the same layout `mindroom threads export` writes.
Inside the agent's own tools that directory is `$MINDROOM_AGENT_WORKSPACE/thread_exports/`.
Each thread file holds `version`, `room` metadata, `thread` metadata including the latest thread summary as `thread.summary`, and a `messages` list.
Each room directory also holds an `index.json` mapping every thread file to its message count, participants, latest summary, and last activity, sorted by most recent activity.
A thread file larger than 64 MiB or holding more than 250,000 YAML nodes, roughly 15,000 messages, is indexed from its header without participants or last activity.
A thread whose messages together pass 128 MiB is not exported: the pass reports it as failed and leaves any previous file for it in place.
A room whose thread files together pass 256 MiB gets an index of its most recently written threads only, listing the rest under `unindexed_files`, with a logged warning.

MindRoom re-exports a room within about two seconds of a message, edit, redaction, or membership change in it, batching everything that arrives in that window into one pass, and runs one full pass at startup and after every config reload.
A full pass also removes exports for threads and rooms that no longer exist or that the agent may no longer read, and clears the export tree of any configured agent whose `thread_exports` was removed.
A full pass that could export no room at all skips that directory-wide removal, because an empty result cannot be told apart from a failed one; the same guard and its manual-cleanup guidance are described under [`threads export`](#threads-export).
Files are rewritten only when a thread's content changed.
Agents may edit or delete their exported files; deleted files return on the next pass that touches the room.
Each workspace is populated only through that agent's running Matrix account and principal-bound event-journal projection, so a pass costs no Matrix history call for threads that agent has seen.

## Security and Retention

Thread export authorization controls which data future passes may write.
Export cleanup is best-effort workspace maintenance, not a revocation or data-erasure boundary.
Previously exported data may remain or may have been copied or committed by the agent.
Principal isolation does not trigger a one-time purge or migration of existing exports.

Shared agents export only rooms where the agent's own Matrix account is currently joined.
Private agents (`private:`) get one export tree per materialized instance under `<storage_root>/private_instances/<scope-key>/<agent>/<private root>/thread_exports/`; each tree stays within that agent's configured and invited rooms and is scoped to the requester's current room memberships, so one requester's private workspace never accumulates other users' conversations.
An instance counts as materialized once MindRoom itself has created or used it for its requester, so an instance last used with v2026.10.52 or earlier exports again after its requester's next turn.
A membership lookup failure blocks new writes for that room and leaves existing files in place until a successful lookup proves that access was revoked.

## Semantic Search Over Exports

The exports are plain YAML, so file-aware tools already cover keyword search.
With an embedder configured (`memory.embedder`), point a knowledge base at the export directory for semantic search through `search_knowledge_base`.

```yaml
knowledge_bases:
  code_threads:
    path: ./mindroom_data/agents/code/workspace/thread_exports
    description: Exported Matrix conversation history for the code agent
    exclude_patterns: ["*/index.json"]
agents:
  code:
    thread_exports: true
    knowledge_bases: [code_threads]
```

For a private agent, index the private-root-relative path instead:

```yaml
agents:
  secret:
    thread_exports: true
    private:
      per: user
      knowledge:
        path: thread_exports
        description: Your exported conversation history
```

The active thread's file rewrites on every message, so a watching semantic index re-embeds that thread per message.
This is negligible with a local embedder but costs real money with paid embedding APIs in busy rooms.

## threads export

Export Matrix threads to YAML files for grep/ripgrep search.
Keep MindRoom running with its API enabled while exporting; both one-shot and `--watch` exports use its live Matrix clients and journal readers.
The CLI calls `--url`, then `MINDROOM_URL` from the selected runtime environment, or `http://127.0.0.1:8765` by default.
Set `MINDROOM_API_KEY` when API authentication is enabled; hosted deployments require an authorized bearer token.
When `MINDROOM_API_KEY` is set, the CLI sends it only over HTTPS or loopback HTTP and refuses a remote `http://` URL before making any request, even with `--watch`; redirects are disabled.
The selected `--config` and `--storage-path` must match the running installation, and output paths refer to that runtime's filesystem.
There is no offline export mode or separate Matrix login.
Rooms joined through authorized invites (user-created rooms) are exported too, each with the invited entity's own account, unless `--no-invited-rooms` is passed.
By default it writes to `<storage>/thread_exports`.
For a continuously updated copy inside an agent's own workspace, set `thread_exports` on the agent instead; see [Thread Exports](#thread-exports).
A thread file is only rewritten when its content changed, so `exported_at` reflects the last content-changing export.
Each thread document includes the latest MindRoom thread summary as `thread.summary` when one exists.
Each room directory also gets an `index.json` mapping every thread file to its message count, participants, latest summary, and last activity, sorted by most recent activity.
A thread file larger than 64 MiB or holding more than 250,000 YAML nodes, roughly 15,000 messages, is indexed from its header without participants or last activity.
A thread whose messages together pass 128 MiB is not exported: the pass reports it as failed and leaves any previous file for it in place.
A room whose thread files together pass 256 MiB gets an index of its most recently written threads only, listing the rest under `unindexed_files`, with a logged warning.
Complete passes normally remove exported room and thread files that are no longer present or authorized; a `--room` pass only reconciles the selected room.
The zero-room guard skips only final directory-wide reconciliation of rooms absent from the pass, while definitive per-room category or membership revocations still delete their exports.
A warning is logged when that guard preserves existing target state because the pass has no positive room evidence.
A complete room enumeration that returns zero threads preserves existing YAML exports for that room and logs a warning because an anomalous empty response cannot be distinguished from deletion of the final thread.
After either warning, verify the source state and remove the preserved export manually only when the deletion is confirmed; workspace git history remains the recovery path for mistaken cleanup.
Enabled targets whose resolved output directories are equal or nested are all skipped before Matrix work.
MindRoom claims an empty output root by writing a `.mindroom-thread-exports` ownership marker.
Any populated markerless root is refused and left unchanged, regardless of whether its contents resemble thread exports.
To use an existing populated root, create `.mindroom-thread-exports` inside it containing exactly `{"format":"mindroom-thread-exports","version":1}` followed by a newline.
Unrelated entries in a marked root, such as `.DS_Store`, a `.git` directory, or your own notes, are never deleted.
A refused root is skipped for the entire pass, so it is neither exported to nor cleaned up, and the skip is reported as a target failure.
Cleanup then removes only recognizable room directories and thread YAML files, leaving unrelated entries untouched and logged.
Retracting a room whose directory still holds unrelated entries removes only the exported files and leaves the directory in place, and repeating the pass stays a quiet no-op.
Output paths with a terminal `.`, `..`, or empty leaf are rejected, as are symlinked final output and room directories.
Thread bodies come from the journal projection, read as the same principal a running bot writes it under, so an exported thread reduces edits, redactions, and long-text sidecars exactly the way agent prompts do.
A thread nobody has read yet is built from the homeserver once and then costs no Matrix history call at all, so a repeated export pass is a local read.
Hydration writes through the runtime's existing journal owner; export does not open another journal or crypto store.
Normal config reloads wait for manual exports; forced replacement and shutdown cancel them and drain their history reads before closing their Matrix clients.
Every runtime replacement also cancels and drains automatic workspace exports, then queues a full pass that waits for publication to finish before borrowing current clients.
Automatic exports resume when replacement admission reopens, including after a failed or cancelled publication.
An interrupted pass preserves completed files; rerun the export to finish the pass and rebuild indexes.
