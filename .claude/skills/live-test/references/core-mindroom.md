## IMPORTANT: Check for an Existing Homeserver First
Some dev machines already run a Matrix homeserver (Tuwunel/Conduwuit) on localhost:8008; do not start a second one there.
Probe first with `curl -s --max-time 5 http://localhost:8008/_matrix/client/versions`.
If it responds, skip `just local-matrix-up`; if it does not, boot the Docker stack with `just local-matrix-up` (and `just local-matrix-down` when you are done).
Either way, use `MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false`.

# Core MindRoom Live Run

Use this reference for the local Matrix stack, the Python backend, Matty smoke tests, disposable Matrix users, and live API checks.

## NixOS Environment (CRITICAL)

On NixOS hosts (e.g., Incus containers), enter the repo Node.js 24 dev shell before running `uv run` commands.
The shell provides `libstdc++.so.6`, which is needed for numpy, qdrant, and chromadb.

```bash
nix-shell shell.nix
# then run normally inside the shell:
uv run pytest tests/test_foo.py -x -n 0 --no-cov -v
uv run mindroom run
```

If `nix-shell shell.nix` cannot resolve `<nixpkgs>`, use:

```bash
nix-shell -I nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos shell.nix
```

Without this shell, imports fail with `AttributeError: module 'mindroom' has no attribute 'bot'`.
The repo root `shell.nix` adds `stdenv.cc.cc.lib` to `LD_LIBRARY_PATH` on Linux, which is what provides `libstdc++.so.6`.

The shell includes Chromium and its shared libraries only on Linux so the backend and Node.js tools also work on macOS.

## Preflight

Run from the repo root.

```bash
uv sync --all-extras
if ! curl -fsS --max-time 5 http://localhost:8008/_matrix/client/versions >/dev/null; then
  just local-matrix-up
fi
curl -s http://localhost:8008/_matrix/client/versions | head -c 200
curl -s http://localhost:9292/v1/models | head -c 200
```

If you switched homeservers or see `M_FORBIDDEN`, stop the backend and clear the selected runtime's Matrix state before restarting MindRoom.

```bash
uv run python -c "from mindroom.constants import matrix_state_file, resolve_runtime_paths; matrix_state_file(resolve_runtime_paths()).unlink(missing_ok=True)"
```

This uses the same environment and `.env` selection as `mindroom run`, including `MINDROOM_CONFIG_PATH` and `MINDROOM_STORAGE_PATH`; if the backend uses `--config` or `--storage-path`, export the matching variable first, because a hardcoded `mindroom_data/matrix_state.yaml` only covers default storage beside the repository's config.
For a full destructive reset of the local homeserver, read the `just local-matrix-reset` warning in `docs/dev/ops.md` first.

## Fast Path: Use Existing Repo Config

Use this when the checked-in `config.yaml` already points at a working local or hosted model provider and there are no port or Matrix ID conflicts.

```bash
MATRIX_HOMESERVER=http://localhost:8008 \
MATRIX_SSL_VERIFY=false \
OPENAI_BASE_URL=http://localhost:9292/v1 \
OPENAI_API_KEY=sk-test \
UV_PYTHON=3.13 \
uv run mindroom run
```

Then wait for health and rooms.

```bash
curl -s http://localhost:8765/api/health
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false \
uv run --python 3.13 matty rooms
```

## Isolated Path: Persistent Config

Use this when the machine already has local MindRoom instances, existing Matrix users, occupied dashboard ports, or stale config.
Keep the config, storage, logs, and other evidence together in a persistent run directory.

```bash
run_root=".baspowers/sdd/live-test-$(date +%Y%m%dT%H%M%S)"
mkdir -p "$run_root"
uv run mindroom config init --matrix-server self-hosted --provider openai --force --path "$run_root/config.yaml"
```

`config init` has no `--minimal` flag; write the config YAML directly when you need a precise minimal shape (models, agents, teams, room membership, `mindroom_user`).
If no local model server is running on 9292 and no provider key is available, a ~60-line FastAPI stub serving `/v1/models` and `/v1/chat/completions` (JSON + SSE stream) is enough for deterministic end-to-end turns; run it with `uvicorn` from the venv.

