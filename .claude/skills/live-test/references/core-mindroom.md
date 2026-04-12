# Core MindRoom Live Test

Use this reference to live-test MindRoom backend changes against the local Matrix homeserver.

## ⛔ CRITICAL SAFETY

- **`mindroom-chat.service` (port 8766)** is PRODUCTION. NEVER restart, stop, or interact with it.
- **`mindroom-lab.service` (port 8765)** is the dev/test instance. Safe to restart.
- **Tuwunel (port 8008)** is the Matrix homeserver — always running, shared by both. NEVER start another. NEVER run `just local-matrix-up`.

## NixOS Requirement

**Must use `nix-shell`** before any `uv run` commands (provides `libstdc++.so.6` for numpy/qdrant/chromadb).

```bash
nix-shell -I nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos shell.nix
```

Without it: `AttributeError: module 'mindroom' has no attribute 'bot'`.

---

## Path 1: Lab Service (PREFERRED)

The lab service (`mindroom-lab.service`) runs from `/srv/mindroom` (main branch) with config at `~/.mindroom-lab/`. It connects to `localhost:8008` with namespace `adb4d443`.

### If your changes are already on `main`:

```bash
# 1. Restart the lab service to pick up your changes
sudo systemctl restart mindroom-lab.service

# 2. Wait for it to come up (~20 seconds)
sleep 20
curl -s http://localhost:8765/api/health

# 3. Create a disposable Matrix user (see "Create Test User" section below)
# 4. Join a room with bots (see "Join a Room" section below)
# 5. Send messages, verify bot responds, capture evidence
```

### If your changes are on a worktree branch:

```bash
# 1. Stop the lab service
sudo systemctl stop mindroom-lab.service

# 2. Run MindRoom from your worktree with the lab config
cd /srv/mindroom-worktrees/<your-branch>
nix-shell -I nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos shell.nix --run '
  set -a; source ~/.mindroom-lab/.env; set +a;
  MINDROOM_CONFIG_PATH=~/.mindroom-lab/config.yaml \
  MINDROOM_STORAGE_PATH=~/.mindroom-lab/mindroom_data \
  uv run mindroom run --api-port 8765
'

# 3. Test in another terminal (create user, join room, send messages, verify)
# 4. Ctrl-C when done
# 5. Restart the lab service
sudo systemctl start mindroom-lab.service
```

---

## Path 2: Isolated Instance (clean slate)

For tests needing a completely fresh MindRoom instance with no existing state.

```bash
# 1. Create temp directory
tmp="$(mktemp -d /tmp/mindroom-live-test.XXXXXX)"

# 2. Initialize minimal config
cd /srv/mindroom-worktrees/<your-branch>
nix-shell -I nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos shell.nix --run \
  "uv run mindroom config init --minimal --provider openai --force --path '$tmp/config.yaml'"

# 3. Patch $tmp/config.yaml:
#    - learning: false
#    - memory.backend: file
#    - matrix_room_access.mode: multi_user
#    - multi_user_join_rule: public
#    - authorization.default_room_access: true

# 4. Create .env (MINDROOM_NAMESPACE must match ^[a-z0-9]{4,32}$, no underscores/hyphens)
cat > "$tmp/.env" << EOF
MATRIX_HOMESERVER=http://localhost:8008
SSL_VERIFY=false
MINDROOM_NAMESPACE=smoketest01
MINDROOM_CONFIG_PATH=$tmp/config.yaml
MINDROOM_STORAGE_PATH=$tmp/mindroom_data
EOF

# 5. Run on a non-conflicting port (NOT 8765 or 8766)
nix-shell -I nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos shell.nix --run \
  "set -a; source '$tmp/.env'; set +a; uv run mindroom run --storage-path '$tmp/mindroom_data' --api-port 9876 --log-level INFO"
```

---

## Create Test User

Registration on this homeserver requires a **two-step UIAA flow** with a registration token.

```bash
username="smoketest$(date +%H%M%S)"; password="smoketestpass"

# Step 1: Get session ID
session=$(curl -sS -X POST 'http://localhost:8008/_matrix/client/v3/register' \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$username\",\"password\":\"$password\"}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["session"])')

# Step 2: Complete registration with token
# Use sed (not cut -d= -f2) because the token value itself contains '=' characters
REG_TOKEN=$(grep MATRIX_REGISTRATION_TOKEN ~/.mindroom-lab/.env | sed 's/MATRIX_REGISTRATION_TOKEN=//')
result=$(curl -sS -X POST 'http://localhost:8008/_matrix/client/v3/register' \
  -H 'Content-Type: application/json' \
  -d "{\"auth\":{\"type\":\"m.login.registration_token\",\"token\":\"$REG_TOKEN\",\"session\":\"$session\"},\"username\":\"$username\",\"password\":\"$password\"}")
echo "$result"

# Extract access token for subsequent API calls
access_token=$(echo "$result" | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')
```

---

## Join a Room

Lab rooms are often **private**. Don't guess which bot is in which room — scan all bots at once.

### Step 1: Scan ALL bots to find one that's actually in a room

