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
- [Egress Broker](#egress-broker)
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
With `workers.backend: static_runner`, the chart rejects a config inside `agents`, `private_instances`, `shared_directories`, or `sandbox-runner` because the sidecar can write those directories.

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
`knowledgeStorage` gives knowledge-base indexes, private agents' indexes included, their own volume, mounted at `<storage.mountPath>/knowledge_db` where MindRoom stores them.
Each block creates a `<fullname>-sessions` or `<fullname>-knowledge` PVC from `size`, `storageClassName`, and `accessModes`, or mounts `existingClaim` instead.
Both volumes must be writable by the runtime user; `podSecurityContext.fsGroup` covers volume types that support ownership management.
Use them to keep these SQLite databases on fast `ReadWriteOnce` storage, sized on their own, when `storage` is `ReadWriteMany` network storage shared with dedicated workers.

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
Images that also contain `find`, `stat`, `md5sum`, `awk`, `readlink`, `chmod`, `chown`, and `id`, as BusyBox does, are updated in place on restart; other images get a full copy on every start.
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
By default the init container makes the target path match the bundle: the same files, directories, symlinks, contents, and modes, as if it removed the path and copied the bundle again.
It deletes entries the bundle no longer contains and rewrites only entries whose type, symlink target, content, mode, or owner changed, so a restart copies only what changed.
It compares the trees with one `stat` and one `md5sum` batch per tree, so the number of processes a restart starts grows with the changes and symlinks, not with the files.
Unchanged files keep their timestamps, and hard-link relationships between bundle files are not guaranteed to be preserved.
With `overwrite: false`, it copies the bundle over the target path and removes nothing.
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
It reads each value as that provider's shared credential and stores no copy of it.
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
Because no credential file is involved, this path also works with encrypted credential storage (`workers.sandbox.credentialsEncryptionKey`), where externally written plaintext JSON files would be refused.

- Rotating or removing the Secret takes effect after a Deployment restart (env values from `secretKeyRef` are fixed at pod start).
- A credential saved for the same provider through the dashboard wins over the env var; deleting it there makes the env var apply again.
- A pre-existing credential file, for example one written manually or by a custom init container, also wins over the env var; delete that file once when migrating to `providerCredentials`.

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
`env.extra`, `env.envFrom`, `extraVolumes`, and `extraVolumeMounts` therefore accept either a list or a map keyed by entry, which Helm merges key by key.
So do the NetworkPolicy rule lists `networkPolicy.apiIngressFrom`, `networkPolicy.extraIngress`, `networkPolicy.extraEgress`, and `egressProxy.networkPolicy.extraEgress`, as well as the `egressProxy.noProxy` hosts.

- Map entries render sorted by key, so choose keys accordingly where order matters, such as `envFrom` precedence or `$(VAR)` references.
- An entry's `name` defaults to its key; set `name` explicitly to mount one volume at several paths.
- NetworkPolicy rule keys only identify the rule, and each `egressProxy.noProxy` map value is a host string.
  Removing every entry of a rule list behaves like an empty list, so `apiIngressFrom` then admits every source to the API port.
- `egressProxy.noProxy` defaults to the map `localhost`, `loopback-ipv4`, and `loopback-ipv6`, so a map adds hosts to those defaults while a list replaces them, with a Helm table warning.
- A `null` entry removes an inherited entry, and a `null` field removes an inherited field, for example to replace `value` with `valueFrom`.
- An env map entry may be a plain string as shorthand for `value`; quote numbers and booleans, which Kubernetes requires as strings anyway.
- Use the same form for a field in every values file, because Helm replaces a list with a map, or a map with a list, wholesale.

String env values in `env.extra` and `workers.kubernetes.extraEnv` are rendered with `tpl`, as are `egressProxy.noProxy` entries.
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

The proxy runs as one pod, so evicting it during a node drain or cluster-autoscaler scale-down interrupts worker egress until its replacement is ready.
Set `approvedEgress.podDisruptionBudget.create: true` to refuse such evictions while the proxy is ready; an unready proxy can still be evicted.
A drain of the proxy's node then waits until you remove the budget or restart the proxy Deployment, which the budget does not block.

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
    internal-api: internal-api.mindroom.svc.cluster.local
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

## Background Script Gateway

Background scripts on Kubernetes workers call governed tools through the primary's capability-authenticated script gateway.
MindRoom admits them only when workers reach that gateway through a listener that serves only the script gateway and capability-authenticated Agent CLI routes, because the general API port exposes more authority.
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

The primary container then exposes a `script-gateway` port where MindRoom serves only `/api/script-gateway` and `/api/agent-cli` routes and returns 404 for every other API route.
The chart renders a `<fullname>-script-gateway` ClusterIP Service for that port and sets `MINDROOM_SCRIPT_GATEWAY_PORT`, `MINDROOM_SCRIPT_GATEWAY_URL`, and `MINDROOM_SCRIPT_GATEWAY_ISOLATED=true` on the primary.
Unless `MINDROOM_AGENT_CLI_PRIMARY_URL` is set, [minimal-mode](../../../docs/tools/agent-cli.md#shell-in-a-worker) worker shells also call MindRoom through that Service, so they need no access to the general API port.
Worker pods receive the Service's cluster-local host name in `NO_PROXY` so gateway calls bypass the egress proxy.
If `workers.kubernetes.extraEnv` replaces `NO_PROXY` or `no_proxy`, include that host name yourself.
A `<fullname>-script-gateway-workers` NetworkPolicy in the worker namespace adds egress from workers to the gateway port on the control-plane pod.
When `networkPolicy.create` is true, a `<fullname>-script-gateway` NetworkPolicy also admits workers to that port on the control-plane pod.

The isolation attestation relies on the chart's worker egress NetworkPolicy, so `scriptGateway.enabled` requires `workers.backend=kubernetes`, `egressProxy.enabled` or `approvedEgress.enabled`, and `egressProxy.networkPolicy.create=true`.
Keep `egressProxy.networkPolicy.extraEgress` and the egress proxy allowlist from opening the control-plane API port or the main runtime Service to workers.
The Service is separate from the main runtime Service so `service.type` never exposes the gateway port outside the cluster.
See [Background Python Scripts](../../../docs/tools/background-scripts.md#worker-and-network-requirements) for the script tool, its gateway contract, and run lifecycle.

## Egress Broker

The egress broker lets agents call authenticated HTTP APIs from Kubernetes workers without the worker ever holding the credential.
The primary runs the broker on its own port, workers send their traffic through it, and it injects the stored credential for the matching service.
For configuration, secret management, and error codes, see [Agent Keys](../../../docs/deployment/egress-broker.md).

The chart supports direct and chain modes:

| Mode | Values | Worker to broker | Broker to internet |
|---|---|---|---|
| Direct | `egressBroker.enabled` | Worker pod to the `<fullname>-egress-broker` Service to the primary pod | Direct from the primary pod |
| Direct with an operator proxy | `egressBroker.enabled` and `egressProxy.enabled` | Same, plus an additive worker egress NetworkPolicy for the broker port | Through the operator proxy only when the primary's env sets `HTTPS_PROXY`, `HTTP_PROXY`, and `NO_PROXY`; otherwise direct (see the warning below) |
| Chain | `egressBroker.enabled` and `approvedEgress.enabled` | Worker to Squid (grant check by pod IP) to the broker as Squid's parent | Direct from the primary pod |

> [!WARNING]
> **Direct mode with `egressProxy` bypasses the operator proxy unless the primary uses it.**
> The broker environment of each `shell` and `python` call of an agent with the `egress_credentials` tool replaces the `HTTP_PROXY` and `HTTPS_PROXY` that `egressProxy.injectWorkerProxyEnv` gives worker pods, so that traffic goes to the broker instead of the operator proxy.
> The broker then dials from the primary pod: matched hosts always, and unmatched hosts too under the default `unmatched_hosts: passthrough`.
> Unless the primary's own environment sets `HTTPS_PROXY`, `HTTP_PROXY`, and `NO_PROXY`, that traffic leaves the cluster directly instead of through the operator proxy.
> Set the operator proxy on the primary through `env.extra`, or set `egress_broker.unmatched_hosts: deny` in the MindRoom config so only the configured services' hosts are reached directly.
> The broker connects through the operator proxy to the IP address it validated, so the proxy must allow CONNECT to IP addresses on ports 80 and 443.
> The chart does not enforce either setting yet.

`egressProxy.noProxy` entries do not apply to brokered `shell` and `python` calls, so their requests to other internal hosts go to the broker (through Squid in chain mode), which refuses private addresses with 403 `destination_blocked`.

Set `egressBroker.enabled` to have the primary serve that listener:

```yaml
workers:
  backend: kubernetes

egressBroker:
  enabled: true
  port: 8768
  # Optional lifetime of the per-call worker tokens, in seconds.
  tokenTtlSeconds: ""
```

The primary container then exposes an `egress-broker` port and gets `MINDROOM_EGRESS_BROKER_PORT`, `MINDROOM_EGRESS_BROKER_HOST=0.0.0.0`, and `MINDROOM_EGRESS_BROKER_URL`, plus `MINDROOM_EGRESS_BROKER_TOKEN_TTL_SECONDS` when `egressBroker.tokenTtlSeconds` is set.
The URL points workers at a `<fullname>-egress-broker` ClusterIP Service for that port.
The Service is separate from the main runtime Service so `service.type` never exposes the broker port outside the cluster.
Workers need no changes: each call carries its own token and CA certificate.
Worker images must still come from a release that includes the broker; see [Worker images](../../../docs/deployment/egress-broker.md#worker-images).
When `networkPolicy.create` is true, a `<fullname>-egress-broker` NetworkPolicy admits workers to that port on the control-plane pod.
When the chart's worker egress NetworkPolicy exists (`egressProxy.enabled` with `egressProxy.networkPolicy.create=true`), a `<fullname>-egress-broker-workers` NetworkPolicy in the worker namespace adds egress from workers to the broker port on the control-plane pod.
Without that policy workers already have unrestricted egress, so the chart adds no worker rule.
If `networkPolicy.extraEgress` restricts the primary, allow TCP 80 and 443 to public addresses yourself, because the chart cannot infer that rule.
The broker runs in the primary: a primary restart interrupts brokered connections in flight, and brokered TLS uses the primary's CPU, so leave room in its resource requests.

With `approvedEgress.enabled` as well, workers keep the approved egress proxy (Squid) as their first hop, so Squid still enforces per-worker grants by pod IP.
The chart then chains Squid to the broker Service and port: after the normal allowlist or grant check, requests that carry a token go to the broker and tokenless requests egress directly from Squid.
`MINDROOM_EGRESS_BROKER_URL` is the Squid URL, the broker NetworkPolicy admits only the approved egress pods instead of workers, and no worker egress rule is added.
Changing `egressBroker.port` rolls the Squid pod.

For signed URLs or other destinations that must skip broker credential injection, set `approvedEgress.parentProxy.bypassDomains`:

```yaml
approvedEgress:
  parentProxy:
    bypassDomains:
      - downloads.example.test
      - .objects.example.test
```

This list defaults to empty and applies only when both `egressBroker` and chart-managed approved egress are enabled.
An entry without a leading dot matches that exact hostname; a leading dot matches the domain and its subdomains, following [Squid domain ACL syntax](https://www.squid-cache.org/Doc/config/acl/).
Use ASCII domain names (Punycode for internationalized names), without URL schemes, ports, paths, `*` wildcards, or whitespace; invalid entries fail chart rendering.
Matching uses the request hostname without reverse DNS lookups.
Bypass destinations still require the normal allowlist or dynamic grant and remain subject to the proxy's destination and port restrictions.
Other token-bearing requests continue through the broker.
Changing the list changes the chart's Squid config checksum and rolls the proxy Deployment automatically.

`egressBroker.enabled` requires `workers.backend=kubernetes`, and `egressBroker.port` must differ from `runtime.apiPort` and `scriptGateway.port`.

### Self-service for users

With the broker on, users without dashboard access manage their own keys, services, account connections, and request log on the personal Connections page at `/connections/egress`; see [Connections pages](../../../docs/deployment/egress-broker.md#connections-pages).
Users open it from MindRoom Chat (see [Connections portal](../../../docs/connections-portal.md)); to sign them in through a gateway instead, the page needs these settings:

- **Trusted upstream auth.** The gateway in front of the runtime must authenticate users and sign a JWT.
  Configure it with the `trustedUpstreamAuth` values, and set an HTTPS `MINDROOM_PUBLIC_URL` through `env.extra`, as below.
- **Routing.** Forward `/connections`, `/connections/*`, `/api/connections`, `/api/connections/*`, and `/api/oauth/*` from the gateway to the runtime Service.
  The chart creates no ingress.
- **`MINDROOM_CONNECTIONS_AGENT`.** Set it to a private agent (`private.per: user` or `user_agent`), as in the example, whenever more than one user can sign in through the gateway.
  Without it, every user the gateway admits gets full dashboard access, whatever `administrators` lists.

```yaml
trustedUpstreamAuth:
  enabled: true
  jwtHeader: X-Trusted-Jwt
  jwksUrl: https://gateway.example.com/.well-known/jwks.json
  jwtAudience: mindroom-dashboard
  jwtIssuer: https://gateway.example.com
  jwtEmailClaim: email
  jwtUserIdClaim: sub
  jwtMatrixUserIdClaim: matrix_user_id
env:
  extra:
    MINDROOM_PUBLIC_URL: "https://assistant.example.org"
    MINDROOM_CONNECTIONS_AGENT: personal
```

See [Trusted Upstream Browser Auth](../../../docs/deployment/trusted-upstream-auth.md) for every setting and what the chart checks, the Matrix identity options, and what users outside `administrators` can reach.
Connecting accounts also needs the providers' [OAuth clients](../../../docs/oauth-framework.md#built-in-providers), and personal GitHub and Atlassian accounts need agents with worker scope `user` or `user_agent` unless the service sets `oauth_on_shared_workers`; see [Personal accounts and shared sandboxes](../../../docs/deployment/egress-broker.md#personal-accounts-and-shared-sandboxes).

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
  The state-storage init container still runs as root for `chown` and `chmod`, and content bundles, the event journal PostgreSQL, and user-supplied containers use the security contexts you set for them.
- The chart can create PostgreSQL for MindRoom's event journal, or use an external PostgreSQL URL from an existing Secret.
- Set `workers.sandbox.proxyToken.existingSecret` or `workers.sandbox.proxyToken.value` when sandbox proxying is enabled.
- Use `providerCredentials` to feed model-provider API keys from existing Kubernetes Secrets into the runtime's credential service.
- Worker tool code can reach the primary API, on `localhost` from the `static_runner` sidecar or over the pod network from dedicated Kubernetes workers, so the chart gives the primary a generated `MINDROOM_API_KEY` from the `<fullname>-api-key` Secret for either backend, and the dashboard and API then require it.
  GitOps and `helm template` workflows should set `apiAuth.existingSecret` (and `apiAuth.key` if the Secret's key is not `MINDROOM_API_KEY`) to read the key from an existing Secret instead, because the generated key changes on every offline render.
  Point it at a Secret the chart does not manage; the chart rejects `<fullname>-api-key`, which it stops rendering once the option is set.
  To keep the current key, copy it from `<fullname>-api-key` into that Secret before switching.
  A `MINDROOM_API_KEY` from `env.extra` or `env.envFrom` also takes precedence over the generated key, but the chart then still renders the unused generated Secret.
  `apiAuth.allowUnauthenticatedPrimary: true` removes the key and is unsafe unless other primary API authentication is configured.
- Set `workers.sandbox.credentialsEncryptionKey.existingSecret` when encrypted credential storage is enabled so the primary runtime receives the Secret-backed key.
- `workers.backend: static_runner` adds a sandbox-runner sidecar to the runtime pod.
  The sidecar never receives the credentials encryption key, and saved settings for each proxied tool reach it as per-call leases from the primary.
  From the storage PVC it mounts only the `agents`, `private_instances`, and `shared_directories` directories read-write over its own `sandbox-runner` directory, so agent workspaces and shared directories persist while the credential store, Matrix state, and the primary's config stay out of reach.
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
- Dedicated worker pods also get the chart's `imagePullSecrets`, so they can pull a worker image from a private registry.
  Pod-level pull secrets replace the worker ServiceAccount's `imagePullSecrets`, so list every Secret workers need in `imagePullSecrets`.
- If workers run in a different namespace, provide storage, service accounts, and network policy behavior that are valid for that namespace.
  Create the `imagePullSecrets` Secrets in that namespace too, because the chart does not copy them and worker pods cannot pull without them.
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