Deep-merge the following patch into the generated config, preserving the `mind` agent's required `display_name` and its `personal` room.
This enables public joins and replies to current room members for the isolated local smoke test.
When you are targeting the local OpenAI-compatible server on `http://localhost:9292/v1`, start with `gpt-oss-low:20b`.
That is the suggested local chat model for this skill because it has been verified to work with MindRoom's `developer` messages in this repo.

Replace `<unique_suffix>` with a unique lowercase-letter/digit suffix before validating or running.
Minimum changes:

```yaml
models:
  default:
    provider: openai
    id: gpt-oss-low:20b
    extra_kwargs:
      base_url: http://localhost:9292/v1

agents:
  mind:
    access:
      current_room_members: true
      members_of_rooms: []
      users: []

memory:
  backend: file

mindroom_user:
  username: mindroom_user_<unique_suffix>

administrators:
  - __MINDROOM_OWNER_USER_ID_FROM_PAIRING__

room_defaults:
  join_policy: public
  invite_users:
    - __MINDROOM_OWNER_USER_ID_FROM_PAIRING__
```

Replace the owner placeholder through pairing or with the concrete disposable Matrix user ID before relying on administrator or invitation behavior.

For a real-model exploratory pass using an existing Codex login, replace the model entry with:

```yaml
models:
  default:
    provider: codex
    id: gpt-6-astra
```

This route uses the existing login; do not add a Codex token to the run's `.env`.
To exercise Mem0 memory instead of `backend: file`, set `memory.backend: mem0`, an `embedder` with `provider: openai`, `config.host: http://localhost:9292/v1`, and a `config.model` the local server serves, such as `embeddinggemma:300m`, and an `llm` with `provider: openai`, `config.model: gpt-oss-low:20b`, and `config.openai_base_url: http://localhost:9292/v1` (see `docs/memory.md`).
Without the embedder `host`, embedding calls go to `OPENAI_BASE_URL` or api.openai.com, and without `memory.llm`, fact extraction expects Ollama `gemma4` on `OLLAMA_HOST` or `http://localhost:11434`.

Then export an isolated runtime.

```bash
export MINDROOM_CONFIG_PATH="$run_root/config.yaml"
export MINDROOM_STORAGE_PATH="$run_root/mindroom_data"
export MINDROOM_NAMESPACE="live$(date +%H%M%S)"
export MATRIX_HOMESERVER=http://localhost:8008
export MATRIX_SSL_VERIFY=false
export OPENAI_API_KEY=sk-test
export UV_PYTHON=3.13
```

`MINDROOM_NAMESPACE` must match `^[a-z0-9]{4,32}$`.
Use lowercase letters and digits only.
Do not use underscores or hyphens.

In practice, it is often cleaner to write `"$run_root/.env"` and `source` it so the live run and later `curl` commands use the same values.

Example:

```bash
cat > "$run_root/.env" <<EOF
MATRIX_HOMESERVER=http://localhost:8008
MATRIX_SSL_VERIFY=false
MINDROOM_STORAGE_PATH=$run_root/mindroom_data
MINDROOM_API_KEY=live-test-$(date +%H%M%S)
OPENAI_API_KEY=sk-test
OPENAI_BASE_URL=http://localhost:9292/v1
EOF

set -a
source "$run_root/.env"
set +a
```

If `"$run_root/.env"` exists, inspect it for `MINDROOM_API_KEY`.
Use that key for `/api/*` requests so you are talking to the same isolated instance you started.

Start the isolated backend on a non-default API port.

```bash
uv run mindroom run --storage-path "$MINDROOM_STORAGE_PATH" --api-port 9876 --log-level INFO
```

With the API enabled and no dashboard build present, `mindroom run` runs `bun install` and `bun run build` in the checkout's `frontend/`; set `MINDROOM_AUTO_BUILD_FRONTEND=0` when the run only needs the backend and `/api/*`, for example when the checkout is a review snapshot or another worktree you must not change.

Health check:

```bash
curl -s http://localhost:9876/api/health
```

