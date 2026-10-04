# Thread Exports

Thread exports write Matrix conversation history to YAML files that agents and operators can search with ordinary file tools.
Only Matrix threads are exported; plain room messages outside threads, including conversations with agents using `thread_mode: room`, are not.
Set `thread_exports` on an agent to keep a continuously updated copy inside its workspace, so its `file` and `shell` tools can grep past threads without Matrix access.
Use [`mindroom threads export`](#threads-export) for a one-shot or watched export to any directory.

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
| `private_room_scope` | `owner_and_agent` or `owner` | `owner_and_agent` | Private agents only. Both values require the requester to be joined to an exported room; `owner_and_agent` also checks the agent's membership. The agent must be joined either way to read the room |

## Export Layout and Contents

Workspace exports land at `<storage_root>/agents/<agent>/workspace/thread_exports/<urlencoded room key>/<urlencoded thread id>.yaml`, which agent tools see as `$MINDROOM_AGENT_WORKSPACE/thread_exports/`.
`mindroom threads export` writes the same layout under its output directory.
Each thread file holds `version`, `room` metadata, `thread` metadata including the latest thread summary as `thread.summary`, and a `messages` list.
Exported messages match the history agents see in their prompts, including edits, redactions, and long messages.
Each room directory also holds an `index.json` mapping every thread file to its message count, participants, latest summary, and last activity, sorted by most recent activity.
A thread file is rewritten only when its content changed, so `exported_at` reflects the last content-changing export.

Size limits:

- A thread file larger than 64 MiB or holding more than 250,000 YAML nodes, roughly 15,000 messages, is indexed without participants or last activity.
- A thread whose messages together pass 128 MiB is not exported; the export reports it as failed and keeps any previous file for it.
- A room whose thread files together pass 256 MiB gets an index of its most recently written threads only, listing the rest under `unindexed_files`, with a logged warning.

## When Workspace Exports Update

MindRoom re-exports a room within about two seconds of a message, edit, redaction, or membership change in it, and runs a full pass at startup and after every config reload.
A full pass removes exports for threads and rooms that no longer exist or that the agent may no longer read, and clears the export tree of any agent whose `thread_exports` was removed.
Agents may edit or delete their exported files; deleted files return on the next pass that touches the room.

If a pass exports no room at all, MindRoom keeps exports of rooms it did not reach, and if a room returns zero threads, it keeps that room's thread files; both cases log a warning.
Rooms excluded by config, such as invited rooms after `invited_rooms: false`, and rooms the agent or requester has left are still removed.
After such a warning, verify the source state and remove the preserved export manually only when the deletion is confirmed.

## Security and Retention

Shared agents export only rooms where the agent's own Matrix account is currently joined.
Private agents (`private:`) get one export tree per instance under `<storage_root>/private_instances/<scope-key>/<agent>/<private root>/thread_exports/`, limited to the agent's configured and invited rooms and to the requester's current room memberships, so one requester's workspace never holds other users' conversations.
A private instance is exported only after MindRoom has created or used it for its requester.

Thread export settings control what future passes write.
Cleanup is not a revocation or data-erasure boundary: previously exported data may remain, or may already have been copied or committed by the agent.

## Semantic Search Over Exports

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

The active thread's file is rewritten on every message, so a watching index re-embeds that thread per message.
This is negligible with a local embedder but costs real money with paid embedding APIs in busy rooms.

## threads export

`mindroom threads export` exports Matrix threads to YAML files through a running MindRoom instance; options are listed in the [CLI reference](https://docs.mindroom.chat/cli/#threads-export).
Keep MindRoom running with its API enabled; there is no offline export mode or separate Matrix login.
The CLI calls `--url`, then `MINDROOM_URL` from the selected runtime environment, then `http://127.0.0.1:8765`.
Set `MINDROOM_API_KEY` when API authentication is enabled; hosted deployments require an authorized bearer token.
With `MINDROOM_API_KEY` set, the CLI refuses a remote `http://` URL with `Use HTTPS when sending MINDROOM_API_KEY to a remote endpoint.`; HTTPS and loopback HTTP work.
The selected `--config` and `--storage-path` must match the running installation, and output paths refer to that runtime's filesystem.
Output defaults to `<storage>/thread_exports`.
Rooms joined through authorized invites are exported with the invited entity's own account unless `--no-invited-rooms` is passed.

A complete pass removes exported room and thread files that are no longer present or authorized; a `--room` pass only cleans up the selected room.
The same zero-room and zero-thread safeguards as [workspace exports](#when-workspace-exports-update) apply.
An interrupted pass keeps completed files; rerun the export to finish it and rebuild indexes.

### Output directory rules

MindRoom claims an empty output directory by writing a `.mindroom-thread-exports` marker file.
A populated directory without that marker is refused, skipped for the whole pass, and reported as a target failure.
To use an existing populated directory, create `.mindroom-thread-exports` inside it containing exactly `{"format":"mindroom-thread-exports","version":1}` followed by a newline.
Cleanup removes only exported room directories and thread files; unrelated entries such as `.DS_Store`, a `.git` directory, or your own notes are never deleted.
Output paths ending in `.`, `..`, or an empty name are rejected, as are symlinked output and room directories.
