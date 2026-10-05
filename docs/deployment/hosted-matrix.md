---
icon: lucide/cloud-cog
---

# Hosted Matrix + Local Backend

In this setup the Matrix homeserver and chat UI are hosted at `mindroom.chat`, and only the MindRoom backend runs on your machine.
This page covers pairing that backend with your MindRoom Chat account (`mindroom connect`), the credentials pairing saves, what the hosted server can see, and how to point the same flow at your own deployment.
For first-run install steps, see [Getting Started](../getting-started.md#recommended-your-computer-mindroom-chat).

## What Runs Where

| Component | Runs on | Purpose |
|----------|---------|---------|
| `chat.mindroom.chat` | Hosted web app | Login and pairing approval |
| `mindroom.chat` | Hosted Matrix + provisioning API | Matrix transport and agent account registration |
| `uvx mindroom run` | Your machine or server | Agent orchestration, tools, model calls |

[Getting Started](../getting-started.md#recommended-your-computer-mindroom-chat) lists what you need before starting.
`mindroom config init` (default `--matrix-server mindroom.chat`) and first-run `mindroom run` write the hosted defaults to `~/.mindroom/.env`: `MATRIX_HOMESERVER=https://mindroom.chat`, `MATRIX_SERVER_NAME=mindroom.chat`, and `MINDROOM_PROVISIONING_URL=https://mindroom.chat`.
`mindroom run` pairs automatically before starting when `MINDROOM_PROVISIONING_URL` is set and none of `MATRIX_REGISTRATION_TOKEN`, `MATRIX_REGISTRATION_SHARED_SECRET`, or saved pairing credentials are present.
After pairing, MindRoom creates its agent accounts on `mindroom.chat`, joins or creates the configured rooms, and starts answering.
While `run` waits for approval, the API port answers health and readiness probes, so container health checks do not restart it with a new code (see [Health & Readiness](operational-log-events.md#health-readiness)).
To run code-execution tools in Docker workers on the same machine, see [Dedicated Docker workers](sandbox-proxy.md#host-machine-dedicated-docker-workers-mindroom_worker_backenddocker).

## connect

`mindroom connect` pairs this machine with your MindRoom Chat account without starting MindRoom.
You usually do not need it, because `mindroom run` pairs automatically and prints a new link whenever the previous one expires.

```bash
mindroom connect
```

| Option | Effect | Default |
| --- | --- | --- |
| `--provisioning-url URL` | Provisioning service to pair with | `MINDROOM_PROVISIONING_URL`, else `https://mindroom.chat` |
| `--client-name NAME` | Name for this machine in the list of paired installs | Host name |
| `--persist-env` / `--no-persist-env` | Save the credentials to `.env` next to `config.yaml`, or only print them as `export` lines | `--persist-env` |
| `--open-browser` | Open the approval link in the default browser | Off |
| `--path`, `-p` | Config file whose `.env` receives the credentials | Auto-detected |
| `--force` | Pair again without asking when this machine is already connected | Off |
| `--graceful-cancel` | SIGTERM while waiting exits with code `130` without saving (used by the macOS app) | Off |

### Approve the pairing

`connect` and `run` print an approval link, a pair code, and, in a terminal, a QR code of the link.
Open the link or scan the QR code while signed in to MindRoom Chat, or enter the code in MindRoom Chat → Settings → Local MindRoom.
Approve only when the page shows the same code as your terminal (`Approve only if the page shows code ABCD-EFGH`), because anyone who sees the link or code can approve it, and the approving account becomes the one your agents trust.
The page also shows the address the request came from.

A pair code is valid for 10 minutes.
`connect` makes one attempt and then exits with `Approval timed out. Run the command again to get a new link.`, while `run` keeps printing new codes.

After approval, and before anything is saved, MindRoom prints the approving account, for example `Approved by @alice:mindroom.chat.`
In a terminal it then asks `Is this your account? [Y/n]`.
Pressing Enter or answering `y` saves the credentials.
Answering `n`, pressing Ctrl+C, or closing input discards them without writing `.env` or `config.yaml`, and the command exits with an error (`run` does not start); revoke that connection in MindRoom Chat → Settings → Local MindRoom.
Without a terminal, such as under a service or the macOS app, or when the provisioning service does not name the approving account, nothing is asked and MindRoom prints the same revoke hint.

### What pairing saves

With `--persist-env`, pairing writes these to `.env` next to `config.yaml`:

- `MINDROOM_PROVISIONING_URL`
- `MINDROOM_LOCAL_CLIENT_ID`
- `MINDROOM_LOCAL_CLIENT_SECRET`
- `MINDROOM_NAMESPACE`
- `MINDROOM_OWNER_USER_ID`, when the approving account is a valid Matrix user ID

It also replaces the owner placeholder `__MINDROOM_OWNER_USER_ID_FROM_PAIRING__` with the approving account in `config.yaml` and every file it pulls in with `!include`.
An approving account that is not a valid Matrix user ID is printed as `Approved by '<value>', which is not a valid Matrix user ID.` and is never saved, so the placeholders stay for you to replace.
`mindroom config init` likewise warns and leaves the placeholders when `MINDROOM_OWNER_USER_ID` in the environment or `.env` is not a valid Matrix user ID.
Placeholders are replaced only once, so when a later pairing is approved by an account missing from `administrators`, MindRoom prints a note and leaves the config unchanged.
Add that account to `administrators`, `room_defaults.invite_users`, and `room_defaults.admins` yourself if it should manage MindRoom.
With `--no-persist-env`, MindRoom prints the variables as `export` lines, including the owner ID, and you replace the placeholders yourself.

### Pair again

Pairing a machine that is already connected creates a new connection and a new agent namespace: existing agents keep working, and new agents get the new namespace.
In a terminal, `connect` asks `Pair again?` first.
Without a terminal it does not pair and exits with code `3`, so scripts and the macOS app can tell this apart from a failure (exit code `1`).
Add `--force` to pair again without asking.

### Self-hosted homeservers

Pairing is only for hosted `mindroom.chat` or your own provisioning service.
When neither `MINDROOM_PROVISIONING_URL` nor `--provisioning-url` is set and `MATRIX_HOMESERVER` is not `mindroom.chat`, `connect` refuses without contacting anything or writing files.
This includes an unset `MATRIX_HOMESERVER`, which defaults to `http://localhost:8008`, so run `mindroom config init --matrix-server mindroom.chat` first for hosted defaults.
Self-hosted servers without a provisioning service register agents with `MATRIX_REGISTRATION_TOKEN` or `MATRIX_REGISTRATION_SHARED_SECRET` instead (see [Configuration](../configuration/index.md)).

## Pairing Credentials

`MINDROOM_LOCAL_CLIENT_ID` and `MINDROOM_LOCAL_CLIENT_SECRET` are **not Matrix access tokens**.
They only authorize provisioning-service requests from this install: registering agent accounts and retrieving the [Google desktop OAuth client](google-services-user-oauth.md).
The provisioning service never learns your agents' Matrix passwords, because MindRoom replaces each new account's one-time password with a locally generated one.
Treat the pairing credentials as secrets anyway, because anyone who has them can make the same requests.
`MINDROOM_NAMESPACE` is appended to managed agent usernames and room aliases so installs do not collide on the shared homeserver.

Revoke a pairing in MindRoom Chat → Settings → Local MindRoom, which also shows when each install was last seen.
Each account keeps at most 20 paired installs, revoked ones included; pairing another at the limit removes a revoked install first, otherwise the one seen least recently.
A running install reports itself to the provisioning service at startup and every six hours, and the report carries no messages, configuration, or other content.
If the service rejects the credentials as invalid or revoked, MindRoom logs a warning to run `mindroom connect` again.

## What the Hosted Server Can See

In end-to-end encrypted rooms, the homeserver stores message bodies as ciphertext, and your local `mindroom run` holds the agent keys and decrypts locally.
Agents share encryption keys with room members' unverified devices (see [Delivery Policy](../matrix.md#delivery-policy)), so a malicious homeserver that adds an attacker-controlled device to a participating account can receive keys for messages sent after that.
This does not give it access to earlier stored history.

The homeserver can still see:

- Metadata: room membership, timestamps, event IDs, sender IDs, and traffic patterns.
- Plaintext of any room that is not encrypted.

Model and tool providers see whatever prompts and data you send them.
See the [Matrix E2EE guide](https://matrix.org/docs/matrix-concepts/end-to-end-encryption/) for device discovery and verification.

## Use Your Own Deployment

Keep the same local flow and change the endpoints in `.env`:

- `MATRIX_HOMESERVER=https://your-matrix.example.com`
- `MATRIX_SERVER_NAME=your-matrix.example.com`
- `MINDROOM_PROVISIONING_URL=https://your-matrix.example.com` (or your dedicated provisioning host)

Then run `mindroom run`, which pairs with your own deployment on first start.
An install already paired with another provisioning service keeps its old pairing, so run `mindroom connect` first to pair it again.
To register agent accounts directly with the homeserver without pairing, set `MATRIX_REGISTRATION_TOKEN`, or set `MATRIX_REGISTRATION_SHARED_SECRET` and unset `MINDROOM_PROVISIONING_URL`.

On the provisioning service itself, set `MINDROOM_PROVISIONING_APPROVE_URL=https://chat.your-matrix.example.com/connect` so pairing links open your own chat UI.
It defaults to `https://chat.mindroom.chat/connect` and is not read by the local `mindroom` process.