If you point the isolated run at a local OpenAI-compatible server, verify the chosen chat model can handle MindRoom's message roles before trusting the chat smoke test.
Some local llama.cpp or llama-swap templates reject `developer` messages and will fail both topic generation and agent replies with an error like `Only user, assistant and tool roles are supported, got developer`.
The repo's documented local path suggests `gpt-oss-low:20b` because it has been live-tested successfully here.

## Create a Disposable Matrix Account

When open registration is enabled on local Synapse, create a throwaway user directly through the Matrix client API.

```bash
set +x
username="smoketest$(date +%H%M%S)"
password="smoketestpass"
registration_response="$(curl -sS -X POST 'http://localhost:8008/_matrix/client/v3/register' \
  -H 'Content-Type: application/json' \
  -d "{\"auth\":{\"type\":\"m.login.dummy\"},\"username\":\"$username\",\"password\":\"$password\"}")"
read -r user_id access_token < <(
  REGISTRATION_RESPONSE="$registration_response" uv run python -c \
    'import json, os; response = json.loads(os.environ["REGISTRATION_RESPONSE"]); print(response["user_id"], response["access_token"])'
)
unset registration_response
```

The response is captured without printing its `access_token`, and shell tracing must remain disabled while the credential variables exist.

## Join a Public Room

If the agent room is public, join it with the Matrix API before using Matty.
Prefer the concrete room ID from backend logs because aliases are not always predictable in isolated runs.

Join by room ID:

```bash
room_id='!example:localhost'
encoded_room_id="$(uv run python -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$room_id")"
curl -sS -X POST "http://localhost:8008/_matrix/client/v3/join/$encoded_room_id" \
  -H "Authorization: Bearer $access_token"
```

Join by alias only when you know the exact alias:

```bash
room_alias='#personal_<namespace>:localhost'
encoded_alias="$(uv run python -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$room_alias")"
curl -sS -X POST "http://localhost:8008/_matrix/client/v3/join/$encoded_alias" \
  -H "Authorization: Bearer $access_token"
```

Use the actual alias created by the active config.

## Read and Send Messages with Matty

Matty accepts per-command credentials with `-u` and `-p`.
Matty restores a token session first, from `MATRIX_ACCESS_TOKEN`, `MATRIX_USER_ID`, and `MATRIX_DEVICE_ID` or from the token `matty auth` saved in `~/.config/matty/config.json`, and that session wins over `-u` and `-p`; make sure neither exists before testing with a disposable account.
Otherwise it logs in with `-u` and `-p`, then `MATRIX_USERNAME` and `MATRIX_PASSWORD` from the environment, then the saved username and password; it also loads the first `.env` above its install location, which is the repository root's `.env` when Matty is installed in the repository's `.venv`.
Matty refers to messages by handles `m1`, `m2`, … and to threads by `t1`, `t2`, …; thread handles persist across sessions.
`matty messages` takes `--limit` (default 20), `matty thread-start "$room_id" m2 "text"` starts a thread from a message, and most commands have a short alias (`r`, `m`, `t`, `th`, `ts`, `tr`, …; see `matty --help`).
Matty may be absent from a fresh worktree venv; if `matty` is not found after `uv sync --all-extras`, fall back to the raw Matrix client API with `curl` (register, `/join/{roomId}`, `PUT /rooms/{roomId}/send/m.room.message/{txn}`, and `GET /rooms/{roomId}/messages?dir=b`).
Correlate replies with the submitted event ID through either `m.thread` relations or the room-mode `m.relates_to["io.mindroom.in_reply_to"]` relation, then apply edits targeting the matched response event; follow the [tester observation protocol](../../../agents/mindroom-tester.md) for sender checks, terminal status, and timeouts.

List rooms:

```bash
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false \
uv run --python 3.13 matty rooms -u "$username" -p "$password" --format json
```

Use the concrete `room_id` for the generated `personal` room from backend logs and the room list.
Inspect its membership:

```bash
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false \
uv run --python 3.13 matty users "$room_id" -u "$username" -p "$password" --format json
```

Copy Mind's full namespaced Matrix user ID from that membership output into `agent_id`.
Send a smoke message:

```bash
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false \
uv run --python 3.13 matty send "$room_id" \
  "Hello $agent_id please reply with pong." \
  -u "$username" -p "$password"
```

