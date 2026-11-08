# Workspace Files

A chat client such as MindRoom Chat can browse and edit the files of an agent's workspace, the folder its file, coding, and shell tools work in, and follow changes to them live.
A person opens a workspace through a room, and sees the workspaces of the agents there that they may use.

## Who may open and change a workspace

A person may open an agent's workspace through a room while they and the agent are both joined to it, the agent's [access rules](https://docs.mindroom.chat/authorization/#responder-access) let them use the agent there, and the room is not one of the agent's [contact rooms](https://docs.mindroom.chat/agent-contacts/).
A shared agent has one workspace for everyone who uses it, and a [private agent](https://docs.mindroom.chat/configuration/agents/#private-instances) gives each person their own, the one its turns for them use.
What a person may do there follows the agent's tools, as its turns build them with their settings:

| Access | When |
|--------|------|
| `read` | The agent has a tool that takes local file paths or runs code, such as `file`, `coding`, `shell`, or `python` |
| `write` | One of those tools can change files in the workspace, and the person may post messages in the room |

A tool can change files when the agent's settings leave on one of its writing functions, such as `save_file` or `replace_file_chunk` of `file` or `write_file` or `edit_file` of `coding`, or when it runs code, as `shell`, `python`, and `script` do.
So `file` with `enable_save_file: false` and `enable_replace_file_chunk: false` gives only `read`, and a person whom the room's power levels keep from posting may read but not change anything.
A tool changes files in the workspace where it runs in the primary process, in a dedicated Docker or Kubernetes worker, or on a `static_runner` with [`MINDROOM_STATIC_RUNNER_AGENT_STORAGE`](https://docs.mindroom.chat/deployment/sandbox-proxy/#filesystem-isolation-and-agent-data); on a `static_runner` that sees only its own storage it changes that storage instead, so it gives only `read`.
An agent with none of these tools opens no workspace.
The agent sees a saved file on its next tool call; nothing needs restarting.

## Paths and hidden files

Paths are relative to the workspace, with `/` between folders, and `""` names the workspace itself.
Absolute paths, paths starting with `~`, `..`, NUL, and backslashes answer 400.
Links are followed while they stay inside the workspace; a path through a link that leads outside answers 403.
Only folders, regular files, and links are listed or read; pipes, sockets, and devices are left out, and reading one answers 400.
Git's own files, `.git` and anything below it, are never saved, made, moved, or deleted (403), although deleting a folder deletes any repository in it.
The workspace itself can't be deleted, moved, or replaced (400).

The workspace is the agent's home folder, so credentials kept there are hidden: they are never listed, read, changed, or reported as changed, even through a link, and a path naming one answers 404 as an absent one does.
Hidden, in any letter case:

- at the top of the workspace: `.ssh/`, `.gnupg/`, `.aws/`, `.azure/`, `.kube/`, `.docker/`, `.password-store/`, `.mindroom/`, `.config/gh/`, `.config/gcloud/`, `.config/hub/`, `.config/git/credentials`, `.claude/.credentials.json`, and `.codex/auth.json`;
- in any folder: `.netrc`, `.git-credentials`, `.pgpass`, `.pypirc`, `.npmrc`, every name starting with `.env` (such as `.env.local` and `.envrc`), and SSH keys (`id_<type>`, with an optional `_sk`, `.pub`, or `-cert.pub`);
- below any `.git` folder: `config`, `credentials`, and names ending in `.credentials`.

A folder that keeps one of the top-level credentials, such as `.config`, `.claude`, or `.codex`, can't be moved or deleted, and nothing can be moved in under its name (403), since its credentials would then show.

## Routes

Clients call these routes with a client session, `Authorization: Bearer <session_token>` from `POST /api/client-sessions` (see [Plugin HTTP routes](https://docs.mindroom.chat/plugins/#http-routes)), from an origin `MINDROOM_COMPUTER_ALLOWED_ORIGINS` lists.
Each route below is under `/api/rooms/{room_id}`, and `{agent_user_id}` is the agent's Matrix user ID.

| Route | Answer |
|-------|--------|
| `GET /workspaces` | `{"workspaces": [{"agent_user_id", "display_name", "private", "files"}]}`, the workspaces the caller may open, in config order; `files` is `read` or `write` |
| `GET /agents/{agent_user_id}/workspace/folder?path=<folder>` | `{"path", "entries", "truncated"}`: the folder's entries, folders and links to folders first, then by name |
| `GET /agents/{agent_user_id}/workspace/file?path=<file>` | `{"path", "encoding", "content", "size", "mtime", "etag"}` |
| `PUT /agents/{agent_user_id}/workspace/file` with `{"path", "content", "encoding", "etag"}` | `{"etag", "size", "mtime"}` of the saved file |
| `POST /agents/{agent_user_id}/workspace/folder` with `{"path"}` | 204; the folder is made with any folders above it |
| `POST /agents/{agent_user_id}/workspace/move` with `{"path", "to"}` | 204; a link moves as a link |
| `DELETE /agents/{agent_user_id}/workspace/entry?path=<path>` | 204; a folder goes with everything in it, and a link without what it leads to |

An entry has `name`, `path`, `kind` (`file`, `folder`, or `link`), `size` (0 for a folder or link), `mtime` in milliseconds since the epoch, and for a link `target`, which is `file` or `folder`, or null when it leads outside, to a hidden credential, or nowhere.
A folder lists at most 5000 entries, and `truncated` says when there were more.
Before the agent's first turn for this person its workspace does not exist yet, and the workspace itself lists empty.
A file's `content` is its text when it is valid UTF-8, with `encoding: "utf-8"`, and base64 otherwise, with `encoding: "base64"`; its `etag` is the SHA-256 of its content, in hex.
Files over 5 MiB answer 413, to read and to save.

A save sends `content` as text, or as base64 with `encoding: "base64"`.
Without `etag` it overwrites the file; with the `etag` the client read, it saves only while the file still has that content, and otherwise answers 409 with `{"detail", "etag"}`, where `etag` is the file's current one, or null once it is gone.
A save is atomic, keeps an existing file's permissions, and makes the folders above a new file; the first save or new folder in a workspace not made yet makes it as the agent's first turn would.
Making a folder where something already is, or moving onto a taken name, answers 409, and moving a folder into itself answers 400.

A missing, unknown, or expired session answers 401.
A person who may not use the agent there, or a workspace the agent's tools don't reach, answers 403, as does any change from someone with only `read`.
While MindRoom can't yet decide whether the person may use the agent, the routes answer 503 as [`RoomReader.agent()`](https://docs.mindroom.chat/plugins/#http-routes) does.

## Following changes

The websocket `/api/rooms/{room_id}/agents/{agent_user_id}/workspace/changes` sends `{"type": "changed", "paths": [...]}` for each batch of changes in the workspace, by the agent or anyone, at most every 150 ms, with the changed paths sorted and relative to the workspace.
It leaves out Git's internals, `node_modules`, `.venv`, `.cache` and other caches, editor swap files, MindRoom's temporary save files, and the hidden credentials, and it follows links only while they stay inside the workspace.
It signs in as [plugin websocket routes](https://docs.mindroom.chat/plugins/#websocket-routes) do, and anyone who may open the workspace, `read` or `write`, may follow it.
It checks again every 15 seconds, under the config served then, and closes with 1008 once the workspace is no longer open to the caller or is no longer the one it followed, as after a config save makes the agent private, and with 1013 while MindRoom can't tell; the client then connects again.
In a workspace with more than 10,000 folders, changes in the folders past that are not reported.
