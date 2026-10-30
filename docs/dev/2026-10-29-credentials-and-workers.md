# Credential placement, environment credentials, and worker identity declared once

This record keeps the decisions behind the October 2026 consolidation of credential placement and worker backend identity that the code cannot show.

## Credential placement

The placement policy used to return five booleans that two decision trees in the credential manager, the dashboard target functions, the egress document helpers, the dashboard listing, the lease path, and Desktop each interpreted again.
Two latent traps followed from that duplication: a scoped read of an OAuth app client config reached the worker store, which worker code can write, and a requester scope without a requester saved into the global store that every unscoped read sees; only callers' pre-branches avoided them.
The policy now names one placement per service and scope, and one resolver maps it to stores, so a new caller cannot reach a store the policy does not name.

Whether the primary builds a tool stays a caller input to the resolver, because it is a tool-registry fact and the policy module must not import the registry.

The dashboard listing is derived by reading each candidate service through the resolver instead of restating the read rules, which also stopped listing worker copies the runtime ignores.

## Environment credentials are a read layer

Environment keys were copied into the store with `_source=env` at every start and never removed, so removing a key from `.env` kept the copy working on the primary and mirrored to granted workers.
Deleting stale copies at each start would have kept two owners of one fact in sync by hand, so the store now reads the environment's declarations under the saved documents and keeps no copy.

- A saved document hides the whole declared document for its service, which matches the import's rule that a dashboard save was never overwritten.
- Only a runtime outside runner mode reads the layer; the sandbox runner app now sets runner mode in the runtime it commits, because runners rehydrate from manifests and their own process environment, and absence of the flag is not evidence of being the primary.
- The declared layer is computed when a primary manager is created and keyed by a digest computed once per `RuntimePaths` snapshot, so lookups add no hashing and changes to referenced files take effect at restart, as the import did.
- The legacy copies are deleted when the primary first opens its store rather than at a startup hook, so `mindroom doctor` run before the first start after an upgrade validates the keys that start will use.
- A legacy reader that hides `_source=env` documents at read time was rejected because it keeps an old-format branch in every read.

## Docker worker currency

Docker decided whether a container was current with six comparisons over a hand-listed payload, so `tmpfs`, `extra_hosts`, `command`, removed environment names, labels, and port shape never reached existing containers unless the image ID changed.
The backend now renders the exact `containers.run` arguments once and stores their hash as a label, like the Kubernetes rendered pod template.

- Inputs that are not run arguments travel as backend-owned labels inside the rendered arguments: the startup manifest's content hash and the configured image reference.
- An existing container's image ID only proves reuse when the configured reference cannot be resolved locally; a replacement always uses the configured reference, pulled when absent, so a changed reference is never masked by the old image and a warm call never needs the registry.
- Config projection validity stays a live check outside the hash, because a rebuilt projection reuses its pathname while a running container's bind mount keeps the old directory.
- Every existing Docker worker is recreated once after the upgrade, which ends its background shells once.

## Backend identity

One enum and alias table names the selectable backends; operations dispatch on it instead of comparing names at each caller.
Binding the worker-manager identity once per process was rejected: Kubernetes managers must rebuild when kubeconfig content rotates and Docker managers when the public environment differs.
Docker's backend environment names now belong to the runtime environment policy beside the Kubernetes ones, which keeps them out of worker startup manifests; because they no longer enter the manager signature through the public environment, the Docker signature covers the whole parsed backend config.

## Static runner lifecycle

Moving the static runner's worker records to the runner was rejected: listing and cleanup through the runner's existing routes would not carry the touches, failures, and in-flight keepalives the primary observes, and new routes for them would cost more than the per-process record they replace.