`matty send` does not currently support `--format json`.
Use the plain send command, then confirm the result with `matty messages --format json` and `matty threads --format json`.

Read recent room messages:

```bash
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false \
uv run --python 3.13 matty messages "$room_id" -u "$username" -p "$password" --format json
```

List threads:

```bash
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false \
uv run --python 3.13 matty threads "$room_id" -u "$username" -p "$password" --format json
```

Read one thread:

```bash
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false \
uv run --python 3.13 matty thread "$room_id" t1 -u "$username" -p "$password" --format json
```

Reply in an existing thread:

```bash
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false \
uv run --python 3.13 matty thread-reply "$room_id" t1 "$agent_id continue" -u "$username" -p "$password"
```

Agents usually reply in threads and may stream by editing the same event, which can take 10 seconds or more.
Inspect the latest `io.mindroom.stream_status` in a client or event view that exposes it: `pending` and `streaming` mean the reply is still in progress, `approval_pending` means it waits for someone to approve or deny a tool call, and `completed` confirms success.
Record `cancelled` or `error` as terminal outcomes, as well as a reply that ends with a `**[Response interrupted…]**` note; body ellipses are not a completion signal.
When reading threads through the raw `/messages` API, the streamed edits are `m.replace` events whose `m.relates_to.event_id` is the placeholder reply, not the thread root, so collect the thread events first and then apply every `m.replace` whose target is one of them; filtering on the root alone leaves the reply stuck at `Thinking...`.
A deterministic stub on 9292 can also emit an OpenAI `tool_calls` delta when the user text contains a marker, which exercises MindRoom's tool hook chain end to end without a provider key.
If `matty threads` looks empty or flaky, use `matty messages --format json` to discover the thread handle and then read it directly with `matty thread`.

## Interaction Mechanics for Lifecycle Tests

- Replies stream only while the requester's Matrix presence is online; check the `Streaming decision ... is_online=` log line.
  A raw-API tester stays offline unless something keeps calling `/sync?set_presence=online`, so run a small keepalive loop for streaming scenarios and set presence `offline` to test the non-streaming path.
- Stop is a `🛑` reaction on the agent reply.
  Approve is a `✅` reaction on the router's `🔒 Approval required` card; deny is a text reply whose `m.in_reply_to` is the card (with `is_falling_back` false in a thread).
- When a follow-up should go to another agent, put that agent in `m.mentions`; the body text alone does not route it.
- Sync tools such as `sleep` run in threads that cannot be preempted: a cancelled job stays `cancel_requested` until the call returns, and the process lingers on SIGTERM until it does.
- Stop backends by exact PID, and wait for the old process to exit before restarting it; otherwise the new backend stops with "Another MindRoom is already running with the storage at …" while the old one keeps serving.
  `pgrep -f "<pattern>"` also matches the shell running that very command, so select the Python process instead, for example `ps -eo pid,args | awk '$2 ~ /\.venv\/bin\/python3$/ && /mindroom run --api-port 9876/ {print $1}'`.
- Give background commands absolute paths, including `nix-shell /abs/path/shell.nix`; a leading `cd` is not always applied to a backgrounded command.
- A Stop or other reaction whose handler keeps failing stays pending and blocks every later event of that agent in the room; check `pending_event_failed` in the log when an agent stops answering.

## Helper Scripts and Run Records

The standard-library scripts in `scripts/` run from the repo root as `uv run python .claude/skills/live-test/scripts/<name>.py`; `--help` shows their options.

- `approval_cards.py list|wait|approve "$room_id"` lists a room's tool-approval cards with their current status, waits for a pending one, or approves one with `✅`, as the account whose token is in `LIVE_TEST_ACCESS_TOKEN`; only the card's approver can approve it.
  A card is an `io.mindroom.tool_approval` event from the router, not an `m.room.message`, with `tool_name`, `arguments`, `agent_name`, `approver_user_id`, `thread_id`, and `status: pending`; each status change is an `m.replace` edit of the same type whose `m.new_content.status` is `approved`, `denied`, or `expired`.
