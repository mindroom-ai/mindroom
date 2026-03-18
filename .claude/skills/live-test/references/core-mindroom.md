# Core MindRoom Live Run

Use this reference for the local Matrix stack, the Python backend, Matty smoke tests, disposable Matrix users, and live API checks.

## Preflight

Run from the repo root.

```bash
uv sync --all-extras
just local-matrix-up
curl -s http://localhost:8008/_matrix/client/versions | head -c 200
curl -s http://localhost:9292/v1/models | head -c 200
```

If you switched homeservers or see `M_FORBIDDEN`, clear local Matrix state before restarting MindRoom.

```bash
rm -f mindroom_data/matrix_state.yaml
```

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

## Isolated Path: Temporary Config

Use this when the machine already has local MindRoom instances, existing Matrix users, occupied dashboard ports, or stale config.

```bash
tmp="$(mktemp -d /tmp/mindroom-live-test.XXXXXX)"
uv run mindroom config init --minimal --provider openai --force --path "$tmp/config.yaml"
```

Patch the generated config so it can run locally without private credentials and without restrictive room auth.

Minimum changes:

```yaml
models:
  default:
    provider: openai
    id: gpt-oss-low:20b
    extra_kwargs:
      base_url: http://localhost:9292/v1

agents:
  assistant:
    learning: false

memory:
  backend: file

mindroom_user:
  username: mindroom_user_<unique_suffix>

matrix_room_access:
  mode: multi_user
  multi_user_join_rule: public

authorization:
  default_room_access: true
  global_users: []
  agent_reply_permissions: {}
```

Then export an isolated runtime.

```bash
export MINDROOM_CONFIG_PATH="$tmp/config.yaml"
export MINDROOM_STORAGE_PATH="$tmp/mindroom_data"
export MINDROOM_NAMESPACE="live$(date +%H%M%S)"
export MATRIX_HOMESERVER=http://localhost:8008
export MATRIX_SSL_VERIFY=false
export OPENAI_API_KEY=sk-test
export UV_PYTHON=3.13
```

In practice, it is often cleaner to write a temporary `"$tmp/.env"` and `source` it so the live run and later `curl` commands use the same values.

Example:

```bash
cat > "$tmp/.env" <<EOF
MATRIX_HOMESERVER=http://localhost:8008
MATRIX_SSL_VERIFY=false
MINDROOM_STORAGE_PATH=$tmp/mindroom_data
MINDROOM_API_KEY=live-test-$(date +%H%M%S)
OPENAI_API_KEY=sk-test
OPENAI_BASE_URL=http://localhost:9292/v1
EOF

set -a
source "$tmp/.env"
set +a
```

If `"$tmp/.env"` exists, inspect it for `MINDROOM_API_KEY`.
Use that key for `/api/*` requests so you are talking to the same isolated instance you started.

Start the isolated backend on a non-default API port.

```bash
uv run mindroom run --storage-path "$MINDROOM_STORAGE_PATH" --api-port 9876 --log-level INFO
```

Health check:

```bash
curl -s http://localhost:9876/api/health
```

## Create a Disposable Matrix Account

When open registration is enabled on local Synapse, create a throwaway user directly through the Matrix client API.

```bash
username="smoketest$(date +%H%M%S)"
password="smoketestpass"
curl -sS -X POST 'http://localhost:8008/_matrix/client/v3/register' \
  -H 'Content-Type: application/json' \
  -d "{\"auth\":{\"type\":\"m.login.dummy\"},\"username\":\"$username\",\"password\":\"$password\"}"
```

The response includes `user_id` and `access_token`.

## Join a Public Room

If the agent room is public, join it with the Matrix API before using Matty.
Prefer the concrete room ID from backend logs because aliases are not always predictable in isolated runs.

Join by room ID:

```bash
room_id='!example:localhost'
encoded_room_id="$(python -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$room_id")"
curl -sS -X POST "http://localhost:8008/_matrix/client/v3/join/$encoded_room_id" \
  -H "Authorization: Bearer $access_token"
```

Join by alias only when you know the exact alias:

```bash
room_alias='#lobby_<namespace>:localhost'
encoded_alias="$(python -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$room_alias")"
curl -sS -X POST "http://localhost:8008/_matrix/client/v3/join/$encoded_alias" \
  -H "Authorization: Bearer $access_token"
```

Use the actual alias created by the active config.

## Read and Send Messages with Matty

Matty accepts per-command credentials with `-u` and `-p`.

List rooms:

```bash
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false \
uv run --python 3.13 matty rooms -u "$username" -p "$password" --format json
```

Inspect room membership:

```bash
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false \
uv run --python 3.13 matty users "Lobby" -u "$username" -p "$password" --format json
```

Send a smoke message:

```bash
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false \
uv run --python 3.13 matty send "Lobby" \
  "Hello @mindroom_assistant:localhost please reply with pong." \
  -u "$username" -p "$password"
```

Read recent room messages:

```bash
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false \
uv run --python 3.13 matty messages "Lobby" -u "$username" -p "$password" --format json
```

List threads:

```bash
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false \
uv run --python 3.13 matty threads "Lobby" -u "$username" -p "$password" --format json
```

Read one thread:

```bash
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false \
uv run --python 3.13 matty thread "Lobby" t1 -u "$username" -p "$password" --format json
```

Agents usually reply in threads and may stream by editing the same event.
If you see partial output, wait and read the thread again.
If `matty threads` looks empty or flaky, use `matty messages --format json` to discover the thread handle and then read it directly with `matty thread`.

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