Bot credentials (username + password) are in `~/.mindroom-lab/mindroom_data/matrix_state.yaml`. Many bots may have **zero** joined rooms (they got kicked and can't rejoin).

**⚠️ Do NOT try bots one-by-one.** Run this single command to scan all of them at once and print only the ones with rooms:

```bash
python3 << 'PYEOF'
import yaml, json, urllib.request, os
state = yaml.safe_load(open(os.path.expanduser('~/.mindroom-lab/mindroom_data/matrix_state.yaml')))
for key, acct in state.get('accounts', {}).items():
    user, pw = acct['username'], acct['password']
    try:
        login = urllib.request.urlopen(urllib.request.Request(
            'http://localhost:8008/_matrix/client/v3/login',
            data=json.dumps({'type':'m.login.password','identifier':{'type':'m.id.user','user':user},'password':pw}).encode(),
            headers={'Content-Type':'application/json'}, method='POST'))
        token = json.loads(login.read())['access_token']
        rooms_resp = urllib.request.urlopen(urllib.request.Request(
            'http://localhost:8008/_matrix/client/v3/joined_rooms',
            headers={'Authorization': f'Bearer {token}'}))
        rooms = json.loads(rooms_resp.read()).get('joined_rooms', [])
        if rooms:
            print(f'{key}: user={user} pw={pw}')
            for r in rooms: print(f'  - {r}')
    except Exception:
        continue
PYEOF
```

This prints ONLY bots that are in rooms, with their credentials and room IDs. Pick one. Routers and the `code` bot are typically the most active — try those first if the output is long.

### Step 2: Login as that bot and invite your test user

```bash
BOT_TOKEN=$(curl -sS -X POST 'http://localhost:8008/_matrix/client/v3/login' \
  -H 'Content-Type: application/json' \
  -d '{"type":"m.login.password","identifier":{"type":"m.id.user","user":"BOT_USERNAME"},"password":"BOT_PASSWORD"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')

# URL-encode room ID: ! → %21, : → %3A
# Example: !Gz7X0zMFF5Wo8EzRW2:mindroom.lab.mindroom.chat → %21Gz7X0zMFF5Wo8EzRW2%3Amindroom.lab.mindroom.chat
curl -sS -X POST "http://localhost:8008/_matrix/client/v3/rooms/ENCODED_ROOM_ID/invite" \
  -H "Authorization: Bearer $BOT_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"user_id\":\"@$username:mindroom.lab.mindroom.chat\"}"
```

**Note:** A successful invite returns an empty JSON object `{}` (no body content). That's normal — it does NOT mean the call failed. Errors return an `errcode` field instead.

### Step 3: Accept invite as test user

```bash
curl -sS -X POST "http://localhost:8008/_matrix/client/v3/join/ENCODED_ROOM_ID" \
  -H "Authorization: Bearer $access_token" -H 'Content-Type: application/json' -d '{}'
```

---

## Send Messages and Verify Bot Response

### Sending a message that triggers a bot

The bot must be **mentioned** via `m.mentions` to trigger a response. Use the bot's full Matrix user ID (from the scan above).

```bash
# URL-encode the room ID
ROOM_ENCODED="ENCODED_ROOM_ID"
BOT_MXID="@bot_username:mindroom.lab.mindroom.chat"
TXNID=$(date +%s%N)

curl -sS -X PUT "http://localhost:8008/_matrix/client/v3/rooms/$ROOM_ENCODED/send/m.room.message/$TXNID" \
  -H "Authorization: Bearer $access_token" -H 'Content-Type: application/json' \
  -d "{
    \"msgtype\": \"m.text\",
    \"body\": \"@BotName What is 2+2? One word answer.\",
    \"format\": \"org.matrix.custom.html\",
    \"formatted_body\": \"<a href=\\\"https://matrix.to/#/$BOT_MXID\\\">@BotName</a> What is 2+2? One word answer.\",
    \"m.mentions\": {\"user_ids\": [\"$BOT_MXID\"]}
  }"
```

### Checking for a response

Wait 10-20 seconds, then read recent messages:

```bash
sleep 15
curl -sS "http://localhost:8008/_matrix/client/v3/rooms/$ROOM_ENCODED/messages?dir=b&limit=5" \
  -H "Authorization: Bearer $access_token" \
  | python3 -c '
import sys, json
data = json.load(sys.stdin)
for e in reversed(data.get("chunk", [])):
    if e["type"] == "m.room.message":
        print(f"{e[\"sender\"]}: {e[\"content\"].get(\"body\", \"\")[:200]}")
'
```

Bots reply in threads and may stream via edits. If output looks partial (e.g. "Thinking..."), wait 10 more seconds and re-read. The bot edits its "Thinking..." message into the final answer.

---

## API Checks

```bash
curl -s http://localhost:8765/api/health
```

For authenticated endpoints, use `MINDROOM_API_KEY` from `~/.mindroom-lab/.env`:
```bash
source ~/.mindroom-lab/.env
curl -s http://localhost:8765/api/config/agent-policies \
  -H "Authorization: Bearer $MINDROOM_API_KEY"
```

Always confirm the port matches the instance you launched.

---

## Troubleshooting

**`M_FORBIDDEN` on room join:** Room is private. Use the bot invite flow above.

**`M_FORBIDDEN` on registration:** Registration token wrong or expired. Check `~/.mindroom-lab/.env`. Use `sed` not `cut` to extract the token (value contains `=` characters).

**Bots not responding:** Check `journalctl -u mindroom-lab.service -o cat | tail -50`. Common issues:
- Bot not joined to room (use the scan script above)
- Bot startup failed (check for `M_FORBIDDEN` join errors)
- Model API unreachable

**Stale state errors:** `rm -f ~/.mindroom-lab/mindroom_data/matrix_state.yaml` then restart lab. ⚠️ This forces re-registration of all bot accounts. Only do this if bots are completely broken AND you're okay with them creating new rooms.

---

## Evidence Requirements

Live tests are a **HARD GATE** — code must not be merged without evidence. Capture:
1. **Commands run** (exact shell commands)
2. **Output observed** (health check, messages sent, bot responses)
3. **Behavior verified** (did the feature/fix work as intended?)
4. Screenshot paths if visual verification needed