- `llm_requests.py` summarizes the request log that `debug.log_llm_requests: true` writes: each request's time, agent, room, thread, requester, and offered tools, with `--grep` and `--show` to search the bodies.
- `wait_for.py` polls a URL, or a log regex after a byte offset, instead of a guessed `sleep`; anchor `--fail-regex` on the level, such as `\[error`, because a bare `invalid` also matches `agent_reply_memberships_invalidated`.

The backend log and `mindroom_data/tracking/tool_calls.jsonl`, which records every tool call with its sanitized arguments and result, use UTC with a `Z` suffix.
The LLM request log `mindroom_data/logs/llm_requests/llm-requests-YYYY-MM-DD.jsonl` uses local time with an offset and names its files by local date, so compare parsed times, not strings; `llm_requests.py` prints UTC.
Before each scenario, note `date -u +%Y-%m-%dT%H:%M:%S` and the backend log's byte size (`stat -c %s`), so later reads and waits start there.

Gotchas:

- In zsh, an unquoted `$var` holding several words stays one argument; write `${=var}` to split it.
- Approving two cards from parallel threads at once reaches race paths that one card at a time never does.
- Changing `personal_rooms.agent` collides on the personal-room aliases unless `personal_rooms.alias_prefix` changes too.
- Changing a plugin's entry in `config.yaml`, its settings included, restarts every bot.
- A user must join a room before the client API reads its timeline; an invitation is not enough.
- `docker logs mindroom-synapse 2>&1 | grep /typing/` shows whether anything sent typing notices.

## Live API Checks

When a change affects the bundled API, hit the live endpoint on the instance you started instead of testing a different local server by accident.

With dashboard auth enabled:

```bash
curl -sS -X POST 'http://localhost:9876/api/config/agent-policies' \
  -H "Authorization: Bearer $MINDROOM_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"defaults":{},"agents":{"helper":{"delegate_to":[]},"leader":{"delegate_to":["mind"]},"mind":{"private":{"per":"user"}}}}'
```

Always confirm the port belongs to the same MindRoom instance you launched.

## Dedicated Docker Workers

Worker-side changes only reach Docker workers through an image built from this checkout, which takes a few minutes in the background:

```bash
docker build -t mindroom-branch:dev -f local/instances/deploy/Dockerfile.mindroom . > "$run_root/docker-build.log" 2>&1
```

Add `MINDROOM_WORKER_BACKEND=docker`, `MINDROOM_DOCKER_WORKER_IMAGE=mindroom-branch:dev`, a unique `MINDROOM_DOCKER_WORKER_NAME_PREFIX`, a random `MINDROOM_SANDBOX_PROXY_TOKEN`, and `MINDROOM_DOCKER_WORKER_READY_TIMEOUT_SECONDS=120` to the run's `.env`, and set `defaults.worker_tools` and `defaults.worker_scope` in its config.
Inspect what each worker mounts with `docker inspect <name> --format '{{range .Mounts}}{{.Source}} -> {{.Destination}} rw={{.RW}}{{"\n"}}{{end}}'`, and remove the prefixed containers after stopping the backend.
Tool-call arguments are not logged at `INFO`; read them from the agent's session database below `mindroom_data/agents/<agent>/sessions/`.
Explicit `access` blocks with `members_of_rooms: []` and only `current_room_members: true` deny every identity-only authorization check, such as inviting that agent to a shared directory, so omit `access` unless the scenario needs it.

## Two Federated Homeservers

