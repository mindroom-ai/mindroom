# MindRoom Runtime Chart

This chart deploys only the MindRoom runtime and its own runtime support resources.
It is for clusters that already provide surrounding platform pieces such as Matrix, ingress, deployment-specific secrets, model gateways, and optional external backing services.

Use the instance chart in `cluster/k8s/instance` when you want a complete MindRoom instance with its own Matrix homeserver.
Use this chart when MindRoom should run inside an existing platform.

## Contents

- [Minimal Install](#minimal-install)
- [Rollout Progress Deadline](#rollout-progress-deadline)
- [Restart Safety](#restart-safety)
- [Event Journal](#event-journal)
- [Config Sources](#config-sources)
- [Runtime State Storage](#runtime-state-storage)
- [Session and Knowledge Storage](#session-and-knowledge-storage)
- [Content Bundles](#content-bundles)
- [Provider API Keys from Kubernetes Secrets](#provider-api-keys-from-kubernetes-secrets)
- [Layering Values Files](#layering-values-files)
- [Control-Plane NetworkPolicy](#control-plane-networkpolicy)
- [Worker Egress Proxy](#worker-egress-proxy)
- [Background Script Gateway](#background-script-gateway)
- [Matrix Managed Account Authentication](#matrix-managed-account-authentication)
- [Existing Platform Example](#existing-platform-example)
- [Notes](#notes)
- [Adopting Existing Resources](#adopting-existing-resources)

## Minimal Install

```bash
helm upgrade --install mindroom-runtime ./cluster/k8s/runtime \
  --namespace mindroom \
  --create-namespace
```

The default values render a self-contained Deployment, Service, ConfigMap, runtime PVC, and PostgreSQL event-journal StatefulSet.
A real deployment should provide a useful config and Matrix settings.
The dashboard and API are fully privileged, so the chart gives the primary a generated `MINDROOM_API_KEY`, and the install notes show how to read it.

## Rollout Progress Deadline

Set `progressDeadlineSeconds` to a positive integer to customize the MindRoom Deployment's rollout progress budget, including scheduling, image initialization, and startup probes.
The chart rejects invalid values and values above Kubernetes' int32 limit of `2147483647` seconds.
Leaving it unset or `null` preserves the Kubernetes default.
This controls when Kubernetes reports a stalled rollout; it does not change probe settings or Helm's wait timeout.

```yaml
progressDeadlineSeconds: 1800
```

## Restart Safety

The Deployment uses the `Recreate` strategy, so an upgrade, restart, or pod-template change stops the running MindRoom process before its replacement starts.
Immediately before such a change, check live work inside the running container:

```bash
kubectl --namespace mindroom exec deploy/mindroom-runtime -c mindroom -- \
  mindroom check-active-responses --details --wait 1800 --url http://127.0.0.1:8765
```

It exits `0` once no live work is active; treat every nonzero result, including `kubectl` and authentication failures, as a blocked restart.
The command uses the container's `MINDROOM_API_KEY`, so the key stays in the pod, and the loopback URL bypasses any ingress.
Background script runs that restart startup would adopt, such as runs on dedicated `kubernetes` workers with an isolated script gateway, are listed as recoverable and do not block.
Wait for them as well when the change alters their recovery contract.
The result is a point-in-time observation, not a drain or restart lock; see the [CLI reference](../../../docs/cli.md#check-active-responses) for its exact scope.

## Event Journal

The runtime chart defaults to PostgreSQL for MindRoom's Matrix event journal, because Kubernetes deployments need a durable database for it.
The chart can either create a small PostgreSQL StatefulSet for the journal or wire the runtime to an externally managed database.

This database is not a cache.
It is the single durable owner of admitted Matrix events, turn records, the response outbox, and history debt, so losing it loses the record of which messages have already been answered rather than merely costing a rebuild.
Size and back it up accordingly.
See [Backup and Restore](../../../docs/deployment/kubernetes.md#backup-and-restore) for the volumes and Secrets that must be restored together with it.

The chart's values and resource names still say `eventCache`, and they keep that name so existing deployments do not have to be renamed.
Wherever this document says event journal, the value to set is `eventCache`.

Use the chart-managed database for a simple cluster deployment:

```yaml
eventCache:
  backend: postgres
  postgres:
    create: true
    persistence:
      size: 20Gi
```

For the chart-managed database, `eventCache.postgres.service.port` configures the PostgreSQL listener, health probes, headless Service, NetworkPolicy, and generated connection URL together (default: `5432`).

For GitOps or `helm template` workflows, set `eventCache.postgres.auth.password` or provide existing Secrets so renders do not rotate generated credentials.
When adopting an existing PostgreSQL StatefulSet, keep the service name and password source stable:

```yaml
eventCache:
  backend: postgres
  postgres:
    create: true
    nameOverride: existing-event-cache-postgres
    selectorLabels:
      app: existing-event-cache-postgres
    auth:
      existingSecret: existing-event-cache-secrets
      passwordKey: POSTGRES_PASSWORD
    persistence:
      volumeName: existing-event-cache-postgres-data
  databaseUrl:
    existingSecret: existing-event-cache-secrets
    key: DATABASE_URL
```

Use an external database by providing a Secret with a full PostgreSQL connection URL:

```yaml
eventCache:
  backend: postgres
  postgres:
    create: false
  databaseUrl:
    existingSecret: event-cache-database-url
    key: DATABASE_URL
```

Use SQLite only for lightweight or local-style installs:

```yaml
eventCache:
  backend: sqlite
```

When `config.create` is enabled and `config.data` is empty, the chart renders a minimal config whose `event_journal` section follows `eventCache`.
When using `config.existingConfigMap` or custom `config.data`, keep that config's `event_journal` settings aligned with the chart values.
The runtime config rejects unknown top-level keys, so a `cache` section makes MindRoom refuse to start.

## Config Sources

By default the chart keeps the existing ConfigMap behavior.
Omit `config.source` or set `config.source: configMap` to mount a chart-created or existing ConfigMap at `config.mountPath`.
Set `config.source: file` when another init container or content bundle places `agent-config.yaml` on the runtime filesystem before MindRoom starts.
In file mode, `config.path` must be an absolute container path.
In file mode, the chart does not render or mount the runtime config ConfigMap.
Dedicated Kubernetes workers receive the same config file path and do not receive worker ConfigMap settings.
Nothing is mounted at that path: neither dedicated Kubernetes workers nor the `static_runner` sidecar mount the config file or its directory, and each request carries the allowlisted config fields they resolve.
With `workers.backend: static_runner`, the chart rejects a config inside `agents`, `private_instances`, or `sandbox-runner` because the sidecar can write those directories.

Use a content bundle as the source of truth for the runtime config:

```yaml
contentBundles:
  - name: team-config
    image: registry.example.com/team/mindroom-config@sha256:1111111111111111111111111111111111111111111111111111111111111111
    targetPath: /app/agent_data/content-bundles/team-config

config:
  source: file
  path: /app/agent_data/content-bundles/team-config/content/environments/prod/agent-config.yaml
```

## Runtime State Storage

MindRoom stores Matrix encryption keys and crash-atomic sync continuity records under `MINDROOM_STORAGE_PATH`.
Hosted installs can keep those restart-critical directories on a dedicated PVC while normal workspace data stays on `storage`.
Use it when `storage` is shared network storage, such as the `ReadWriteMany` volume dedicated workers need, so this state gets a volume that you can provision and snapshot on its own.
The chart mounts the same state PVC at `stateStorage.mountPath` and overlays the configured subpaths where MindRoom already reads and writes those directories.
The optional init container creates the state directories and applies the configured ownership before the runtime starts.
`initPermissions.runAsUser` and `initPermissions.fsGroup` are the ownership targets used by the init container.
By default, the init container runs as UID 0 because it performs `chown` and `chmod`, while the main runtime container keeps its own security context.
Upgrades from chart versions that used `stateStorage.syncTokens` must rename that values block to `stateStorage.syncContinuity` and use the `sync_continuity` subpath.
The old sync-token data is intentionally not migrated, so the first upgraded start performs a cold sync while the history fence prevents duplicate replies.

Use an existing PVC when another platform layer owns durable storage:

```yaml
storage:
  create: false
  existingClaim: mindroom-workspace
  mountPath: /app/agent_data

stateStorage:
  enabled: true
  existingClaim: mindroom-state
  mountPath: /app/mindroom_state
  encryptionKeys:
    enabled: true
    mountPath: /app/agent_data/encryption_keys
    subPath: encryption_keys
  syncContinuity:
    enabled: true
    mountPath: /app/agent_data/sync_continuity
    subPath: sync_continuity
  initPermissions:
    enabled: true
    runAsUser: 1000
    fsGroup: 1000
```

Let the chart create the state PVC for simpler hosted installs:

```yaml
stateStorage:
  enabled: true
  create: true
  size: 20Gi
```

`stateStorage.extraSubPaths` overlays more state PVC subpaths, and the init container creates and chowns them with the built-in ones.
Each entry needs a `name` and an absolute `mountPath`, and `subPath` defaults to `name`.
For example, keep the tracking journal on the state PVC when `storage` is shared network storage:

```yaml
stateStorage:
  enabled: true
  existingClaim: mindroom-state
  extraSubPaths:
    - name: tracking
      mountPath: /app/agent_data/tracking
```

On an existing install, the state PVC's subpaths start empty and hide the directories already on `storage`.
After the [Restart Safety](#restart-safety) check, apply the change with `replicaCount: 0`, copy `encryption_keys/`, `sync_continuity/`, and each `extraSubPaths` directory such as `tracking/` from the storage claim to the matching subpaths of the state claim, for example from a temporary pod that mounts both, then set `replicaCount` back to 1.
Without the copy, agents fail to start with `The bound Matrix device store is missing`, and an empty `tracking/` drops the records kept there, such as room and thread overrides and, with `eventCache.backend: sqlite`, the event journal itself.

## Session and Knowledge Storage

`sessionStorage` gives agent and team session databases their own volume.
The chart mounts it at `sessionStorage.mountPath` (default `/app/session_state`) and sets `MINDROOM_SESSION_STORAGE_PATH` to that path, so do not also set that variable in `env.extra`.
`knowledgeStorage` gives shared knowledge-base indexes their own volume, mounted at `<storage.mountPath>/knowledge_db` where MindRoom stores them.
Each block creates a `<fullname>-sessions` or `<fullname>-knowledge` PVC from `size`, `storageClassName`, and `accessModes`, or mounts `existingClaim` instead.
Both volumes must be writable by the runtime user; `podSecurityContext.fsGroup` covers volume types that support ownership management.
Use them to keep these SQLite and Chroma databases on fast `ReadWriteOnce` storage, sized on their own, when `storage` is `ReadWriteMany` network storage shared with dedicated workers.

```yaml
sessionStorage:
  enabled: true
  existingClaim: mindroom-sessions

knowledgeStorage:
  enabled: true
  size: 100Gi
  storageClassName: fast-rwo
```

On an existing install, these volumes start empty, so copy the data first as described for [Runtime State Storage](#runtime-state-storage).
Copy every `sessions/` directory, such as `agents/<name>/sessions/` and `teams/<name>/sessions/`, to the same relative path on the sessions volume; otherwise agents and teams start new, empty session databases while the old ones stay unused on `storage`.
Copy the contents of `knowledge_db/` to the root of the knowledge volume too, or MindRoom rebuilds each index from its sources and repeats every embedding call, as described in [Rebuildable data](../../../docs/deployment/kubernetes.md#rebuildable-data).

## Content Bundles

Hosted deployments can copy immutable private content into MindRoom storage before the runtime starts.
Use `contentBundles` for plugins, skills, agent templates, workspace templates, policy files, and small seed scripts that are packaged into an OCI image.
The chart does not clone repositories or download release assets at pod startup.
Private registry access should use normal Kubernetes image pull credentials through `imagePullSecrets`.

Each bundle image must be pinned by digest and must contain a POSIX shell, `cp`, and `mkdir`, because the chart runs the bundle image as a copy init container.
Because `overwrite` defaults to true, bundle images also need `rm` unless every bundle sets `overwrite: false`.
Package content under `/bundle` by default:

```dockerfile
FROM busybox:1.36
COPY . /bundle
```

Configure one or more bundles:

```yaml
imagePullSecrets:
  - name: private-registry-pull

contentBundles:
  - name: team-config
    image: registry.example.com/team/mindroom-config@sha256:1111111111111111111111111111111111111111111111111111111111111111
    sourcePath: /bundle
    targetPath: /app/agent_data/content-bundles/team-config
    seed:
      enabled: true
      command:
        - /app/agent_data/content-bundles/team-config/scripts/seed-content.sh

  - name: policy-pack
    image: registry.example.com/team/policy-pack@sha256:2222222222222222222222222222222222222222222222222222222222222222
```

If `targetPath` is omitted, the chart copies to `/app/agent_data/content-bundles/<name>`.
The init container removes the target path before copying unless `overwrite: false` is set.
`seed.command` runs after the copy and should point at a short script or executable supplied by the bundle instead of embedding deployment-specific shell in Helm values.
The script runs in the same bundle image, with MindRoom storage mounted at `storage.mountPath`.
Raw `initContainers`, `extraVolumes`, and `extraVolumeMounts` still work for deployments that need lower-level Kubernetes wiring.

For a writable configuration tree, transport the candidate to one directory and let the runtime initialize a separate active directory:

```yaml
contentBundles:
  - name: config-source
    image: registry.example.org/team/config@sha256:1111111111111111111111111111111111111111111111111111111111111111
    targetPath: /app/agent_data/config-source

config:
  source: file
  path: /app/agent_data/active-config/config.yaml
  bootstrapContentBundle:
    name: config-source
    subPath: environments/prod

workers:
  backend: kubernetes
```

`config.bootstrapContentBundle` is optional and disabled by default.
It adds `--bootstrap-config-bundle` and `--bootstrap-config-bundle-revision` to `mindroom run`.
The bootstrap source is the named bundle's `targetPath` joined with `subPath`, so the example installs from `/app/agent_data/config-source/environments/prod`.
`subPath` is relative to the bundle root and defaults to the root itself.
The revision is the sha256 of the bundle image digest and the selected image directory, `sourcePath` joined with `subPath`.
Pin the digest once in `contentBundles`; a new image or a different `sourcePath` or `subPath` becomes a new revision.
The chart rejects an unknown bundle name and an absolute or `..` subPath.
It also rejects a selected bundle with `overwrite: false`, or with a `volumeMounts` path that equals, contains, or lies inside its `targetPath` or `sourcePath`, because the source would no longer match the image digest.
The derived form is for trees that only the selected image writes.
Do not point another bundle, any seed script (including the selected bundle's own), raw `initContainers`, or `extraVolumeMounts` at the selected source, because the revision would not change with their content.
A matching stored revision preserves the active tree across restarts, including later hot updates and guarded rollbacks.
A changed revision validates and installs the candidate under the native installer's non-force drift rules.

When another mechanism, such as a seed script, raw `initContainers`, or `extraVolumeMounts`, provides or changes the source tree, set `config.bootstrapBundlePath` to its absolute directory instead and advance the revision yourself.
`config.bootstrapBundleRevision` is optional, requires `bootstrapBundlePath`, and is an opaque, nonblank string of at most 128 UTF-8 bytes with no control characters.
Neither explicit value can be combined with `config.bootstrapContentBundle`.
Native installation and validation run in the main runtime container, with its image, mounts, and environment, before runtime startup.
The target directory and filename come from `config.path`; the source must contain that filename at its root.
The target must be its own directory below `storage.mountPath`, not the storage root or a ConfigMap mount.
The chart rejects a normalized bootstrap source that equals, contains, or lies inside the target config directory, and a selected bundle `targetPath` that contains the target config directory, because transport removes that path on every start.

Without a revision, initialization preserves any existing active directory, including authored edits, across restarts and content image changes.
Bundle transport can refresh the separate source directory normally.
To activate a changed revision without a restart, run `mindroom config apply-bundle SOURCE --target TARGET --config FILENAME --rollback-on-failure --json` in the runtime container, where TARGET and FILENAME are the directory and filename of `config.path`.
It installs the tree, waits for the runtime to apply its fingerprint, restores the digest-pinned `TARGET.previous` only when the runtime rejects the change, and exits `0` only for `applied`.
It refuses changes outside the YAML/include sources that a config reload rereads, such as `.env` or plugin files; [Updating a Bootstrapped Config](#updating-a-bootstrapped-config) shows the full hot path.
See [`config apply-bundle`](../../../docs/deployment/config-bundles.md#config-apply-bundle) for receipt statuses and exit codes, and [`config install-bundle`](../../../docs/deployment/config-bundles.md#config-install-bundle) for drift protection, manual rollback, and filesystem limits.
Content images need no MindRoom binary.

Bootstrap cannot be combined with `workers.backend: static_runner`: its sidecar starts concurrently and could capture the environment before installation.
The chart rejects this combination until startup ordering is guaranteed.
Bootstrap also requires `replicaCount` 0 or 1, because concurrent pods would install into the same config directory at once.

Reference copied plugins from `config.yaml` with absolute paths:

```yaml
plugins:
  - /app/agent_data/content-bundles/team-config/plugins/review-tools
  - /app/agent_data/content-bundles/policy-pack/plugins/policy-tools
```

### Updating a Bootstrapped Config

With `config.bootstrapContentBundle`, a config change reaches the runtime through a restart or a hot install.

The restart path ships any change, including `.env`, plugins, and other files a config reload does not reread:

1. Push the new bundle image and pin its digest in `contentBundles[].image`.
2. Run the [Restart Safety](#restart-safety) check and continue only when it exits `0`.
3. Run `helm upgrade`; the new digest is a new revision, so startup validates the bundle and installs it before MindRoom loads the config.

A candidate that fails validation, or an active tree edited outside the installer, for example by a dashboard save, stops startup with `Bundle initialization failed` and leaves the active tree unchanged; pin the previous digest to start again.

The hot path applies changes limited to the YAML/include sources without a restart.
Copy the candidate tree, the bundle directory that `subPath` selects, into the running pod, then install it and wait for the runtime to apply it:

```bash
pod=$(kubectl -n mindroom get pods -l app.kubernetes.io/instance=mindroom-runtime,app.kubernetes.io/component=runtime -o jsonpath='{.items[0].metadata.name}')
kubectl -n mindroom exec "$pod" -c mindroom -- rm -rf /app/agent_data/config-candidate
kubectl -n mindroom cp ./environments/prod "$pod:/app/agent_data/config-candidate" -c mindroom
kubectl -n mindroom exec "$pod" -c mindroom -- mindroom config apply-bundle /app/agent_data/config-candidate --target /app/agent_data/active-config --rollback-on-failure --json
```

A hot install keeps the stored revision, so restarts preserve it until the next bundle digest replaces the tree; ship the same change in that image.

## Provider API Keys from Kubernetes Secrets

The MindRoom runtime natively honors model-provider API key env vars such as `OPENAI_API_KEY` and `ANTHROPIC_API_KEY`.
At startup it syncs each value into its credential service, which stores it as `<storage.mountPath>/credentials/<provider>_credentials.json`.
Use `providerCredentials` to bind those env vars to existing Kubernetes Secrets without writing any credential files yourself:

```yaml
providerCredentials:
  - provider: openai
    existingSecret: my-llm-secret
    key: api-key
  - provider: anthropic
    existingSecret: my-llm-secret
    key: anthropic-api-key
```

Supported provider names are `anthropic`, `azure`, `openai`, `google`, `openrouter`, `deepseek`, `cerebras`, `groq`, `zai`, and `ollama` (which expects a host URL instead of an API key).
Each entry renders only a `secretKeyRef` env var on the runtime container, so no secret material passes through ConfigMaps or appears in rendered manifests.
The chart rejects entries whose env var is also set through `env.extra`, but variables arriving through `env.envFrom` cannot be collision-checked because Secret and ConfigMap keys are not visible at template time.
If an `env.envFrom` source contains the same variable name, the container-level `env` entry rendered by `providerCredentials` takes precedence in Kubernetes.
Because the runtime writes the credential files itself, this path also works with encrypted credential storage (`workers.sandbox.credentialsEncryptionKey`), where externally written plaintext JSON files would be refused.

Sync semantics follow the runtime's `_source` tracking:

- Env-sourced credentials are refreshed on every startup, so rotating the Secret takes effect after a Deployment restart (env values from `secretKeyRef` are fixed at pod start).
- Credentials saved through the dashboard (`_source: ui`) are never overwritten by env sync.
- A pre-existing credential file without `_source: env`, for example one written manually or by a custom init container, is also left untouched; delete that file once when migrating to `providerCredentials`.

Non-secret companion settings such as `OPENAI_BASE_URL` or `AZURE_OPENAI_ENDPOINT` are plain config and belong in `env.extra`.
For credential services beyond the supported providers, the runtime accepts declared credential seeds through the `MINDROOM_CREDENTIAL_SEEDS_JSON` env var, whose JSON contains only env-var references while the secret values still arrive via `secretKeyRef` entries in `env.extra`:

```yaml
env:
  extra:
    - name: MY_GATEWAY_API_KEY
      valueFrom:
        secretKeyRef:
          name: my-gateway-secret
          key: api-key
    - name: MINDROOM_CREDENTIAL_SEEDS_JSON
      value: '[{"service": "my_gateway", "credentials": {"api_key": {"env": "MY_GATEWAY_API_KEY"}}}]'
```

## Layering Values Files

Helm replaces lists wholesale when it merges several values files, so an environment overlay cannot change one list entry without restating the whole list.
`env.extra`, `env.envFrom`, `workers.kubernetes.agentVault.server.extraEnv`, `workers.kubernetes.agentVault.server.envFrom`, `extraVolumes`, and `extraVolumeMounts` therefore accept either a list or a map keyed by entry, which Helm merges key by key.

- Map entries render sorted by key, so choose keys accordingly where order matters, such as `envFrom` precedence or `$(VAR)` references.
- An entry's `name` defaults to its key; set `name` explicitly to mount one volume at several paths.
- A `null` entry removes an inherited entry, and a `null` field removes an inherited field, for example to replace `value` with `valueFrom`.
- An env map entry may be a plain string as shorthand for `value`; quote numbers and booleans, which Kubernetes requires as strings anyway.
- Use the same form for a field in every values file, because Helm replaces a list with a map, or a map with a list, wholesale.

String env values in `env.extra`, `workers.kubernetes.agentVault.server.extraEnv`, and `workers.kubernetes.extraEnv` are rendered with `tpl`, as are `egressProxy.noProxy` entries.
Shared values can therefore reference the release namespace or other values instead of repeating environment-specific names.
Write `{{ "{{" }}` for a literal `{{`.
A `null` in `workers.kubernetes.extraEnv` removes an inherited or chart-injected worker variable.

```yaml
# shared-values.yaml
matrix:
  serverName: chat.example.com
env:
  extra:
    MINDROOM_PUBLIC_URL: "https://{{ .Values.matrix.serverName }}"
    OPENAI_BASE_URL: "http://llm-proxy.{{ .Release.Namespace }}.svc.cluster.local:8080/v1"
    OPENAI_API_KEY:
      valueFrom:
        secretKeyRef:
          name: mindroom-secrets
          key: OPENAI_API_KEY
    UPSTREAM_JWT_AUDIENCE:
      value: shared-audience
  envFrom:
    10-secrets:
      secretRef:
        name: mindroom-secrets
extraVolumes:
  ca-bundle:
    secret:
      secretName: custom-ca-bundle
  scratch:
    emptyDir: {}
extraVolumeMounts:
  ca-bundle:
    mountPath: /etc/ssl/custom
    readOnly: true
  scratch:
    mountPath: /scratch
```

```yaml
# staging-values.yaml, passed after shared-values.yaml
matrix:
  serverName: staging.example.com
env:
  extra:
    OPENAI_API_KEY:
      valueFrom:
        secretKeyRef:
          name: staging-secrets
    UPSTREAM_JWT_AUDIENCE:
      value: null
      valueFrom:
        secretKeyRef:
          name: staging-secrets
          key: UPSTREAM_JWT_AUDIENCE
  envFrom:
    10-secrets:
      secretRef:
        name: staging-secrets
extraVolumes:
  scratch: null
extraVolumeMounts:
  scratch: null
```

## Control-Plane NetworkPolicy

The chart can create an optional NetworkPolicy for the control-plane pod, so operators can restrict runtime API ingress to an edge proxy and known clients.
The policy targets the runtime pods through the chart selector labels and allows the API port only from the configured peers.
NetworkPolicy targets pods rather than Services, so each `apiIngressFrom` entry must match the actual client pods.
A bare `podSelector` entry only matches pods in the release namespace, so combine `namespaceSelector` and `podSelector` in one entry for cross-namespace clients such as an ingress controller.

```yaml
networkPolicy:
  create: true
  apiIngressFrom:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: my-ingress
      podSelector:
        matchLabels:
          app.kubernetes.io/name: my-ingress
    - podSelector:
        matchLabels:
          app.kubernetes.io/name: my-api-client
```

The API port follows `runtime.apiPort`, so the policy stays aligned with the Deployment without a separate port value.
When `apiIngressFrom` is empty, the policy allows the API port from all sources while still denying other ingress to the pod.
Use `networkPolicy.extraIngress` and `networkPolicy.extraEgress` for raw Kubernetes rules beyond the API rule.
Setting any `extraEgress` entry adds `Egress` to `policyTypes`, so those rules must then cover every egress flow the runtime needs, such as DNS, the Matrix homeserver, model APIs, the event journal database, workers, and the Kubernetes API server when `workers.backend` is `kubernetes`.

## Worker Egress Proxy

For dedicated Kubernetes workers, the chart can either deploy the approved egress proxy or point workers at an existing HTTP proxy Service.
In both modes, the worker egress NetworkPolicy keeps worker internet access on the proxy path instead of allowing direct public egress.

Use `approvedEgress` when you want the runtime chart to manage the proxy side too:

```yaml
workers:
  backend: kubernetes
  sandbox:
    proxyToken:
      existingSecret: mindroom-sandbox-proxy
      key: MINDROOM_SANDBOX_PROXY_TOKEN

approvedEgress:
  enabled: true
  image:
    tag: v0.1.10
  allowlist:
    domains:
      - example.com
      - .docs.example.com
```

This renders the proxy Deployment, Service, ServiceAccount, RBAC, allowlist ConfigMap, persistence PVC, and proxy ingress NetworkPolicy.
By default, chart-managed proxy resources use the release-derived `<fullname>-egress-proxy` name; set `approvedEgress.service.name` only when an explicit shared name is required.
The control-plane pod receives `MINDROOM_APPROVED_EGRESS_API_URL`, `MINDROOM_APPROVED_EGRESS_ALLOWLIST_PATH`, `MINDROOM_APPROVED_EGRESS_TOKEN`, and `MINDROOM_APPROVED_EGRESS_MAX_TTL_SECONDS`.
The control-plane pod also mounts the same allowlist file so the built-in `approved_egress` tool can avoid asking for domains that are already static-allowed.
The proxy reads the allowlist only at startup, so the chart hashes inline `approvedEgress.allowlist.domains` into a `checksum/allowlist` pod annotation and any change rolls the proxy.
The chart cannot read an `approvedEgress.allowlist.existingConfigMap`, so after editing that ConfigMap restart the proxy Deployment (`approvedEgress.service.name`, or `<fullname>-egress-proxy` when unset) with `kubectl rollout restart`, or have the tooling that renders it put a content hash in `approvedEgress.podAnnotations` (for example `checksum/allowlist`).
The control-plane copy of the allowlist refreshes only when that pod restarts, so after removing a domain also restart the control-plane Deployment (`<fullname>`); until then `request_network_access` reports that domain as already allowed and creates no grant, while the proxy blocks it.
The control-plane pod also receives `MINDROOM_APPROVED_EGRESS_ENABLED=true`, so MindRoom adds `approved_egress` and the required Matrix approval rule at runtime even when you use `config.data` or `config.existingConfigMap`.
Set `approvedEgress.manageRuntimeConfig: false` to skip that flag when the authored config already assigns `approved_egress` deliberately, for example to specific agents instead of `defaults.tools`; the proxy and the other approved egress env vars are still wired.
The proxy pod reads `MINDROOM_APPROVED_EGRESS_TOKEN` from `approvedEgress.token.existingSecret` when set, otherwise it reuses `workers.sandbox.proxyToken`.
Pin `approvedEgress.image.tag` or `approvedEgress.image.digest` before enabling the feature.

Use `egressProxy` when another chart or platform layer already manages the proxy:

```yaml
workers:
  backend: kubernetes

egressProxy:
  enabled: true
  service:
    name: mindroom-egress-proxy
    namespace: mindroom
    port: 3128
  noProxy:
    - localhost
    - 127.0.0.1
    - ::1
    - internal-api.mindroom.svc.cluster.local
  networkPolicy:
    create: true
    proxyPodSelector:
      matchLabels:
        app.kubernetes.io/name: mindroom-egress-proxy
    extraEgress:
      - to:
          - podSelector:
              matchLabels:
                app.kubernetes.io/name: internal-api
        ports:
          - protocol: TCP
            port: 8080
```

When `egressProxy.injectWorkerProxyEnv` is true, worker pods receive `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, and lowercase variants through `MINDROOM_KUBERNETES_WORKER_ENV_JSON`.
`workers.kubernetes.extraEnv` is still applied after those defaults, so platform values can override individual variables.
When `approvedEgress.enabled` is true, the chart automatically points worker proxy settings and the worker egress policy at the chart-managed proxy.
When `egressProxy.networkPolicy.create` is true, workers can egress only to DNS, the selected proxy pods, and `egressProxy.networkPolicy.extraEgress`.
For externally managed proxies, NetworkPolicy targets pods rather than Services, so `proxyPodSelector` must match the existing proxy Deployment labels.

When Agent Vault is also enabled, keep approved egress as the first network hop.
The proxy resolves dynamic `request_network_access` grants from the worker pod's source IP; if worker traffic goes to Agent Vault first, every request reaches Squid from the vault pod IP and worker identity cannot be resolved.
Enable `approvedEgress.parentProxy` to route only token-bearing Agent Vault tool traffic through the vault parent after the allowlist/grant check:

```yaml
workers:
  backend: kubernetes
  kubernetes:
    agentVault:
      enabled: true
      cliImage: infisical/agent-vault:<pinned>
      ownerEmail: owner@example.test
      server:
        enabled: true
      # Leave proxyUrl at the default; parentProxy makes workers use approved
      # egress first and Squid forwards Proxy-Authorization traffic to the vault.

approvedEgress:
  enabled: true
  image:
    tag: v0.1.10
  parentProxy:
    enabled: true
    host: agent-vault
    port: 14322
```

With this setup, workers mint Agent Vault tokens through the API port (`14321`), workers proxy tool egress to the approved egress Service, Squid enforces allowlists and dynamic grants using the real worker IP, and Squid forwards requests carrying `Proxy-Authorization` to Agent Vault (`login=PASSTHRU`) for credential injection.
Tokenless traffic egresses directly from Squid after the normal policy check.
For signed URLs or other destinations that must skip parent-proxy credential injection, set `approvedEgress.parentProxy.bypassDomains`:

```yaml
approvedEgress:
  parentProxy:
    enabled: true
    bypassDomains:
      - downloads.example.test
      - .objects.example.test
```

This list defaults to empty and applies only when chart-managed approved egress and its parent proxy are enabled.
An entry without a leading dot matches that exact hostname; a leading dot matches the domain and its subdomains, following [Squid domain ACL syntax](https://www.squid-cache.org/Doc/config/acl/).
Use ASCII domain names (Punycode for internationalized names), without URL schemes, ports, paths, `*` wildcards, or whitespace; invalid entries fail chart rendering.
Matching uses the request hostname without reverse DNS lookups.
Bypass destinations still require the normal allowlist or dynamic grant and remain subject to the proxy's destination and port restrictions.
Other token-bearing requests continue through the configured parent.
Changing the list changes the chart's Squid config checksum and rolls the proxy Deployment automatically.

When `agentVault.accessTool.enabled` is set, self-service vault grants also keep `agentVault.ownerEmail` as an admin on each worker vault; the Kubernetes worker init container uses that owner account to mint and attach the proxy-role agent token.
The chart rejects the unsafe default combination of chart-managed approved egress plus Agent Vault without `approvedEgress.parentProxy`, because that would be vault-first and break dynamic grants.

### Agent Vault Server Environment

Use `workers.kubernetes.agentVault.server.extraEnv` for raw Kubernetes `EnvVar` entries, including `valueFrom` Secret references.
Use `server.envFrom` to import variables from existing Secrets or ConfigMaps.
Both default to empty, accept the map form described in [Layering Values Files](#layering-values-files), and apply only to the chart-managed Agent Vault server.
Keep sensitive values in Secrets and avoid duplicate environment variable names; use the dedicated master-password and SMTP settings for chart-managed variables.
The chart rejects `extraEnv` entries that repeat the master-password variable or any SMTP variable emitted when `server.smtp.enabled` is true.

```yaml
workers:
  kubernetes:
    agentVault:
      server:
        enabled: true
        image: registry.example.com/agent-vault@sha256:1111111111111111111111111111111111111111111111111111111111111111
        extraEnv:
          - name: AGENT_VAULT_ADDR
            value: https://vault.example.com
          - name: AGENT_VAULT_OAUTH_GITHUB_CLIENT_SECRET
            valueFrom:
              secretKeyRef:
                name: vault-oauth
                key: client-secret
        envFrom:
          - secretRef:
              name: vault-extra-env
          - configMapRef:
              name: vault-settings
```

### Agent Vault Server NetworkPolicy

The chart-managed Agent Vault server gets its own NetworkPolicy by default, because the API port can create vaults and agent tokens and the proxy port injects stored credentials.
Ingress to the API port (`server.apiPort`) is limited to the in-chart clients that are enabled: dedicated workers when `agentVault.enabled` is true, the control plane when `accessTool.enabled` is true, the bootstrap Job, and the access-grants Job.
Workers are matched like the worker NetworkPolicy and the runtime's own worker listing: the worker namespace, the generic worker labels, and `workers.kubernetes.extraLabels`.
The selector matches any worker pod that carries all of those labels, so releases that share a worker namespace must each set a different value for the same `workers.kubernetes.extraLabels` key, for example `mindroom.ai/instance`.
If one release's `extraLabels` are empty or a subset of another release's, its vault also admits the other release's workers.
Ingress to the proxy port (`server.mitmPort`) is limited to the approved egress proxy when `approvedEgress.parentProxy.enabled` is true, or to dedicated workers when `approvedEgress` is disabled and workers use `agentVault.proxyUrl` directly.
Egress is limited to DNS and, to any address, TCP `80` and `443` for proxied upstreams and OAuth providers and `server.smtp.port` when SMTP is enabled.
When `egressProxy` or `approvedEgress` is enabled and `egressProxy.networkPolicy.create` is true (its default), vault DNS is limited to the same `egressProxy.networkPolicy.dns` destinations as worker DNS (default kube-dns in `kube-system`), and otherwise to port 53 on any address.
Kubelet health probes are unaffected.

Clients the chart does not render, such as an SSO proxy in front of the vault UI or an ingress controller, need `server.networkPolicy.extraIngress`.
Destinations beyond the defaults, such as upstream services on other ports, need `server.networkPolicy.extraEgress`.
Set `server.networkPolicy.create: false` to manage the vault policy yourself.

```yaml
workers:
  kubernetes:
    agentVault:
      server:
        enabled: true
        networkPolicy:
          extraIngress:
            - from:
                - podSelector:
                    matchLabels:
                      app.kubernetes.io/name: vault-ui-proxy
              ports:
                - protocol: TCP
                  port: 14321
```

### Agent Vault Access Grants

Agent Vault has two separate access questions.
Runtime use is controlled by MindRoom worker scope and tool authorization.
Raw secret visibility is controlled by Agent Vault vault membership.

Use `worker_scope: user` or `worker_scope: user_agent` when each requester should manage and use their own credentials.
Those requester-isolated workers can use the self-service `agent_vault_access` tool to grant the requester admin access to their own worker vault.

Use `worker_scope: shared` when a shared agent should use a common service credential.
In that model, all users who can invoke credential-capable tools on that shared agent may indirectly use the credentials through the worker proxy path.
Only trusted maintainers should receive Agent Vault admin access, because admin access lets them view, edit, and manage the raw secret values in the vault.
Access grants do not restrict who can cause a shared worker to use credentials at runtime.

Set `workers.kubernetes.agentVault.accessGrants` to declaratively grant vault admin access without deployment-specific inline scripts:

```yaml
workers:
  backend: kubernetes
  kubernetes:
    agentVault:
      enabled: true
      cliImage: infisical/agent-vault:<pinned>
      ownerEmail: owner@example.test
      accessGrants:
        enabled: true
        grants:
          - email: maintainer@example.com
            workerScope: shared
            agent: example-agent
            role: admin
          - email: user@example.com
            workerScope: user_agent
            requester: "@user:example.com"
            agent: example-agent
            role: admin
          - email: user@example.com
            workerScope: user
            requester: "@user:example.com"
            role: admin
```

The chart renders no access-grant resources by default.
When access grants are enabled and at least one grant is configured, the chart renders a ConfigMap plus a Job that runs `python -m mindroom.agent_vault_access_grants apply` from the MindRoom image.
The helper resolves worker keys and vault names through MindRoom's worker-routing code, creates or joins the vault when needed, and grants the configured email the `admin` role.
The helper is idempotent, so running it again with the same grants changes nothing.
If an email has not registered and verified in Agent Vault yet, the helper reports a warning and the grant can be applied again after registration.

For `workerScope: shared`, `agent` is required and `requester` must be omitted.
For `workerScope: user_agent`, both `requester` and `agent` are required.
For `workerScope: user`, `requester` is required and `agent` must be omitted because the worker key is per-user, not per-agent.
The initial supported role is `admin`.
If your deployment sets `CUSTOMER_ID` or `ACCOUNT_ID` for tenant-specific worker keys, set `accessGrants.tenantId` or `accessGrants.accountId` to the same value.
When `agentVault.bootstrap.enabled` is true, the bootstrap Job publishes the owner-role admin token Secret used by the access-grant Job.
If `accessTool` and `accessGrants` use the same admin-token Secret name, configure the same key for both or use different Secret names.
When bootstrap is disabled, provide `accessGrants.adminTokenSecret` yourself.

`workers.kubernetes.agentVault.jobNaming` controls how the access-grant and bootstrap Jobs run again.
With the default `fixed`, both Jobs keep stable names and the access-grant Job is a Helm `post-install,post-upgrade` hook, so `helm install` and `helm upgrade` replace and rerun it.
The bootstrap Job is not a hook, so changing its pod template requires deleting the finished Job first, even with `helm upgrade`.
Workflows that apply rendered manifests, such as `kubectl kustomize --enable-helm` followed by `kubectl apply`, ignore Helm hook annotations, and Job pod templates are immutable.
With `fixed`, those workflows do not rerun an existing access-grant Job for a changed grant list, and they fail to apply a changed pod template until the old Job is deleted.
Set `jobNaming: contentHash` for those workflows.
The chart then drops the hook and appends a hash of each Job's rendered pod spec, plus the grant config for the access-grant Job, to the Job name.
The grants ConfigMap name gets a hash of its config too, so each access-grant Job reads the grants it was created for; `kubectl apply` leaves earlier grants ConfigMaps in place until you delete them or apply with `--prune`.
Applying changed inputs creates a new Job, and applying unchanged inputs leaves the existing Job alone.
A chart upgrade that leaves those inputs unchanged keeps the Job names, so it does not rerun the Jobs.
Finished Jobs are deleted after 24 hours by `ttlSecondsAfterFinished`, so the first apply after that runs the idempotent Job again.
To rerun a Job with unchanged inputs, for example after a grant recipient registers, delete it by label and apply again with `kubectl delete job -l app.kubernetes.io/component=agent-vault-access-grants`.

## Background Script Gateway

Background scripts on Kubernetes workers call governed tools through the primary's capability-authenticated script gateway.
MindRoom admits them only when workers reach that gateway through a listener that serves nothing else, because the general API port exposes more authority.
Set `scriptGateway.enabled` to have the primary serve that listener on its own port:

```yaml
workers:
  backend: kubernetes

# Any worker egress setup from Worker Egress Proxy whose NetworkPolicy is enabled.
approvedEgress:
  enabled: true
  image:
    tag: v0.1.10

scriptGateway:
  enabled: true
  port: 8767
```

The primary container then exposes a `script-gateway` port where MindRoom serves only `/api/script-gateway` routes and returns 404 for every other API route.
The chart renders a `<fullname>-script-gateway` ClusterIP Service for that port and sets `MINDROOM_SCRIPT_GATEWAY_PORT`, `MINDROOM_SCRIPT_GATEWAY_URL`, and `MINDROOM_SCRIPT_GATEWAY_ISOLATED=true` on the primary.
Worker pods receive the Service's cluster-local host name in `NO_PROXY` so gateway calls bypass the egress proxy.
If `workers.kubernetes.extraEnv` replaces `NO_PROXY` or `no_proxy`, include that host name yourself.
A `<fullname>-script-gateway-workers` NetworkPolicy in the worker namespace adds egress from workers to the gateway port on the control-plane pod.
When `networkPolicy.create` is true, a `<fullname>-script-gateway` NetworkPolicy also admits workers to that port on the control-plane pod.

The isolation attestation relies on the chart's worker egress NetworkPolicy, so `scriptGateway.enabled` requires `workers.backend=kubernetes`, `egressProxy.enabled` or `approvedEgress.enabled`, and `egressProxy.networkPolicy.create=true`.
Keep `egressProxy.networkPolicy.extraEgress` and the egress proxy allowlist from opening the control-plane API port or the main runtime Service to workers.
The Service is separate from the main runtime Service so `service.type` never exposes the gateway port outside the cluster.
See [Background Python Scripts](../../../docs/tools/background-scripts.md#worker-and-network-requirements) for the script tool, its gateway contract, and run lifecycle.

## Matrix Managed Account Authentication

`matrix.managedAccountAuth` selects how MindRoom creates and logs in its router, agent, team, and internal-user Matrix accounts.
The default `password` mode uses `matrix.registrationToken` when the homeserver requires token-gated registration.
The `appservice` mode creates passwordless accounts and requires `matrix.appserviceToken`:

```yaml
matrix:
  homeserverUrl: http://matrix.example.svc.cluster.local:8008
  serverName: example.com
  managedAccountAuth: appservice
  appserviceToken:
    existingSecret: matrix-appservice
    key: MATRIX_APPSERVICE_TOKEN
```

The chart mounts the appservice token as a Secret file and sets `MATRIX_APPSERVICE_TOKEN_FILE`.
Do not configure `matrix.registrationToken` in appservice mode.
The homeserver must register the matching application service and grant it an exclusive namespace containing every MindRoom-managed user ID.

## Existing Platform Example

```yaml
image:
  repository: ghcr.io/mindroom-ai/mindroom
  tag: latest

config:
  create: false
  existingConfigMap: mindroom-config
  key: config.yaml

storage:
  create: false
  existingClaim: mindroom-data
  mountPath: /app/agent_data

stateStorage:
  enabled: true
  existingClaim: mindroom-state

matrix:
  homeserverUrl: http://matrix.example.svc.cluster.local:8008
  serverName: example.com
  registrationToken:
    existingSecret: mindroom-secrets
    key: MATRIX_REGISTRATION_TOKEN

env:
  envFrom:
    - secretRef:
        name: mindroom-secrets
    - configMapRef:
        name: mindroom-env

eventCache:
  backend: postgres
  postgres:
    create: false
  databaseUrl:
    existingSecret: event-cache-database-url
    key: DATABASE_URL

workers:
  backend: kubernetes
  sandbox:
    proxyTools: shell,file,python,coding
    proxyToken:
      existingSecret: mindroom-sandbox-proxy
      key: MINDROOM_SANDBOX_PROXY_TOKEN
  kubernetes:
    serviceAccount:
      name: mindroom-worker
    port: 8766
    storageSubpathPrefix: workers
    readyTimeoutSeconds: 180
    idleTimeoutSeconds: 3600
```

## Notes

- The chart does not create ingress or a Matrix homeserver.
- Set `networkPolicy.create: true` to restrict control-plane API ingress to known client pods.
- Containers that run the runtime image default to `securityContext.runAsNonRoot: true`, which the kubelet enforces against the image's numeric uid 1000.
  The chart-managed Agent Vault server and the bootstrap Job's `bootstrap-owner` container default to `runAsNonRoot: true` with `runAsUser: 65532`, because the `infisical/agent-vault` image names its user (`agentvault`) and the kubelet can only verify a numeric user.
  A custom image that runs as root, or an Agent Vault image with a different uid, needs matching `securityContext` overrides.
  The bootstrap Job's `publish-ca` container leaves out `runAsNonRoot` because `kubectlImage` may run as root, so add it to `workers.kubernetes.agentVault.bootstrap.kubectlSecurityContext` for a non-root image or a restricted Pod Security policy.
  The state-storage init container still runs as root for `chown` and `chmod`, and content bundles, the event journal PostgreSQL, and user-supplied containers use the security contexts you set for them.
- The chart can create PostgreSQL for MindRoom's event journal, or use an external PostgreSQL URL from an existing Secret.
- Set `workers.sandbox.proxyToken.existingSecret` or `workers.sandbox.proxyToken.value` when sandbox proxying is enabled.
- Use `providerCredentials` to feed model-provider API keys from existing Kubernetes Secrets into the runtime's credential service.
- Worker tool code can reach the primary API, on `localhost` from the `static_runner` sidecar or over the pod network from dedicated Kubernetes workers, so the chart gives the primary a generated `MINDROOM_API_KEY` from the `<fullname>-api-key` Secret for either backend, and the dashboard and API then require it.
  A `MINDROOM_API_KEY` from `env.extra` or `env.envFrom` takes precedence, which GitOps and `helm template` workflows should use because the generated key changes on every offline render.
  `apiAuth.allowUnauthenticatedPrimary: true` removes the key and is unsafe unless other primary API authentication is configured.
- Set `workers.sandbox.credentialsEncryptionKey.existingSecret` when encrypted credential storage is enabled so the primary runtime receives the Secret-backed key.
- `workers.backend: static_runner` adds a sandbox-runner sidecar to the runtime pod.
  The sidecar never receives the credentials encryption key, and saved settings for each proxied tool reach it as per-call leases from the primary.
  From the storage PVC it mounts only the `agents` and `private_instances` directories read-write over its own `sandbox-runner` directory, so agent workspaces persist while the credential store, Matrix state, and the primary's config stay out of reach.
  It mounts neither the config ConfigMap nor a file-sourced config; each request carries the allowlisted config fields the runner resolves.
  An init container creates those directories as the runtime user.
- `workers.backend: kubernetes` lets the runtime create dedicated worker Deployments and Services on demand.
  In the release namespace, the chart stores derived worker tokens as entries in one chart-created worker-auth Secret and grants only `get` and `patch` on that Secret.
  Worker pods never receive the credentials encryption key, so with encrypted credential storage enabled saved tool settings reach them only as per-call leases from the primary.
  When `workers.kubernetes.namespace` points at a separate worker namespace, the chart uses per-worker auth Secrets and grants Secret CRUD only in that namespace.
  The chart can create the worker-manager RBAC and a worker NetworkPolicy.
- With `workers.kubernetes.reconcilePodTemplates` (default `true`), each cleanup pass recreates scaled-down worker Deployments whose pod template (image, env, resources) drifted from the configured spec, so existing workers do not need manual recycling after upgrades.
  Running workers are recreated on their next provisioning after they scale down.
- `workers.kubernetes.runtimeClassName` optionally applies one RuntimeClass to the entire generated worker pool, including background-script workers. Verify the RuntimeClass handler on every eligible node and validate that it supports the worker storage driver and access mode before enabling it. Changing the value can recreate existing workers when they are next ensured, so finish active work first.
- If workers run in a different namespace, provide storage, service accounts, and network policy behavior that are valid for that namespace.
  Kubernetes owner references are only set by default for same-namespace workers.
  The sandbox proxy token secret is only needed by the primary runtime; dedicated worker pods receive per-worker derived runner tokens.
- Mount arbitrary platform-specific files, projected secrets, ConfigMaps, init containers, and sidecars through `extraVolumes`, `extraVolumeMounts`, `initContainers`, and `extraContainers`.
- Use `nodeSelector`, `affinity`, `tolerations`, `topologySpreadConstraints`, and `podDisruptionBudget` for cluster-specific scheduling and availability policy.
- Set `selectorLabels` when adopting an existing Deployment with an immutable selector.
- Set `storage.volumeName`, `eventCache.postgres.selectorLabels`, `eventCache.postgres.persistence.volumeName`, or `workers.kubernetes.networkPolicy.name` when adopting existing resources with established names.
- Set `eventCache.postgres.persistence.includeChartLabels: false` when adopting an existing PostgreSQL StatefulSet whose volume claim template has no chart labels.
- Override `probes.*.custom` when a deployment needs custom Kubernetes startup, readiness, or liveness probes.

## Adopting Existing Resources

When replacing hand-written manifests for an existing runtime, keep immutable and externally referenced names stable in values:

```yaml
fullnameOverride: mindroom

selectorLabels:
  app: mindroom

config:
  create: false
  existingConfigMap: mindroom-config
  key: config.yaml

storage:
  create: false
  existingClaim: mindroom-data
  volumeName: data

eventCache:
  backend: postgres
  postgres:
    nameOverride: existing-event-cache-postgres
    selectorLabels:
      app: existing-event-cache-postgres
    auth:
      existingSecret: existing-event-cache-secrets
      passwordKey: POSTGRES_PASSWORD
    persistence:
      volumeName: existing-event-cache-postgres-data
      includeChartLabels: false
  databaseUrl:
    existingSecret: existing-event-cache-secrets
    key: DATABASE_URL

workers:
  backend: kubernetes
  kubernetes:
    networkPolicy:
      name: mindroom-workers

probes:
  liveness:
    custom:
      tcpSocket:
        port: api
      periodSeconds: 30
      timeoutSeconds: 10
      failureThreshold: 6
```

Render and diff the chart before applying it to existing objects:

```bash
helm template mindroom ./cluster/k8s/runtime \
  --namespace mindroom \
  -f runtime-values.yaml
```
