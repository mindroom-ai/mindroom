---
icon: lucide/radio-tower
---

# External Triggers

External triggers let a watcher wake MindRoom without keeping an agent turn alive.

A watcher process runs outside the agent loop, detects a meaningful change, and sends one signed HTTP event to MindRoom.

MindRoom verifies the signature, checks replay and size limits, then posts a Matrix message to the configured room or thread with the configured agent or team mention.

MindRoom does not run watcher code and does not poll external systems from the agent turn loop.

## Use Cases

- A campground cancellation watcher checks an external booking site and sends an event only when a matching site opens.
- A Git repo change watcher tracks a branch, tag, or webhook payload and sends an event only when the observed commit or digest changes.

## Configuration

Add `external_triggers` to `config.yaml`.

Each trigger is keyed by the trigger ID used in `mindroom trigger send`.

```yaml
agents:
  ops:
    display_name: Ops
    role: Watch external systems and report actionable changes.
    model: default
    rooms: [lobby]

models:
  default:
    provider: openai
    id: gpt-5.5

external_triggers:
  campground:
    description: Campground availability watcher
    auth: ed25519
    key_id: default
    public_key: "BASE64_PUBLIC_KEY_FROM_KEYGEN"
    target:
      room_id: "!room:example.org"
      thread_id: "$thread-event-id"
      agent: ops
      new_thread: false
    allowed_kinds:
      - campground.availability
    replay_window_seconds: 300
    max_body_bytes: 65536
```

`auth` defaults to `ed25519`.

`key_id` defaults to `default` and must match `--key-id` when a non-default key ID is used.

`public_key` is the base64 Ed25519 public key printed by `mindroom trigger keygen`.

Only the public key belongs in `config.yaml`.

`target.room_id` is the Matrix room that receives the trigger message.

MindRoom treats `target.room_id` as a configured room for the router and `target.agent`, so both bots try to join and listen there.

`target.thread_id` is optional.

`target.agent` must name a configured agent or team.

`target.new_thread: true` sends a fresh room message instead of appending to `target.thread_id`.

`allowed_kinds` is optional.

When `allowed_kinds` is empty or omitted, any signed `kind` is accepted.

`replay_window_seconds` defaults to `300` and limits accepted signature age.

Future signature timestamps are rejected.

`max_body_bytes` defaults to `65536` and rejects larger signed bodies.

## CLI

Generate a private key file and copy the printed public key into `external_triggers.<id>.public_key`.

```bash
mindroom trigger keygen --private-key-file /etc/mindroom/triggers/campground.key
```

Send a signed event when the watcher detects a real change.

```bash
mindroom trigger send campground \
  --url http://127.0.0.1:8765 \
  --key-file /etc/mindroom/triggers/campground.key \
  --kind campground.availability \
  --event-id reserveamerica:yosemite:site-42:2026-07-04 \
  --title "Campground site opened" \
  --message "Site 42 is available for July 4." \
  --data-json '{"campground":"Yosemite","site":"42","date":"2026-07-04"}'
```

Use `--key-id` when the trigger config uses a key ID other than `default`.

Use `--no-verify-tls` only for local development against a trusted endpoint.

The CLI posts to `POST /api/triggers/<trigger_id>`.

The request body contains `kind`, `message`, optional `event_id`, optional `title`, and optional `data`.

## Idempotency

Use a stable `--event-id` for the same external event.

For example, use the external reservation ID, Git commit SHA, release tag, webhook delivery ID, or a deterministic hash of the changed state.

If the first delivery succeeds, a later signed request with the same `event_id` is treated as a duplicate and does not post another Matrix message while the replay record is retained.

Retries must create a fresh signed request with the same `--event-id`.

Each nonce-bearing HTTP request is single-use, even if delivery fails before MindRoom records the event as delivered.

Do not reuse the same HTTP request body and headers as a retry strategy.

An in-progress event claim expires after one day to recover from process crashes without redelivering slow in-flight requests.

After delivery succeeds, the `event_id` stays recorded for one day so duplicate retries do not post another Matrix message.

If `--event-id` is omitted, the CLI generates a random event ID, so repeated sends are not idempotent.

## Watcher Behavior

Watcher code should call `mindroom trigger send` only when something meaningful changes.

For a polling watcher, store the last observed state and compare before sending.

For a webhook watcher, deduplicate webhook delivery IDs before calling MindRoom.

MindRoom receives trigger events.

MindRoom does not host watcher loops, schedule watcher polls, or keep an agent turn alive while waiting for external state.

## Security Modes

### Kubernetes Hardened Mode

Keep the trigger private key outside the agent sandbox.

Do not mount the private key into the agent sandbox.

Store only the public key in MindRoom config.

Kubernetes worker pods can scale to zero when idle.

Worker `extraContainers` are bound to the generated worker pod lifecycle.

Use `workers.kubernetes.extraContainers` and `workers.kubernetes.extraVolumes` only for worker-scoped helper behavior that should exist while a worker pod exists.

Always-on polling watchers should run as a top-level runtime chart `extraContainers` entry, a CronJob, or an external deployment.

With Kubernetes workers, use `workers.kubernetes.extraContainers` and `workers.kubernetes.extraVolumes` to add worker-scoped helper containers and secret volumes to generated worker pods.

The extra volume is available to containers that explicitly mount it.

In this example, the secret volume is mounted only by the worker-scoped `campground-watcher`, not by `sandbox-runner`.

The generated worker pod runs containers with UID/GID `1000` and `fsGroup: 1000`, so this secret volume uses group-readable mode for sidecar access.

This example is not an always-on polling watcher deployment pattern.

Put this in a `cluster/k8s/runtime` Helm values file passed with `helm -f`, not in `config.yaml`.

MindRoom `config.yaml` rejects top-level `workers`.

```yaml
workers:
  backend: kubernetes
  kubernetes:
    extraVolumes:
      - name: campground-trigger-key
        secret:
          secretName: campground-trigger-key
          defaultMode: 0440
    extraContainers:
      - name: campground-watcher
        image: ghcr.io/example/campground-watcher:2026-06-22
        imagePullPolicy: IfNotPresent
        env:
          - name: MINDROOM_URL
            value: http://mindroom-runtime:8765
          - name: TRIGGER_ID
            value: campground
          - name: TRIGGER_KEY_FILE
            value: /trigger-secrets/private-key
        volumeMounts:
          - name: campground-trigger-key
            mountPath: /trigger-secrets
            readOnly: true
        command:
          - /bin/sh
          - -c
        args:
          - |
            exec campground-watcher \
              --mindroom-url "${MINDROOM_URL}" \
              --trigger-id "${TRIGGER_ID}" \
              --key-file "${TRIGGER_KEY_FILE}"
```

Use your deployed MindRoom service URL for `MINDROOM_URL`.

The watcher image must contain the watcher code and the `mindroom` CLI if the watcher shells out to `mindroom trigger send`.

The watcher should call `mindroom trigger send campground ...` with a stable `--event-id` only after it detects changed campground availability.

### Personal VM Or Unsandboxed Mode

A cron job can run as the same user as MindRoom and call `http://127.0.0.1:8765`.

This is convenient for a personal VM.

This is not a secret boundary if the agent has unsandboxed shell access as that same user.

In that mode, the agent can usually read the same user's files or invoke the same local tools.

Use a separate OS user, sandbox, Kubernetes sidecar, or CronJob isolation when the private key must be hidden from agent code.