Use this when a scenario crosses homeservers, such as contacts between deployments.
Run two `ghcr.io/mindroom-ai/mindroom-tuwunel` containers on one Docker network, each with a `tuwunel.toml` naming `server_name = "hsa.test"` (or `hsb.test`), `port = 8008`, `allow_registration = true`, a `registration_token`, `allow_invalid_tls_certificates = true`, `ip_range_denylist = []`, and `trusted_servers = []`, and publish each container's 8008 on its own localhost port.
The image has no direct TLS (`tuwunel was not built with direct TLS support ("direct_tls")`), and federation needs HTTPS on 8448, so give each homeserver an `nginx:alpine` sidecar on the network with `--network-alias` set to its server name (`hsa.test`, `hsb.test`) that terminates TLS on 8448 with a self-signed certificate for that name and proxies to its own Tuwunel container's 8008.
Without `ip_range_denylist = []` the servers refuse each other's private Docker addresses.
Check federation before starting MindRoom: register a user on each server, create a room on one, invite the other server's user, and join it.
Give each deployment its own `.env` with `MATRIX_HOMESERVER` set to its localhost port, `MATRIX_SERVER_NAME` set to its server name, `MATRIX_REGISTRATION_TOKEN`, a unique `MINDROOM_NAMESPACE`, its own config and storage, and its own `--api-port`.
`personal_rooms.auto_join_requester: true` needs homeserver admin rights these servers do not grant, so leave it off and join the personal-room invitation as the user.
An agent's own access token is in its deployment's `mindroom_data/matrix_state.yaml`, which lets the harness read a room from that agent's side.

## Upgrade Test: Old Version Then This Branch On One `mindroom_data`

Use this to prove a storage or persistence change against real history written by the previous release.

1. Check out the old version in its own worktree with its own environment, so both versions can run against the same storage root in turn.
```bash
upgrade_root="$HOME/.codex/worktrees/mindroom-live-upgrade"
mkdir -p "$upgrade_root"
git worktree add --detach "$upgrade_root/old" origin/main
(cd "$upgrade_root/old" && uv sync --all-extras)
```
2. Start the old backend detached with an absolute interpreter path; a relative `.venv/bin/mindroom` from another directory fails silently and a plain `nohup ... &` dies with the shell that started it.
```bash
setsid nohup "$upgrade_root/old/.venv/bin/mindroom" run --storage-path "$MINDROOM_STORAGE_PATH" --api-port 9877 --log-level INFO >> old.log 2>&1 < /dev/null & disown
```
3. Build history, stop it (`kill "$(lsof -ti :9877)"`), copy the session database aside for comparison, then start this branch's `.venv/bin/mindroom` on the same `MINDROOM_STORAGE_PATH`, namespace, and config.
4. Verify with the database, not the chat: check that `<root>/agents/<agent>/sessions/<agent>.db.pre-storage-v2` exists and matches the copy from step 3 (`cmp`), then `sqlite3 <root>/agents/<agent>/sessions/<agent>.db` and inspect `<agent>_sessions` (`session_data`, `metadata`), the `<agent>_sessions_log_*` tables (`_log_runs`, `_log_messages`, `_log_tools`, `_log_summaries`, `_log_removed`, `_log_meta`), and `PRAGMA journal_mode`.

Compaction knobs that make it fire within a few turns:

- Text compaction runs before a reply when its whole first request (system prompt, tools, history, and prompt) exceeds `replay_window_tokens - reserve_tokens`, or exceeds `threshold_tokens` on a route without native compaction; with native compaction the provider compacts past the threshold instead. The request without history is a few thousand tokens (`static_tokens` in the log line below), so set `replay_window_tokens` to roughly `static_tokens + 1000`, `reserve_tokens: 100`, and `threshold_tokens` to the same value as the replay window.
- The summary budget comes from the model `context_window`: `context_window - reserve - 2000 - 10%` must exceed 2,000, so keep `context_window` at 64000 or more while capping `replay_window_tokens`.
- Read the decision from the `History preparation check` log line: `compaction_decision`, `compaction_reason`, `request_tokens` (the first request's size), `reported` (whether a provider-reported size anchored it), `limit_tokens`, `static_tokens`, `unavailable_reason`.
- To check sizing itself, make the stub report each request's size as `usage.prompt_tokens` (for example an o200k count of every string in the sent body) and compare it with `request_tokens`; a later request anchors on that reported size.
- Hot reload on older versions did not apply model `context_window` or agent `compaction` changes; restart the backend after editing them.

Reading the thread and the model stub:

- Compaction lifecycle notices start with `📦` and count as agent messages; exclude them when waiting for a reply.
- Redactions are applied when the next reply in that thread is prepared, not when the redaction arrives; send another turn to observe the cleanup.
- Make the stub log each request (roles, replayed turn markers) to a JSONL file; that is the only way to see what history the model actually received.
